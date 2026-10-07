"""Triggers: HTTP calls (webhooks, 0.12.0) and scheduled clock ticks (cron, 0.15.0) that fire a named playbook.

A trigger is defined in `.console-kit/triggers.json`:

    {
      "triggers": {
        "pr-merged": {
          "playbook": "security-review",
          "token_sha256": "<sha256 of the bearer token, hex, lowercase>"
        }
      }
    }

The server exposes `POST /api/trigger/<name>` on the owner door. The caller
must satisfy BOTH gates:

1. **Cloudflare Access** — configured on the Access application to accept a
   service token (`CF-Access-Client-Id` + `CF-Access-Client-Secret`) in addition
   to interactive logins. The console-kit never sees the Access headers; the
   tunnel validates them.
2. **Per-trigger bearer token** — the caller sends `Authorization: Bearer <token>`
   (or `X-Console-Trigger-Token: <token>`). The server hashes the received
   token with SHA-256 and compares to the configured `token_sha256`.

The server NEVER stores the plaintext token. Rotate by generating a new token,
putting its sha256 into the file, and giving the plaintext to the sender.

**GitHub-webhook users**: GitHub signs with HMAC, not bearer. A 10-line Worker
or small shim can accept GitHub's `X-Hub-Signature-256`, verify it, and re-emit
`Authorization: Bearer <token>` to this endpoint.

Rate limit: at most one firing per `MIN_INTERVAL_S` seconds per trigger (in-process).
A rejected call responds 429 and names the next-allowed time.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import threading
from dataclasses import dataclass
from pathlib import Path

from . import rootfs as RF
from .registry import RegistryError, read_regular

FILE = ".console-kit/triggers.json"
MAX_FILE = 32 * 1024
MAX_TRIGGERS = 64
MAX_BODY = 256 * 1024
MIN_INTERVAL_S = 10.0
NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}\Z")
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}\Z")


class TriggerError(ValueError):
    pass


@dataclass(frozen=True)
class Trigger:
    name: str
    playbook: str
    token_sha256: str | None    # None → webhook disabled (cron-only trigger)
    cron: tuple | None          # (minutes, hours, days-of-month, months, days-of-week) each a frozenset of ints

    def to_public(self) -> dict:
        """For /view: token hash + cron shape aren't exposed; only name + playbook + what trigger kinds are armed."""
        kinds = []
        if self.token_sha256:
            kinds.append("webhook")
        if self.cron:
            kinds.append("cron")
        return {"name": self.name, "playbook": self.playbook, "kinds": kinds}


HISTORY_LIMIT = 100   # most-recent firings kept in-process (bounded ring); older ones drop off


class Store:
    """Thread-safe in-process registry + rate limiter + firing history (0.16.0)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._triggers: dict[str, Trigger] = {}
        self._last_fired: dict[str, float] = {}
        import collections
        self._history: collections.deque = collections.deque(maxlen=HISTORY_LIMIT)

    def set_all(self, triggers: dict[str, Trigger]) -> None:
        with self._lock:
            self._triggers = dict(triggers)
            self._last_fired = {k: v for k, v in self._last_fired.items() if k in triggers}

    def get(self, name: str) -> Trigger | None:
        with self._lock:
            return self._triggers.get(name)

    def all(self) -> dict[str, Trigger]:
        with self._lock:
            return dict(self._triggers)

    def claim(self, name: str, now: float) -> float | None:
        """Record a firing at `now` and return None; or return the next-allowed time when rate-limited."""
        with self._lock:
            last = self._last_fired.get(name)
            if last is not None and now - last < MIN_INTERVAL_S:
                return last + MIN_INTERVAL_S
            self._last_fired[name] = now
            return None

    def log(self, name: str, kind: str, ts: str, records: int, skipped: int, error: str | None = None) -> None:
        """Append one firing record to the in-process history ring, newest-first on reads via `recent`."""
        with self._lock:
            self._history.append({"name": name, "kind": kind, "ts": ts,
                                  "records": records, "skipped": skipped, "error": error})

    def recent(self, limit: int = 20) -> list[dict]:
        """The newest `limit` firings, newest first."""
        with self._lock:
            items = list(self._history)
        items.reverse()
        return items[: max(1, min(HISTORY_LIMIT, limit))]


def load(root: Path) -> dict[str, Trigger]:
    """Read `.console-kit/triggers.json` and return every well-formed trigger, keyed by name.

    Returns {} when the file is absent. A single malformed trigger raises TriggerError NAMING the first
    problem, so the server refuses to start half-configured. The playbook name is NOT resolved here;
    the webhook refuses at firing time if the playbook file is missing.
    """
    try:
        if RF.confined():
            raw = RF.read(Path(root), FILE, MAX_FILE)
        else:
            raw = read_regular(Path(root) / FILE, MAX_FILE)
    except (RegistryError, RF.Refused, ValueError, OSError) as e:
        raise TriggerError(str(e)) from None
    if raw is None:
        return {}
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise TriggerError(f"{FILE} is not JSON: {e}") from None
    if not isinstance(doc, dict):
        raise TriggerError(f"{FILE}: top-level must be an object")
    triggers = doc.get("triggers")
    if triggers is None:
        return {}
    if not isinstance(triggers, dict):
        raise TriggerError(f"{FILE}: triggers must be an object mapping names to definitions")
    if len(triggers) > MAX_TRIGGERS:
        raise TriggerError(f"{FILE}: {len(triggers)} triggers; at most {MAX_TRIGGERS}")
    out: dict[str, Trigger] = {}
    for name, cfg in triggers.items():
        if not isinstance(name, str) or not NAME.match(name):
            raise TriggerError(f"{FILE}: trigger name {name!r} is lowercase letters, digits, '_' or '-' (1-64)")
        out[name] = _validate(name, cfg)
    return out


def _validate(name: str, cfg: object) -> Trigger:
    if not isinstance(cfg, dict):
        raise TriggerError(f"{FILE}: triggers[{name!r}] must be an object")
    playbook = cfg.get("playbook")
    if not isinstance(playbook, str) or not NAME.match(playbook):
        raise TriggerError(f"{FILE}: triggers[{name!r}].playbook must name a playbook (slug-shaped)")
    token_sha = cfg.get("token_sha256")
    if token_sha is not None:
        if not isinstance(token_sha, str) or not _SHA256_HEX.match(token_sha):
            raise TriggerError(f"{FILE}: triggers[{name!r}].token_sha256 is 64 lowercase hex chars "
                               "(sha256 of the bearer token you give the sender)")
    cron_raw = cfg.get("cron")
    cron_fields: tuple | None = None
    if cron_raw is not None:
        if not isinstance(cron_raw, str):
            raise TriggerError(f"{FILE}: triggers[{name!r}].cron must be a 5-field cron string "
                               "like '0 9 * * MON' or '*/15 * * * *'")
        try:
            cron_fields = parse_cron(cron_raw)
        except ValueError as e:
            raise TriggerError(f"{FILE}: triggers[{name!r}].cron: {e}") from None
    if token_sha is None and cron_fields is None:
        raise TriggerError(f"{FILE}: triggers[{name!r}] must set at least one of token_sha256 (webhook) "
                           "or cron (schedule)")
    return Trigger(name=name, playbook=playbook, token_sha256=token_sha, cron=cron_fields)


# -- cron parser ------------------------------------------------------------------------
# Minute Hour DayOfMonth Month DayOfWeek. `*`, `*/n`, ranges `a-b`, lists `a,b,c`, and `a-b/n`. Named
# months (JAN..DEC) and days-of-week (SUN..SAT) are accepted; shortcuts like `@daily` are not — spell
# the full form so a reader of the config can see exactly when it fires. DoW: 0 or 7 = Sunday.

_MONTH = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,"JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}
_DOW = {"SUN":0,"MON":1,"TUE":2,"WED":3,"THU":4,"FRI":5,"SAT":6}
_FIELDS = (("minute", 0, 59, {}),
           ("hour", 0, 23, {}),
           ("day-of-month", 1, 31, {}),
           ("month", 1, 12, _MONTH),
           ("day-of-week", 0, 6, _DOW))


def parse_cron(spec: str) -> tuple:
    """Parse a 5-field cron string. Returns a tuple of frozensets, one per field."""
    parts = spec.strip().split()
    if len(parts) != 5:
        raise ValueError(f"cron has 5 fields separated by spaces; got {len(parts)}")
    out = []
    for raw, (fname, lo, hi, aliases) in zip(parts, _FIELDS):
        out.append(frozenset(_parse_cron_field(raw, lo, hi, aliases, fname)))
    return tuple(out)


def _parse_cron_field(raw: str, lo: int, hi: int, aliases: dict, fname: str) -> set:
    def lookup(v: str) -> int:
        v = v.strip().upper()
        if v in aliases:
            return aliases[v]
        try:
            return int(v)
        except ValueError:
            raise ValueError(f"{fname} cannot read {v!r}") from None
    out: set = set()
    for piece in raw.split(","):
        step = 1
        if "/" in piece:
            piece, step_s = piece.rsplit("/", 1)
            try:
                step = int(step_s)
            except ValueError:
                raise ValueError(f"{fname} step {step_s!r} is not an integer") from None
            if step < 1:
                raise ValueError(f"{fname} step must be >= 1")
        if piece == "*" or piece == "":
            a, b = lo, hi
        elif "-" in piece:
            a_s, b_s = piece.split("-", 1)
            a, b = lookup(a_s), lookup(b_s)
        else:
            v = lookup(piece)
            a, b = v, v
        # Day-of-week accepts 7 as a Sunday alias (same as 0).
        if fname == "day-of-week":
            if a == 7: a = 0
            if b == 7: b = 0
        if a < lo or b > hi or a > b:
            raise ValueError(f"{fname} {piece!r} is out of range {lo}..{hi}")
        out.update(range(a, b + 1, step))
    return out


def cron_matches(cron: tuple, now) -> bool:
    """True when the local-time `now` matches every cron field. `now` is a datetime.datetime."""
    minutes, hours, dom, months, dow = cron
    return (now.minute in minutes and now.hour in hours and now.day in dom
            and now.month in months and (now.weekday() + 1) % 7 in dow)
#         ^ Python weekday: Mon=0..Sun=6; cron: Sun=0..Sat=6. (Mon=0 -> cron 1); (Sun=6 -> cron 0).


def token_from_header(auth: str | None, extra: str | None) -> str | None:
    """Pull a bearer token from either `Authorization: Bearer <token>` or `X-Console-Trigger-Token: <token>`.

    Returns the token string without the `Bearer ` prefix, or None when neither header carries one.
    """
    for v in (auth, extra):
        if isinstance(v, str) and v:
            s = v.strip()
            if s.lower().startswith("bearer "):
                s = s[7:].strip()
            if s:
                return s
    return None


def token_matches(trigger: Trigger, token: str) -> bool:
    """Constant-time comparison of sha256(token) against the configured sha256."""
    if not isinstance(token, str) or not token:
        return False
    got = hashlib.sha256(token.encode("utf-8", errors="strict")).hexdigest()
    return hmac.compare_digest(got, trigger.token_sha256)


def mint() -> tuple[str, str]:
    """Mint a fresh (token, token_sha256) pair for the operator to copy into triggers.json + the sender."""
    import secrets
    token = "ck_trg_" + secrets.token_urlsafe(32)
    return token, hashlib.sha256(token.encode("utf-8")).hexdigest()

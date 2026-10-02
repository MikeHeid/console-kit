"""The owner console server (spec §4.4 and §4.5; decisions D1, D5, D7 and D8).

One process, and the store's only writer (see `store.py`). It has two doors:

- **The owner's door**: HTTP on 127.0.0.1 only, reached through a Cloudflare
  tunnel. Every request must carry a `Cf-Access-Jwt-Assertion` that verifies
  against the team's signing keys, with this application's `aud` and the
  team's `iss`. A request without one is refused **on loopback too** (D5), so
  no HTTP path skips the check: a tunnel, a proxy or a local process all meet
  the same gate. Owner writes are stamped `by: owner` here; the page never
  chooses its own author.
- **The agent's door**: HTTP over a Unix socket that only this user can open
  (mode 0600, in a 0700 directory). It carries no Access token, because the
  agent has none; the operating system's file permissions are its check, and
  nothing on the network can reach it. Agent writes are stamped `by: agent`.

Every owner write also appends one line to the doorbell file (D8), which an
open agent session watches. The view is computed per request and never
stored (R5).

The live console (0.7.0) adds four owner routes, each behind the same gate
(and, for the write, the same Origin check): `GET /api/wait` (a long poll on
the store's seq), `GET /api/feed`, `GET /api/evidence` and
`POST /api/lock-all`. No route was added to the agent's door or the health door.

0.8.0 adds one owner route, `GET /api/visual?id=RECORD_ID`, behind the same
gate. It serves an agent's visual with its own headers: an HTML mock goes out
under a Content-Security-Policy whose `sandbox` directive (no allow-scripts,
no allow-same-origin) and `default-src 'none'` hold even if the URL is opened
in its own tab, and the page only ever shows it in an `<iframe sandbox="">`.
The agent's door gains `POST /transcript` and `POST /visual`. `/api/view`
also carries each question's and round's suggested next steps (`tags.py`).

0.8.1: the server stores a visual in its STATE directory and serves it from
there; it never writes into the project's working tree (`visuals.py`). The
agent's door gains `POST /visual-export`, a read that hands stored visuals to
`agent.py visual-export`, which writes them into the agent's own worktree.

0.8.2: an agent may name itself on the agent's door, in the `X-Console-Agent`
header (`agent.py --as NAME`). The name is checked (`names.problem`) and kept
in STATE/names.jsonl beside the store, never in a store record, so a 0.8.1 kit
still reads the store (see `names.py`). The "agent active" marks are kept per
agent name (unnamed sessions share the bucket "agent"), and a cursor post
clears only the caller's bucket.

`/health` (0.6.0) is answered on the agent's door, and on a third door only
when `--health-port` asks for one. It is NEVER answered on the owner's door
without the Access token: that door keeps its rule that no path skips the
gate. See `HealthHandler` for why the health door is safe to leave ungated.
"""

from __future__ import annotations

import argparse
import calendar
import collections
import hashlib
import ipaddress
import json
import math
import os
import re
import secrets
import socket
import socketserver
import stat
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlsplit

from . import __version__
from . import anchors as A
from . import doorbell as D
from . import gitseam as G
from . import items as IT
from . import pagesnap as PS
from . import names as N
from . import projectcfg as PC
from . import publish as P
from . import schema as S
from . import stewardgit as SG
from . import tags as T
from . import view as V
from . import visuals as VIS
from .store import Store, StoreError

HOST = "127.0.0.1"          # never configurable: the tunnel is the only way in
DRAIN_SECONDS = 2.0         # at most this long, in all, reading a body an early refusal left unread
BODY_SECONDS = 30.0         # at most this long, in all, receiving one request body (the per-read timeout restarts)
MAX_BODY = 64 * 1024        # far above any real answer (MAX_TEXT is 20 000 characters)
OWNER_ROUTES = {"/api/message": "message", "/api/answer": "answer", "/api/lock": "lock"}
# The live console (0.7.0). A long poll: the page asks "has anything changed
# since seq S?" and the server answers the moment something does, or after at
# most WAIT_MAX seconds with "no". Well inside Cloudflare's 100 s response
# timeout, and one request per owner tab at a time; a server-sent stream was
# not used because a tunnel may buffer one, and it needs nothing a plain GET
# behind the same gate does not already have.
WAIT_MAX = 25.0
MAX_WAITERS = 16            # open long polls at once; past this a poll is told to back off (429)
# The chat (0.7.0): at most this many owner messages a minute and an hour.
CHAT_PER_MINUTE = 6
CHAT_PER_HOUR = 60
MAX_LOCK_ALL = 12           # entries in one "Lock all & process" (a round holds at most 5)
LOCK_ALL_NONCE = 48         # characters: room for the per-record suffixes within the nonce limit
AGENT_ROUTES = {"/question": "question", "/message": "message", "/transcript": "transcript"}
# The agent's door takes larger bodies (0.8.0): a roar transcript is up to 48 KiB
# and a visual up to 256 KiB, and JSON escaping can grow either several times.
# The door is a user-only Unix socket; the owner's door keeps MAX_BODY.
AGENT_MAX_BODY = 2 << 20
# /view's answer when a stored file fails its check: what to do, never where the file is.
VIEW_UNREADABLE = ("the console's stored state could not be read just now; if the stored items are the cause, "
                   "the steward should push them again (agent.py items-push)")
# An HTML mock is shown ONLY in <iframe sandbox="">. This policy holds even if the
# URL is opened in its own tab: `sandbox` (no tokens) gives it an opaque origin
# and no script, `default-src 'none'` lets it load nothing from anywhere, and
# inline styles and data: images are what a self-contained mock needs.
VISUAL_HTML_CSP = ("sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:; "
                   "base-uri 'none'; form-action 'none'; frame-ancestors 'self'")
VISUAL_TEXT_CSP = "sandbox; default-src 'none'; frame-ancestors 'none'"
WRITER_FIELDS = ("by", "type", "schemaVersion")
# Fields only the server computes on a lock (0.5.0): a page that sends one is refused.
SERVER_LOCK_FIELDS = ("anchors",)
DEFAULT_RELOCK_REASON = "Re-locked unchanged: the owner checked what changed and the answer still holds."
SECURITY_HEADERS = (
    ("Cache-Control", "no-store"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("Content-Security-Policy", "frame-ancestors 'none'"),
)


class BoardError(Exception):
    pass


class AuthError(Exception):
    """A request the Access check refuses. The message is safe to show; it never echoes the token."""


class RequestError(Exception):
    def __init__(self, code: int, message: str, extra: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.extra = extra or {}  # more to say than one line: "Lock all" names each question's outcome


def access_verifier(team_domain: str, aud: str,
                    key_for: Callable[[str], object] | None = None) -> Callable[[str | None], dict]:
    """Return a function that verifies an Access JWT and returns its claims, or raises AuthError.

    `key_for(token)` returns the public key for the token's `kid`. By default it
    reads the team's certificate endpoint, cached; tests pass their own.
    """
    import jwt  # PyJWT, pinned in requirements.txt; imported here so the kit's other modules need no dependency

    issuer = f"https://{team_domain}"
    if key_for is None:
        client = jwt.PyJWKClient(f"{issuer}/cdn-cgi/access/certs", cache_keys=True, lifespan=600)

        def key_for(token: str) -> object:
            return client.get_signing_key_from_jwt(token).key

    def verify(token: str | None) -> dict:
        if not token:
            raise AuthError("no Access token: this server only answers through Cloudflare Access")
        try:
            return jwt.decode(token, key_for(token), algorithms=["RS256"], audience=aud, issuer=issuer,
                              options={"require": ["exp", "iat", "aud", "iss"]}, leeway=30)
        except jwt.PyJWTError as e:  # includes a key that cannot be fetched: fail closed
            raise AuthError(f"Access token refused ({type(e).__name__})") from None

    return verify


@dataclass(frozen=True)
class Config:
    root: Path          # the project checkout: valid_if paths resolve against it
    page: Path | None   # NOT READ since Q28 (the page is STATE's snapshot); kept so a unit's --page still parses
    state: Path         # store.jsonl, inbox.jsonl, cursor.json and agent.sock live here
    adapter: Path
    team_domain: str
    aud: str
    hostname: str       # the public hostname; a POST from any other browser Origin is refused
    port: int = 4793
    project: str = ""
    health_port: int = 0  # 0: no health port; /health is still on the agent socket
    usage_file: Path | None = None    # 0.8.6: a status line's usage snapshot, read for the footer
    account_file: Path | None = None  # 0.8.6: Claude Code's settings file; only the account email is read

    @property
    def store(self) -> Path:
        return self.state / "store.jsonl"

    @property
    def inbox(self) -> Path:
        return self.state / "inbox.jsonl"

    @property
    def cursor(self) -> Path:
        return self.state / "cursor.json"

    @property
    def working(self) -> Path:
        return self.state / "working.json"

    @property
    def socket(self) -> Path:
        return self.state / "agent.sock"


# -- 0.8.6: the footer's usage and account probe --------------------------------------------
# Both files belong to someone else (a status line plugin, Claude Code), so each is read with a
# size cap, parsed defensively, and reduced to the named fields: nothing else in either file ever
# reaches the page. Every problem becomes a short sentence, never the file's contents.

USAGE_MAX_BYTES = 16 * 1024
ACCOUNT_MAX_BYTES = 8 * 1024 * 1024  # a long-used settings file holds project history
_WINDOWS = ("five_hour", "seven_day")


class UsageProblem(Exception):
    pass


def _read_capped(path: Path, cap: int, what: str) -> tuple[bytes, float]:
    """The file's bytes and its modification time, read from the one open file."""
    try:
        # O_NONBLOCK: a FIFO put where the file should be must not hold a handler thread open.
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        with open(fd, "rb") as f:
            st = os.fstat(f.fileno())
            if not stat.S_ISREG(st.st_mode):
                raise UsageProblem(f"the {what} is not a regular file")
            data = f.read(cap + 1)
    except FileNotFoundError:
        raise UsageProblem(f"the {what} does not exist yet") from None
    except OSError:
        raise UsageProblem(f"the {what} cannot be read") from None
    if len(data) > cap:
        raise UsageProblem(f"the {what} is larger than {cap // 1024} KB")
    return data, st.st_mtime


_EPOCH_MAX = 253402300799  # 9999-12-31T23:59:59Z, the last second a datetime can hold
_MTIME_SLACK_S = 60  # a write time this far ahead of our clock is still taken as now


def _utc(seconds: float, what: str) -> str:
    if not 0 <= seconds <= _EPOCH_MAX:
        raise UsageProblem(f"the usage file's {what} is out of range")
    try:  # the platform's own ceiling can sit below datetime's (Windows: year 3000)
        t = datetime.fromtimestamp(int(seconds), timezone.utc)
    except (OverflowError, OSError, ValueError):
        raise UsageProblem(f"the usage file's {what} is out of range") from None
    return t.isoformat().replace("+00:00", "Z")


def _iso(value, what: str) -> str:
    """A timestamp re-emitted in one form, so the page never parses a string it did not check.
    Whole epoch seconds are accepted too: Claude Code's own status line input carries them."""
    if isinstance(value, int) and not isinstance(value, bool):
        return _utc(value, what)
    if not isinstance(value, str) or len(value) > 64:
        raise UsageProblem(f"the usage file's {what} is not a timestamp")
    try:
        t = datetime.fromisoformat(value)
    except ValueError:
        raise UsageProblem(f"the usage file's {what} is not a timestamp") from None
    if t.tzinfo is None:
        raise UsageProblem(f"the usage file's {what} has no time zone")
    try:  # a year-1 or year-9999 date overflows on the way to UTC
        return t.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except (OverflowError, ValueError):
        raise UsageProblem(f"the usage file's {what} is out of range") from None


def _window(raw, name: str) -> dict:
    if not isinstance(raw, dict):
        raise UsageProblem(f"the usage file has no {name} window")
    pct = raw.get("used_percentage")
    if pct is not None and (isinstance(pct, bool) or not isinstance(pct, (int, float))
                            or not 0 <= pct <= 100 or not math.isfinite(pct)):
        # The range check comes first: math.isfinite raises OverflowError on a huge integer.
        raise UsageProblem(f"the usage file's {name} percentage is not 0-100")
    resets = raw.get("resets_at")
    return {"used_percentage": None if pct is None else float(pct),
            "resets_at": None if resets is None else _iso(resets, f"{name} reset time")}


def usage_snapshot(path: Path) -> dict:
    """Two shapes are read. claude-hud's snapshot: the windows at the top level, with updated_at.
    Claude Code's own status line input, saved as it arrives: the windows under rate_limits and
    no updated_at, so the file's write time stands in. The status line rewrites it on every
    redraw, so that time says a session is running, not when the limits were last fetched; with
    no session open it goes stale. A file with windows at the top level is read from there only."""
    raw, mtime = _read_capped(path, USAGE_MAX_BYTES, "usage file")
    try:
        doc = json.loads(raw)
    except (ValueError, RecursionError):  # bad JSON, bad UTF-8, a huge integer, deep nesting
        raise UsageProblem("the usage file is not JSON") from None
    if not isinstance(doc, dict):
        raise UsageProblem("the usage file is not a JSON object")
    src = doc
    updated = doc.get("updated_at")
    if not any(w in doc for w in _WINDOWS):
        if isinstance(doc.get("rate_limits"), dict):
            src = doc["rate_limits"]
        elif updated is None:  # Claude Code sends no rate_limits before the first API answer
            raise UsageProblem("the usage file has no usage limits yet")
    if updated is None and src is not doc:
        if mtime > time.time() + _MTIME_SLACK_S:  # a skewed clock or touch -d, never "fresh"
            raise UsageProblem("the usage file's write time is in the future")
        out = {"updated_at": _utc(mtime, "write time")}
    else:
        out = {"updated_at": _iso(updated, "updated_at")}
    for w in _WINDOWS:
        out[w] = _window(src.get(w), w)
    return out


def account_email(path: Path) -> str | None:
    """The signed-in account's email, or None. Nothing else is taken from the file."""
    try:
        doc = json.loads(_read_capped(path, ACCOUNT_MAX_BYTES, "account file")[0])
    except (UsageProblem, ValueError, RecursionError):
        return None
    acct = doc.get("oauthAccount") if isinstance(doc, dict) else None
    email = acct.get("emailAddress") if isinstance(acct, dict) else None
    if (not isinstance(email, str) or not 3 <= len(email) <= 254 or email.count("@") != 1
            or any(c.isspace() or not c.isprintable() for c in email)):
        return None
    return email


def read_usage(cfg: Config) -> dict:
    out = {"enabled": cfg.usage_file is not None or cfg.account_file is not None,
           "usage": None, "usage_problem": None, "account": None}
    # Anything unforeseen in a file someone else writes becomes a fixed sentence, never a 500:
    # the log names the exception's type only, so no file content reaches the journal either.
    if cfg.usage_file is not None:
        try:
            out["usage"] = usage_snapshot(cfg.usage_file)
        except UsageProblem as e:
            out["usage_problem"] = str(e)
        except Exception as e:
            sys.stderr.write(f"console usage: {type(e).__name__} reading the usage file\n")
            out["usage_problem"] = "the usage file could not be read"
    if cfg.account_file is not None:
        try:
            out["account"] = account_email(cfg.account_file)
        except Exception as e:
            sys.stderr.write(f"console usage: {type(e).__name__} reading the account file\n")
    return out


class Console:
    """The store, the adapter and the doorbell, behind one lock."""

    def __init__(self, cfg: Config, adapter) -> None:
        self.cfg = cfg
        self.adapter = adapter
        cfg.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(cfg.state, 0o700)  # mkdir's mode is ignored for a directory that already exists
        # 0.8.0: specs_dir and visuals_dir from the project's .console-kit.json (data only;
        # a bad key raises ConfigError here, by name, so the server never starts half-configured).
        self.project = PC.load(cfg.root)
        self.store = Store(cfg.store)
        self.names = N.Names(cfg.state)   # 0.8.2: record id -> agent name, beside the store
        self._lock = threading.Lock()
        self._working_lock = threading.Lock()   # working.json is read, changed and replaced by several agents
        # The live console (0.7.0): every change a page could show bumps `_epoch`
        # and wakes the long polls waiting on `_changed`. `_boot` tells a page
        # that the server restarted, so its old token can never match by luck.
        self._changed = threading.Condition()
        self._epoch = 0
        self._boot = secrets.token_hex(4)
        self._waiters = 0
        self._chat_times: collections.deque[float] = collections.deque()

    # -- live updates (0.7.0) --------------------------------------------------

    def version(self) -> str:
        return f"{self._boot}.{self._epoch}"

    def _bump(self) -> None:
        with self._changed:
            self._epoch += 1
            self._changed.notify_all()

    def wait(self, since: int, ver: str | None, timeout: float) -> dict:
        """Return as soon as the store has moved past `since` or anything else changed since `ver`, or after `timeout` s.

        The answer always carries the store's seq, the version to send next
        time, and the cursor (sync time, agent listening/active), which is
        cheap to read and changes without a store record.
        """
        def moved() -> bool:
            return self.store.seq() != since or (ver is not None and ver != self.version())
        with self._changed:
            if not moved():
                if self._waiters >= MAX_WAITERS:
                    raise RequestError(429, f"{MAX_WAITERS} live updates are already waiting; try again shortly")
                self._waiters += 1
                try:
                    self._changed.wait_for(moved, timeout)
                finally:
                    self._waiters -= 1
            seq, ver_now = self.store.seq(), self.version()
        return {"seq": seq, "ver": ver_now, "changed": seq != since, "cursor": self.read_cursor()}

    def feed(self, kinds: set[str] | None, item: str | None, before: int | None, limit: int) -> dict:
        return V.feed(self.store, self.items(), kinds=kinds, item=item, before=before, limit=limit,
                      names=self.names.mapping())

    def evidence(self, qid: str) -> dict:
        """A question's evidence rows with the cited lines as they are now, read server-side (0.7.0)."""
        q = self.store.question(qid)
        if q is None:
            raise RequestError(404, f"no question {qid}")
        rows = q.get("evidence") or []
        tree = A.Tree(self.cfg.root, self._status(self.items()))
        return {"qid": qid, "evidence": A.evidence_now(rows, tree)}

    def _fill_evidence(self, body: dict) -> dict:
        """Read a new question's evidence lines from the tree, or refuse the question naming each bad row."""
        if "evidence" not in body:
            return body
        errs = S.check_evidence(body["evidence"], stored=False)
        if errs:
            raise RequestError(400, "; ".join(errs))
        rows, errs = A.fill_evidence(body["evidence"], A.Tree(self.cfg.root, {}))
        if errs:
            raise RequestError(400, "; ".join(errs))
        return {**body, "evidence": rows}

    def _chat_gate(self) -> None:
        """Refuse an owner chat message past CHAT_PER_MINUTE a minute or CHAT_PER_HOUR an hour; the caller holds `_lock`."""
        now = time.monotonic()
        while self._chat_times and now - self._chat_times[0] >= 3600:
            self._chat_times.popleft()
        last_min = [t for t in self._chat_times if now - t < 60]
        if len(last_min) >= CHAT_PER_MINUTE:
            wait = int(60 - (now - last_min[0])) + 1
            raise RequestError(429, f"at most {CHAT_PER_MINUTE} chat messages a minute: try again in {wait}s. "
                                    f"Nothing you typed was lost")
        if len(self._chat_times) >= CHAT_PER_HOUR:
            wait = int(3600 - (now - self._chat_times[0])) + 1
            raise RequestError(429, f"at most {CHAT_PER_HOUR} chat messages an hour: try again in {wait // 60 + 1} "
                                    f"min. Nothing you typed was lost")

    def items(self) -> dict[str, dict]:
        """The register as it is now. The store refuses writes to an item that is not in it (R7)."""
        items = self.adapter.items()
        self.store.set_items(items)
        return items

    def seed(self) -> list[str]:
        """Append every seed question not yet in the store. A qid is minted once, so this is idempotent."""
        added = []
        with self._lock:
            self.items()
            for q in self.adapter.seed_questions():
                if self.store.question(q["qid"]) is None:
                    q = self._fill_evidence(dict(q))  # a seed's evidence is read like any other question's
                    added.append(self.store.append({**q, "type": "question",
                                                    "schemaVersion": S.SCHEMA_VERSION})["qid"])
        if added:
            self._bump()
        return added

    def payload(self) -> dict:
        items = self.items()
        holds = A.evaluator(self.cfg.root, self._status(items), snapshot=True)  # one reading per request
        view = V.build(self.store, items, holds, names=self.names.mapping())
        view["tags"] = self.tags(view, items)
        view["config"] = {"specs_dir": self.project.specs_dir, "visuals_dir": self.project.visuals_dir}
        pushed = getattr(self.adapter, "pushed", None)
        if getattr(self.adapter, "problem", None):   # the items() above already read (and logged) it
            view["items_note"] = IT.UNREADABLE
        else:
            view["items_note"] = IT.NOT_PUSHED if callable(pushed) and not pushed() else None   # F63: never a blank
        return {"view": view, "items": items, "cursor": self.read_cursor()}

    def page_payload(self) -> dict:
        """The owner door's /api/view: `payload()` and the version it was built at, for the page's live loop.

        `ver` is read FIRST, so a change while the view is built makes the next
        wait wake rather than be missed. With it the page's first /api/wait also
        wakes on a version-only change (the first items-push moves no store
        record); without it that wait slept through one and took its version as
        the baseline. Only the page's door carries it: it names this process's
        boot, so the agent socket's /view stays the same bytes across restarts.
        """
        ver = self.version()
        return {**self.payload(), "ver": ver}

    def push_items(self, body: object) -> dict:
        """`items-push` (Q24, K3 §3.6): the steward's adapter output, as data. Kept in STATE/items.json, then seeded.

        The only way items reach either server: neither ever imports or runs the
        project's adapter, which is project code an agent can write.
        """
        why = IT.snapshot_problem(body)
        if why:
            raise RequestError(400, why)
        try:
            IT.store_snapshot(self.cfg.state, body)
        except ValueError as e:   # over MAX_ITEMS: nothing was written
            raise RequestError(413, str(e)) from None
        try:
            added = self.seed()
        except StoreError as e:   # a pushed seed the store refuses: the items stand, the seed is named
            raise RequestError(400, f"items kept; a seed question was refused: {e}") from None
        self._bump()
        return {"items": len(body["items"]), "seeds_added": added}

    def tags(self, view: dict, items: dict[str, dict]) -> dict:
        """Suggested next steps (0.8.0). A failure here costs the chips, never the page."""
        try:
            return T.compute(self.store, view, items, self.cfg.root, self.project.specs_dir,
                             times=None if G.is_open() else SG.PushedTimes(self.cfg.state))
        except Exception as e:  # a spec caught mid-write, git gone odd: named in the log
            sys.stderr.write(f"console tags: {type(e).__name__}: {e}\n")
            return {"questions": {}, "forks": {}, "specs_dir": self.project.specs_dir, "basis": [],
                    "notes": ["the suggested next steps could not be worked out just now"],
                    **self._no_git("git")}

    # -- visuals (0.8.0) -----------------------------------------------------------

    def add_visual(self, body: object, agent: str | None = None) -> dict:
        """Store an agent's visual in STATE/visuals/ and append its record (0.8.1: never in the project).

        The request is checked against the store's rules on a trial copy
        FIRST, so a visual the store would refuse writes no file. No
        `visuals_dir` is needed: that is only where `agent.py visual-export`
        lands visuals by PR, and the server never writes into the project.
        """
        keys = {"request", "format", "title", "content", "text", "nonce"}
        if not isinstance(body, dict) or set(body) != keys:
            raise RequestError(400, f"visual takes exactly {', '.join(sorted(keys))}")
        fmt, content = body["format"], body["content"]
        if fmt not in S.VISUAL_FORMATS:
            raise RequestError(400, f"format {fmt!r} is not one of {', '.join(S.VISUAL_FORMATS)}")
        if not isinstance(content, str) or not content.strip():
            raise RequestError(400, "content must be the visual's text: a Mermaid block or an HTML document")
        data = content.encode("utf-8")
        if len(data) > S.MAX_VISUAL:
            raise RequestError(413, f"the visual is {len(data)} bytes; the limit is {S.MAX_VISUAL}. It is "
                                    f"refused, not cut: split it into smaller views")
        req_id = body["request"]
        with self._lock:
            items = self.items()
            req = self.store.get(req_id) if isinstance(req_id, str) else None
            if req is None or req["type"] != "message" or req.get("intent") != "visual":
                raise RequestError(400, f"request {req_id!r} is not an owner visual request")
            sha = hashlib.sha256(data).hexdigest()
            path, doc_path = VIS.paths(req["item"], req["id"], fmt, sha)
            rec = {"item": req["item"], "request": req["id"], "format": fmt, "title": body["title"],
                   "text": body["text"], "path": path, "doc_path": doc_path, "sha256": sha, "bytes": len(data),
                   "nonce": body["nonce"]}
            full = {**rec, "type": "visual", "schemaVersion": S.SCHEMA_VERSION, "by": "agent"}
            done = self.store.existing(full)
            if done is None:
                try:
                    self.store.trial().append(full)  # every rule, nothing written
                except StoreError as e:
                    raise RequestError(400, str(e)) from None
            try:
                name = path.rsplit("/", 1)[1]
                VIS.store(self.cfg.state, path, doc_path, data,
                          VIS.doc_markdown(body["title"], req["item"], fmt, name, body["text"], req["text"]))
            except (VIS.VisualError, OSError) as e:
                raise RequestError(409, f"the visual was not stored: {e}") from None
            stored = done or self._append("visual", rec, "agent", items)
            self._name(stored, agent)
        vdir = self.project.visuals_dir
        land = (f"stored in the console's state; land it by PR with `agent.py visual-export --project "
                f"YOUR_WORKTREE`, which writes it under {vdir}/ there" if vdir else
                "stored in the console's state and shown in the console; this project sets no visuals_dir, "
                "so it has nowhere to land in the repository")
        return {"record": stored, "land": land}

    def visual(self, rid: str) -> tuple[bytes, str]:
        """A stored visual's bytes and format, read from STATE and checked now; refused by name when changed."""
        rec = self.store.get(rid)
        if rec is None or rec["type"] != "visual":
            raise RequestError(404, f"no visual {rid}")
        try:
            return VIS.read(self.cfg.state, rec), rec["format"]
        except VIS.VisualError as e:
            raise RequestError(409, str(e)) from None

    def visual_export(self, body: object) -> dict:
        """Stored visuals for `agent.py visual-export`, each read from STATE and re-checked (0.8.1).

        Writes nothing. `ids` names visual records; empty means every one. A
        visual that fails its check is listed under `refused` with why, never
        sent. `root` is the directory this server runs from, so the agent can
        refuse to export into it; `records` is every visual record, for the
        destination's INDEX.md.
        """
        if not isinstance(body, dict) or set(body) != {"ids"} or not isinstance(body["ids"], list) or not all(
                isinstance(i, str) and S.RECORD_ID.match(i) for i in body["ids"]):
            raise RequestError(400, "visual-export takes exactly ids: a list of visual record ids (may be empty)")
        vdir = self.project.visuals_dir
        if not vdir:
            raise RequestError(400, "this project sets no visuals_dir in .console-kit.json, so a visual has "
                                    "nowhere to land in the repository; it stays viewable in the console")
        records = [r for r in self.store.records() if r["type"] == "visual"]
        by_id = {r["id"]: r for r in records}
        missing = [i for i in body["ids"] if i not in by_id]
        if missing:
            raise RequestError(404, f"no visual {', '.join(missing)}")
        chosen = [by_id[i] for i in dict.fromkeys(body["ids"])] if body["ids"] else records
        out, refused = [], []
        for r in chosen:
            req = self.store.get(r["request"]) or {}
            try:
                content = VIS.read(self.cfg.state, r).decode("utf-8")
                doc = VIS.read_doc(self.cfg.state, r, req.get("text", ""))
            except (VIS.VisualError, UnicodeDecodeError) as e:
                refused.append({"id": r["id"], "why": str(e)})
                continue
            out.append({**r, "content": content, "doc": doc})
        return {"root": str(Path(self.cfg.root).resolve()), "visuals_dir": vdir, "visuals": out,
                "refused": refused, "records": records}

    @staticmethod
    def _status(items: dict[str, dict]) -> dict[str, str | None]:
        return {k: v.get("status") for k, v in items.items()}

    def check(self) -> dict:
        """Why each stale answer is stale, condition by condition (0.5.0).

        A read, like `payload`, so it does not hold the write lock. The server
        starts no git (CONSOLE-kit/Q23, `gitseam.close` in `serve`): a condition
        that would have needed the version it was locked against says git
        history is unavailable, and so does `history` here.
        """
        items = self.items()
        return {"stale": A.check(self.store, self.cfg.root, self._status(items), history=self._history()),
                **self._history_note()}

    @staticmethod
    def _no_git(key: str) -> dict:
        """`{key: "unavailable (no git in the server)"}` once the seam is closed (as `serve` closes it), else {}."""
        return {} if G.is_open() else {key: G.UNAVAILABLE}

    def _history(self):
        """Where past versions come from: git here, or (seam closed) only blobs the steward pushed and proved."""
        return None if G.is_open() else SG.PushedHistory(self.cfg.state)

    def _history_note(self) -> dict:
        """`history`: unset with git here; else what the steward last pushed, or the label when it never has."""
        if G.is_open():
            return {}
        pushed = SG.pushed_at(self.cfg.state)
        return {"history": f"{SG.FROM}, pushed {pushed}" if pushed else G.UNAVAILABLE}

    # -- git, from the steward (CONSOLE-kit/Q23, part 2) ----------------------------------------

    def history_wants(self) -> dict:
        """What `agent.py history-push` should compute: every path in it is the server's own, never the client's."""
        items = self.items()
        return SG.wants(self.store, self.cfg.root, self._status(items),
                        T.cited_specs(self.store, self.cfg.root, self.project.specs_dir))

    def push_history_blob(self, body: object) -> dict:
        try:
            with self._lock:
                out = SG.push_blob(self.cfg.state, self.store, body)
        except SG.PushError as e:
            raise RequestError(400, str(e)) from None
        self._bump()
        return out

    def push_history_specs(self, body: object) -> dict:
        wanted = T.cited_specs(self.store, self.cfg.root, self.project.specs_dir)
        try:
            with self._lock:
                out = SG.push_specs(self.cfg.state, self.store, body, wanted)
        except SG.PushError as e:
            raise RequestError(400, str(e)) from None
        self._bump()
        return out

    def reanchor(self, body: object) -> dict:
        """Re-anchor every stale lock that git history can justify; with dry_run, only say what would change.

        The anchors are computed here from the tree and its history, never taken
        from the request, and each goes in as a new `anchor` record: the store is
        append-only, so nothing already written changes. The server reads no git
        history (CONSOLE-kit/Q23), so today every file-hash condition is left
        stale with that reason, and `history` says so.
        """
        if not isinstance(body, dict) or set(body) - {"dry_run"} or not isinstance(body.get("dry_run", True), bool):
            raise RequestError(400, 'reanchor takes {"dry_run": true|false}')
        dry = body.get("dry_run", True)
        # Planned outside the write lock (git can take seconds); a lock that is no
        # longer current by the time it is written is skipped, never re-anchored.
        plan = A.plan_reanchor(self.store, self.cfg.root, self._status(self.items()), history=self._history())
        if dry:
            return {"dry_run": True, "plan": plan, **self._history_note()}
        with self._lock:
            for p in plan:
                if not p["changes"]:
                    continue
                head = self.store.head(p["qid"])
                lk = self.store.lock_of(head["id"]) if head is not None else None
                if (lk is None or lk["id"] != p["lock"]
                        or A.conditions_for(self.store, self.store.question(p["qid"]))[0] != p["base"]):
                    p["skipped"] = "the answer or its anchors changed while this ran; run it again"
                    continue
                # The plan read the files before this lock was taken: read them again, and
                # write nothing a file changed in the meantime no longer supports.
                why_not = A.still_supported(p, A.Tree(self.cfg.root, self._status(self.items())))
                if why_not:
                    p["skipped"] = why_not
                    continue
                basis = "; ".join(f"{c['from']['path']}: {c['why']}" for c in p["changes"])
                try:
                    rec = self.store.append({"type": "anchor", "schemaVersion": S.SCHEMA_VERSION, "by": "agent",
                                             "qid": p["qid"], "lock": p["lock"], "anchors": p["anchors"],
                                             "basis": basis, "nonce": secrets.token_urlsafe(12)})
                except StoreError as e:  # named in the result; the rest of the run goes on
                    p["error"] = str(e)
                    continue
                p["record"] = rec["id"]
        if any("record" in p for p in plan):
            self._bump()
        return {"dry_run": dry, "plan": plan, **self._history_note()}

    def _lock_anchors(self, body: dict, items: dict[str, dict]) -> None:
        """Give a RE-lock fresh anchors from the tree as it is now (0.5.0); a first lock takes none.

        A first lock is decided by the question's `valid_if`, exactly as in
        0.4.0. A lock on a question that was locked before starts from the
        conditions that decided the previous lock and re-reads each one (see
        `anchors.fresh_anchors`). They are written only when they differ from
        the question's `valid_if`.
        """
        q = self.store.question(body.get("qid")) if isinstance(body.get("qid"), str) else None
        before = self.store.locks(q["qid"]) if q is not None else []
        if not before:
            return
        prev = before[-1]
        a = self.store.anchor_of(prev["id"])
        base = a["anchors"] if a else prev.get("anchors", q["valid_if"])
        fresh = A.fresh_anchors(base, A.Tree(self.cfg.root, self._status(items)))
        if fresh != q["valid_if"]:
            body["anchors"] = fresh

    def board(self) -> dict | None:
        """The page's live values (AB-2/Q4), or None when the adapter offers none.

        The committed page stays the page; this only lets an open tab catch up.
        The adapter is project code, so its answer is checked here rather than
        trusted: a malformed one is an error, never something the page applies.
        """
        fn = getattr(self.adapter, "board", None)
        if not callable(fn):
            return None
        got = fn()
        if got is None and isinstance(self.adapter, IT.SnapshotAdapter):
            return None   # the steward pushed no board (or nothing yet): the door's 404, as on the one server
        values = got.get("values") if isinstance(got, dict) else None
        if (not isinstance(got.get("shape") if isinstance(got, dict) else None, str)
                or not isinstance(values, dict)
                or not all(isinstance(k, str) and isinstance(v, str) for k, v in values.items())):
            raise BoardError("the adapter's board() must return {shape: str, values: {str: str}}")
        return {"shape": got["shape"], "values": values}

    def page(self) -> str:
        """The steward's snapshot from STATE with the console injected (CONSOLE-kit/Q28); never a project file.

        `cfg.page` is not read, by this server or the one server: a page an agent
        can edit would put its script in the owner's browser.
        """
        block = P.console_block(json.dumps({"api": "/api", "project": self.cfg.project}))
        return PS.render(self.cfg.state, block)

    def push_page_snapshot(self, body: object) -> dict:
        """`page-snapshot` (Q28): a page the steward read from a commit, as data.

        It is only STAGED (Q29): shown to the owner as a proposal, never served,
        until they press "Use this page" (`publish_page`, the owner door only).
        """
        why = PS.snapshot_problem(body)
        if why:
            raise RequestError(400, why)
        with self._lock:
            PS.stage(self.cfg.state, body)
        return {"staged": body["commit"], "ref": body["ref"], "reviewed": body["reviewed"]}

    def publish_page(self, body: object) -> dict:
        """The owner's "Use this page" (Q29): the staged page, re-checked, becomes the served one.

        No agent route reaches this. `commit` must be the staged commit the
        owner's page showed, so a page staged after they looked is refused.
        The ancestor check `agent.py` ran cannot be repeated here: this server
        runs no git.
        """
        if not isinstance(body, dict) or set(body) != {"commit"}:
            raise RequestError(400, 'page-publish takes {"commit": <the staged commit id>}')
        with self._lock:
            try:
                return PS.publish(self.cfg.state, body["commit"])
            except PS.PublishError as e:
                raise RequestError(e.code, str(e)) from None

    def staged_page(self) -> bytes | None:
        """The staged page's bytes for the owner's sandboxed preview; None when nothing (readable) is staged."""
        return PS.staged_page(self.cfg.state)

    def relock(self, body: object) -> list[dict]:
        """Re-lock a locked answer as it stands: one answer superseding it, word for word, and its lock.

        This is how the owner clears a stale marker once they have checked what
        changed. The new lock is re-anchored like any re-lock. Both records go
        in under one hold of the lock, so no other write lands between them; a
        retry with the same nonce returns what the first try wrote.
        """
        if not isinstance(body, dict) or set(body) - {"qid", "reason", "nonce"}:
            raise RequestError(400, "relock takes qid, an optional reason, and nonce")
        qid, nonce = body.get("qid"), body.get("nonce")
        reason = body.get("reason") or DEFAULT_RELOCK_REASON
        if not isinstance(nonce, str) or not S.NONCE.match(nonce) or len(nonce) > 60:
            raise RequestError(400, "nonce must be 8-60 of [A-Za-z0-9_-]")
        if not isinstance(qid, str):
            raise RequestError(400, "qid must be <itemId>/Q<n>")
        with self._lock:
            items = self.items()
            head = self.store.head(qid)
            if head is not None and head.get("nonce") == nonce + "-a":
                ans = head  # a retry: the answer landed the first time
            elif head is None or self.store.lock_of(head["id"]) is None:
                raise RequestError(400, f"{qid} has no locked answer to re-lock")
            else:
                ans = self._append("answer", {"qid": qid, "picks": head["picks"], "own_text": head["own_text"],
                                              "supersedes": head["id"], "reason": reason,
                                              "nonce": nonce + "-a"}, "owner", items)
            return [ans, self._append("lock", {"qid": qid, "answer": ans["id"], "nonce": nonce + "-l"},
                                      "owner", items)]

    def lock_all(self, body: object) -> dict:
        """"Lock all & process" (0.7.0): answer and lock each drafted question of one round, then send ONE process request.

        The store is append-only, so a batch cannot be undone halfway. It is
        made as close to all-or-nothing as that allows, and resumable where it
        is not:

        1. **Refused whole, before anything is written.** The whole batch,
           the process request included, is first appended to a throwaway
           copy of the store (`Store.trial`) under the write lock. If the
           rules refuse ANY of it, nothing is written, and the answer names
           every question's outcome (409).
        2. **Then written for real, under the same hold of the lock**, so no
           other write can land in between and make the trial wrong.
        3. **A failure while writing** (a full disk) stops there. What was
           written stays written, and the answer names each question as
           `locked` or `not_written` (500). Sending the same drafts again
           resumes: a question already locked with exactly the drafted answer
           is `already_locked` and skipped, and the process request, same
           nonce and same words, is stored once.

        A question whose current answer was locked with a DIFFERENT answer
        since the drafts were made is refused, never superseded here: a
        supersede needs the owner's reason (D3), from that question's card.
        """
        keys = {"fork", "entries", "nonce"}
        if not isinstance(body, dict) or set(body) != keys:
            raise RequestError(400, 'lock-all takes {"fork": RECORD_ID, "entries": [{"qid", "picks", "own_text"}], '
                                    '"nonce"}')
        fork_id, entries, nonce = body["fork"], body["entries"], body["nonce"]
        if not isinstance(nonce, str) or not S.NONCE.match(nonce) or len(nonce) > LOCK_ALL_NONCE:
            raise RequestError(400, f"nonce must be 8-{LOCK_ALL_NONCE} of [A-Za-z0-9_-]")
        if not isinstance(fork_id, str) or not S.RECORD_ID.match(fork_id):
            raise RequestError(400, "fork must be the id of the round's fork message (24 lowercase hex)")
        if (not isinstance(entries, list) or not 1 <= len(entries) <= MAX_LOCK_ALL
                or not all(isinstance(e, dict) and set(e) == {"qid", "picks", "own_text"} for e in entries)):
            raise RequestError(400, f"entries must be 1 to {MAX_LOCK_ALL} objects of exactly qid, picks, own_text")
        qids = [e["qid"] for e in entries]
        if not all(isinstance(q, str) for q in qids) or len(set(qids)) != len(qids):
            raise RequestError(400, "each entry names a different qid")
        with self._lock:
            items = self.items()
            fork = self.store.get(fork_id)
            if fork is None or fork["type"] != "message" or fork.get("intent") != "fork":
                raise RequestError(400, f"fork {fork_id} is not an owner fork message")
            proc_body = {"item": fork["item"], "intent": "process", "nonce": nonce + "-p",
                         "text": "Answers are in: process them. Locked from the review form (round "
                                 f"{fork_id[:8]}): {', '.join(qids)}."}
            # 1. The trial: every rule, nothing written.
            trial = self.store.trial()
            results, plan, ok = [], [], True
            for n, e in enumerate(entries):
                res = {"qid": e["qid"]}
                results.append(res)
                try:
                    step = self._lock_all_step(trial, fork_id, n, e, nonce, None, items)
                except RequestError as err:
                    res.update(status="refused", error=str(err))
                    ok = False
                    continue
                res["status"] = "already_locked" if step is None else "ready"
                plan.append((res, e, n, step is not None))
            try:
                trial.append({**proc_body, "type": "message", "schemaVersion": S.SCHEMA_VERSION, "by": "owner"})
            except StoreError as err:
                ok = False
                results.append({"qid": None, "status": "refused", "error": f"the process request: {err}"})
            if not ok:
                raise RequestError(409, "nothing was locked: the store would refuse part of this batch, named "
                                        "below. Fix those and press Lock all again", {"results": results})
            # 2. For real, under the same hold of the lock.
            for i, (res, e, n, todo) in enumerate(plan):
                if not todo:
                    continue
                try:
                    ans, lk = self._lock_all_step(self.store, fork_id, n, e, nonce, self._append, items)
                except (RequestError, OSError) as err:
                    res.update(status="not_written", error=f"{type(err).__name__}: {err}")
                    for later, _e, _n, later_todo in plan[i + 1:]:
                        if later_todo:
                            later.update(status="not_written", error="not reached: an earlier write failed")
                    raise RequestError(500, "the batch stopped part way: the questions marked locked are "
                                            "locked. Nothing else was written. Press Lock all again to finish; "
                                            "what is already locked is skipped", {"results": results}) from None
                res.update(status="locked", answer=ans["id"], lock=lk["id"])
            try:
                proc = self._append("message", proc_body, "owner", items)
            except (RequestError, OSError) as err:
                raise RequestError(500, f"every answer is locked, but the process request was not written "
                                        f"({type(err).__name__}: {err}). Press Lock all again to send it",
                                   {"results": results}) from None
        return {"results": results, "process": proc}

    def _lock_all_step(self, store: Store, fork_id: str, n: int, e: dict, nonce: str,
                       append, items: dict) -> tuple[dict, dict] | None:
        """Answer (if the draft differs from the current answer) and lock one question; None when already locked so.

        With `append` None it appends to `store` directly (the trial); with
        `self._append`, it writes for real (anchors, doorbell, live bump).
        """
        qid = e["qid"]
        q = store.question(qid)
        if q is None or q.get("forked_from") != fork_id:
            raise RequestError(400, f"{qid} is not a question of this round")
        picks, own = e["picks"], e["own_text"]
        head = store.head(qid)
        same = (head is not None and isinstance(picks, list) and isinstance(own, str)
                and sorted(head["picks"]) == sorted(p for p in picks if isinstance(p, str))
                and len(head["picks"]) == len(picks) and head["own_text"] == own)
        if head is not None and store.lock_of(head["id"]) is not None:
            if same:
                return None
            raise RequestError(409, f"{qid} was locked with a different answer since this was drafted; to change "
                                    f"it, supersede it from its card and give a reason")

        def put(kind: str, rec: dict) -> dict:
            if append is not None:
                return append(kind, rec, "owner", items)
            try:
                return store.append({**rec, "type": kind, "schemaVersion": S.SCHEMA_VERSION, "by": "owner"})
            except StoreError as err:
                raise RequestError(400, str(err)) from None
        ans = head if same else put("answer", {"qid": qid, "picks": picks, "own_text": own,
                                                "nonce": f"{nonce}-{n}a"})
        lk = put("lock", {"qid": qid, "answer": ans["id"], "nonce": f"{nonce}-{n}l"})
        return ans, lk

    def write(self, kind: str, body: object, by: str, agent: str | None = None) -> dict:
        if agent is not None and by != "agent":
            raise RequestError(400, "only the agent's door names an agent")
        if not isinstance(body, dict):
            raise RequestError(400, "the body must be a JSON object")
        named = [f for f in WRITER_FIELDS + (SERVER_LOCK_FIELDS if kind == "lock" else ()) if f in body]
        if named:
            raise RequestError(400, f"{', '.join(named)} is the server's to set, not the writer's")
        if kind == "question":
            secrets_named = S.secret_condition_paths(body.get("valid_if"))
            if secrets_named:
                raise RequestError(400, "; ".join(secrets_named))
            body = self._fill_evidence(body)  # reads files: outside the write lock
        with self._lock:
            chat = kind == "message" and by == "owner" and body.get("item") == S.CHAT_ITEM
            if chat and self.store.existing({**body, "type": kind, "by": by}) is None:
                self._chat_gate()  # a retry of a message already stored is not a new message
            before = self.store.seq()
            rec = self._append(kind, body, by, self.items())
            if chat and self.store.seq() > before:
                self._chat_times.append(time.monotonic())
            self._name(rec, agent)
            return rec

    def _name(self, rec: dict, agent: str | None) -> None:
        """Keep the agent's name for `rec` beside the store (0.8.2); the caller holds `self._lock`.

        The record is already stored. If the name cannot be written (a full
        disk), the record stays, unnamed, and the failure is logged by name:
        the name is who wrote it, never what was written.
        """
        if agent is None:
            return
        try:
            added = self.names.add(rec["id"], agent)
        except (OSError, N.NamesError) as e:
            sys.stderr.write(f"console names: record {rec['id']} stays unnamed: {type(e).__name__}: {e}\n")
            return
        if added:
            self._bump()   # an open page shows the name without waiting out a poll

    def _append(self, kind: str, body: dict, by: str, items: dict[str, dict]) -> dict:
        """Append one record; the caller holds `self._lock`."""
        if kind == "lock":
            done = self.store.lock_of(body.get("answer")) if isinstance(body.get("answer"), str) else None
            if done is not None and done.get("nonce") == body.get("nonce") and done["qid"] == body.get("qid"):
                return done  # a retried lock: its anchors were computed the first time
            body = dict(body)
            self._lock_anchors(body, items)
        before = len(self.store.records())
        try:
            rec = self.store.append({**body, "type": kind, "schemaVersion": S.SCHEMA_VERSION, "by": by})
        except StoreError as e:
            raise RequestError(400, str(e)) from None
        if len(self.store.records()) > before:  # a retried write rings nothing and wakes nobody
            if by == "owner":
                self._ring(rec)
            self._bump()
        return rec

    def _ring(self, rec: dict) -> None:
        line = {"seq": rec["seq"], "type": rec["type"], "ts": rec["ts"]}
        # `intent` lets the queue show a fork without reading the store (§6.3).
        line.update({k: rec[k] for k in ("qid", "item", "intent", "about_qid") if k in rec})
        with open(self.cfg.inbox, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, sort_keys=True) + "\n")

    def read_cursor(self) -> dict:
        try:
            cur = json.loads(self.cfg.cursor.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cur = {}
        by = self.read_working_by()
        return {"last_synced_at": cur.get("last_synced_at"), "last_error": cur.get("last_error"),
                "working": self._merged(by), "working_by": by, "listening": D.listening(self.cfg.state)}

    def health(self) -> tuple[bool, dict]:
        """Whether the server can do its job, in words that hold no owner content and no secret.

        `ok` needs the register to load, since every page and write goes
        through it. Its error is logged, never returned: an adapter's message
        can carry a path or an item's text.
        """
        ok = True
        try:
            self.adapter.items()
        except Exception as e:  # a register caught mid-write, or a broken adapter
            sys.stderr.write(f"console health: register: {type(e).__name__}: {e}\n")
            ok = False
        # Degraded, not down: unreadable stored items (the single server serves on) stay a 200, since a restart
        # cannot fix them and only a push can; a 503 would invite a supervisor's restart loop. The state is in
        # the body instead: register "error" and a note that names what to do, never the file's path.
        degraded = ok and bool(getattr(self.adapter, "problem", None))
        agent = D.listening(self.cfg.state)["state"]
        body = {"ok": ok, "version": __version__, "store_seq": len(self.store.records()),
                "register": "ok" if ok and not degraded else "error", "agent": agent,
                "agent_listening": agent == "listening"}
        if degraded:
            body["register_note"] = IT.UNREADABLE
        return ok, body

    # "Agent active" (owner, 2026-09-29): an agent that picks up a request marks
    # the items it is working on, and its next cursor post (synced, or an error)
    # clears them. "Awaiting agent" alone only says the owner wrote last, which
    # is just as true when no session is running, so the console says "active"
    # only on this mark, and only while it is fresh: a session that dies
    # mid-work cannot leave the owner a standing false "active".
    #
    # 0.8.2 (owner, 2026-09-30, "Names + own markers ★"): the marks are kept per
    # agent, {bucket: {item: time}}, where a bucket is an agent's name and every
    # session that gives none shares the bucket "agent". A cursor post clears
    # only the caller's bucket, so one session finishing never wipes another's
    # marks. A 0.8.1 file (a flat {item: time}) reads as the "agent" bucket. A
    # 0.8.1 kit reading a 0.8.2 file skips every entry (a bucket's value is not a
    # time) and shows no marks: nothing breaks, the marks are only not shown.
    def read_working(self) -> dict:
        """Every fresh mark as {item: time}, the newest time where several agents mark one item."""
        return self._merged(self.read_working_by())

    @staticmethod
    def _merged(by: dict[str, dict[str, str]]) -> dict[str, str]:
        out: dict[str, str] = {}
        for marks in by.values():
            for item, ts in marks.items():
                if ts > out.get(item, ""):
                    out[item] = ts
        return out

    def read_working_by(self) -> dict[str, dict[str, str]]:
        """Every fresh mark by bucket: {agent name or "agent": {item: time}}; an empty bucket is left out."""
        try:
            raw = json.loads(self.cfg.working.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        now = time.time()

        def fresh(marks: dict) -> dict[str, str]:
            got = {}
            for item, ts in marks.items():
                try:
                    age = now - calendar.timegm(time.strptime(ts, TS_FORMAT))
                except (TypeError, ValueError):
                    continue
                if isinstance(item, str) and S.ITEM_ID.match(item) and 0 <= age < WORKING_TTL:
                    got[item] = ts
            return got

        out: dict[str, dict[str, str]] = {}
        legacy = {k: v for k, v in raw.items() if isinstance(v, str)}      # a 0.8.1 file: item -> time
        for bucket, marks in raw.items():
            if isinstance(marks, dict) and (bucket == N.UNNAMED or N.problem(bucket) is None):
                out[bucket] = fresh(marks)
        if legacy:
            out[N.UNNAMED] = {**fresh(legacy), **out.get(N.UNNAMED, {})}
        return {b: m for b, m in sorted(out.items()) if m}

    def _write_working(self, by: dict[str, dict[str, str]]) -> None:
        tmp = self.cfg.working.with_suffix(".tmp")
        tmp.write_text(json.dumps({b: m for b, m in by.items() if m}, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.cfg.working)

    def set_working(self, body: object, agent: str | None = None) -> dict:
        items = body.get("items") if isinstance(body, dict) and set(body) == {"items"} else None
        if not (isinstance(items, list) and 0 < len(items) <= MAX_WORKING
                and all(isinstance(i, str) and len(i) <= 128 and S.ITEM_ID.match(i) for i in items)):
            raise RequestError(400, f"working takes {{\"items\": [1 to {MAX_WORKING} item ids]}}")
        now = time.strftime(TS_FORMAT, time.gmtime())
        bucket = agent or N.UNNAMED
        with self._working_lock:
            by = self.read_working_by()
            by[bucket] = {**by.get(bucket, {}), **{i: now for i in items}}
            if len(by[bucket]) > MAX_WORKING:  # one agent holds at most MAX_WORKING marks: its newest
                by[bucket] = dict(sorted(by[bucket].items(), key=lambda kv: kv[1])[-MAX_WORKING:])
            self._write_working(by)
        self._bump()  # the page shows "agent active" without waiting out a poll
        return self._merged(by)

    def set_cursor(self, body: object, agent: str | None = None) -> dict:
        if not isinstance(body, dict) or set(body) - {"last_synced_at", "last_error"}:
            raise RequestError(400, "the cursor takes only last_synced_at and last_error")
        synced, err = body.get("last_synced_at"), body.get("last_error")
        if synced is not None and not (isinstance(synced, str) and len(synced) <= 40):
            raise RequestError(400, "last_synced_at must be a timestamp string or null")
        if err is not None and S.one_line(err, "last_error"):
            raise RequestError(400, f"last_error must be one line of at most {S.MAX_LINE} characters, or null")
        cur = {"last_synced_at": synced, "last_error": err}
        tmp = self.cfg.cursor.with_suffix(".tmp")
        tmp.write_text(json.dumps(cur, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.cfg.cursor)
        # Synced or failed, THIS agent is no longer at work; every other agent's marks stay (0.8.2).
        with self._working_lock:
            by = self.read_working_by()
            if by.pop(agent or N.UNNAMED, None) is not None:
                self._write_working(by)
        self._bump()
        return cur


class _Handler(BaseHTTPRequestHandler):
    console: Console
    server_version = "console-kit"
    sys_version = ""
    timeout = 30  # seconds per socket read, so a stalled client cannot hold a thread for ever
    max_body = MAX_BODY
    _body_taken = False

    def finish(self) -> None:
        """Read a body no route read (an early refusal: 403, 404, 415) before the connection closes.

        A client may send its body after its headers, in a second write; closing
        with that write still coming makes it fail with EPIPE instead of reading
        the answer it was sent. Only a body within `max_body` is read: a larger
        one is refused unread, as `_body` refuses it.
        """
        try:
            raw = self.headers.get("Content-Length") if getattr(self, "headers", None) is not None else None
            if not self._body_taken and raw and re.fullmatch(r"[0-9]{1,12}", raw.strip()) \
                    and 0 < int(raw) <= self.max_body:
                self._drain(int(raw))
        except OSError:
            pass   # the client went away, or the deadline passed: close, the answer is already sent
        finally:
            super().finish()

    def _drain(self, left: int) -> None:
        """Read and drop up to `left` bytes within DRAIN_SECONDS IN ALL, then stop."""
        self._read_within(left, DRAIN_SECONDS, keep=False)

    def _read_within(self, left: int, seconds: float, keep: bool = True) -> bytes | None:
        """Up to `left` bytes, read within `seconds` IN ALL: what arrived before EOF, or None past the deadline.

        The socket timeout alone is per read and restarts with every byte, so a
        client trickling one byte at a time could hold this thread for hours,
        and ThreadingMixIn caps nothing. Each read gets only the time that is
        left of one total deadline. `keep=False` drops what it reads.
        """
        deadline = time.monotonic() + seconds
        chunks: list[bytes] = []
        try:
            while left > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self.connection.settimeout(remaining)
                got = self.rfile.read1(min(left, 64 * 1024))
                if not got:
                    break
                if keep:
                    chunks.append(got)
                left -= len(got)
        except TimeoutError:
            return None
        finally:
            self.connection.settimeout(self.timeout)   # the answer is written under the usual per-write timeout
        return b"".join(chunks)

    def _send_raw(self, code: int, data: bytes, ctype: str, csp: str) -> None:
        """A body that is not JSON, under its own Content-Security-Policy (0.8.0, a stored visual)."""
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in SECURITY_HEADERS:
            if k != "Content-Security-Policy":
                self.send_header(k, v)
        self.send_header("Content-Security-Policy", csp)
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.end_headers()
        self.wfile.write(data)

    def _send(self, code: int, body: object, ctype: str = "application/json") -> None:
        data = (body if isinstance(body, str) else json.dumps(body)).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype if ctype != "application/json" else "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for k, v in SECURITY_HEADERS:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _view(self, build: Callable[[], dict]) -> None:
        """/view and /api/view: a stored file that fails its check is a 503 naming the problem, never a dropped
        connection. The path stays in the server's stderr: the answer goes to a browser or an agent."""
        try:
            view = build()
        except StoreError as e:
            sys.stderr.write(f"console view: {e}\n")
            return self._send(503, {"error": VIEW_UNREADABLE})
        self._send(200, view)

    def _body(self) -> object:
        if not (self.headers.get("Content-Type") or "").split(";")[0].strip() == "application/json":
            raise RequestError(415, "send application/json")
        raw = self.headers.get("Content-Length")
        if raw is None:
            raise RequestError(411, "send a Content-Length")
        # ASCII digits only. int() takes "-1", and read(-1) reads to the end of the stream, uncapped;
        # str.isdigit() takes "²", which int() then refuses with an uncaught ValueError.
        if not re.fullmatch(r"[0-9]{1,12}", raw.strip()):
            raise RequestError(400, "bad Content-Length")
        n = int(raw)
        if n > self.max_body:
            raise RequestError(413, f"the body is over {self.max_body} bytes")
        self._body_taken = True
        data = self._read_within(n, BODY_SECONDS)
        if data is None:   # the connection is closed after this answer, so the unread rest never matters
            raise RequestError(408, f"the body did not arrive within {BODY_SECONDS:g} seconds")
        try:
            return json.loads(data or b"null")
        except (ValueError, RecursionError):   # bad JSON, bad UTF-8, or nesting deeper than the decoder recurses
            raise RequestError(400, "the body is not JSON") from None

    def log_message(self, fmt: str, *args) -> None:  # the request line only: never a header, so never a token
        sys.stderr.write(f"console {self.address_string()} {fmt % args}\n")


class OwnerHandler(_Handler):
    verify: Callable[[str | None], dict]

    def _send(self, code: int, body: object, ctype: str = "application/json") -> None:
        try:
            super()._send(code, body, ctype)
        except (BrokenPipeError, ConnectionResetError):
            # The page went away mid-answer: a closed tab ends its long poll this way
            # (0.7.0). Nothing is owed to a client that is gone, and no traceback is logged.
            self.close_connection = True

    def _gate(self) -> bool:
        try:
            self.verify(self.headers.get("Cf-Access-Jwt-Assertion"))
            return True
        except AuthError as e:
            self._send(403, {"error": str(e)})
            return False

    def do_GET(self) -> None:
        if not self._gate():
            return
        if self.path in ("/", "/index.html"):
            return self._send(200, self.console.page(), "text/html; charset=utf-8")
        if self.path == "/api/view":
            return self._view(self.console.page_payload)   # with `ver`: the live loop's baseline
        if self.path == "/api/board":
            return self._board()
        if self.path == "/api/check":
            return self._check()
        if self.path == "/api/usage":  # 0.8.6: the footer's probe
            return self._send(200, read_usage(self.console.cfg))
        if self.path == "/api/page-staged":   # Q29: the proposal's preview, framed sandbox="" and sandboxed here too
            data = self.console.staged_page()
            if data is None:
                return self._send(404, {"error": "no dashboard page is staged"})
            return self._send_raw(200, data, "text/html; charset=utf-8", VISUAL_HTML_CSP)
        route, query = self._query()
        live = {"/api/wait": self._wait, "/api/feed": self._feed, "/api/evidence": self._evidence,
                "/api/visual": self._visual}.get(route)
        if live is not None and query is not None:
            try:
                return live(query)
            except RequestError as e:
                return self._send(e.code, {"error": str(e)})
        if live is not None:
            return self._send(400, {"error": "a query string must be plain key=value pairs"})
        self._send(404, {"error": "not found"})

    # -- the live console (0.7.0): every route below is behind `_gate` in do_GET --

    def _query(self) -> tuple[str, dict[str, str] | None]:
        """The path and its query as one value per key; None for a query with a repeated or blank key."""
        parts = urlsplit(self.path)
        try:
            q = parse_qs(parts.query, keep_blank_values=True, strict_parsing=bool(parts.query), max_num_fields=8)
        except ValueError:
            return parts.path, None
        if any(len(v) != 1 for v in q.values()):
            return parts.path, None
        return parts.path, {k: v[0] for k, v in q.items()}

    @staticmethod
    def _only(query: dict[str, str], allowed: set[str], route: str) -> None:
        extra = sorted(set(query) - allowed)
        if extra:
            raise RequestError(400, f"{route} takes only {', '.join(sorted(allowed))}; not {', '.join(extra)}")

    @staticmethod
    def _int(v: str | None, name: str, lo: int, hi: int) -> int | None:
        if v is None:
            return None
        if not re.fullmatch(r"[0-9]{1,9}", v) or not lo <= int(v) <= hi:
            raise RequestError(400, f"{name} must be a whole number from {lo} to {hi}")
        return int(v)

    def _wait(self, query: dict[str, str]) -> None:
        self._only(query, {"since", "ver", "timeout"}, "/api/wait")
        since = self._int(query.get("since"), "since", 0, 10**9)
        if since is None:
            raise RequestError(400, "/api/wait needs since=<the store seq the page holds>")
        ver = query.get("ver")
        if ver is not None and not re.fullmatch(r"[0-9a-f]{8}\.[0-9]{1,12}", ver):
            raise RequestError(400, "ver must be the token a previous /api/wait returned")
        raw = query.get("timeout", str(int(WAIT_MAX)))
        if not re.fullmatch(r"[0-9]{1,2}(\.[0-9]{1,3})?", raw) or float(raw) > WAIT_MAX:
            raise RequestError(400, f"timeout is 0 to {WAIT_MAX:g} seconds")
        self._send(200, self.console.wait(since, ver, float(raw)))

    def _feed(self, query: dict[str, str]) -> None:
        self._only(query, {"kind", "item", "before", "limit"}, "/api/feed")
        kinds = None
        if query.get("kind"):
            kinds = set(query["kind"].split(","))
            bad = sorted(kinds - set(V.FEED_KINDS))
            if bad:
                raise RequestError(400, f"kind {', '.join(bad)} is not one of {', '.join(V.FEED_KINDS)}")
        item = query.get("item") or None
        if item is not None and item != S.CHAT_ITEM and (len(item) > 128 or not S.ITEM_ID.match(item)):
            raise RequestError(400, "item must be an item id")
        before = self._int(query.get("before"), "before", 1, 10**9)
        limit = self._int(query.get("limit"), "limit", 1, V.FEED_MAX) or V.FEED_DEFAULT
        self._send(200, self.console.feed(kinds, item, before, limit))

    def _evidence(self, query: dict[str, str]) -> None:
        self._only(query, {"qid"}, "/api/evidence")
        qid = query.get("qid")
        if not isinstance(qid, str) or not S.QID.match(qid):
            raise RequestError(400, "qid must be <itemId>/Q<n>")
        try:
            out = self.console.evidence(qid)
        except RequestError:
            raise
        except Exception as e:  # a file caught mid-write: this read is skipped, named in the log
            sys.stderr.write(f"console evidence: {type(e).__name__}: {e}\n")
            raise RequestError(503, "the cited lines could not be read just now") from None
        self._send(200, out)

    def _visual(self, query: dict[str, str]) -> None:
        """A stored visual (0.8.0). HTML goes out sandboxed by its own CSP; Mermaid as plain text."""
        self._only(query, {"id"}, "/api/visual")
        rid = query.get("id")
        if not isinstance(rid, str) or not S.RECORD_ID.match(rid):
            raise RequestError(400, "id must be a visual's record id (24 lowercase hex)")
        data, fmt = self.console.visual(rid)
        if fmt == "html":
            return self._send_raw(200, data, "text/html; charset=utf-8", VISUAL_HTML_CSP)
        self._send_raw(200, data, "text/plain; charset=utf-8", VISUAL_TEXT_CSP)

    def _check(self) -> None:
        try:
            out = self.console.check()
        except Exception as e:  # a file caught mid-write, or git gone odd: this check is skipped, named
            sys.stderr.write(f"console check: {type(e).__name__}: {e}\n")
            return self._send(503, {"error": "the stale check could not run just now"})
        self._send(200, out)

    def _board(self) -> None:
        try:
            board = self.console.board()
        except Exception as e:  # a register caught mid-write, or a bad adapter: this poll is skipped, the page stands
            sys.stderr.write(f"console board: {type(e).__name__}: {e}\n")
            return self._send(503, {"error": "the board could not be read just now"})
        if board is None:
            return self._send(404, {"error": "this project's adapter offers no board()"})
        self._send(200, board)

    def _other_method(self) -> None:
        # Every method a client may send meets the gate first (D5). A method name http.server
        # does not know at all is answered 501 before any handler runs, and reveals nothing.
        if self._gate():
            self._send(405, {"error": "method not allowed"})

    do_HEAD = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _other_method

    def do_POST(self) -> None:
        if not self._gate():
            return
        kind = OWNER_ROUTES.get(self.path)
        if kind is None and self.path not in ("/api/relock", "/api/lock-all", "/api/page-publish"):
            return self._send(404, {"error": "not found"})
        # Browsers send Origin on every POST, same-origin included, so a missing one is refused too:
        # an absent header must not read as "trusted".
        if self.headers.get("Origin") != f"https://{self.console.cfg.hostname}":
            return self._send(403, {"error": "a write from another site, or with no Origin, is refused"})
        try:
            if self.path == "/api/relock":
                return self._send(200, {"records": self.console.relock(self._body())})
            if self.path == "/api/lock-all":
                return self._send(200, self.console.lock_all(self._body()))
            if self.path == "/api/page-publish":   # Q29: the owner's "Use this page"; no agent route publishes
                return self._send(200, self.console.publish_page(self._body()))
            self._send(200, {"record": self.console.write(kind, self._body(), "owner")})
        except RequestError as e:
            self._send(e.code, {**e.extra, "error": str(e)})


class AgentHandler(_Handler):
    max_body = AGENT_MAX_BODY

    def address_string(self) -> str:
        return "agent"

    def do_GET(self) -> None:
        if self.path == "/view":
            return self._view(self.console.payload)
        if self.path == "/check":
            return OwnerHandler._check(self)
        if self.path == "/health":
            ok, body = self.console.health()
            return self._send(200 if ok else 503, body)
        if self.path == "/history-wants":   # Q23 part 2: what `agent.py history-push` should compute
            return self._send(200, self.console.history_wants())
        self._send(404, {"error": "not found"})

    def _agent(self) -> str | None:
        """The caller's agent name from `X-Console-Agent` (0.8.2), None when it gives none; refused by name when bad."""
        values = self.headers.get_all(N.HEADER) or []
        if not values:
            return None
        if len(values) > 1:
            raise RequestError(400, f"{N.HEADER} is sent once")
        why = N.problem(values[0])
        if why:
            raise RequestError(400, why)
        return values[0]

    def do_POST(self) -> None:
        try:
            agent = self._agent()
            if self.path == "/cursor":
                return self._send(200, {"cursor": self.console.set_cursor(self._body(), agent)})
            if self.path == "/working":  # the agent socket only: the owner's side cannot set it
                return self._send(200, {"working": self.console.set_working(self._body(), agent)})
            if self.path == "/reanchor":
                return self._send(200, self.console.reanchor(self._body()))
            if self.path == "/visual":  # 0.8.1: the server stores the file in STATE, never in the project
                return self._send(200, self.console.add_visual(self._body(), agent))
            if self.path == "/visual-export":  # 0.8.1: a read; agent.py writes into the agent's own worktree
                return self._send(200, self.console.visual_export(self._body()))
            if self.path == "/history-blob":   # Q23 part 2: one past version, proved by its own hash
                # Set on the INSTANCE, and safe there: the handler speaks HTTP/1.0 (BaseHTTPRequestHandler's
                # default protocol_version), so one handler object serves exactly one request and the larger
                # limit cannot carry over to another route on a kept-alive connection.
                self.max_body = SG.MAX_BLOB_BODY
                return self._send(200, self.console.push_history_blob(self._body()))
            if self.path == "/history-specs":  # Q23 part 2: spec last-commit times, and a prune of the blobs
                return self._send(200, self.console.push_history_specs(self._body()))
            if self.path == "/items":   # Q24: the ONLY way items reach a server; the adapter ran in the steward
                return self._send(200, self.console.push_items(self._body()))
            if self.path == "/page-snapshot":   # Q28: the ONLY way a page reaches a server; git ran in the steward
                self.max_body = PS.MAX_BODY   # per instance, safe for the same reason as /history-blob above
                return self._send(200, self.console.push_page_snapshot(self._body()))
            kind = AGENT_ROUTES.get(self.path)
            if kind is None:
                return self._send(404, {"error": "not found"})
            self._send(200, {"record": self.console.write(kind, self._body(), "agent", agent)})
        except RequestError as e:
            self._send(e.code, {"error": str(e)})


class HealthHandler(_Handler):
    """The opt-in health door (`--health-port`): GET /health and nothing else, with no Access token.

    Why this is safe without the gate, when the owner's door is not:
    - It is its OWN listener. The tunnel's ingress names the owner's port, so
      the edge cannot route here unless someone re-points the tunnel, and the
      owner's door keeps its rule that no path skips the gate (D5).
    - It is bound to 127.0.0.1 (HOST, never configurable), and a peer that is
      not loopback is refused anyway.
    - A loopback peer is NOT proof of a local caller: cloudflared itself
      connects from loopback. So a request carrying any header a proxy or the
      Cloudflare edge adds (`PROXY_HEADERS`) is refused. The edge sets
      `Cf-Connecting-Ip` and `Cf-Ray` on every request it forwards, and a
      visitor cannot strip them, so a tunnel mis-pointed here still meets 403.
    - `Host` must name loopback, so a web page that re-binds its own hostname
      to 127.0.0.1 cannot read it from the owner's browser.
    - Every method meets these checks first (`_dispatch`): a proxied or
      foreign-Host request is 403 whatever its method, and only a clean local
      request learns anything else (404 for another path, 405 for another method).
    - What it says is not secret and holds nothing the owner wrote: a version,
      a record count, and two words of state.
    """

    PROXY_HEADERS = ("Cf-Connecting-Ip", "Cf-Ray", "Cf-Visitor", "Cf-Ipcountry", "Cf-Warp-Tag-Id",
                     "Cf-Access-Jwt-Assertion", "Cf-Access-Authenticated-User-Email", "Cdn-Loop",
                     "X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto", "X-Real-Ip", "Forwarded", "Via")
    # The listener is AF_INET on 127.0.0.1 only, so no IPv6 peer can connect: `[::1]`
    # is not listed, because no honest client of this port sends it.
    LOCAL_HOSTS = ("127.0.0.1", "localhost")

    def _refusal(self) -> str | None:
        if not ipaddress.ip_address(self.client_address[0]).is_loopback:
            return "health answers loopback callers only"
        if any(self.headers.get(h) is not None for h in self.PROXY_HEADERS):
            return "health answers local callers only, never through a proxy or tunnel"
        m = re.fullmatch(r"(\[[^\]]*\]|[^:\[\]]+)(:[0-9]{1,5})?", (self.headers.get("Host") or "").strip().lower())
        if m is None or m.group(1) not in self.LOCAL_HOSTS:
            return "health answers only a Host of 127.0.0.1 or localhost"
        return None

    def _send(self, code: int, body: object, ctype: str = "application/json") -> None:
        if self.command != "HEAD":
            return super()._send(code, body, ctype)
        # HEAD: the same status and headers, and no body.
        data = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for k, v in SECURITY_HEADERS:
            self.send_header(k, v)
        self.end_headers()

    def _dispatch(self) -> None:
        why = self._refusal()
        if why:
            return self._send(403, {"error": why})
        if self.command != "GET":
            return self._send(405, {"error": "method not allowed"})
        if self.path != "/health":
            return self._send(404, {"error": "this port answers /health only"})
        ok, body = self.console.health()
        self._send(200 if ok else 503, body)

    do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _dispatch


def health_server(console: Console, port: int) -> ThreadingHTTPServer:
    if port == console.cfg.port and port != 0:
        raise SystemExit("--health-port must differ from --port: the owner's door is never ungated")
    srv = ThreadingHTTPServer((HOST, port), type("BoundHealthHandler", (HealthHandler,), {"console": console}))
    srv.daemon_threads = True
    return srv


class UnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def owner_server(console: Console, verify: Callable[[str | None], dict], port: int) -> ThreadingHTTPServer:
    handler = type("BoundOwnerHandler", (OwnerHandler,), {"console": console, "verify": staticmethod(verify)})
    srv = ThreadingHTTPServer((HOST, port), handler)
    srv.daemon_threads = True
    return srv


WORKING_TTL = 3600         # seconds an "agent active" mark stays true without being renewed
MAX_WORKING = 32
TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
SOCKET_PATH_MAX = 107  # sun_path is 108 bytes on Linux, one of them the terminating NUL


def agent_server(console: Console) -> UnixHTTPServer:
    path = console.cfg.socket
    if len(os.fsencode(path)) > SOCKET_PATH_MAX:
        raise SystemExit(f"the agent socket path is {len(os.fsencode(path))} bytes, over the "
                         f"{SOCKET_PATH_MAX} a Unix socket allows: pass a shorter --state ({path})")
    if path.exists() or path.is_symlink():
        if not stat.S_ISSOCK(path.lstat().st_mode):
            raise SystemExit(f"{path} exists and is not a socket; refusing to replace it")
        path.unlink()
    old = os.umask(0o177)
    try:
        srv = UnixHTTPServer(str(path), type("BoundAgentHandler", (AgentHandler,), {"console": console}))
    finally:
        os.umask(old)
    os.chmod(path, 0o600)
    return srv


def serve(cfg: Config, verify: Callable[[str | None], dict] | None = None) -> None:
    # CONSOLE-kit/Q23: this process starts no git, whatever any later code asks for. First, before
    # anything reads the project: a git here would obey a .git/config an agent can write.
    G.close()
    # CONSOLE-kit/Q24 ("items_push_now"): this process never imports or runs the project's adapter either.
    # It is project code an agent can write; it runs in the steward (`agent.py items-push`), and only its
    # output reaches here, as data, kept in STATE/items.json. `cfg.adapter` is not read.
    # A stored items.json that fails its check does not stop this server (tolerant): it starts, logs one
    # line, shows the board's UNREADABLE note and a not-ok /health, and the next push recovers it live.
    try:
        console = Console(cfg, IT.SnapshotAdapter(cfg.state, tolerant=True))
        added = console.seed()
    except (PC.ConfigError, N.NamesError, StoreError) as e:   # the register itself, or the project's config
        raise SystemExit(f"console: {e}") from None
    sys.stderr.write(f"console: {len(added)} seed question(s) added; store {cfg.store}\n")
    if cfg.page is not None:   # Q28: named, never opened, stat'ed or resolved, in a project root or out of one
        sys.stderr.write(f"console: --page {cfg.page} is not read (CONSOLE-kit/Q28): the page is served from "
                         f"the steward's snapshot in {cfg.state}; run `agent.py page-snapshot` in the project's "
                         f"checkout\n")
    agent = agent_server(console)
    threading.Thread(target=agent.serve_forever, daemon=True).start()
    health = health_server(console, cfg.health_port) if cfg.health_port else None
    if health is not None:
        threading.Thread(target=health.serve_forever, daemon=True).start()
        sys.stderr.write(f"console: health door http://{HOST}:{cfg.health_port}/health (loopback, no proxy)\n")
    owner = owner_server(console, verify or access_verifier(cfg.team_domain, cfg.aud), cfg.port)
    sys.stderr.write(f"console: owner door http://{HOST}:{cfg.port}, agent door {cfg.socket}\n")
    try:
        owner.serve_forever()
    finally:
        agent.shutdown()
        if health is not None:
            health.shutdown()
        cfg.socket.unlink(missing_ok=True)


def agent_request(sock_path: Path, method: str, path: str, body: object = None,
                  agent: str | None = None, token: str | None = None) -> tuple[int, dict]:
    """Call the agent door. Used by `agent.py` and the tests. `agent` (0.8.2) names the calling session;
    `token` (K4) is the project's token for the one server's `/p/<project>/…` routes, sent as a bearer."""
    import http.client

    class _Conn(http.client.HTTPConnection):
        def connect(self) -> None:
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.connect(str(sock_path))

    conn = _Conn("localhost", timeout=10)
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {} if data is None else {"Content-Type": "application/json"}
    if agent is not None:
        headers[N.HEADER] = agent
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    conn.request(method, path, body=data, headers=headers)
    resp = conn.getresponse()
    out = json.loads(resp.read() or b"{}")
    conn.close()
    return resp.status, out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Serve the owner console behind Cloudflare Access.")
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--page", type=Path, default=None,
                    help="IGNORED since Q28: the server never reads a page from a project; it serves the "
                         "steward's snapshot (agent.py page-snapshot). Still accepted so an existing unit starts")
    ap.add_argument("--state", type=Path, required=True)
    ap.add_argument("--adapter", type=Path, default=None,
                    help="IGNORED since Q24: the server never runs the adapter; the steward pushes items "
                         "(agent.py items-push). Still accepted so an existing unit starts")
    ap.add_argument("--team-domain", required=True, help="e.g. yourteam.cloudflareaccess.com")
    ap.add_argument("--aud", required=True, help="the Access application's AUD tag")
    ap.add_argument("--hostname", required=True, help="the public hostname, e.g. console.example.com")
    ap.add_argument("--port", type=int, default=4793)
    ap.add_argument("--project", default="")
    ap.add_argument("--health-port", type=int, default=0,
                    help="also answer GET /health on this loopback port, ungated (default: off; "
                         "/health is always on the agent socket)")
    ap.add_argument("--usage-file", type=Path, default=None,
                    help="a status line's usage snapshot (JSON with five_hour/seven_day), shown in the "
                         "console's footer (default: off)")
    ap.add_argument("--account-file", type=Path, default=None,
                    help="Claude Code's settings file; the footer shows its account email and nothing "
                         "else is read from it (default: off)")
    a = ap.parse_args(argv)
    if not 0 <= a.health_port <= 65535:
        ap.error("--health-port takes 0 (off) or a port number")
    if a.health_port and a.health_port == a.port:
        ap.error("--health-port must differ from --port: the owner's door is never ungated")
    for flag, p in (("--usage-file", a.usage_file), ("--account-file", a.account_file)):
        if p is not None and not p.is_absolute():
            ap.error(f"{flag} takes an absolute path")
    if a.adapter is not None:
        sys.stderr.write("console: --adapter is ignored: this server never runs the adapter; items arrive when "
                         "the steward runs `agent.py items-push`\n")
    serve(Config(root=a.root, page=a.page, state=a.state, adapter=a.adapter or Path(os.devnull),
                 team_domain=a.team_domain,
                 aud=a.aud, hostname=a.hostname, port=a.port, project=a.project, health_port=a.health_port,
                 usage_file=a.usage_file, account_file=a.account_file))
    return 0

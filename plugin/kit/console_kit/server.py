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

`/health` (0.6.0) is answered on the agent's door, and on a third door only
when `--health-port` asks for one. It is NEVER answered on the owner's door
without the Access token: that door keeps its rule that no path skips the
gate. See `HealthHandler` for why the health door is safe to leave ungated.
"""

from __future__ import annotations

import argparse
import calendar
import collections
import ipaddress
import json
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlsplit

from . import __version__
from . import anchors as A
from . import doorbell as D
from . import publish as P
from . import schema as S
from . import view as V
from .fold import load_adapter
from .store import Store, StoreError

HOST = "127.0.0.1"          # never configurable: the tunnel is the only way in
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
AGENT_ROUTES = {"/question": "question", "/message": "message"}
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
    page: Path          # the committed lane board the console is injected into
    state: Path         # store.jsonl, inbox.jsonl, cursor.json and agent.sock live here
    adapter: Path
    team_domain: str
    aud: str
    hostname: str       # the public hostname; a POST from any other browser Origin is refused
    port: int = 4793
    project: str = ""
    health_port: int = 0  # 0: no health port; /health is still on the agent socket

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


class Console:
    """The store, the adapter and the doorbell, behind one lock."""

    def __init__(self, cfg: Config, adapter) -> None:
        self.cfg = cfg
        self.adapter = adapter
        cfg.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(cfg.state, 0o700)  # mkdir's mode is ignored for a directory that already exists
        self.store = Store(cfg.store)
        self._lock = threading.Lock()
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
        return V.feed(self.store, self.items(), kinds=kinds, item=item, before=before, limit=limit)

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
        return {"view": V.build(self.store, items, holds), "items": items, "cursor": self.read_cursor()}

    @staticmethod
    def _status(items: dict[str, dict]) -> dict[str, str | None]:
        return {k: v.get("status") for k, v in items.items()}

    def check(self) -> dict:
        """Why each stale answer is stale, condition by condition (0.5.0).

        A read, like `payload`, so it does not hold the write lock: its git
        calls can take seconds, and the owner's writes must not wait on them.
        """
        items = self.items()
        return {"stale": A.check(self.store, self.cfg.root, self._status(items))}

    def reanchor(self, body: object) -> dict:
        """Re-anchor every stale lock that git history can justify; with dry_run, only say what would change.

        The anchors are computed here from the tree and its history, never taken
        from the request, and each goes in as a new `anchor` record: the store is
        append-only, so nothing already written changes.
        """
        if not isinstance(body, dict) or set(body) - {"dry_run"} or not isinstance(body.get("dry_run", True), bool):
            raise RequestError(400, 'reanchor takes {"dry_run": true|false}')
        dry = body.get("dry_run", True)
        # Planned outside the write lock (git can take seconds); a lock that is no
        # longer current by the time it is written is skipped, never re-anchored.
        plan = A.plan_reanchor(self.store, self.cfg.root, self._status(self.items()))
        if dry:
            return {"dry_run": True, "plan": plan}
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
        return {"dry_run": dry, "plan": plan}

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
        values = got.get("values") if isinstance(got, dict) else None
        if (not isinstance(got.get("shape") if isinstance(got, dict) else None, str)
                or not isinstance(values, dict)
                or not all(isinstance(k, str) and isinstance(v, str) for k, v in values.items())):
            raise BoardError("the adapter's board() must return {shape: str, values: {str: str}}")
        return {"shape": got["shape"], "values": values}

    def page(self) -> str:
        block = P.console_block(json.dumps({"api": "/api", "project": self.cfg.project}))
        return P.inject(self.cfg.page.read_text(encoding="utf-8"), block)

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

    def write(self, kind: str, body: object, by: str) -> dict:
        if not isinstance(body, dict):
            raise RequestError(400, "the body must be a JSON object")
        named = [f for f in WRITER_FIELDS + (SERVER_LOCK_FIELDS if kind == "lock" else ()) if f in body]
        if named:
            raise RequestError(400, f"{', '.join(named)} is the server's to set, not the writer's")
        if kind == "question":
            body = self._fill_evidence(body)  # reads files: outside the write lock
        with self._lock:
            chat = kind == "message" and by == "owner" and body.get("item") == S.CHAT_ITEM
            if chat and self.store.existing({**body, "type": kind, "by": by}) is None:
                self._chat_gate()  # a retry of a message already stored is not a new message
            before = self.store.seq()
            rec = self._append(kind, body, by, self.items())
            if chat and self.store.seq() > before:
                self._chat_times.append(time.monotonic())
            return rec

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
        return {"last_synced_at": cur.get("last_synced_at"), "last_error": cur.get("last_error"),
                "working": self.read_working(), "listening": D.listening(self.cfg.state)}

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
        agent = D.listening(self.cfg.state)["state"]
        return ok, {"ok": ok, "version": __version__, "store_seq": len(self.store.records()),
                    "register": "ok" if ok else "error", "agent": agent,
                    "agent_listening": agent == "listening"}

    # "Agent active" (owner, 2026-09-29): an agent that picks up a request marks
    # the items it is working on, and its next cursor post (synced, or an error)
    # clears them. "Awaiting agent" alone only says the owner wrote last, which
    # is just as true when no session is running, so the console says "active"
    # only on this mark, and only while it is fresh: a session that dies
    # mid-work cannot leave the owner a standing false "active".
    def read_working(self) -> dict:
        try:
            raw = json.loads(self.cfg.working.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        now = time.time()
        out = {}
        for item, ts in (raw.items() if isinstance(raw, dict) else ()):
            try:
                age = now - calendar.timegm(time.strptime(ts, TS_FORMAT))
            except (TypeError, ValueError):
                continue
            if isinstance(item, str) and S.ITEM_ID.match(item) and 0 <= age < WORKING_TTL:
                out[item] = ts
        return out

    def set_working(self, body: object) -> dict:
        items = body.get("items") if isinstance(body, dict) and set(body) == {"items"} else None
        if not (isinstance(items, list) and 0 < len(items) <= MAX_WORKING
                and all(isinstance(i, str) and len(i) <= 128 and S.ITEM_ID.match(i) for i in items)):
            raise RequestError(400, f"working takes {{\"items\": [1 to {MAX_WORKING} item ids]}}")
        now = time.strftime(TS_FORMAT, time.gmtime())
        marks = {**self.read_working(), **{i: now for i in items}}
        tmp = self.cfg.working.with_suffix(".tmp")
        tmp.write_text(json.dumps(marks, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.cfg.working)
        self._bump()  # the page shows "agent active" without waiting out a poll
        return marks

    def set_cursor(self, body: object) -> dict:
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
        self.cfg.working.unlink(missing_ok=True)  # synced or failed, the agent is no longer at work
        self._bump()
        return cur


class _Handler(BaseHTTPRequestHandler):
    console: Console
    server_version = "console-kit"
    sys_version = ""
    timeout = 30  # seconds per socket read, so a stalled client cannot hold a thread for ever

    def _send(self, code: int, body: object, ctype: str = "application/json") -> None:
        data = (body if isinstance(body, str) else json.dumps(body)).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype if ctype != "application/json" else "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for k, v in SECURITY_HEADERS:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

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
        if n > MAX_BODY:
            raise RequestError(413, f"the body is over {MAX_BODY} bytes")
        try:
            return json.loads(self.rfile.read(n) or b"null")
        except ValueError:
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
            return self._send(200, self.console.payload())
        if self.path == "/api/board":
            return self._board()
        if self.path == "/api/check":
            return self._check()
        route, query = self._query()
        live = {"/api/wait": self._wait, "/api/feed": self._feed, "/api/evidence": self._evidence}.get(route)
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
        if kind is None and self.path not in ("/api/relock", "/api/lock-all"):
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
            self._send(200, {"record": self.console.write(kind, self._body(), "owner")})
        except RequestError as e:
            self._send(e.code, {**e.extra, "error": str(e)})


class AgentHandler(_Handler):
    def address_string(self) -> str:
        return "agent"

    def do_GET(self) -> None:
        if self.path == "/view":
            return self._send(200, self.console.payload())
        if self.path == "/check":
            return OwnerHandler._check(self)
        if self.path == "/health":
            ok, body = self.console.health()
            return self._send(200 if ok else 503, body)
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        try:
            if self.path == "/cursor":
                return self._send(200, {"cursor": self.console.set_cursor(self._body())})
            if self.path == "/working":  # the agent socket only: the owner's side cannot set it
                return self._send(200, {"working": self.console.set_working(self._body())})
            if self.path == "/reanchor":
                return self._send(200, self.console.reanchor(self._body()))
            kind = AGENT_ROUTES.get(self.path)
            if kind is None:
                return self._send(404, {"error": "not found"})
            self._send(200, {"record": self.console.write(kind, self._body(), "agent")})
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
    console = Console(cfg, load_adapter(cfg.adapter))
    added = console.seed()
    sys.stderr.write(f"console: {len(added)} seed question(s) added; store {cfg.store}\n")
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


def agent_request(sock_path: Path, method: str, path: str, body: object = None) -> tuple[int, dict]:
    """Call the agent door. Used by `agent.py` and the tests."""
    import http.client

    class _Conn(http.client.HTTPConnection):
        def connect(self) -> None:
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.connect(str(sock_path))

    conn = _Conn("localhost", timeout=10)
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {} if data is None else {"Content-Type": "application/json"}
    conn.request(method, path, body=data, headers=headers)
    resp = conn.getresponse()
    out = json.loads(resp.read() or b"{}")
    conn.close()
    return resp.status, out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Serve the owner console behind Cloudflare Access.")
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--page", type=Path, required=True)
    ap.add_argument("--state", type=Path, required=True)
    ap.add_argument("--adapter", type=Path, required=True)
    ap.add_argument("--team-domain", required=True, help="e.g. yourteam.cloudflareaccess.com")
    ap.add_argument("--aud", required=True, help="the Access application's AUD tag")
    ap.add_argument("--hostname", required=True, help="the public hostname, e.g. console.example.com")
    ap.add_argument("--port", type=int, default=4793)
    ap.add_argument("--project", default="")
    ap.add_argument("--health-port", type=int, default=0,
                    help="also answer GET /health on this loopback port, ungated (default: off; "
                         "/health is always on the agent socket)")
    a = ap.parse_args(argv)
    if not 0 <= a.health_port <= 65535:
        ap.error("--health-port takes 0 (off) or a port number")
    if a.health_port and a.health_port == a.port:
        ap.error("--health-port must differ from --port: the owner's door is never ungated")
    serve(Config(root=a.root, page=a.page, state=a.state, adapter=a.adapter, team_domain=a.team_domain,
                 aud=a.aud, hostname=a.hostname, port=a.port, project=a.project, health_port=a.health_port))
    return 0

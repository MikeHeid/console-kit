"""The project's items, as its steward pushes them: the one schema and the one store, for both servers (Q24).

No server runs a project's adapter: it is project code an agent can write, so
it runs in the steward's process (`agent.py items-push`), and a server only
receives its three results, `{"items", "seed_questions", "board"}`, as data,
checked by `snapshot_problem` and kept in STATE/items.json. K3 built this for
the one server; Q24 ("items_push_now") closes the single server the same way.

STATE/items.json is read and written relative to a held descriptor of STATE
(`atfile`): opened O_NOFOLLOW, replaced whole by an O_EXCL temporary and a
rename, never resolved by path after STATE was opened.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

from . import atfile as AF
from . import schema as S
from .store import StoreError

ITEMS = "items.json"
MAX_ITEMS = 4 << 20
MAX_ITEM_COUNT = 10_000
ITEM_FIELDS = {"title": (str,), "parent": (str, type(None)), "status": (str, type(None))}
STATE_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC   # STATE itself is the server's own configured folder

# What the board says before the steward's first push: never fake items, never a silent blank (F63).
NOT_PUSHED = ("No items yet: the server never runs the project's adapter, so items appear when the "
              "steward pushes them (agent.py items-push).")
# ... and what it says when a stored items.json cannot be read (the single server tolerates it, below).
UNREADABLE = ("The stored items cannot be read, so none are shown: the steward should push them again "
              "(agent.py items-push).")
SET_ASIDE = ".items.json.set-aside."   # what AF.set_aside_dir renames a directory at items.json to, + hex


def snapshot_problem(doc: object) -> str | None:
    """Why a pushed (or stored) snapshot cannot be served, naming the first bad field; None when it can.

    One check for both doors in: `items-push` refuses with it, and a stored
    STATE/items.json that fails it refuses that project, so no request ever
    reaches the view with an item it cannot read (a list `parent` is unhashable
    there, a number `title` breaks the feed).
    """
    if not isinstance(doc, dict) or set(doc) - {"items", "seed_questions", "board"} or "items" not in doc:
        return 'items-push sends {"items": {...}, "seed_questions": [...], "board": ...}'
    items, seeds, board = doc["items"], doc.get("seed_questions", []), doc.get("board")
    if not isinstance(items, dict) or len(items) > MAX_ITEM_COUNT:
        return f"items is an object of at most {MAX_ITEM_COUNT} item ids"
    for k, v in items.items():
        if not (isinstance(k, str) and len(k) <= 128 and S.ITEM_ID.match(k)):
            return f"items: {k[:40]!r} is not an item id" if isinstance(k, str) else "items: an id is not a string"
        if not isinstance(v, dict):
            return f"items.{k} is not an object"
        if set(v) - set(ITEM_FIELDS):
            return f"items.{k} has unknown fields {sorted(set(v) - set(ITEM_FIELDS))[:5]}; known: {sorted(ITEM_FIELDS)}"
        if "title" not in v:
            return f"items.{k}.title is missing"
        for f, types in ITEM_FIELDS.items():
            if f in v and not isinstance(v[f], types):
                want = " or ".join("null" if t is type(None) else t.__name__ for t in types)
                return f"items.{k}.{f} must be {want}, not {type(v[f]).__name__}"
    if seeds is None:
        seeds = []
    if not isinstance(seeds, list) or not all(isinstance(q, dict) for q in seeds):
        return "seed_questions is a list of question objects"
    for n, q in enumerate(seeds):   # the seed reads qid before the store checks the rest
        if not isinstance(q.get("qid"), str):
            return f"seed_questions[{n}].qid must be str"
    if board is not None and not (isinstance(board, dict) and isinstance(board.get("shape"), str)
                                  and isinstance(board.get("values"), dict)
                                  and all(isinstance(k, str) and isinstance(v, str)
                                          for k, v in board["values"].items())):
        return "board is null or {shape: str, values: {str: str}}"
    return None


def store_snapshot(state: Path, body: dict) -> None:
    """Write a CHECKED snapshot to STATE/items.json, whole or not at all.

    Raises ValueError, naming the cap, when it is over MAX_ITEMS; nothing is written then.

    The rename replaces whatever is at items.json without following it (a bad file, a symlink, a FIFO), so a
    push is the way back from an unreadable one. A directory cannot be renamed over, so it is first renamed
    aside, unread and kept, to SET_ASIDE + hex.
    """
    items, seeds, board = body["items"], body.get("seed_questions") or [], body.get("board")
    data = json.dumps({"items": items, "seed_questions": seeds, "board": board}, sort_keys=True).encode()
    if len(data) > MAX_ITEMS:
        raise ValueError(f"the pushed items are over {MAX_ITEMS} bytes")
    sfd = os.open(state, STATE_FLAGS)
    try:
        aside = AF.set_aside_dir(sfd, ITEMS)
        if aside:
            sys.stderr.write(f"console items: a directory at {Path(state, ITEMS)} was set aside as {aside}\n")
        AF.write_at(sfd, ITEMS, data)
    finally:
        os.close(sfd)


class SnapshotAdapter:
    """The project's items, seed questions and board as its steward last pushed them (§3.6), read as data.

    Absent until the first `items-push`: no items, no seeds, no board, and
    `pushed()` False, so the board can say so. The file is re-read only when it
    changes, so a request costs a stat.

    A stored file that fails its check raises StoreError, which the one server
    turns into refusing that project. `tolerant=True` (the single server, which
    has only this one project) instead serves no items, keeps the reason in
    `problem` for the board's note and /health, logs it once to stderr, and
    reads the file again on the next request, so a fresh push recovers live.
    """

    def __init__(self, state: Path, tolerant: bool = False) -> None:
        self.state = Path(state)
        self.path = self.state / ITEMS   # named in errors only: every read goes through a STATE descriptor
        self.tolerant = tolerant
        self.problem: str | None = None   # why the stored file cannot be served (tolerant only); never served itself
        self._lock = threading.Lock()
        self._seen: tuple[int, int] | None = None
        self._doc: dict = {"items": {}, "seed_questions": [], "board": None}

    def _load(self) -> dict:
        with self._lock:
            try:
                doc = self._read()
            except (StoreError, OSError) as e:
                if isinstance(e, OSError):   # EACCES, EIO, ELOOP on a parent...: as unreadable as a bad file
                    e = StoreError(f"{self.path} cannot be read: {e.strerror or type(e).__name__}")
                if not self.tolerant:
                    raise e from None
                if str(e) != self.problem:   # once per distinct problem, not once per request
                    sys.stderr.write(f"console items: {e}; serving no items until the steward pushes again\n")
                self.problem, self._seen = str(e), None
                self._doc = {"items": {}, "seed_questions": [], "board": None}
                return self._doc
            self.problem = None
            return doc

    def _read(self) -> dict:
        """The stored snapshot, re-read only when its (mtime, size) changed; StoreError when it fails its check."""
        sfd = os.open(self.state, STATE_FLAGS)
        try:
            try:
                st = os.stat(ITEMS, dir_fd=sfd, follow_symlinks=False)
            except FileNotFoundError:
                self._seen, self._doc = None, {"items": {}, "seed_questions": [], "board": None}
                return self._doc
            key = (st.st_mtime_ns, st.st_size)
            if key != self._seen:
                raw = AF.read_at(sfd, ITEMS, MAX_ITEMS)
                if raw is None:
                    raise StoreError(f"{self.path} is not a plain file")
                if len(raw) > MAX_ITEMS:
                    raise StoreError(f"{self.path} is over {MAX_ITEMS} bytes")
                try:
                    doc = json.loads(raw.decode("utf-8"))
                except (ValueError, RecursionError) as e:   # UnicodeDecodeError, JSONDecodeError, deep nesting
                    raise StoreError(f"{self.path} is not JSON: {e}") from None
                why = snapshot_problem(doc)
                if why:
                    raise StoreError(f"{self.path}: {why}")
                self._doc = {"items": doc["items"], "seed_questions": doc.get("seed_questions") or [],
                             "board": doc.get("board")}
                self._seen = key
            return self._doc
        finally:
            os.close(sfd)

    def pushed(self) -> bool:
        """True once a push has been kept, a restart included: the board's empty state reads this."""
        self._load()
        return self._seen is not None

    def items(self) -> dict[str, dict]:
        return dict(self._load()["items"])

    def seed_questions(self) -> list[dict]:
        return [dict(q) for q in self._load()["seed_questions"]]

    def board(self) -> dict | None:
        return self._load()["board"]

    def record(self, entries, dry_run):   # the fold runs in the steward's process (§4), never here
        return []

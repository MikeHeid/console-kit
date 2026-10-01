"""The one console server: every hosted project's console in one process, a store each (K3, spec §3.4).

    python3 server.py --all          (reads server.json beside the user's registry)

**K3 ALONE MUST NEVER BE DEPLOYED.** Its agent door admits any caller on its
user-only socket (`authorize`, below), the trust today's per-project
`agent.sock` already has; K4 replaces that one function with the project
token check, and K7 is the cut-over, after K4.

What this process holds, and the rules it keeps:

- **A `Store` per project, never a shared one** (§3.4): `store.py` rests on
  one writer process per file, so each project is its own `Console` with its
  own `Store`, opened from that project's state dir.
- **No other server may write a store this one opened**: it takes an
  exclusive `flock` on `STATE/server.lock` per project, and refuses to open a
  state dir whose old `STATE/agent.sock` still answers (a 0.8.x server takes
  no lock, so the socket is how it is seen).
- **One project's fault is its own** (§3.10): a store the loader refuses, an
  unreadable root, a held lock or a live old server leaves THAT project
  refused, by name and with the reason, while every other project is served.
- **No project code runs here** (§3.6): items, seed questions and the board
  arrive as data (`items-push`, kept in `STATE/items.json`); the adapter is
  never imported, and no process is spawned, git included (the git seam is
  closed for the whole process).
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import socket
import threading
from dataclasses import dataclass, field
from pathlib import Path

from . import projectcfg as PC
from . import names as N
from . import serverfile as SF
from .store import StoreError

LOCK = "server.lock"
ITEMS = "items.json"
OLD_SOCKET = "agent.sock"
MAX_ITEMS = 4 << 20


class SnapshotAdapter:
    """The project's items, seed questions and board as its steward last pushed them (§3.6), read as data.

    Absent until the first `items-push`: no items, no seeds, no board. The
    file is re-read only when it changes, so a request costs a stat.
    """

    def __init__(self, state: Path) -> None:
        self.path = Path(state) / ITEMS
        self._lock = threading.Lock()
        self._seen: tuple[int, int] | None = None
        self._doc: dict = {"items": {}, "seed_questions": [], "board": None}

    def _load(self) -> dict:
        with self._lock:
            try:
                st = os.stat(self.path, follow_symlinks=False)
            except FileNotFoundError:
                self._seen, self._doc = None, {"items": {}, "seed_questions": [], "board": None}
                return self._doc
            key = (st.st_mtime_ns, st.st_size)
            if key != self._seen:
                fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(fd, "rb") as fh:
                    raw = fh.read(MAX_ITEMS + 1)
                if len(raw) > MAX_ITEMS:
                    raise StoreError(f"{self.path} is over {MAX_ITEMS} bytes")
                doc = json.loads(raw.decode("utf-8"))
                self._doc = {"items": doc.get("items") or {}, "seed_questions": doc.get("seed_questions") or [],
                             "board": doc.get("board")}
                self._seen = key
            return self._doc

    def items(self) -> dict[str, dict]:
        return dict(self._load()["items"])

    def seed_questions(self) -> list[dict]:
        return [dict(q) for q in self._load()["seed_questions"]]

    def board(self) -> dict | None:
        return self._load()["board"]

    def record(self, entries, dry_run):   # the fold runs in the steward's process (§4), never here
        return []


@dataclass
class Hosted:
    """One project on the one server: its console, or the reason it is refused."""

    name: str
    entry: dict
    console: object | None = None
    fault: str | None = None
    lock_fd: int | None = field(default=None, repr=False)

    def close(self) -> None:
        if self.lock_fd is not None:
            os.close(self.lock_fd)   # closing releases the flock
            self.lock_fd = None


def old_server_answers(state: Path) -> bool:
    """True when something accepts a connection on STATE/agent.sock: a per-project 0.8.x server is running."""
    p = Path(state) / OLD_SOCKET
    try:
        if not p.is_socket():
            return False
    except OSError:
        return False
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(1.0)
    try:
        s.connect(str(p))
        return True
    except OSError:
        return False   # a stale socket file from a server that is gone
    finally:
        s.close()


def _take_lock(state: Path) -> int:
    """An exclusive flock on STATE/server.lock, never blocking; OSError(EWOULDBLOCK) when another server holds it."""
    fd = os.open(Path(state) / LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise
    return fd


def open_project(name: str, entry: dict, team_domain: str) -> Hosted:
    """Open one project's console, or return it refused with the reason. Never raises for a project fault."""
    from . import server as SV   # imported here: server.py imports nothing from this module
    h = Hosted(name, entry)
    problems = SF.entry_problems(name, entry)
    if problems:
        h.fault = "; ".join(problems)
        return h
    state = Path(entry["state"])
    try:
        state.mkdir(mode=0o700, parents=True, exist_ok=True)
        if old_server_answers(state):
            h.fault = (f"a console server still answers on {state / OLD_SOCKET}; stop and disable it before this "
                       f"server opens {name}'s store")
            return h
        h.lock_fd = _take_lock(state)
    except OSError as e:
        if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
            h.fault = f"another console server holds {state / LOCK}"
        else:
            h.fault = f"cannot open {state}: {e.strerror}"
        return h
    root = Path(entry["root"])
    cfg = SV.Config(root=root, page=root / entry["page"], state=state, adapter=Path(os.devnull),
                    team_domain=team_domain, aud=entry["aud"], hostname=entry["hostname"], port=entry["port"],
                    project=name)
    try:
        h.console = SV.Console(cfg, SnapshotAdapter(state))
        h.console.seed()
    except (StoreError, PC.ConfigError, N.NamesError, OSError, ValueError) as e:
        h.console = None
        h.fault = f"{name}'s console cannot open: {e}"
        h.close()
    return h


class MultiServer:
    """Every project in server.json, each opened independently."""

    def __init__(self, path: Path | None = None) -> None:
        doc = SF.load(path)
        self.team_domain = doc["team_domain"] or ""
        self.projects: dict[str, Hosted] = {}
        for name in sorted(doc["projects"]):
            self.projects[name] = open_project(name, doc["projects"][name], self.team_domain)

    def served(self) -> list[str]:
        return [n for n, h in self.projects.items() if h.fault is None]

    def refused(self) -> dict[str, str]:
        return {n: h.fault for n, h in self.projects.items() if h.fault is not None}

    def stores(self) -> list:
        return [h.console.store for h in self.projects.values() if h.console is not None]

    def close(self) -> None:
        for h in self.projects.values():
            h.close()

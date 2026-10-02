"""The one console server: every hosted project's console in one process, a store each (K3, spec §3.4).

    python3 server.py --all          (reads server.json beside the user's registry)

**The agent door admits by project token (K4, spec §3.5).** `authorize`,
below, is the one admission point: a request on `/p/<project>/…` is served
only when its `Authorization: Bearer ck1_…` hashes to the `token_sha256`
server.json holds for THAT project, compared in constant time. The hashes are
re-read whenever server.json changes on disk, so `token rotate` refuses the
old token from the next request with no restart. Every refusal (no token, a
malformed one, another project's, a rotated one, a project not hosted) is the
one 403 with the one body, naming nothing.

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

import argparse
import errno
import fcntl
import hashlib
import hmac
import os
import re
import socket
import stat
import sys
import threading
from dataclasses import dataclass, field
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Callable

from . import gitseam as G
from .items import (ITEM_FIELDS, ITEMS, MAX_ITEM_COUNT, MAX_ITEMS, SnapshotAdapter,  # noqa: F401 (MS.<name>)
                    snapshot_problem)
from . import projectcfg as PC
from . import registry as R
from . import rootfs as RF
from . import names as N
from . import serverfile as SF
from . import server as SV
from .store import StoreError

LOCK = "server.lock"
OLD_SOCKET = "agent.sock"
SOCKET = "server.sock"
PROJECT_PATH = re.compile(r"^/p/([a-z](?:[a-z0-9]|-(?=[a-z0-9])){0,31})(/[^?#]*)\Z")
FORBIDDEN = {"error": "forbidden"}   # one body for every refusal at the agent door: it names nothing (§3.5)
# The agent POST routes the base handler serves; anything else is the 404 below. `_Handler.finish` drains the
# unread body of that 404, and of every 403 and 503 here, within its own total deadline.
POST_ROUTES = {"/cursor", "/working", "/reanchor", "/visual", "/visual-export", "/history-blob", "/history-specs",
               "/items", "/page-snapshot", "/anchor-proposal", *SV.AGENT_ROUTES}
BEARER = re.compile(r"^Bearer (ck1_[A-Za-z0-9_-]{43})\Z")
NO_HASH = "0" * 64   # compared against when a project has no hash, so an unknown project costs the same compare


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


class ProjectConsole(SV.Console):
    """One project's console on the one server: today's Console, items pushed as data.

    What git would have given is labelled by Console itself once the seam is
    closed (`tags.git`, `check.history`, `reanchor.history`, F63), the same keys
    the page renders for a single-project server: nothing here adds a second label.
    """

    # page() is Console's: the project's STATE snapshot (CONSOLE-kit/Q28). server.json's "page" names only the
    # default path `agent.py page-snapshot` reads from a commit; this server never reads it from the root.

    def board(self) -> dict | None:
        """None (the door's 404) until a push carries a board; a pushed one is checked as the adapter's was."""
        if self.adapter.board() is None:
            return None
        return super().board()


def open_project(name: str, entry: dict, team_domain: str, registry: dict | str) -> Hosted:
    """Open one project's console, or return it refused with the reason. Never raises for a project fault.

    `registry` is the user's registry as `R.load` read it (or why it could not
    be read): server.json is checked against it again here, at every start,
    because a hand edit between `server add` and now could point a project at
    a root the user never registered on that state.
    """
    h = Hosted(name, entry)
    problems = SF.entry_problems(name, entry)
    if problems:
        h.fault = "; ".join(problems)
        return h
    if isinstance(registry, str):
        h.fault = f"the registry cannot be read, so {name}'s root cannot be checked: {registry}"
        return h
    roots = SF.registered_roots(entry["state"], registry)
    if os.path.realpath(entry["root"]) not in roots:
        h.fault = (f"{name}'s root {entry['root']} is not registered on the console at {entry['state']} "
                   f"(registered: {', '.join(roots) or 'none'}); re-run `agent.py server add`")
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
    try:
        RF.hold(root)   # the one descriptor every read of this project's tree starts from (§3.6)
    except OSError as e:
        h.fault = f"{name}'s root cannot be opened: {e.strerror}"
        h.close()
        return h
    cfg = SV.Config(root=root, page=None, state=state, adapter=Path(os.devnull),   # Q28: no page is read here
                    team_domain=team_domain, aud=entry["aud"], hostname=entry["hostname"], port=entry["port"],
                    project=name)
    try:
        h.console = ProjectConsole(cfg, SnapshotAdapter(state))
        h.console.seed()
    except Exception as e:   # anything a project's own files can provoke is that project's fault (§3.10)
        named = isinstance(e, (StoreError, PC.ConfigError, N.NamesError, OSError, ValueError))
        h.console = None
        h.fault = f"{name}'s console cannot open: {e if named else f'{type(e).__name__}: {e}'}"
        h.close()
    return h


class MultiServer:
    """Every project in server.json, each opened independently."""

    def __init__(self, path: Path | None = None) -> None:
        path = Path(path or SF.location())
        self.path = path
        self._hash_lock = threading.Lock()
        self._hash_seen: tuple | None = None
        self._hashes: dict[str, str] = {}
        doc = SF.load(path)
        self.team_domain = doc["team_domain"] or ""
        try:   # the registry beside server.json: the pair always travels together
            registry: dict | str = R.load(path.parent / R.FILE)
        except (R.RegistryError, OSError) as e:
            registry = str(e)
        self.projects: dict[str, Hosted] = {}
        for name in sorted(doc["projects"]):
            self.projects[name] = open_project(name, doc["projects"][name], self.team_domain, registry)

    def token_hash(self, project: str) -> str | None:
        """The token hash server.json holds for `project` NOW: re-read whenever the file changes (K4, AC4.3).

        The file's (inode, mtime, size) is checked on every call, so a rotation
        (written by rename, hence a new inode) is seen by the very next request;
        nothing is cached across a change. A file that cannot be read or parsed
        holds no hash for anyone: every token is refused until it is fixed.
        """
        with self._hash_lock:
            try:
                st = os.stat(self.path)
                key = (st.st_ino, st.st_mtime_ns, st.st_size, st.st_ctime_ns)
            except OSError:
                key = None
            if key is None or key != self._hash_seen:
                hashes: dict[str, str] = {}
                if key is not None:
                    try:
                        for name, e in SF.load(self.path)["projects"].items():
                            h = e.get(SF.TOKEN_KEY) if isinstance(e, dict) else None
                            if isinstance(name, str) and isinstance(h, str) and SF.TOKEN_HASH.match(h):
                                hashes[name] = h
                    except (SF.ServerFileError, R.RegistryError, OSError, RecursionError) as err:
                        sys.stderr.write(f"console-server: {self.path} cannot be read, so every agent token is "
                                         f"refused until it is fixed: {err}\n")
                self._hashes, self._hash_seen = hashes, key
            return self._hashes.get(project)

    def served(self) -> list[str]:
        return [n for n, h in self.projects.items() if h.fault is None]

    def refused(self) -> dict[str, str]:
        return {n: h.fault for n, h in self.projects.items() if h.fault is not None}

    def stores(self) -> list:
        return [h.console.store for h in self.projects.values() if h.console is not None]

    def close(self) -> None:
        for h in self.projects.values():
            h.close()


# -- the doors ---------------------------------------------------------------------------------

def authorize(ms: "MultiServer", project: str, headers) -> bool:
    """THE ONE ADMISSION POINT of the agent door: may this request act on `project`? (K4, §3.5)

    Yes only when the request carries exactly one `Authorization: Bearer
    ck1_…` whose SHA-256 equals the hash server.json holds for `project`, the
    PATH's project: never the project the token belongs to. The comparison is
    `hmac.compare_digest`, and a project with no hash (unknown, or not hosted)
    is compared against a dummy, so the time taken says nothing about which.
    Every /p/ route passes here first; a test pins that refusing here refuses
    every route of every project.
    """
    values = headers.get_all("Authorization") or []
    m = BEARER.match(values[0]) if len(values) == 1 and isinstance(values[0], str) else None
    presented = hashlib.sha256((m.group(1) if m else "").encode("ascii")).hexdigest()
    want = ms.token_hash(project)
    same = hmac.compare_digest(presented.encode("ascii"), (want or NO_HASH).encode("ascii"))
    return bool(m) and want is not None and same


class MultiAgentHandler(SV.AgentHandler):
    """The one agent socket: `/p/<project>/<route>` reaches that project's console and nothing else."""

    multi: "MultiServer"

    def _project(self) -> Hosted | None:
        m = PROJECT_PATH.match(self.path)
        name = m.group(1) if m else None
        h = self.multi.projects.get(name) if name else None
        if name is None or not authorize(self.multi, name, self.headers) or h is None:
            self._send(403, FORBIDDEN)   # an unknown project and a refused one look the same
            return None
        if h.fault is not None:
            self._send(503, {"error": f"{name}: {h.fault}"})
            return None
        self.console = h.console      # this request's project, for every route the handler already has
        self.path = m.group(2)
        return h

    def do_GET(self) -> None:
        if self.path == "/health":    # no token: the server's own liveness, naming no project (§3.5)
            return self._send(200, {"ok": True, "server": "console-kit", "version": SV.__version__})
        if self._project() is not None:
            super().do_GET()

    def do_POST(self) -> None:
        if self._project() is None:
            return
        if self.path not in POST_ROUTES:
            return self._send(404, {"error": "not found"})
        super().do_POST()   # /items included: SV.AgentHandler serves it for both servers (Q24)


class MultiOwnerHandler(SV.OwnerHandler):
    """A project's owner door on the one server: today's gate and routes, the page from STATE's snapshot (Q28)."""


FAULT_BODY = {"error": "this project is not being served right now; the console server's log names why"}


class FaultOwnerHandler(SV._Handler):
    """A refused project's owner door: past the same Access gate, every request is 503.

    The body is the same for every refused project and names no path: the
    reason (a lock file, a state dir) is the operator's, so it goes to the
    server's log (at start, and once per refused request here), never to the
    browser, even past the gate.
    """

    verify: Callable[[str | None], dict]
    fault: str

    def _dispatch(self) -> None:
        try:
            self.verify(self.headers.get("Cf-Access-Jwt-Assertion"))
        except SV.AuthError as e:
            return self._send(403, {"error": str(e)})
        sys.stderr.write(f"console-server: refused request on a refused project: {self.fault}\n")
        self._send(503, FAULT_BODY)

    do_GET = do_POST = do_HEAD = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _dispatch


def socket_path(server_file: Path) -> Path:
    return Path(server_file).parent / SOCKET


def _agent_server(ms: "MultiServer", path: Path) -> SV.UnixHTTPServer:
    if len(os.fsencode(path)) > SV.SOCKET_PATH_MAX:
        raise SystemExit(f"the agent socket path {path} is over {SV.SOCKET_PATH_MAX} bytes, the limit a Unix "
                         f"socket path has (108 bytes on Linux, including the terminator). It sits beside "
                         f"server.json, so shorten XDG_CONFIG_HOME (the console-kit folder under it), or pass "
                         f"--socket with a shorter path")
    parent = path.parent
    try:
        st = parent.lstat()
    except OSError as e:
        raise SystemExit(f"the agent socket's folder {parent} cannot be read: {e.strerror}") from None
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise SystemExit(f"the agent socket's folder {parent} must be a folder you own with mode 0700 (it is "
                         f"{stat.filemode(st.st_mode)}): any user who can enter it could reach every project's "
                         f"agent door. Run: chmod 700 {parent}")
    if path.exists() or path.is_symlink():
        if not stat.S_ISSOCK(path.lstat().st_mode):
            raise SystemExit(f"{path} exists and is not a socket; refusing to replace it")
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.connect(str(path))
            raise SystemExit(f"another console server answers on {path}")
        except OSError:
            pass
        finally:
            probe.close()
        path.unlink()
    old = os.umask(0o177)
    try:
        srv = SV.UnixHTTPServer(str(path), type("BoundMultiAgentHandler", (MultiAgentHandler,), {"multi": ms}))
    finally:
        os.umask(old)
    os.chmod(path, 0o600)
    return srv


@dataclass
class Running:
    ms: MultiServer
    agent: SV.UnixHTTPServer
    socket: Path
    owners: dict[str, ThreadingHTTPServer]

    def ports(self) -> dict[str, int]:
        return {n: s.server_address[1] for n, s in self.owners.items()}

    def shutdown(self) -> None:
        for s in (self.agent, *self.owners.values()):
            s.shutdown()
            s.server_close()
        self.socket.unlink(missing_ok=True)
        self.ms.close()


def start(server_file: Path | None = None, sock: Path | None = None,
          verify_for: Callable[[str], Callable[[str | None], dict]] | None = None,
          port_for: Callable[[Hosted], int] | None = None) -> Running:
    """Open every project and start every door, each serving in its own threads. Closes the git seam first."""
    G.close()   # this process holds every project's store: no git, i.e. no project-controlled program, runs here
    RF.confine()   # and every read of a project's tree starts from its held root, following no symlink
    sf = Path(server_file or SF.location())
    ms = MultiServer(sf)
    verify_for = verify_for or (lambda aud: SV.access_verifier(ms.team_domain, aud))
    port_for = port_for or (lambda h: h.entry["port"])
    path = Path(sock or socket_path(sf))
    agent = _agent_server(ms, path)
    threading.Thread(target=agent.serve_forever, daemon=True).start()
    owners = {}
    try:
        for name, h in ms.projects.items():
            if not isinstance(h.entry, dict) or SF.entry_problems(name, h.entry):
                continue   # no port or gate can be trusted from an entry this broken; its refusal is on the agent door
            verify = verify_for(h.entry["aud"])
            if h.console is not None:
                handler = type("BoundMultiOwnerHandler", (MultiOwnerHandler,),
                               {"console": h.console, "verify": staticmethod(verify)})
            else:
                handler = type("BoundFaultOwnerHandler", (FaultOwnerHandler,),
                               {"verify": staticmethod(verify), "fault": f"{name}: {h.fault}"})
            try:
                srv = ThreadingHTTPServer((SV.HOST, port_for(h)), handler)
            except OSError as e:   # a port in use is this project's fault, never the server's (§3.10)
                h.console = None
                h.fault = f"{name}'s owner door cannot listen on port {h.entry['port']}: {e.strerror}"
                h.close()
                continue
            srv.daemon_threads = True
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            owners[name] = srv
    except BaseException:
        for s in (agent, *owners.values()):
            s.shutdown()
            s.server_close()
        path.unlink(missing_ok=True)
        ms.close()
        raise
    return Running(ms, agent, path, owners)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="The one console server: every project in server.json, each "
                                 "project's agents admitted by that project's own token (K3, K4).")
    ap.add_argument("--all", action="store_true", required=True, help="serve every project in server.json")
    ap.add_argument("--server-file", type=Path, help="default: server.json beside your registry")
    ap.add_argument("--socket", type=Path, help="the agent socket (default: server.sock beside server.json)")
    a = ap.parse_args(argv)
    r = start(a.server_file, a.socket)
    for name, h in r.ms.projects.items():
        line = f"serving on port {r.ports().get(name)}" if h.fault is None else f"REFUSED: {h.fault}"
        sys.stderr.write(f"console-server: {name}: {line}\n")
    sys.stderr.write(f"console-server: agent door {r.socket}\n")
    sys.stderr.flush()
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        r.shutdown()
    return 0

"""SessionStart: tell a new session what the owner sent while no session was watching (spec §7.4).

**Trust comes from the user, never from the repository** (§7.7). The plugin is
installed per user and this hook runs, unasked, in every project that user
opens. So it acts only in a project the user registered with `agent.py
register`, which writes the user's own registry:

    ${XDG_CONFIG_HOME:-~/.config}/overture/projects.json
    {"projects": {"<absolute project root>": {"state": "<absolute dir>", "kit": "<absolute dir>",
                                              "steward": "<agent name, optional (0.8.3)>"}}}

When the entry names a steward, the note says so: the steward session (the one
whose name is that name) is told it alone watches, syncs and folds, and mirrors
live answers to the console; every other session is told to sign its posts,
never to run those, and that its AskUserQuestion is blocked (`ask_guard.py`).

**A session's name (0.8.4)** is the one the user typed in it, `/overture:as
NAME` (recorded by `name_session.py` in STATE/sessions.jsonl against the
session id Claude Code gives every hook on stdin), else OVERTURE_AGENT from
the environment the session was started with. This hook also writes `export
OVERTURE_SESSION=<session id>` to CLAUDE_ENV_FILE, so the session's Bash
commands (`agent.py`, `fold.py`) can look the same name up. A session with no
name, on a console with a steward, is told to type `/overture:as NAME`.

In any other project it prints nothing and exits 0, whatever files the project
carries: a clone's `.overture.json` or fake doorbell is never read. It
never fails a session: a problem is one line of context, never a non-zero exit.

**It executes nothing and imports nothing from outside this file.** It reads
the registry and the doorbell itself, with the standard library, by the kit's
rules (`overture/registry.py`, `overture/doorbell.py`); the kit's tests
hold both copies to one fixture. It reads only regular files, never blocking
(a FIFO is refused), and within a size cap. The agent's cursor moves only when
the agent says it processed a line (`agent.py synced --through SEQ`).

What it prints enters the session's context, so every field shown is held to
a shape: a seq is an int, an item an item id, a time a timestamp, a path an
absolute path of plain characters. Anything else is `?`, never passed through.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import sys
import tempfile
import time
from pathlib import Path

WAKE_INTENTS = ("process", "fork", "chat", "visual", "scan")     # overture/doorbell.py
CURSOR_FILE = "agent-cursor.json"      # overture/doorbell.py
MAX_DOORBELL = 64 << 20                # overture/doorbell.py
MAX_CURSOR = 4096                      # overture/doorbell.py
REGISTRY_FILE = "projects.json"        # overture/registry.py
MAX_REGISTRY = 1 << 20                 # overture/registry.py
PLAIN_PATH = re.compile(r"^/[A-Za-z0-9_./\-]{0,511}\Z")   # overture/registry.py
MAX_LISTED = 20                        # a long backlog is summarised, never pasted whole
ITEM_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}\Z")   # overture/schema.py ITEM_ID, length-capped
TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
AGENT_ENV = "OVERTURE_AGENT"                                      # overture/names.py ENV
AGENT_NAME = re.compile(r"^[a-z](?:[a-z0-9]|-(?=[a-z0-9])){0,31}\Z")  # overture/names.py NAME
MAX_NAME = 32                                                        # overture/names.py
RESERVED = frozenset({"agent", "owner"})                            # overture/names.py
STEWARD = "steward"                                                  # overture/registry.py (0.8.3)
SESSIONS_FILE = "sessions.jsonl"                                     # overture/sessions.py FILE (0.8.4)
SESSION_ENV = "OVERTURE_SESSION"                                  # overture/sessions.py ENV
SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,127}\Z")      # overture/sessions.py SESSION_ID
MAX_SESSIONS = 64 << 10                                              # overture/sessions.py MAX_FILE
KEEP_SESSIONS = 256                                                  # overture/sessions.py KEEP
SESSIONS_LOCK = ".sessions.lock"
MAX_INPUT = 1 << 20
# 0.9.4: a per-project record of which kit version this STATE last saw, so an upgrade note prints once.
VERSION_FILE = "last_seen_kit_version"
MAX_VERSION_FILE = 64
VERSION_RE = re.compile(r"""^__version__\s*=\s*['"]([0-9]+\.[0-9]+\.[0-9]+(?:[-+][\w.]+)?)['"]""", re.M)
KIT_INIT = "overture/__init__.py"
MAX_INIT = 1 << 16


class Unsafe(ValueError):
    pass


def _read_regular(path: Path, cap: int, nofollow: bool = False) -> bytes | None:
    """As overture/registry.read_regular: a regular file's bytes, None when absent, never blocking.

    With `nofollow` a symlink is refused too (the sessions file, whose writer refuses one).
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | (os.O_NOFOLLOW if nofollow else 0))
    except FileNotFoundError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise Unsafe(f"{path.name} is not a regular file")
        if st.st_size > cap:
            raise Unsafe(f"{path.name} is over the {cap}-byte limit")
        return os.read(fd, cap + 1)[:cap]
    finally:
        os.close(fd)


def _registry_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "overture" / REGISTRY_FILE


def is_name(v: object) -> bool:
    """As overture/names.problem(v) is None: an agent name."""
    return isinstance(v, str) and v not in RESERVED and len(v) <= MAX_NAME and AGENT_NAME.match(v) is not None


def projects() -> dict:
    """The registry's "projects" object ({} when there is no registry); Unsafe when it cannot be read."""
    raw = _read_regular(_registry_path(), MAX_REGISTRY)
    if raw is None:
        return {}
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise Unsafe("the registry is not JSON") from None
    found = doc.get("projects") if isinstance(doc, dict) else None
    if not isinstance(found, dict):
        raise Unsafe("the registry needs a \"projects\" object")
    return found


def checked(e: object) -> dict:
    """A registry entry held to the kit's shape (overture/registry.entry_problems), or Unsafe."""
    if not (isinstance(e, dict) and {"state", "kit"} <= set(e) <= {"state", "kit", STEWARD}
            and all(isinstance(e[k], str) and PLAIN_PATH.match(e[k]) for k in ("state", "kit"))):
        raise Unsafe("this project's registry entry is not two absolute plain paths (and an optional steward)")
    if STEWARD in e and not is_name(e[STEWARD]):
        raise Unsafe("this project's registry entry names a steward that is not an agent name")
    return e


def registered(start: Path, found: dict) -> dict | None:
    """The registry entry of the NEAREST registered project holding `start` (itself or an ancestor), or None.

    The same walk as `overture/registry.enclosing`, which `fold.py`'s steward
    lock uses. A copy, not an import, because a hook may import nothing from a
    kit path; the kit's tests hold the two to one fixture.
    """
    p = Path(os.path.realpath(start))
    for d in (p, *p.parents):
        e = found.get(str(d))
        if e is not None:
            return checked(e)
    return None


def enclosing_entry(payload: dict) -> dict | None:
    """The entry of the registered project the session works in: the input's `cwd`, then CLAUDE_PROJECT_DIR."""
    found = projects()
    for start in (payload.get("cwd"), os.environ.get("CLAUDE_PROJECT_DIR")):
        if isinstance(start, str) and start.startswith("/"):
            e = registered(Path(start), found)
            if e is not None:
                return e
    return None


def read_input() -> dict:
    """The hook's JSON input from stdin, {} when there is none or it is not an object (never raises)."""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return {}
        raw = sys.stdin.read(MAX_INPUT + 1)
        v = json.loads(raw) if raw.strip() and len(raw) <= MAX_INPUT else {}
    except Exception:  # noqa: BLE001 - an input it cannot read is no input
        return {}
    return v if isinstance(v, dict) else {}


def session_id(payload: dict) -> str | None:
    v = payload.get("session_id")
    return v if isinstance(v, str) and SESSION_ID.match(v) else None


def session_records(data: bytes) -> dict[str, dict]:
    """As overture/sessions.mapping, keeping each session's whole line: the last well-formed one wins."""
    out: dict[str, dict] = {}
    for line in data.decode("utf-8", errors="replace").split("\n")[:-1]:  # the last piece may still be written
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if (isinstance(rec, dict) and set(rec) == {"agent", "session", "ts"}
                and isinstance(rec["session"], str) and SESSION_ID.match(rec["session"])
                and is_name(rec["agent"])):
            out.pop(rec["session"], None)
            out[rec["session"]] = rec
    return out


def session_name(state: Path, sid: object) -> str | None:
    """As overture/sessions.lookup: the name the user gave this session, or None (never raises)."""
    if not isinstance(sid, str) or not SESSION_ID.match(sid):
        return None
    try:
        data = _read_regular(Path(state) / SESSIONS_FILE, MAX_SESSIONS, nofollow=True)
    except (OSError, Unsafe):
        return None
    rec = None if data is None else session_records(data).get(sid)
    return rec["agent"] if rec else None


def whoami(state: Path, sid: object) -> str:
    """This session's name: its /overture:as, else OVERTURE_AGENT; "" when it has none."""
    named = session_name(state, sid)
    if named:
        return named
    me = os.environ.get(AGENT_ENV, "")
    return me if is_name(me) else ""


def _read_tail(path: Path, cap: int) -> tuple[bytes, int]:
    """The writer's read: (at most the last `cap` bytes of the file, its whole size); (b"", 0) when absent.

    Unlike the reader it accepts a file of any size, so a file grown past the
    reader's cap (by hand, or a damaged write) is still repaired by the next
    /overture:as, never refused for ever. A cut-off first line is dropped. A
    symlink or anything but a regular file is refused (Unsafe or OSError).
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except FileNotFoundError:
        return b"", 0
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise Unsafe(f"{path.name} is not a regular file")
        start = max(0, st.st_size - cap)
        os.lseek(fd, start, os.SEEK_SET)
        chunks, left = [], st.st_size - start
        while left > 0:
            b = os.read(fd, min(left, 1 << 16))
            if not b:
                break
            chunks.append(b)
            left -= len(b)
        data = b"".join(chunks)
        if start > 0:
            data = data[data.find(b"\n") + 1:] if b"\n" in data else b""
        return data, st.st_size
    finally:
        os.close(fd)


def _write_whole(state: Path, path: Path, records: dict) -> None:
    """Rewrite the sessions file as each session's last line, the KEEP_SESSIONS newest; atomic (rename)."""
    body = "".join(json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n"
                   for r in list(records.values())[-KEEP_SESSIONS:])
    for stale in Path(state).glob(f".{SESSIONS_FILE}.*.tmp"):   # a crashed writer's leftovers (we hold the lock)
        try:
            stale.unlink()
        except OSError:
            pass
    fd, tmp = tempfile.mkstemp(prefix=f".{SESSIONS_FILE}.", suffix=".tmp", dir=state)   # 0600, unique
    try:
        try:
            os.write(fd, body.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def record_session(state: Path, sid: str, name: str) -> bool:
    """Record `sid -> name` in STATE/sessions.jsonl (0600); False when that is already its name.

    Writers take a lock file beside it; readers take none, so a compaction
    writes a new file and renames it over the old one (a reader sees one or the
    other, whole). The file is rewritten, with each session's last line, the
    KEEP_SESSIONS most recently named, whenever it is already over MAX_SESSIONS
    (the reader's cap: then the reader sees no names at all, so even "already
    that name" rewrites it) or this line would take it over. Raises Unsafe or
    OSError; the caller says so.
    """
    if not SESSION_ID.match(sid) or not is_name(name):
        raise Unsafe("not a session id and an agent name")
    if not os.path.isdir(state):
        raise Unsafe(f"the console's state directory {state} does not exist")
    path = Path(state) / SESSIONS_FILE
    flags = os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    lock = os.open(Path(state) / SESSIONS_LOCK, os.O_RDWR | os.O_CREAT | flags, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data, size = _read_tail(path, 4 * MAX_SESSIONS)
        records = session_records(data)
        oversized = size > MAX_SESSIONS
        if sid in records and records[sid]["agent"] == name and not oversized:
            return False
        rec = {"agent": name, "session": sid, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        line = json.dumps(rec, sort_keys=True, separators=(",", ":")) + "\n"
        if oversized or size + len(line) + 1 > MAX_SESSIONS:
            records.pop(sid, None)
            records[sid] = rec
            _write_whole(Path(state), path, records)
            return True
        if data and not data.endswith(b"\n"):
            line = "\n" + line      # never glue a line onto a cut-short one
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NONBLOCK | flags, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise Unsafe(f"{SESSIONS_FILE} is not a regular file")
            os.fchmod(fd, 0o600)
            os.write(fd, line.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        return True
    finally:
        os.close(lock)


def export_session(project: Path, sid: str | None) -> None:
    """In a registered project, put this session's id into its Bash environment (CLAUDE_ENV_FILE).

    `agent.py` and `fold.py`, run from the session's Bash, look their name up
    with it. Only in a registered project, like everything this hook does; any
    failure is silent (the session then falls back to OVERTURE_AGENT).
    """
    env_file = os.environ.get("CLAUDE_ENV_FILE")
    if not env_file or sid is None:
        return
    try:
        if _entry(project) is None:
            return
        with open(env_file, "a+b") as fh:                      # append: other hooks write here too
            end = fh.seek(0, os.SEEK_END)
            glue = b""
            if end > 0:
                fh.seek(end - 1)
                glue = b"" if fh.read(1) == b"\n" else b"\n"   # never extend another hook's last line
            fh.write(glue + f"export {SESSION_ENV}={sid}\n".encode("ascii"))   # SESSION_ID-shaped: no quoting
    except Exception:  # noqa: BLE001 - never cost the owner a session
        return


def _entry(project: Path) -> dict | None:
    """This project's registry entry, None when unregistered; Unsafe when the entry is malformed."""
    e = projects().get(os.path.realpath(project))
    return None if e is None else checked(e)


def steward_line(e: dict, agent: str, me: str | None = None) -> tuple[str | None, bool]:
    """The steward note for this session (0.8.3), and whether this session is the steward.

    The session's own name `me` is the one the user gave it (`whoami`: its
    /overture:as, else OVERTURE_AGENT), never anything from the repository.
    """
    name = e.get(STEWARD)
    if not name:
        return None, True
    if me is None:
        me = os.environ.get(AGENT_ENV, "")
    if is_name(me) and me == name:
        return (f"Owner console steward: this session is the steward, {name}. It alone runs `watch`, `synced` "
                f"and the fold for this console. It may ask the owner live (AskUserQuestion), but mirrors each "
                f"live answer to the console for the record: post the question with `{agent} ask FILE` and reply "
                f"on its item with the owner's answer (`{agent} reply ITEM TEXT`)."), True
    you = f"--as {me}" if is_name(me) else "--as YOUR-NAME"
    unnamed = ("" if is_name(me) else
               f"This session is not named: the user names it by typing `/overture:as NAME` in it (the "
               f"steward's name is {name}); tell the user so. ")
    return (f"Owner console steward: {name} holds this console's doorbell, and this session is not it. {unnamed}Sign "
            f"every post with `{you}` (or start the session with {AGENT_ENV}=YOUR-NAME), and never run "
            f"`watch`, `synced` or the fold: they are refused here. AskUserQuestion is blocked in this session: "
            f"post each question with the console-ask skill (`{agent} {you} ask FILE...`); it reaches the owner's "
            f"inbox, and the steward, who asks the owner live, mirrors each live answer to the console."), False


def _cursor(state: Path, key: str = "through") -> int:
    try:
        data = _read_regular(state / CURSOR_FILE, MAX_CURSOR)
        v = json.loads(data.decode("utf-8")).get(key, 0) if data is not None else 0
    except (OSError, ValueError, AttributeError):
        return 0
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0


def _rx_cursor(state: Path) -> int:
    return _cursor(state, "rx_through")


def _rx(r: dict) -> int | None:
    v = r.get("rx")
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _waiting(bell: Path, since: int, rx_since: int = 0) -> list[dict]:
    """The owner's requests after `since` (a scan's: after `rx_since`), as the kit reads them (doorbell.pending)."""
    data = _read_regular(bell, MAX_DOORBELL)
    if data is None:
        return []
    out = []
    for line in data.decode("utf-8", errors="replace").split("\n")[:-1]:  # the last piece is still being written
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if (isinstance(rec, dict) and isinstance(rec.get("seq"), int) and not isinstance(rec["seq"], bool)
                and (rec["seq"] > since if _rx(rec) is None else _rx(rec) > rx_since)
                and rec.get("intent") in WAKE_INTENTS):
            out.append(rec)
    return out


def _shown(value: object, shape: re.Pattern) -> str:
    return value if isinstance(value, str) and shape.match(value) else "?"


def _kit_version(kit: Path) -> str | None:
    """0.9.4: parse __version__ from <kit>/overture/__init__.py; None when unreadable or missing."""
    try:
        data = _read_regular(kit / KIT_INIT, MAX_INIT)
    except (OSError, Unsafe):
        return None
    if data is None:
        return None
    try:
        text = data.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return None
    m = VERSION_RE.search(text)
    return m.group(1) if m else None


def _last_seen(state: Path) -> str | None:
    """0.9.4: the kit version STATE last recorded; None when the file is absent or malformed."""
    try:
        data = _read_regular(state / VERSION_FILE, MAX_VERSION_FILE)
    except (OSError, Unsafe):
        return None
    if data is None:
        return None
    v = data.decode("utf-8", errors="replace").strip()
    return v if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][\w.]+)?", v) else None


def _record_version(state: Path, version: str) -> None:
    """0.9.4: write STATE/last_seen_kit_version so the upgrade note prints at most once per project per bump.

    Atomic rename; a failure is silent so the note at worst prints twice.
    """
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=state, delete=False,
                                         prefix=f".{VERSION_FILE}.", suffix=".tmp") as tmp:
            tmp.write(version + "\n")
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp_path = tmp.name
        os.replace(tmp_path, state / VERSION_FILE)
    except OSError:
        try:
            os.unlink(tmp_path)
        except (OSError, NameError, UnboundLocalError):
            pass


BANNER = (
    "   ___ ___  _  _ ___  ___  _    ___     _  _____ _____ \n"
    "  / __/ _ \\| \\| / __|/ _ \\| |  | __|___| |/ /_ _|_   _|\n"
    " | (_| (_) | .` \\__ \\ (_) | |__| _|___| ' < | |   | |  \n"
    "  \\___\\___/|_|\\_|___/\\___/|____|___|  |_|\\_\\___|  |_|  "
)


def _upgrade_note(agent: str, state: Path, kit: Path, running: str, last: str | None) -> str | None:
    """0.9.4: a one-time list of useful commands when the kit's version changed since this STATE last ran.

    Prints on first install (last is None) and on every upgrade. Short on purpose: the full CHANGELOG is in
    the kit's docs. Caller records the new version in STATE/last_seen_kit_version AFTER emitting the note.
    """
    if last == running:
        return None
    kind = "installed" if last is None else "upgraded"
    headline = (f"overture {kind} to {running}" if last is None
                else f"overture upgraded {last} → {running} on this project")
    return (
        f"{BANNER}\n\n"
        f"{headline}. The commands most useful right now:\n"
        f"  {agent} items-push --adapter .overture/adapter.py          # push the project's items + board\n"
        f"  {agent} items-watch --adapter .overture/adapter.py         # re-push on change (0.9.3+)\n"
        f"  {agent} scaffold-dashboard --project .                        # one-command dashboard (0.9.3+)\n"
        f"  {agent} page-snapshot --path <page>                           # send the dashboard page from a commit\n"
        f"  {agent} prs-push                                              # push open + recent PRs\n"
        f"  {agent} inbox                                                 # what the owner is asking right now\n"
        f"Full notes: see CHANGELOG.md in {kit}."
    )


def context(project: Path, sid: str | None = None) -> str | None:
    """The note for this session, or None when there is nothing to say."""
    try:
        e = _entry(project)
        if e is None:
            return None
        state, kit = Path(e["state"]), Path(e["kit"])
        cursor = _cursor(state)
        wake = _waiting(state / "inbox.jsonl", cursor, _rx_cursor(state))
    except (Unsafe, OSError) as err:
        return f"Owner console: not checked, because {err}. See `agent.py register` (spec §7.7)."
    agent = f"python3 {kit / 'agent.py'} --state {state}"
    # 0.9.4: on install (no last_seen yet) and on every upgrade, prepend a short list of useful commands,
    # then record the version so the note stops repeating until the next bump.
    running_version = _kit_version(kit)
    upgrade = None
    if running_version is not None:
        upgrade = _upgrade_note(agent, state, kit, running_version, _last_seen(state))
        if upgrade is not None:
            _record_version(state, running_version)
    # Standing, in every session of a registered project: a question the owner
    # must decide is posted to the console, not left in a document (owner, 2026-09-29).
    ask = ("Owner console: this project is registered. Post every question the owner must decide "
           f"to the console with the console-ask skill (`{agent} ask FILE...`); a question written "
           "only in a document never reaches the owner's inbox.")
    steward, mine = steward_line(e, agent, whoami(state, sid))
    if steward:
        ask += "\n\n" + steward
    prefix = (upgrade + "\n\n") if upgrade else ""
    if not wake:
        return prefix + ask
    if not mine:  # the doorbell is the steward's: another session is told it waits, never how to take it
        return (prefix + f"Owner console: {len(wake)} request{'' if len(wake) == 1 else 's'} from the owner "
                f"waiting after doorbell seq {cursor}; the steward, {e[STEWARD]}, handles them.\n\n" + ask)
    lines = ([upgrade, ""] if upgrade else []) + [
        f"Owner console: {len(wake)} request{'' if len(wake) == 1 else 's'} from the owner "
        f"waiting after doorbell seq {cursor}:"]
    for r in wake[:MAX_LISTED]:
        if r["intent"] == "chat":  # the chat is not a register item (0.7.0)
            lines.append(f"- seq {r['seq']} ({_shown(r.get('ts'), TS)}): answer the owner's chat message")
            continue
        if r["intent"] == "scan":  # CONSOLE-kit/Q40: stale rulings to answer; numbered by `rx`, not by the store
            n = r.get("qids")
            lines.append(f"- rx {_rx(r)} ({_shown(r.get('ts'), TS)}): answer the owner's scan for a resolve"
                         + (f" ({n} stale ruling{'' if n == 1 else 's'})" if isinstance(n, int) and
                            not isinstance(n, bool) else ""))
            continue
        what = {"process": "process the answers", "visual": "draw the visual the owner asked for"}.get(
            r["intent"], "run a deliberation round")
        lines.append(f"- seq {r['seq']} ({_shown(r.get('ts'), TS)}): {what} on `{_shown(r.get('item'), ITEM_ID)}`")
    if len(wake) > MAX_LISTED:
        lines.append(f"- … and {len(wake) - MAX_LISTED} more (`{agent} inbox`)")
    lines += ["",
              "Use the console-process skill for `process`, `chat` and `scan`, the console-fork skill for "
              "`fork`, and the console-visual skill for `visual`. "
              f"`{agent} inbox` lists them; after handling one, `{agent} synced --through SEQ` "
              "(a scan: `--rx-through RX`) records it so it is not offered again.", "", ask]
    return "\n".join(lines)


def main() -> int:
    project = Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    sid = session_id(read_input())
    export_session(project, sid)
    try:
        note = context(project, sid)
    except Exception as e:  # a broken hook must never cost the owner a session
        note = f"Owner console: the SessionStart check failed ({type(e).__name__})."
    if note:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                                 "additionalContext": note}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""SessionStart: tell a new session what the owner sent while no session was watching (spec §7.4).

**Trust comes from the user, never from the repository** (§7.7). The plugin is
installed per user and this hook runs, unasked, in every project that user
opens. So it acts only in a project the user registered with `agent.py
register`, which writes the user's own registry:

    ${XDG_CONFIG_HOME:-~/.config}/console-kit/projects.json
    {"projects": {"<absolute project root>": {"state": "<absolute dir>", "kit": "<absolute dir>"}}}

In any other project it prints nothing and exits 0, whatever files the project
carries: a clone's `.console-kit.json` or fake doorbell is never read. It
never fails a session: a problem is one line of context, never a non-zero exit.

**It executes nothing and imports nothing from outside this file.** It reads
the registry and the doorbell itself, with the standard library, by the kit's
rules (`console_kit/registry.py`, `console_kit/doorbell.py`); the kit's tests
hold both copies to one fixture. It reads only regular files, never blocking
(a FIFO is refused), and within a size cap. The agent's cursor moves only when
the agent says it processed a line (`agent.py synced --through SEQ`).

What it prints enters the session's context, so every field shown is held to
a shape: a seq is an int, an item an item id, a time a timestamp, a path an
absolute path of plain characters. Anything else is `?`, never passed through.
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
from pathlib import Path

WAKE_INTENTS = ("process", "fork")     # console_kit/doorbell.py
CURSOR_FILE = "agent-cursor.json"      # console_kit/doorbell.py
MAX_DOORBELL = 64 << 20                # console_kit/doorbell.py
MAX_CURSOR = 4096                      # console_kit/doorbell.py
REGISTRY_FILE = "projects.json"        # console_kit/registry.py
MAX_REGISTRY = 1 << 20                 # console_kit/registry.py
PLAIN_PATH = re.compile(r"^/[A-Za-z0-9_./\-]{0,511}$")   # console_kit/registry.py
MAX_LISTED = 20                        # a long backlog is summarised, never pasted whole
ITEM_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$")   # console_kit/schema.py ITEM_ID, length-capped
TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class Unsafe(ValueError):
    pass


def _read_regular(path: Path, cap: int) -> bytes | None:
    """As console_kit/registry.read_regular: a regular file's bytes, None when absent, never blocking."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
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
    return Path(base) / "console-kit" / REGISTRY_FILE


def _entry(project: Path) -> dict | None:
    """This project's registry entry, None when unregistered; Unsafe when the entry is malformed."""
    raw = _read_regular(_registry_path(), MAX_REGISTRY)
    if raw is None:
        return None
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise Unsafe("the registry is not JSON") from None
    projects = doc.get("projects") if isinstance(doc, dict) else None
    if not isinstance(projects, dict):
        raise Unsafe("the registry needs a \"projects\" object")
    e = projects.get(os.path.realpath(project))
    if e is None:
        return None
    if not (isinstance(e, dict) and set(e) == {"state", "kit"}
            and all(isinstance(e[k], str) and PLAIN_PATH.match(e[k]) for k in ("state", "kit"))):
        raise Unsafe("this project's registry entry is not two absolute plain paths")
    return e


def _cursor(state: Path) -> int:
    try:
        data = _read_regular(state / CURSOR_FILE, MAX_CURSOR)
        v = json.loads(data.decode("utf-8")).get("through", 0) if data is not None else 0
    except (OSError, ValueError, AttributeError):
        return 0
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0


def _waiting(bell: Path, since: int) -> list[dict]:
    """The owner's requests after `since`: complete lines only, damaged ones skipped, as the kit reads them."""
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
                and rec["seq"] > since and rec.get("intent") in WAKE_INTENTS):
            out.append(rec)
    return out


def _shown(value: object, shape: re.Pattern) -> str:
    return value if isinstance(value, str) and shape.match(value) else "?"


def context(project: Path) -> str | None:
    """The note for this session, or None when there is nothing to say."""
    try:
        e = _entry(project)
        if e is None:
            return None
        state, kit = Path(e["state"]), Path(e["kit"])
        cursor = _cursor(state)
        wake = _waiting(state / "inbox.jsonl", cursor)
    except (Unsafe, OSError) as err:
        return f"Owner console: not checked, because {err}. See `agent.py register` (spec §7.7)."
    agent = f"python3 {kit / 'agent.py'} --state {state}"
    # Standing, in every session of a registered project: a question the owner
    # must decide is posted to the console, not left in a document (owner, 2026-09-29).
    ask = ("Owner console: this project is registered. Post every question the owner must decide "
           f"to the console with the console-ask skill (`{agent} ask FILE...`); a question written "
           "only in a document never reaches the owner's inbox.")
    if not wake:
        return ask
    lines = [f"Owner console: {len(wake)} request{'' if len(wake) == 1 else 's'} from the owner "
             f"waiting after doorbell seq {cursor}:"]
    for r in wake[:MAX_LISTED]:
        what = "process the answers" if r["intent"] == "process" else "run a deliberation round"
        lines.append(f"- seq {r['seq']} ({_shown(r.get('ts'), TS)}): {what} on `{_shown(r.get('item'), ITEM_ID)}`")
    if len(wake) > MAX_LISTED:
        lines.append(f"- … and {len(wake) - MAX_LISTED} more (`{agent} inbox`)")
    lines += ["",
              "Use the console-process skill for `process` and the console-fork skill for `fork`. "
              f"`{agent} inbox` lists them; after handling one, `{agent} synced --through SEQ` "
              "records it so it is not offered again.", "", ask]
    return "\n".join(lines)


def main() -> int:
    try:
        note = context(Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()))
    except Exception as e:  # a broken hook must never cost the owner a session
        note = f"Owner console: the SessionStart check failed ({type(e).__name__})."
    if note:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                                 "additionalContext": note}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())

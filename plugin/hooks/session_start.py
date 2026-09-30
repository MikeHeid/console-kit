"""SessionStart: tell a new session what the owner sent while no session was watching (spec §7.4).

**Trust comes from the user, never from the repository** (§7.7). The plugin is
installed per user and this hook runs, unasked, in every project that user
opens. So it acts only in a project the user registered with `agent.py
register`, which writes the user's own registry:

    ${XDG_CONFIG_HOME:-~/.config}/console-kit/projects.json
    {"projects": {"<absolute project root>": {"state": "<absolute dir>", "kit": "<absolute dir>",
                                              "steward": "<agent name, optional (0.8.3)>"}}}

When the entry names a steward, the note says so: the steward session (the one
whose CONSOLE_KIT_AGENT is that name) is told it alone watches, syncs and folds,
and mirrors live answers to the console; every other session is told to sign
its posts, never to run those, and that its AskUserQuestion is blocked
(`ask_guard.py`).

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

WAKE_INTENTS = ("process", "fork", "chat", "visual")     # console_kit/doorbell.py
CURSOR_FILE = "agent-cursor.json"      # console_kit/doorbell.py
MAX_DOORBELL = 64 << 20                # console_kit/doorbell.py
MAX_CURSOR = 4096                      # console_kit/doorbell.py
REGISTRY_FILE = "projects.json"        # console_kit/registry.py
MAX_REGISTRY = 1 << 20                 # console_kit/registry.py
PLAIN_PATH = re.compile(r"^/[A-Za-z0-9_./\-]{0,511}\Z")   # console_kit/registry.py
MAX_LISTED = 20                        # a long backlog is summarised, never pasted whole
ITEM_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}\Z")   # console_kit/schema.py ITEM_ID, length-capped
TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
AGENT_ENV = "CONSOLE_KIT_AGENT"                                      # console_kit/names.py ENV
AGENT_NAME = re.compile(r"^[a-z](?:[a-z0-9]|-(?=[a-z0-9])){0,31}\Z")  # console_kit/names.py NAME
MAX_NAME = 32                                                        # console_kit/names.py
RESERVED = frozenset({"agent", "owner"})                            # console_kit/names.py
STEWARD = "steward"                                                  # console_kit/registry.py (0.8.3)


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


def is_name(v: object) -> bool:
    """As console_kit/names.problem(v) is None: an agent name."""
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
    """A registry entry held to the kit's shape (console_kit/registry.entry_problems), or Unsafe."""
    if not (isinstance(e, dict) and {"state", "kit"} <= set(e) <= {"state", "kit", STEWARD}
            and all(isinstance(e[k], str) and PLAIN_PATH.match(e[k]) for k in ("state", "kit"))):
        raise Unsafe("this project's registry entry is not two absolute plain paths (and an optional steward)")
    if STEWARD in e and not is_name(e[STEWARD]):
        raise Unsafe("this project's registry entry names a steward that is not an agent name")
    return e


def _entry(project: Path) -> dict | None:
    """This project's registry entry, None when unregistered; Unsafe when the entry is malformed."""
    e = projects().get(os.path.realpath(project))
    return None if e is None else checked(e)


def steward_line(e: dict, agent: str) -> tuple[str | None, bool]:
    """The steward note for this session (0.8.3), and whether this session is the steward.

    The session's own name comes from its environment (CONSOLE_KIT_AGENT, which
    the hook inherits from the session), never from the repository.
    """
    name = e.get(STEWARD)
    if not name:
        return None, True
    me = os.environ.get(AGENT_ENV, "")
    if is_name(me) and me == name:
        return (f"Owner console steward: this session is the steward, {name}. It alone runs `watch`, `synced` "
                f"and the fold for this console. It may ask the owner live (AskUserQuestion), but mirrors each "
                f"live answer to the console for the record: post the question with `{agent} ask FILE` and reply "
                f"on its item with the owner's answer (`{agent} reply ITEM TEXT`)."), True
    you = f"--as {me}" if is_name(me) else "--as YOUR-NAME"
    return (f"Owner console steward: {name} holds this console's doorbell, and this session is not it. Sign "
            f"every post with `{you}` (or start the session with {AGENT_ENV}=YOUR-NAME), and never run "
            f"`watch`, `synced` or the fold: they are refused here. AskUserQuestion is blocked in this session: "
            f"post each question with the console-ask skill (`{agent} {you} ask FILE...`); it reaches the owner's "
            f"inbox, and the steward, who asks the owner live, mirrors each live answer to the console."), False


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
    steward, mine = steward_line(e, agent)
    if steward:
        ask += "\n\n" + steward
    if not wake:
        return ask
    if not mine:  # the doorbell is the steward's: another session is told it waits, never how to take it
        return (f"Owner console: {len(wake)} request{'' if len(wake) == 1 else 's'} from the owner waiting "
                f"after doorbell seq {cursor}; the steward, {e[STEWARD]}, handles them.\n\n" + ask)
    lines = [f"Owner console: {len(wake)} request{'' if len(wake) == 1 else 's'} from the owner "
             f"waiting after doorbell seq {cursor}:"]
    for r in wake[:MAX_LISTED]:
        if r["intent"] == "chat":  # the chat is not a register item (0.7.0)
            lines.append(f"- seq {r['seq']} ({_shown(r.get('ts'), TS)}): answer the owner's chat message")
            continue
        what = {"process": "process the answers", "visual": "draw the visual the owner asked for"}.get(
            r["intent"], "run a deliberation round")
        lines.append(f"- seq {r['seq']} ({_shown(r.get('ts'), TS)}): {what} on `{_shown(r.get('item'), ITEM_ID)}`")
    if len(wake) > MAX_LISTED:
        lines.append(f"- … and {len(wake) - MAX_LISTED} more (`{agent} inbox`)")
    lines += ["",
              "Use the console-process skill for `process` and `chat`, the console-fork skill for `fork`, "
              "and the console-visual skill for `visual`. "
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

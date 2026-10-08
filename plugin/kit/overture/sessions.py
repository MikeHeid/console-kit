"""Session names (0.8.4): which agent a Claude Code session is, said by the user once, in the session.

Before 0.8.4 a session's agent name came only from `--as` or from
OVERTURE_AGENT, which the user had to put on the command line that started
Claude Code (`OVERTURE_AGENT=agent-5 claude`). Now the user can type

    /overture:as agent-5

in the session instead. The plugin's UserPromptSubmit hook
(`hooks/name_session.py`) sees that prompt with the session's id and appends
one line to the console's state directory:

    STATE/sessions.jsonl    {"agent": NAME, "session": SESSION_ID, "ts": TIME}

The SessionStart hook puts the session's id into the session's Bash
environment as OVERTURE_SESSION (through CLAUDE_ENV_FILE), so `agent.py`
and `fold.py`, run from that session's Bash, can look their name up here.

**The file only maps a session to a name.** Who the steward is stays in the
user's registry (`registry.py`), so a stale or damaged line can never make
someone the steward the registry does not name, and never stops the steward
from being named again: the LAST line for a session is its name, and a line
that is not well formed is skipped. Reading it fails OPEN: a file that cannot
be read gives no name, and the session falls back to OVERTURE_AGENT.

This module only reads. The writer is the hook, which may import nothing from
the kit, so it carries its own copy of these rules (`hooks/session_start.py`);
the kit's tests hold the two to one fixture.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from . import names as N
from . import registry as R

FILE = "sessions.jsonl"
ENV = "OVERTURE_SESSION"
# A Claude Code session id is a UUID; accept any short plain token, never a path or a newline.
SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,127}\Z")
MAX_FILE = 64 << 10   # the writer compacts before a line would take the file past this
KEEP = 256            # how many sessions a compaction keeps (the most recently named)


def mapping(data: bytes) -> dict[str, str]:
    """session id -> agent name from a sessions file's bytes: complete, well-formed lines only, the last one wins."""
    out: dict[str, str] = {}
    for line in data.decode("utf-8", errors="replace").split("\n")[:-1]:  # the last piece may still be written
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if (isinstance(rec, dict) and set(rec) == {"agent", "session", "ts"}
                and isinstance(rec["session"], str) and SESSION_ID.match(rec["session"])
                and N.problem(rec["agent"]) is None):
            out.pop(rec["session"], None)   # re-insert, so the dict's order is "most recently named last"
            out[rec["session"]] = rec["agent"]
    return out


def lookup(state: Path, session: object) -> str | None:
    """The agent name the user gave `session` for the console at `state`, or None (never raises)."""
    if not isinstance(session, str) or not SESSION_ID.match(session):
        return None
    try:
        data = R.read_regular(Path(state) / FILE, MAX_FILE, nofollow=True)   # the writer refuses a symlink too
    except (OSError, R.RegistryError):
        return None
    return None if data is None else mapping(data).get(session)


def resolve(given: str | None, state: Path | None, environ=os.environ) -> tuple[str | None, str]:
    """This session's agent name and where it came from: `--as`, then this session's
    /overture:as (OVERTURE_SESSION looked up at `state`), then OVERTURE_AGENT.

    An empty environment variable counts as unset. The name is returned as
    given, unchecked: the caller refuses a bad one, naming where it came from.
    """
    if given is not None:
        return given, "--as"
    if state is not None:
        named = lookup(state, environ.get(ENV))
        if named:
            return named, f"/overture:as (session {environ.get(ENV)})"
    if environ.get(N.ENV):
        return environ[N.ENV], N.ENV
    return None, "--as"

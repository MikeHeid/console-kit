"""UserPromptSubmit: `/console-kit:as NAME` names this Claude Code session as agent NAME (0.8.4).

Before 0.8.4 the user named a session only when starting it,
`CONSOLE_KIT_AGENT=agent-5 claude`. Now they may type, in the session,

    /console-kit:as agent-5

Claude Code hands every UserPromptSubmit hook the prompt's text as typed (a
slash command arrives literally, "/console-kit:as agent-5") and the session's
id. When the prompt is exactly that command with a valid agent name (the kit's
name rule, `console_kit/names.py`), this hook appends `session id -> name` to
STATE/sessions.jsonl of the registered project the session works in, the
state directory from the user's own registry, as the other hooks find it. The
skill of the same name (`skills/as`) only tells the user what happened; it is
user-only (`disable-model-invocation: true`), so Claude cannot run it.

The file names a session; it never makes one the steward: the steward is named
only in the user's registry, and `ask_guard.py`, the SessionStart note,
`agent.py` and `fold.py` compare the session's name with it. So a stale or
damaged line never locks the steward out: typing the command again writes a
new last line, which wins.

Every other prompt is not this hook's business: it prints nothing and exits 0.
It never blocks a prompt and never fails a session: a problem is said in the
note Claude reads, which the skill tells Claude to pass on to the user. It
executes nothing and imports nothing but `session_start.py`, beside it.

Who can fire it: a prompt submitted in the session. That is the user typing,
and also any prompt a SessionStart hook injects (`initialUserMessage`, measured
to reach this hook as the literal command): code the user installed, which
could as well write the file itself. Claude calling a tool does not fire it.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.dont_write_bytecode = True  # never leave a __pycache__ in the installed plugin
sys.path.insert(0, str(Path(__file__).resolve().parent))
import session_start as SS  # noqa: E402  (this plugin's own hook module, never the project's)

COMMAND = "/console-kit:as"
# The command, then whatever follows it on the line. The name is checked by the kit's rule, not here.
PROMPT = re.compile(r"\A\s*/console-kit:as(?:[ \t]+(?P<arg>[^\n]*?))?[ \t]*\n?\s*\Z")


def note_for(payload: object) -> str | None:
    """What to tell Claude (and, through the skill, the user) about this prompt; None when it is not the command."""
    if not isinstance(payload, dict):
        return None
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or len(prompt) > 4096:
        return None
    m = PROMPT.match(prompt)
    if m is None:
        return None
    name = (m.group("arg") or "").strip()
    if not name:
        return f"Owner console: nothing recorded. Say the name: `{COMMAND} NAME`, e.g. `{COMMAND} agent-5`."
    if not SS.is_name(name):
        return (f"Owner console: nothing recorded: {name!r} is not an agent name. Use 1 to {SS.MAX_NAME} "
                f"characters of lowercase letters, digits and single hyphens, starting with a letter "
                f"(e.g. agent-6); \"agent\" and \"owner\" are reserved.")
    sid = SS.session_id(payload)
    if sid is None:
        return "Owner console: nothing recorded: Claude Code gave this hook no session id."
    try:
        e = SS.enclosing_entry(payload)
    except (SS.Unsafe, OSError) as err:
        return f"Owner console: nothing recorded, because {err}. See `agent.py register`."
    if e is None:
        return ("Owner console: nothing recorded: this project is not registered for an owner console "
                "(`agent.py register`), so there is no console to name this session for.")
    state = Path(e["state"])
    try:
        wrote = SS.record_session(state, sid, name)
    except (SS.Unsafe, OSError) as err:
        return f"Owner console: nothing recorded, because {err}."
    agent = f"python3 {Path(e['kit']) / 'agent.py'} --state {state}"
    head = (f"Owner console: this session is now {name}" if wrote else
            f"Owner console: this session was already {name}") + (
        f" (session {sid}, recorded in {state / SS.SESSIONS_FILE}). Its console posts are signed {name} without "
        f"--as, and it keeps the name across --resume and --continue; /clear and --fork-session start a new "
        f"session, which needs naming again.")
    steward, _ = SS.steward_line(e, agent, name)
    return head + ("\n\n" + steward if steward else "")


def main() -> int:
    try:
        note = note_for(SS.read_input())
    except Exception as e:  # noqa: BLE001 - a broken hook never costs the owner a prompt
        note = f"Owner console: /console-kit:as failed ({type(e).__name__}); nothing may have been recorded."
    if note:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": note}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())

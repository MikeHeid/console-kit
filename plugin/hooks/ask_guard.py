"""PreToolUse on AskUserQuestion: only the console's steward asks the owner live (0.8.3).

Owner, 2026-09-30, "Lock + hook others ★": several sessions share one owner
console, and one of them, the STEWARD named in the user's own registry, holds
its doorbell. Every other session posts its questions to the console with
`agent.py ask` (the console-ask skill), so they reach the owner's inbox and the
record. The steward may still ask the owner live, and mirrors each live answer
to the console.

**Which session is the steward.** A hook learns nothing from Claude Code about
who the session is beyond a session id (the input's `session_id`), which
changes on /clear and --fork-session and is kept by --resume and --continue.
So the user says it (0.8.4): typing `/console-kit:as agent-5` in the session
records that session id as agent-5 in the console's STATE/sessions.jsonl
(`name_session.py`). Starting the session as `CONSOLE_KIT_AGENT=agent-5 claude`
still works, as the fallback when the session was given no name. The steward
itself is named only in the user's registry (`agent.py steward NAME`), never by
the repository, and never by the sessions file: that file only says which name
a session goes by.

The decision, for a call to AskUserQuestion:

- the session's directory (the input's `cwd`, then CLAUDE_PROJECT_DIR) is in
  no registered project, or its console has no steward: allowed, as in 0.8.2;
- the session's name (its /console-kit:as, else CONSOLE_KIT_AGENT) is the
  steward's name: allowed;
- otherwise: denied, with the full `agent.py ask` command (KIT and STATE from
  the registry) as the reason Claude reads.

**It fails OPEN.** An unreadable registry, a malformed entry, input it cannot
parse, or any error at all lets the call through, silently: a guardrail never
costs the owner a session. It reads the registry as the SessionStart hook does
(`session_start.py`, in this same folder, the only thing it imports), and
executes nothing from the project.

This is a guardrail between the owner's own cooperating sessions, not a
security boundary: any session can set CONSOLE_KIT_AGENT to the steward's name,
and anything that runs code as the user can write the sessions file.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.dont_write_bytecode = True  # never leave a __pycache__ in the installed plugin
sys.path.insert(0, str(Path(__file__).resolve().parent))
import session_start as SS  # noqa: E402  (this plugin's own hook module, never the project's)

TOOL = "AskUserQuestion"
MAX_INPUT = 1 << 20


_registered = SS.registered   # the walk lives beside the other copies of the kit's rules (0.8.4)


def reason(payload: object) -> str | None:
    """Why this AskUserQuestion call is blocked, or None to let it through. Raises on anything unreadable."""
    if not isinstance(payload, dict) or payload.get("tool_name") != TOOL:
        return None
    e = SS.enclosing_entry(payload)
    if e is None or not e.get(SS.STEWARD):
        return None
    steward = e[SS.STEWARD]
    me = SS.whoami(Path(e["state"]), payload.get("session_id"))
    if me == steward:
        return None
    you = f"--as {me}" if me else "--as YOUR-NAME"
    cmd = f"python3 {Path(e['kit']) / 'agent.py'} --state {e['state']} {you} ask QUESTION.json"
    return (f"Owner console: AskUserQuestion is blocked in this session. This console's steward is {steward}, and "
            f"this session is not it, so it does not ask the owner live. Post the question to the owner's console "
            f"instead, following the console-ask skill (one JSON file per question: text, options with what each "
            f"costs, your recommendation):\n\n    {cmd}\n\n"
            f"It reaches the owner's inbox, where the owner answers and locks it; the steward, {steward}, who asks "
            f"the owner live, mirrors each live answer to the console, so every question reaches the owner either "
            f"way. Then carry on with other work, or stop and say which question is waiting on the console."
            + ("" if me else f" (If this session is the steward, the user names it by typing "
                             f"`/console-kit:as {steward}`; tell the user.)"))


def main() -> int:
    try:
        raw = sys.stdin.read(MAX_INPUT + 1)
        why = reason(json.loads(raw)) if raw.strip() and len(raw) <= MAX_INPUT else None
    except Exception:  # noqa: BLE001 - fail open: a broken guardrail never blocks a session
        return 0
    if why:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                 "permissionDecisionReason": why}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())

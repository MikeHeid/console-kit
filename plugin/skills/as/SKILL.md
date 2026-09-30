---
name: as
description: Name this Claude Code session as an owner-console agent, e.g. /console-kit:as agent-5 (typed by the user only).
argument-hint: NAME
disable-model-invocation: true
---

# This session's agent name

The user typed `/console-kit:as $ARGUMENTS`. The console-kit plugin's
UserPromptSubmit hook has already handled it, before this text reached you:
it either recorded this session under that name for the owner console, or
refused, and it said which in an "Owner console:" note in your context for
this prompt.

Tell the user, in one or two sentences, exactly what that note says: the name
recorded (or why nothing was), and, when the note says so, whether this
session is the console's steward. Do nothing else: run no command, and do not
write the name anywhere yourself. If there is no "Owner console:" note, say
that nothing was recorded (the plugin's hook did not run, or this project is
not registered for an owner console), and that starting the session as
`CONSOLE_KIT_AGENT=NAME claude` names it instead.

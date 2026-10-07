---
name: playbook
description: Use when the user wants to run a named playbook on this project's console — a codified multi-step delegation (round, visual, chat). The playbook files live under .console-kit/playbooks/<slug>.json. This skill lists what is available, confirms which to run, and dispatches it through the console's agent socket; each step then arrives in the owner's inbox like any other delegation.
---

# Run a named playbook

A playbook is a short JSON file at `.console-kit/playbooks/<slug>.json`
that lists steps the owner wants fired as one:

- `round` — start a deliberation on an item.
- `visual` — ask the agent to draw a diagram or mock.
- `chat` — a free-text message in the chat thread.

The console serves `view.playbooks` with every playbook's name, one-line
description, and the number of steps. This skill reads that list, asks
the user which one to run, and dispatches it through
`agent.py playbook <name>`. The server fans each step out as an owner
message write; the watching steward picks them up like any other
delegation and the Inbox shows the firing under **Triggers → Recent
firings**.

## What you must never do here

- **Never post** `agent.py items-push`, `prs-push`, `page-snapshot` or
  any other owner-privileged write "to help the playbook along". A
  playbook is a complete, named unit; running it is one tap, and the
  server fans the steps out itself.
- **Never run a playbook that is not already a file.** If a slug the
  user names has no file under `.console-kit/playbooks/`, say so and
  stop. Writing a new playbook file is a project-code change the owner
  reviews and commits, not an in-session move.
- **Never retry a failing playbook automatically.** Each playbook run
  mints a nonce from the slug and the second; a retry under the same
  second is intentionally idempotent (the server dedupes). Tell the
  user what the server reported (`records`, `skipped`, any `error`)
  and let them decide.

## Flow

1. **Confirm the slug.** If the user named one, use it; otherwise read
   `view.playbooks` and offer the names + descriptions. Show at most 10
   and let the user pick.
2. **Confirm the run.** Say the playbook's step count and the kinds in
   it so the user knows what fan-out they're approving.
3. **Dispatch.** Run:
       python3 ~/.local/share/console-kit/kit/agent.py --state <STATE> playbook <slug>
   `<STATE>` is the registry's state path for this project; the
   SessionStart banner names it. The agent flag (`--as`) is this
   session's name if one was given.
4. **Report the result.** The server returns `{records, skipped}`.
   Each `records[i]` is one step's `message` record; each `skipped[i]`
   names the step index and why. Tell the user:
   - how many steps landed,
   - any skips (usually an item id that isn't in the register),
   - that the Inbox now shows the firing under **Triggers → Recent
     firings** and each step as an inbox row.

## Who writes what

- **This skill** reads `view.playbooks`, dispatches, reports the
  result. Nothing else.
- **The server** writes each step's `message` record as the owner,
  rings the doorbell per record, and logs the firing in the trigger
  log (`view.trigger_log` carries the newest 20).
- **The steward** picks up the doorbell line on its next `watch`
  and handles each delegation as usual.

---
name: scaffold-dashboard
description: Use when the user wants a dashboard page for the owner console (an onboarded project where the console shows only the starter page, or no page yet), or asks how to make the console's page show live project data. Writes a dashboard HTML template with Items / Rollout / Engine / Spec sections marked for live refresh, and injects a matching board() method into the project's adapter if missing. Idempotent: re-runs add missing sections without clobbering hand edits.
---

# Scaffold a dashboard page for this project's console

The owner console serves whatever `page-snapshot` sent (frozen at a commit).
Beside that it serves the latest values from the adapter's optional `board()`
method as data: a page whose elements carry `data-live="..."` refreshes those
elements from the board without a new snapshot.

This skill writes a working dashboard and the matching `board()`. Both are
templates the project then edits; nothing is written into the project's own
record.

## What you must never do here

- **Never run** `agent.py page-snapshot` yourself: it writes a page the console
  then serves to the owner. The user runs that one.
- **Never run** `agent.py items-push` yourself unless the user explicitly asks:
  the agent sends live values from THIS process to the server.
- **Never clobber the user's hand edits.** The scaffold uses markers
  (`<!-- scaffold:<name> start -->` / `<!-- scaffold:<name> end -->` and
  `# scaffold:board start/end`) and refuses to overwrite content between them.
  If a section's markers are missing but a section with that heading exists,
  tell the user and ask whether to insert markers or skip it.

## Before you call anything

Confirm with the user:

1. **The project's name** that should appear in the dashboard header (the
   directory name is the default).
2. **The page path**. The onboarding default is `.console-kit/page.html`,
   which 0.9.1 accepts (dotfile first segment). Many projects prefer
   `docs/console/page.html` so the page is kept with the other docs. Ask
   which they want; the default is `docs/console/page.html`.
3. **The adapter path**. The onboarding default is
   `.console-kit/adapter.py`. Only override when the user already moved
   the adapter.
4. **Which sections**: all of Header / Items / Rollout / Engine / Spec /
   Footer by default. The user may ask to skip Engine or Spec; both are
   static placeholders the user fills in anyway.

## The command

Once the user has confirmed, run:

```bash
python3 ~/.local/share/console-kit/kit/agent.py --state <STATE> scaffold-dashboard \
    --project <PROJECT_ROOT> --page <PAGE_PATH> --adapter <ADAPTER_PATH> --name <PROJECT_NAME>
```

Show the user the full command (the paths, the state, the name) before you
run it. The command is **idempotent**: if the page is already scaffolded, it
reports "already in place; nothing to write." Re-running after the user
has edited the page keeps every edit between the markers.

## After the command runs

Print the operator steps **the user runs themselves**:

1. **Review** both files; both are theirs to edit. The `board()` function
   has `"—"` placeholders for `open_questions`, `open_prs`, `last_fold`;
   point them at the counters the project already has (open issues from
   the tracker, open PRs from the steward's `prs-push`, the `fold` log).
2. **Commit** the page and the adapter so `page-snapshot` can read them:
   `git add -A && git commit -m 'scaffold dashboard'`.
3. **Snapshot the page** and send it to the console:
   `agent.py --state <STATE> page-snapshot --path <PAGE>`
   then press **"Use this page"** in the console.
4. **Push the first items + board**:
   `agent.py --state <STATE> items-push --adapter <ADAPTER>`.
5. **(Optional) keep it live.** Run `items-watch` in a terminal (or as a
   systemd user unit) to re-push whenever the adapter, `items.json` or
   `.console-kit.json` changes:
   `agent.py --state <STATE> items-watch --adapter <ADAPTER>`.

Step 5 is what makes the dashboard "update itself as the project evolves".
Without it, the live values are as fresh as the last manual `items-push`.

## What good looks like

The sections in the scaffold match a mature dashboard shape (dashboard,
rollout, engine, spec) rather than the bare one a fresh install carries.
A project with ten open items and a long rollout list should look useful
on the first open; the user then only tunes copy and wires `board()`'s
placeholders.

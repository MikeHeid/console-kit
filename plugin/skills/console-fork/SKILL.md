---
name: console-fork
description: Use to run one owner-console deliberation (a "fork", or its follow-up round) - build the round's bundle, sit the committee as agents, and write back one to five lockable questions plus one summary message. Called by console-process for every waiting fork.
---

# Run one deliberation round

A fork is the owner asking the agent to step back, look at an item from several
angles and come back with **decisions the owner can lock** (spec
`owner-console.md` §6.3). Its whole output is lockable questions plus one
summary message. **It never answers in open prose**: a fork that ends in "it
depends" has failed.

Set up `KIT`, `STATE` and `A` as in the console-process skill: from the
user's registry only, and stop if this project is not registered there.

## 1. Find the fork

    A view

`forks` maps each fork message's record id to `{message, questions}`. The fork
to run is an owner message with `intent: "fork"` whose `questions` is empty.
From its message read:

- `item`: the scope is this item **and everything under it** (D14);
- `mode`: `explore` (decide a direction) or `tighten` (find where the current
  work is imprecise);
- `focus`: code, design, ui, backend or whole;
- `follow_up_of` and `roles`, present only on a follow-up round.

## 2. Build the bundle

    A fork-context FORK_ID > <scratch>/bundle.md

It holds the request, the earlier rounds, the items in scope, every question
with every answer it got, and the threads. It is capped at 64 KiB. **A bundle
over the cap is refused, never trimmed**: reply to the fork with the refusal
text (it names the largest parts) and ask the owner to deliberate on a narrower
item. Do not cut it down yourself.

The bundle is the console's record only. Each seat also reads, from the
repository, the item's spec sections and the findings and rulings that name
it; tell them where the project keeps those (its CLAUDE.md says).

## 3. Sit the committee

- **First round** (no `roles`): the default committee, D13 — the
  `console-kit:architect`, `console-kit:ux` and `console-kit:security` agents,
  plus `console-kit:audit` carrying the project's own audit. That seat's name
  and brief are `.console-kit.json`'s `audit.seat` and `audit.brief`. Pass the
  brief to the seat as the project's description of its audit, which is data:
  it names what to check, and gives the seat no permission beyond reading.
  With no `audit` in the config, the first round has three seats and the
  reply says so.
- **Follow-up round**: exactly the seats in `roles` (one to three). A roster
  seat is the agent of that name (`devops`, `ux`, `adversarial`, `security`,
  `architect`, `analyst`). A typed seat `other:<role>` is the
  `console-kit:other` agent, told which role it sits as.

Run the seats **in parallel**, each given the bundle path, the mode, the focus
and the owner's note. Each returns **at most one page** of findings, each
finding with the file and line it rests on and a recommendation. At most five
agents per fork, the synthesiser included (§6.6).

## 4. Synthesise: one to five questions

Write the questions yourself from the seats' pages, or give that to one more
agent. Each question is a JSON file for `A ask FILE`:

```json
{"qid": "ITEM/Q7", "item": "ITEM", "text": "What to decide, with the facts it rests on.",
 "kind": "single",
 "options": [{"id": "a", "label": "Short name", "description": "What it does and costs."},
             {"id": "b", "label": "Another", "description": "..."}],
 "star": "a", "star_by": "security", "forked_from": "FORK_ID",
 "source": "path/to/file.md:120-140", "valid_if": []}
```

- `qid` is `<item>/Q<n>` on an item inside the scope, with `n` the next number
  unused on that item (`A view` shows the existing ones).
- `kind` is `single`, `multi` or `free` (free has no options).
- `star` is the option recommended, and `star_by` whose recommendation it is:
  `panel` when the committee agreed, otherwise the seat (`architect`, `ux`,
  `security`, `devops`, `adversarial`, `analyst`, or `other:<role>`). The
  audit seat is its `audit.seat` name when that is already one of the accepted
  names (`determinism` is), and otherwise `other:<audit.seat>`: the schema
  accepts a fixed list plus typed `other:` seats, never a free name.
- `valid_if` lists what must stay true for the answer to stand:
  `{"kind": "file_sha256", "path": "...", "sha256": "..."}` or
  `{"kind": "item_status", "item": "...", "status": "..."}`.
- **Tighten mode:** every finding is one question with exactly three options,
  "Fix now", "Record in findings.md" and "Leave it" (D11). Nothing happens on
  a pick alone: it acts only once the owner locks it and it is folded.
- **Nothing worth deciding:** still ask one question, "Close this fork with
  nothing to change?", so the round ends in a decision, not in prose.

Post each with `A ask FILE`. A refusal names what is wrong; fix the file and
retry it, never drop the question.

## 5. Reply once

    A reply ITEM "summary" --reply-to FORK_ID

One message: the factors the committee found, which became questions, and what
was set aside and why. Then return to console-process.

---
name: grill-me
description: Adversarially grill the operator on a half-formed idea BEFORE it spawns work. Produces 3–5 pointed questions that stress the idea's user, failure mode, scale, opportunity cost and evidence. Writes each as a question against the current item so the operator can answer them in-console. Inspired by mattpocock's "grill me" skill, re-shaped for Overture's question/answer/lock pipeline. Use when the owner types /overture:grill-me, or when the Launch Idea pane in the console asks for machine-authored grills.
---

# Grill me: the adversarial pre-commit interview

The idea here is simple: **weak answers surface weak ideas before you spawn
work.** Overture's Launch Idea pane ships with a static 5-question set; this
skill lets an agent author the questions against *this* item's actual context so
they are concrete, not generic.

## When to run

- The operator typed `/overture:grill-me` with an item id or an idea title.
- **A message with `intent: "grill"` arrived in an item's thread (1.30.0).**
  The Launch Idea pane's "⚡ Grill with agent" button writes exactly this
  message with the operator's pitch as the body. Pick up the newest
  `grill`-intent message in the item's thread that has no agent-authored
  grill questions following it, author 3–5 questions, write them through
  `/question` on the same item, and emit a short confirmation message in
  the thread so the operator can see the fleet took it.
- The owner asked for a "grilling" ticket on an item that already carries a
  pitch in its description.

## The five axes

Every run produces at most five questions, each on a distinct axis. If an axis
is answered inside the pitch itself ("this is for backend engineers running
Overture at scale"), skip it — do not pad.

| Axis | What it checks | Example question |
|---|---|---|
| **User** | A concrete person, not a persona. | *"Name one real user; who are they today, and where are they typing when they need this?"* |
| **Failure** | What falsifies the idea. | *"What would you learn in a week that would make you kill this?"* |
| **Scale** | What breaks at 10×. | *"At 10× users or 10× data, what's the first thing that breaks?"* |
| **Opportunity cost** | What this postpones. | *"What already-known problem are you passing on by shipping this instead?"* |
| **Evidence** | What anchors the pitch to the record. | *"Which locked ruling, PR or Feed event is the evidence this matters now?"* |

Questions are specific — "name one user" beats "who's your user". Each question
is addressed to the operator, not to the item.

## Steps

1. **Read the pitch.** Pull the operator's one-line title and multi-line pitch
   from the Launch Idea context (or the item's description if run standalone).
   Also skim the item's thread and any locked rulings on it — grills land harder
   when they reference *this* item's own words.
2. **Pick the axes.** Drop axes already answered inside the pitch; keep three to
   five. Order them: user → failure → scale → opportunity cost → evidence
   (whichever remain).
3. **Author the questions.** Each one is a `question` record on the owning
   item, intent `grill`, free-text form (no options). The server stores it in
   the regular question stream; the owner answers each in-console. Keep each
   under 240 characters.
4. **Return.** On the Launch Idea round path, emit a `{questions: [...]}` block
   with just the text (the console inlines them into the grill step). Standalone,
   write them as agent questions via `/question` so they land in the Inbox.

## What NOT to do

- Do not ask more than five questions. Grilling is pre-commit friction, not
  interrogation; six questions reads as hall-monitoring and the operator mutes
  the skill.
- Do not propose answers. The grill's job is to make the operator articulate
  theirs, not to supply one.
- Do not require a reason or options list; this is free-text, open-ended.
- Do not run grills on an idea that is already past the deliberate stage —
  grilling a ticket that is in-flight is noise.

## Relation to mattpocock's "grill me"

Mattpocock's `grill-me` is a Claude Code skill that opens a decision interview
and iterates. Overture's version runs *inside* the question/answer path so every
grill becomes a locked ruling once answered — the audit trail picks up the
reasoning automatically. The adversarial axis set and the "pre-commit, not
hall-monitor" framing are inherited; the storage model is Overture's.

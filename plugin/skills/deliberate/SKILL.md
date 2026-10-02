---
name: deliberate
description: Work through one decision with the owner - frame what depends on it, lay out two to four options with their real costs, recommend one with a star, and name what would change the recommendation. Use when the user types /console-kit:deliberate (with a question, or "open" to list the undecided items), or when an owner decision needs laying out before it is made.
---

# Deliberate: lay a decision out so the owner only picks

Decision support, usable at any point in a project. **Never ask the owner to
produce an answer from scratch**: lay the choice out so they only have to
pick, edit or veto. And **never answer or lock for them**: you recommend, they
decide.

## Where the project keeps its decisions

This skill reads, and never assumes, the project's layout:

1. `.console-kit.json`'s `specs_dir`, if set, is where its specs live.
2. The project's `CLAUDE.md` or README, which usually names its decision log,
   findings register and spec folder.
3. Otherwise the **App Architect convention**, if the project uses it: an
   `architect/` folder holding `digest.md` (a running summary with a
   `## Decisions` section), `10-stack.md`, `30-segments.md` (the segment map),
   `40-specs/` (one spec per segment) and `60-review/findings.md`. It is a
   convention, not a requirement: use whichever of these files exist.

If none of these exists, say so, and work from what the conversation and the
code show.

## Find the question

If the argument is `open` (or empty), gather every undecided item: entries
marked "architect default" or "builder's choice" in specs, questions a spec
lists as open, recommendations nobody acted on, and TODO markers in the
decision files. Present them as a numbered list and let the owner pick one.
Otherwise the argument is the question.

## Lay it out

1. **Frame it** in one sentence: what actually depends on this decision.
2. **Two to four options** as a table: option / what it looks like in THIS
   project (its real nouns, files and screens, not generic examples) / upside
   / cost.
3. **Mark your recommendation ★**, with two or three sentences of reasoning
   against the project's own goals, constraints and earlier decisions, each
   reason with the file and line it rests on where one exists.
4. **Name what would change your mind**: the condition under which another
   option wins.

The owner picks, edits or asks follow-ups; go as many rounds as needed.

## Record it

- **Inside an owner-console round** (you reached this from the console-fork
  skill, or the project is registered for the console and the decision is the
  owner's): the decision goes out as a **lockable question**, never as an
  edit. Post it with the console-ask skill (or, in a fork, exactly as
  console-fork step 4 says): the options above become its options, your ★
  its `star`, the framing and the reasons its `text` and `evidence`. For a
  "deliberate before answering" fork, console-fork section 9 applies
  instead: the result is one reply whose first line is `Result: ★ <option
  id>`. **Nothing is written into the project before the owner locks.** Once
  the locked answer is folded (console-fold), the file it belongs in changes
  by pull request, and a decision-log or digest line is added at merge time,
  not in the branch.
- **Standalone**, after the owner has decided in the conversation: write the
  decision into the file it belongs to (spec, stack, segment map), add one
  line to the decision log (`digest.md`'s `## Decisions`, under the App
  Architect convention), and commit it on a branch with a message like
  `decide: <short label>`, landing it the way the project lands changes. Never
  record a decision the owner has not made.

## The deliberation contract

Never hand the owner a blank question. Any time input is needed, present
lettered options with a one-line tradeoff each and a ★ recommended default,
grounded in this project's goals. "You choose" or "defaults" is always a valid
answer, and means: take every ★ and record each as an **architect default**,
not as an owner decision, so a later review can audit it. Inside a console
round "defaults" still goes out as questions with a ★: the owner locks them.

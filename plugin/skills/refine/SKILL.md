---
name: refine
description: Revise one existing spec or planning document so it says what was decided - read it and the decisions and findings that bear on it, propose the changes as a change list before touching it, and name the other documents the change ripples into. Use when the user types /overture:refine, or for an owner-console "Refine" fork (the overture:refine next_step).
---

# Refine: revise one document to match what was decided

A targeted revision of something that already exists. (To go one level down
into something that has no spec yet, use drill instead.)

## Where the project keeps its documents

1. `.overture.json`'s `specs_dir`, if set, is where its specs live.
2. The project's `CLAUDE.md` or README, which usually names its spec folder,
   decision log and findings register.
3. Otherwise the **App Architect convention**, if the project uses it: an
   `architect/` folder holding `digest.md` (a running summary with a segment
   map and a `## Decisions` section), `10-stack.md`, `30-segments.md`,
   `40-specs/<segment>.md`, `50-master/` (generated copies) and
   `60-review/findings.md`. Use whichever of these exist.

## Find the target

Resolve the argument to **one** file: a spec under the specs folder, or a
planning document (stack, segment map, an orchestrator or master copy).
Inside an owner-console round the target is what the locked answer, or the
round's answers, cite: their `source`, `evidence` and `valid_if` paths, and
the item's spec. If it resolves to nothing, or to several files, say so and
stop rather than guess.

## Steps

1. **Read** that file, the decision log, and the findings that bear on it
   (only the relevant ones, if finding numbers were given). In a console
   round, also read the bundle (`A fork-context FORK_ID`): the owner's own
   words on the locked answer beat the old text.
2. **Propose the revision before touching the file**, as a change list:
   "current -> proposed, because ...", one line per change, citing the file
   and line it changes. Where a change embeds a real choice, do not pick
   silently: present lettered options with a ★ recommendation, as the
   deliberate skill does.
3. **Name the ripple.** If the file is a spec or the stack, check the segment
   map (or whatever links documents in this project) and name every OTHER
   file that now needs revising.
4. **Generated copies.** If the target has a generated copy (a `50-master/`
   suite under the App Architect convention), the copy is regenerated from
   its source, never edited by hand, so the two never diverge.

## Landing it

- **Inside an owner-console round** (a fork with `step: "refine"`, run by
  the console-fork skill's section 8): **nothing is written before the owner
  locks.** The change list and every embedded choice go out as one to five
  **lockable questions**, `forked_from` the fork, exactly as console-fork step
  4 says; the tighten options (`fix`, `record`, `leave`) when the fork's mode
  is `tighten`, which is a refine fork's default. Then one `Result:` reply.
  Do not edit the spec, the decision log, the findings or any other project
  file in that session. After the owner locks and the answers are folded,
  the revision lands **by pull request**, and any decision-log or digest line
  is added **at merge time**.
- **Standalone**, after the owner approves the change list in the
  conversation: apply it on a branch, mark the findings it addresses as
  resolved, add one line to the decision log, and commit with a message like
  `refine: <target> (<finding refs>)`, landing it the way the project lands
  changes. The old version stays in history: never rewrite it.

## The deliberation contract

Never hand the owner a blank question. Any time input is needed, present
lettered options with a one-line tradeoff each and a ★ recommended default,
grounded in this project's goals. "You choose" or "defaults" means: take every
★ and record each as an **architect default**, not as an owner decision, so a
later review can audit it. Never answer or lock for the owner.

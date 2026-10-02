---
name: drill
description: Drill one level down into a segment or concept that has no spec yet - list the functions it must capture, ask the three to five questions that would change its spec (each with lettered options and a star), then write a short standalone spec. Use when the user types /console-kit:drill, or for an owner-console "Drill" fork (the console-kit:drill next_step).
---

# Drill: one segment, from functions to a spec

One segment (or one concept a decision introduced) per run. (To revise a spec
that already exists, use refine instead.)

## Where the project keeps its specs

1. `.console-kit.json`'s `specs_dir`, if set, is where specs live.
2. The project's `CLAUDE.md` or README, which usually names its spec folder
   and decision log.
3. Otherwise the **App Architect convention**, if the project uses it: an
   `architect/` folder holding `digest.md` (a running summary with a segment
   map), `10-stack.md` (the locked stack), `30-segments.md` (each segment with
   a `status`) and `40-specs/<segment>.md`. Use whichever of these exist.

## Find the target

- **Standalone**: resolve the argument against the segment list
  (`30-segments.md` under the convention). `next` means the first segment not
  yet at status `spec`.
- **Inside an owner-console round** (a fork with `step: "drill"`, run by the
  console-fork skill's section 8): the target is the thing the locked answer,
  or the round's answers, introduced that no spec covers yet. The owner's own
  words are in the bundle (`A fork-context FORK_ID`).

## Steps

1. **Analyse.** Read the decision log or digest first, not the whole tree.
   List the functions this segment must capture: at most eight concrete
   capabilities. Check the segment map and flag any boundary that overlaps
   another segment.
2. **Clarify.** Write three to five questions specific to THIS segment whose
   answers would materially change its spec. Never a bare question: each has
   two or three lettered options with a one-line tradeoff, and your
   recommendation marked ★ with one sentence on why.
3. **Spec.** Once the questions are answered, deliberate on the edge cases
   and integration points, and write the spec, under about 600 words, with
   the headings Purpose / Functions (each with acceptance criteria) / Data and
   Integration Points / Out of Scope / Build Instructions. It must stand alone
   as a build prompt and respect the project's locked stack. Record every
   question that took the ★ by default as an **architect default**.

## Landing it

- **Inside an owner-console round: nothing is written before the owner
  locks.** Step 2's questions go out as one to five **lockable questions**,
  `forked_from` the fork, exactly as console-fork step 4 says (the fork's
  mode is `explore` by default), then one `Result:` reply naming the target.
  Do not write the spec, the segment status, the digest or any project file in
  that session. After the owner locks and the answers are folded, the spec
  lands **by pull request**, and the segment status and any digest line are
  added **at merge time**.
- **Standalone**: ask the questions in the conversation and wait. Anything
  left unanswered takes its ★, recorded as an architect default. Then write
  the spec on a branch, set the segment's status to `spec`, add a one-line
  summary to the digest, and commit with a message like `spec: <segment>`,
  landing it the way the project lands changes. Finish by reporting how many
  segments now have a spec and what comes next.

## The deliberation contract

Never hand the owner a blank question. Any time input is needed, present
lettered options with a one-line tradeoff each and a ★ recommended default,
grounded in this project's goals. "You choose" or "defaults" means: take every
★ and record each as an **architect default**, not as an owner decision, so a
later review can audit it. Never answer or lock for the owner.

---
name: roar
description: Run a three-perspective panel (architect, ux, and an advisor seat) on a question, decision or design - independent reads in parallel, a deliberation round where the panelists challenge each other, then a full implementation plan. Use when the user types /overture:roar followed by a question, decision or design under review. Inside an owner-console round (a fork with roles ["roar"]) the console-fork skill's section 7 governs, and the round ends in lockable questions, not a plan.
---

# Roar: three seats, one deliberation, one plan

`/overture:roar <prompt>` runs a **three-round** panel that ends in a
complete implementation plan. The user sees a short status block before each
round.

1. **Round 1, independent reads.** Three panelists answer in parallel. None
   sees the others.
2. **Round 2, deliberation.** Each panelist sees the other two's Round 1
   claims and concedes, pushes back or refines.
3. **Round 3, synthesis.** A full implementation plan built on where Round 2
   converged.

The plan is the deliverable. Rounds 1 and 2 exist to pressure-test it.

## Which mode you are in

- **Inside an owner-console round.** You reached this from the console-fork
  skill (a fork whose `roles` is `["roar"]`, with a fork id and a bundle from
  `A fork-context FORK_ID`). Follow console-fork **section 7** for the rules
  that are the console's: at most one roar per lock, the `ck-fork:FORK_ID ROLE`
  prefix on every seat's task description, the transcript stored with
  `A transcript`, and **Round 3 ends in one to five lockable questions, not a
  plan**. Nothing is written into the project before the owner locks; a change
  those answers call for lands later, by pull request. This skill supplies the
  panel's shape and prompts only.
- **Standalone** (the user typed the command in a session). Run all three
  rounds below and end with the plan and a waiting-for-confirmation line.

Either way, **never answer or lock for the owner**: the panel recommends, the
owner decides.

## The seats

The panel uses the plugin's own committee agents, so it works on a fresh
install with nothing else in place:

| Seat | Agent | Angle |
|---|---|---|
| advisor | `overture:other`, told to sit as **advisor** | strategy, scope, build or buy, what not to build |
| architect | `overture:architect` | structure, data model, coupling, invariants, tech-debt risk |
| ux | `overture:ux` | what the user or operator sees: discoverability, first run, errors, accessibility |

The committee agents expect a **bundle file** to read first. Standalone,
write one yourself to a scratch file (never into the repository): the
user's prompt, 2 to 6 sentences of context from the conversation (what is
being built, which files, earlier decisions, what has shipped), and the
paths a seat should read. Inside a console round the bundle is
`A fork-context FORK_ID`'s output.

## Status blocks

Before each round, print one compact code block. Plain text and box
characters only, no emoji. Progress is a 20-cell bar: `█` done, `▋` a
half cell, `░` empty. Panelist marks: `○` queued, `●` running, `✓` done,
`✕` failed.

```text
Roar - "<one-line topic>"

██████▋░░░░░░░░░░░░░  33%  > Round 1 complete, deliberating

Panelists
  ✓ advisor       "<one-line verdict>"   (N claims)
  ✓ architect     "<one-line verdict>"   (N claims)
  ✓ ux            "<one-line verdict>"   (N claims)

Rounds
  ✓ Round 1  independent reads       done
  ● Round 2  deliberation            running
  ○ Round 3  synthesis               waiting
```

Before Round 1 the bar is empty and every panelist `○ queued`; before Round 3
it is at 66% and each panelist line reads `conceded N, pushed back on N,
unresolved N`. The plan itself needs no block. If the user cancels mid-round,
print a last block with `✕` for every panelist still pending.

## Round 1: independent reads

Dispatch the three seats **in parallel**: one message, three Agent calls.
Tailor each prompt to its seat. Reuse the same facts, but ask the advisor
strategic questions, the architect system and data questions, and ux
user-journey questions. Each prompt:

- names the bundle file to read first, and for `overture:other` says
  "sit as advisor";
- caps the answer at **about 300 words**;
- asks for every claim to rest on a file and line where one exists, verified
  rather than recalled;
- ends with: "Number your claims so they can be referenced in a follow-up
  round."

## Round 2: deliberation

Once all three return, print the status block, then build a compact panel
transcript: one paragraph per seat, its verdict and its numbered claims.
Dispatch the same three seats again, **in parallel**, each with:

```
Round 1 of this panel is complete. You gave the following read:
<its own numbered claims, verbatim>

Here are the other two panelists' reads:
<the other two seats' numbered claims, grouped by seat>

Deliberate, under 250 words, in this structure:
- Concede: which of their claims you now agree with, and why.
- Push back: which you still disagree with, and the strongest counter-argument,
  with evidence.
- Refine: if Round 1 shifted your position, state the updated stance.
- Unresolved: anything the panel has not actually answered.
Do not re-argue your original case. Move the debate forward.
```

## Round 3: the plan (standalone)

Print the status block, then write a working plan that absorbs the debate.
It is not a summary of it.

```markdown
# Roar plan: <one-line topic>

**Panel verdict after deliberation:** Green-lit / Green-lit with corrections / Caution / Rework

## Panel convergence
One paragraph: where the panel landed, which points are still contested, and
how the plan resolves each. Do not re-list the seats' outputs.

## Requirements restatement
- What is being asked for, in your own words
- What problem it solves
- What success looks like

## Key decisions
One line each: the pick and the reason. Every decision that came out of the
panel appears here, especially where Round 2 corrected Round 1.

## Risks and mitigations
| Risk | Severity | Mitigation |
|------|----------|------------|

## Implementation phases
Numbered. For each:
- **Scope**: what ships
- **Files touched**: paths, new or modified
- **Effort**: a rough band (30m / 1h / 2-3h / half-day / day)
- **Dependencies**: what blocks it
- **Verification**: how we know it works, with the project's own checks
Order the phases so each can land on its own and leaves the system working.

## Deferred / out of scope
What is consciously not done this round, and why, including the panel's
follow-ups.

## Verification plan
- [ ] The project's own build, lint and type checks
- [ ] Tests: <paths>
- [ ] Manual check: <steps>
- [ ] Anything only real hardware, data or a real user can show

## Rollout
How it lands: the project's merge path, a flag if needed, and the way back.

## Open questions for the owner
Numbered, each with lettered options and a ★ recommendation, so the owner
can reply "1: a, 2: b". In a project registered for the owner console, post
each one as a lockable question too (the console-ask skill), so it reaches the
owner's inbox.

---

**Waiting for confirmation to proceed.**
```

When Round 2 fully converged and the plan is small (one phase, one file,
under an hour), compress it. Do not pad.

## Round 3 inside a console round

Follow console-fork section 7, step 3: one to five lockable questions written
from where the panel converged, `forked_from` the fork, `star_by: "panel"`
where the panel agreed and the seat's name where one seat's view carried it,
and an `evidence` row for each claim the owner should be able to check. An
unresolved disagreement becomes a question with each side as an option.
Store the transcript first (`A transcript FORK_ID transcript.md`), then post
the questions, then the one `Result:` reply.

## Rules

- **A status block before each round.** Never run six agents for minutes in
  silence.
- **Parallel, never sequential**, in Rounds 1 and 2.
- **Never skip Round 2.** Without it you have three reports, not one
  decision.
- **Never skip Round 3.** Standalone, the plan is the deliverable; in a
  console round, the questions are.
- **Do not hide disagreement.** A contested point goes in the convergence
  paragraph, or becomes a question, with the resolution picked.
- **Never start implementing during a roar.** Standalone, the plan ends
  waiting for confirmation. In a console round, nothing is written before the
  owner locks.
- **A prompt too vague to act on** (`/overture:roar thoughts?`): ask one
  clarifying question before Round 1 rather than spend six agent runs on it.
- **A seat that fails in Round 2**: continue with the two that answered, say
  so in the convergence paragraph ("architect did not re-weigh"), and mark it
  `✕` in the last status block.

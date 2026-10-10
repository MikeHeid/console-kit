# Overture strategy review (v1.31.0)

This document is a review of Overture 1.31.0 from two groups. Both groups
were simulated: each seat was one review perspective run on the repository.

- **An advisory board** of five seats: product strategy, principal
  engineering, DevOps/SRE, security, and developer-tools market analysis.
- **A psychology team** of four seats: cognitive psychology, organizational
  psychology, human factors/HCI, and behavioral science.

Both groups read the README, [USER-GUIDE.md](USER-GUIDE.md), CHANGELOG
1.23.0–1.31.0, the skill definitions, and the core modules (`anchors.py`,
`fold.py`, `refactor.py`, `triggers.py`, `multiserver.py`, `schema.py`).

Two caveats apply to everything below:

- The progress percentages are judgement calls, not measurements.
- The psychological effects are inferred from the design, not measured on
  users.

The README's [Where it's going](../README.md#where-its-going) section
summarises this document.

---

## Part 0: The thesis

AI agents now write a large share of the code, so the things that run short
are **the owner's attention** and **a reliable record of why the code is the
way it is**. When the diff is cheap, the valuable artifact is the *decision*
that shaped it. Today that decision gets lost in a few places:

- a terminal prompt that is gone when the session ends;
- a `CLAUDE.md` that keeps growing;
- nowhere at all, because the agent guessed.

Overture turns each such decision into a constraint with four properties:

- **typed:** options, a ★ and who recommended it, and the cost of each option;
- **evidenced:** cited `path:lines`;
- **falsifiable:** `valid_if` conditions that can stop holding;
- **owned:** it is folded into the project's own git through a reviewed PR.

Agents must cite it, the owner can audit it, and the code itself checks it.

**How it compounds.**

1. Within a repository, every lock is a constraint that later sessions
   inherit.
2. Across a portfolio of projects, the priority ribbon pools one owner's
   attention.
3. Across the industry, the step is Wave 4: publish the
   question/ruling/`valid_if` format as an open spec, an "ADR for agents",
   that any agent vendor can emit and any console can check.

Standards come from formats, not products. Opening the format gives up the
proprietary schema and keeps what cannot be copied: the engine's judgement
calls and each team's history of rulings.

---

## Part 1: Advisory board

### Seat verdicts

- **Product strategist.** The moat is **Living rulings**: a decision that
  notices when the code it rests on has moved. Release 1.25 named it with the
  Living Rulings badge. Playbooks, the Portfolio and the Shift report are a
  convenience layer that a funded competitor could rebuild in a quarter. The
  data model behind stale rulings is much harder to rebuild.
- **Principal engineer.** The moat is the **anchor and re-anchor engine**
  (`anchors.py`). It has these properties:
  - An `excerpt` anchor holds anywhere in the file and ignores whitespace.
  - When a ruling is locked again, it gets fresh anchors. The server computes
    them; the browser page never does.
  - `plan_reanchor` converts a whole-file hash into an excerpt only when the
    pushed git history proves the cited lines, and only when those lines
    appear exactly once.
  - Owner acts go to an append-only side record (`refactor.py`). The acts are
    withdraw, untrack, proposal, confirm, replaces, scan and advice.

  Together this is a long run of judgement calls written into code.
- **DevOps/SRE.** The moat is the **trust discipline**:
  - The server runs no project code and no git (0.8.10, 0.8.11).
  - `gh` runs only in the steward.
  - Writes are atomic and use `O_NOFOLLOW`.
  - Every push is checked against a closed schema.
  - Every request needs a valid Access JWT, including requests over loopback.

  The same discipline is also a liability. Overture runs only under systemd,
  only on POSIX, and only behind Cloudflare.
- **Security lead.** The moat is an **auditable record with clear
  authorship**:
  - The store is append-only JSONL.
  - Owner-only intents are enforced by the schema. 1.30.1 closed a hole where
    agents could forge them through playbooks.
  - 1.31 added a parser-based sanitizer, a per-request CSP nonce, CSRF
    rotation, author names on backlinks, and a GRILL-ORIGIN provenance badge.

  The weakness is that "owner" is a role, not a person. The record cannot say
  *who* decided.
- **Developer-tools market analyst.** The moat is the **causal thread from
  ruling to PR and issue** ("Shipped:" and "Mentioned in" badges, added in
  1.25, 1.27 and 1.31), together with the fold into the repository's own
  decision log. This makes Overture the system of record for *why*, which no
  other tool in the AI-coding stack holds. That advantage only compounds if a
  team writes into the record, not just one person.

### Ranked moats

1. **Living rulings: anchors plus the stale-handling workflow.** It is hard to
   copy because the value is in the edge cases already handled:
   - normalising whitespace;
   - refusing secrets, symlinks and oversized files;
   - re-anchoring only on git evidence;
   - refusing whole-file hashes where an excerpt fits;
   - owner rulings (CONSOLE-kit/Q30–Q41) on what a stale decision *means*.
2. **The fold, a reviewed gate from console to repository record**
   (`fold.py`):
   - It has two steps: export to committed files, then fold through a PR.
   - It never reads the live store.
   - It is all-or-nothing.
   - A ledger makes it safe to run again.

   It puts the decision log in the customer's git rather than in a vendor
   database. That is the trust argument against SaaS competitors.
3. **The decision-to-outcome data link.** This covers PR and issue backlinks
   and the Shift metrics (median time-to-answer, merged PRs per ruling). Today
   the link is a regex over titles and bodies. Anyone could copy that
   mechanism. Nobody can copy the history of decisions that builds up.

### Role in the toolchain

| Layer | Owns |
|---|---|
| Claude Code | Execution: sessions, skills, hooks |
| GitHub | Change: PRs, issues, review of diffs |
| Cloudflare | Identity and ingress: Access, tunnel |
| **Overture** | **The human rulings agents need, kept honest against the code** |

Overture owns several things that no other layer provides:

- **A typed question protocol.** Each question has options, a ★
  recommendation with who made it, a cost, evidence as `file:lines`, and a
  `valid_if` condition.
- **Lock semantics and staleness.**
- **A single processor per project.** One steward session holds a doorbell
  cursor, so each message is processed exactly once.
- **The fold into the project's own decision log.**

Claude Code's `AskUserQuestion` prompts are lost when the session ends.
Overture's `ask_guard.py` hook sends those questions to the console instead.

### Preconditions for becoming the standard

- **Agents must ask good questions.** Today this depends on skill discipline
  (`console-ask`, `deliberate`, `drill`).
- **Anchors must survive normal refactors.** Text that moves should still
  hold, and text that is edited should go stale.
- **Clearing a stale ruling must be cheap.** Today it takes about 1–2 clicks
  (reanchor, Confirm, Withdraw or Keep).
- **The project needs a decision log for the adapter to write into.**

### Risks and counterarguments

1. **Platform risk.** Claude Code or GitHub could ship a native feature for
   agent questions.
2. **Single operator.** Locks carry no identity: `by: "owner"`
   (`schema.py`), and the server strips the Access email header. There are no
   reviewer roles.
3. **Setup friction.** Onboarding needs a Cloudflare account, an Access
   application, a tunnel, systemd and five onboarding values.
4. **Anchors are lexical, not semantic.** A behaviour change that keeps the
   cited text intact never goes stale.
5. **Backlinks depend on someone typing the qid literally.** They can also be
   spammed. 1.31 shows the author but does not filter by trust.
6. **Question fatigue leads to rubber-stamping.**
7. **Feature breadth dilutes the moat story.** Launch Idea, Branch Out,
   tickets and visuals all compete for attention.

### Findings the board recorded in passing

- **Release signing.** The README used to claim "signed releases".
  `build_zip.py` builds a reproducible zip and prints its sha256, but there is
  no signature and no attestation. The claim has been removed from the README,
  and real signing is a Wave 2 item.
- **Out-of-date user guide.** `docs/USER-GUIDE.md` still describes one
  console server per project as the current state, and calls the shared
  server "spec'd, not built". The shared server now ships as `server.py --all`
  with per-project tokens.
- **Locks are single-operator in the data model, not only in the UI.** See
  risk 2 above.

---

## Part 2: Psychology team

### (a) Cognitive load and interruption cost

- **Interruptions become a batch the owner pulls.** `ask_guard.py` blocks
  `AskUserQuestion` in every session except the steward's and sends those
  questions to the inbox. Gloria Mark's research shows that recovering from an
  interruption is costly, and that people work faster but under more stress
  when interrupted. With the inbox, the owner decides on their own schedule
  instead of N agents' schedules. The guard fails open, so it never stops a
  session.
- **Grouping and round forms keep context switches down.**
  - "Answer these together" groups questions that one named session asked
    within five minutes.
  - A round is one keyboard form: `1`–`9` to pick, `←`/`→` to move, and
    single-choice questions advance by themselves.
  - Options, costs and evidence arrive already assembled, so the owner holds
    less in working memory.
- **The ★ is a default, with both effects that defaults have.**
  - *Benefit:* defaults steer choices (Thaler & Sunstein; Johnson &
    Goldstein). On low-stakes calls the owner can accept the ★ quickly, in
    System 1 mode, and save System 2 attention for the questions that need
    it.
  - *Risk:* anchoring (Tversky & Kahneman) and automation misuse (Parasuraman
    & Riley). Auto-advance plus `1`–`9` makes accepting the ★ the easiest
    path.
  - *Partial mitigation:* `star_by` shows whose recommendation it is, which
    invites the owner to weigh the source.

### (b) Decision quality and accountability

- **Locking is a commitment.** Locked answers become rulings, and the steward
  folds them into the project's record through a reviewable PR. Research on
  public commitment (Cialdini) and on accountability (Lerner & Tetlock) shows
  that people think harder when they expect to justify a decision. This holds
  when they don't know in advance which answer the audience prefers. The
  "your own words" box records the owner's reasoning, not just the button
  pressed.
- **Provenance is visible at several points:**
  - `star_by` names who recommended an option;
  - backlink chips show the author, e.g. `#142 by @alice` (1.31);
  - the ⚡ GRILL-ORIGIN badge (1.31) marks questions an agent wrote because the
    owner asked to be grilled;
  - deliberation seats give a ★ but never answer or lock for the owner.
- **Staleness gives calibrated trust instead of blind trust.** Lee & See argue
  that trust in automation should match how reliable it actually is. The
  `valid_if` anchors, the Living rulings badge (1.25) and **Why stale?** make
  each ruling state the premise it depends on, and tell the owner when that
  premise changes. The stale-handling acts make the owner decide again on
  purpose: re-lock, Withdraw, Keep (stop checking) or Replace.

### (c) Situation awareness across many agents and projects

Endsley's model has three levels of situation awareness. Overture has
features for each:

| Endsley level | What it means here | Overture features |
|---|---|---|
| Perception | What is happening | Status bar, status chip (1.23), "Agents at work N/64" (1.28), active-agents fold, Portfolio tallies |
| Comprehension | What it means | Per-item Status flowchart, Project map coloured open > stale > unlocked > locked, "What's new since last look" (1.16) |
| Projection | What comes next | Priority ribbon with traffic-light ages (0.24), stale count as debt that is building up |

The 1.23 "Visible truth" release added toasts and a "Draft not saved" notice.
Before it, the owner could believe a save had worked when it had not. In
Norman's terms, this closes the gulf of evaluation.

### (d) Team dynamics

- **A shared memory of why.** Each ruling carries the question, options,
  costs, evidence, the owner's own words, and forward links to PRs and issues.
  Together they form a transactive memory system (Wegner). A new teammate can
  learn why the code is the way it is from Export, the Shift report and the
  folded log, without finding the right person to ask.
- **Built-in dissent.** The adversarial, security and Roar seats make
  challenge part of the process rather than a personal risk. This follows
  Edmondson's work on psychological safety. Launch Idea's five grill questions
  run a pre-mortem (Klein) before work starts.
- **Less bikeshedding.** Parkinson's law of triviality predicts long debates
  over cheap decisions. Bounded options with a stated cost limit how wide a
  debate can get, and the cost of deliberation is shown up front.
- **Caveat: the design centres on one owner.** Today a team benefits mostly
  through artifacts (PRs, exports) rather than by deciding together.

### (e) Habit loop and adoption

The Fogg behavior model says a behavior happens when motivation, ability and a
prompt meet (B = MAP). Overture covers all three:

- **Prompt:** desktop notifications for questions waiting on the owner
  (`?you`) and stale rulings (`!stale`), the doorbell, and the badge count.
- **Ability:** one-tap lock, the `1`–`9` keys, Ctrl+K, the first-run hero with
  a one-line copy command (1.24), and `onboard.py verify`.
- **Motivation:** unblocking agents, plus visible progress. The Shift report's
  "N rulings locked" and the **✓ Caught up** state draw on Amabile's progress
  principle.

### (f) Risks and recommended mitigations

| Risk | Source | Recommendation |
|---|---|---|
| Automation bias on the ★ | The default, auto-advance, and seats that also give a ★ | Show the ★-acceptance rate per `star_by` in the Shift report. Add a blind-pick mode for high-cost questions, where the ★ appears only after a first pick. Show where seats disagree. |
| Rubber-stamping | "Lock all", one-tap lock, and the Shift report's median time-to-answer (Goodhart's law) | Require a one-line rationale on fast locks of high-cost or irreversible questions. Report withdrawn and replaced rulings next to the speed metrics. Leave questions whose evidence changed out of "Lock all". |
| Alert fatigue | Notifications, chimes, badges, the ribbon, and noise from whole-file hashes | Add quiet-hour schedules, beyond today's one-hour snooze. Notify only above a priority threshold. Send a daily digest. Group stale rulings by cause, e.g. "12 stale from one refactor". Keep preferring excerpt anchors. |
| Single-operator bottleneck | One owner and one steward | Add delegated owners per item or category, with the delegate's identity recorded. Add "standing rulings": policies that agents apply without asking, still audited through `valid_if`. Escalate when the ribbon goes red. |
| Vigilance decrement during stale review | A large number of Living rulings | Order stale rulings by downstream impact. Re-confirm a random sample each week instead of all of them. |
| Framing manipulation by agent-written questions | How the options are worded | Building on GRILL-ORIGIN, flag a ★ option whose description is much longer or more favourable than the others. |

### Overall

Overture works as a cognitive prosthesis for a person supervising many agents:

- it turns interruptions into a batch the owner pulls;
- it makes commitments explicit and auditable;
- it ties trust in each ruling to whether that ruling's premise still holds.

Its main psychological risk comes from what makes it fast: the ★ default
combined with very quick locking. Reducing that risk calls for metrics and
friction aimed at decision quality rather than speed.

---

## Part 3: Lanes and waves

| Lane | Progress | Next items |
|---|---|---|
| Core decision engine | 70% | Symbol/AST anchors; "rulings touching this path" lookup before an agent edits; rulings as a CI check; an open spec for the schema; ruling search and dependencies |
| Trust & security | 65% | sigstore/minisign signing; operator identity on locks; same-UID isolation recipe; built-in webhook HMAC; collaborator-only backlinks |
| Ecosystem & integrations | 40% | MCP server for non-Claude agents; Linear/Jira adapters; Slack/email doorbell; ruling-aware PR comments; MADR/ADR templates |
| Onboarding & distribution | 35% | Identity backend other than Cloudflare (Tailscale/OIDC/solo loopback); launchd and Docker; hosted demo; 5-minute path with no adapter; fix the USER-GUIDE drift |
| Team & multi-operator | 15% | Named operators; roles; two-key locks; routing; per-operator handoff |
| **Overall**, weighted toward the category 1.0 | **≈45%** | |

Each wave below builds on the one before it.

- **Wave 1, The solo cockpit (shipped).** Releases 0.7 through 1.31.
- **Wave 2, Rulings that bite.**
  - *Goal:* install in under 10 minutes, and a broken ruling blocks a bad
    merge.
  - *Scope:* symbol anchors, a CI Action, ruling lookup before an agent acts,
    real signing, Docker/launchd, and a solo mode without Cloudflare.
- **Wave 3, Teams.**
  - *Goal:* an audit trail that holds up in a team review or a compliance
    setting.
  - *Scope:* operator identity on every record, roles, routing, two-key locks,
    and Slack/Linear.
- **Wave 4, Protocol.**
  - *Goal:* Overture becomes the reference implementation of a format that
    other tools emit.
  - *Scope:* publish the question/ruling/`valid_if` format as an open spec,
    plus an MCP reference server and adapters for other agent vendors.

### Board bottom line

The engine (Living rulings plus the fold) is real, well tested and unusually
careful. What limits the product is not missing features. The ceiling is set
by three things:

- one operator per console;
- Cloudflare and systemd only;
- no published protocol.

Releases 1.26–1.30 mostly added features. The next releases should go to
**identity, enforcement and distribution**.

# console-kit user guide

A practical walk-through for someone new to console-kit: what it is, how to
install and run it, and what a normal day looks like on each side, the owner's
and the agents'. It links to the reference docs instead of repeating them:

- [INSTALL.md](../INSTALL.md): installing the plugin, onboarding, release-by-release upgrade notes;
- [README.md](../README.md): every feature, by release;
- [kit/docs/CLOUDFLARE.md](../plugin/kit/docs/CLOUDFLARE.md): the Access application, tunnel and DNS;
- [kit/docs/ADAPTER.md](../plugin/kit/docs/ADAPTER.md): connecting the console to your project's work items and record;
- [kit/docs/DEPLOY.md](../plugin/kit/docs/DEPLOY.md) and [kit/docs/RUNBOOK.md](../plugin/kit/docs/RUNBOOK.md): running it, and fixing it;
- [MIGRATION.md](MIGRATION.md): moving a project that vendors the kit onto the pinned install.

## What it is

Your agents (Claude Code sessions working in a project) keep hitting decisions
only you can make. console-kit turns each one into a **structured question**:
the options, a recommended pick (★), and what each costs. Those questions land
in an **inbox on a web page** that you open from any browser, behind Cloudflare
Access. You answer, in your own words if you like, and **lock** the answer.
The next agent session in that project is told what arrived. It processes your
answers, and **folds** the locked ones into the project's own record through a
pull request.

It has three parts:

| Part | Where it runs | What it does |
|---|---|---|
| **server** (`server.py`) | a `systemd --user` service on loopback, one per project today | serves the page, keeps the store and the doorbell in a state directory, checks every request's Access token |
| **agent side** (`agent.py`, `fold.py`) | in the agent's shell | asks, replies, reads the inbox, waits on the doorbell, folds |
| **Claude Code plugin** | in every Claude session you open | a SessionStart note of what you sent, the skills (`console-process`, `console-fork`, `console-fold`, `console-ask`, `console-visual`, `as`, `console-onboard`), the committee seat agents |

The plugin acts **only in projects you registered** in your own registry
(`~/.config/console-kit/projects.json`). A repository can never register
itself.

## Install: one pinned copy per machine

Use **one user-level install of a pinned release** for every project on the
machine. Do not keep a copy inside each repository. (This is the owner's
ruling, CONSOLE-kit/Q13.)

- **From the release zip or GitHub:** follow [INSTALL.md](../INSTALL.md) §1. The plugin
  carries the kit, and `deploy/install.sh` copies it to
  `~/.local/share/console-kit/kit`, with its venv beside it. Every project you
  install from that plugin runs that one copy, at the plugin's version.
- **From a git clone, pinned to a tag:** follow [MIGRATION.md](MIGRATION.md) §1. It
  makes a detached worktree at the tag under `~/.local/share/console-kit/releases/`
  and a `current` symlink to it. Use this if you develop the kit, or if a
  project used to vendor it.

Either way, an upgrade is something you do on purpose. Moving a project's
branch never changes the kit.

**Still to come (spec'd, not built):** one console **server** for all your
projects. Each project's install will link into it through an API with a
per-project token, and keep its own store. The design is in
`console-kit-multiproject.md`. Until it ships, each project runs its own
server process from the shared install.

## Register a project

You run this, never an agent:

    python3 <kit>/agent.py --state <state dir> register --project <project root>

`<kit>` is the directory holding the `agent.py` you run (the pinned install's
`plugin/kit`, or `~/.local/share/console-kit/kit`). `register` records that
directory, so run the copy you want sessions to use. Add
`--steward agent-5` to name the one session allowed to watch, sync and fold
(see "Stewards", below).

## Onboard a project

In a Claude session in the project:

    /console-kit:console-onboard

It asks for five values (name, team domain, AUD tag, hostname, port). It writes
the project's non-secret settings and a starter page and adapter, then
**prints the commands you run yourself**: install the services, register, set
up Access, create the tunnel. The skill never runs them. The table and the
manual equivalent are in [INSTALL.md](../INSTALL.md) §2–3.

Then connect the console to your project's work items, so questions hang off
real items and folds land in your decision record:
[ADAPTER.md](../plugin/kit/docs/ADAPTER.md).

## Run it: server, tunnel, Access

- The server binds `127.0.0.1:<port>` only and refuses **every** request
  without a valid Cloudflare Access token, loopback included. `curl` on
  loopback answering **403** is the healthy answer.
- Cloudflare Tunnel carries the hostname to that port, and Access puts a login
  in front of it: [CLOUDFLARE.md](../plugin/kit/docs/CLOUDFLARE.md).
- Agents never go through Access. They talk to `<state>/agent.sock`, a socket
  only your user can open.
- Health, from any shell: `python3 <kit>/agent.py --state <state> health`
  exits 0 and prints the version. When something is wrong, start at
  [RUNBOOK.md](../plugin/kit/docs/RUNBOOK.md) "First: health".
- Upgrades and rollbacks: [DEPLOY.md](../plugin/kit/docs/DEPLOY.md), and for a pinned
  install [MIGRATION.md](MIGRATION.md) "Upgrading the pinned install".

## The owner's day

1. **Open the inbox.** Everything waiting on you, by item. The status bar says
   whether an agent session is **listening** right now. The **Feed** tab is
   the whole history, newest first.
2. **Answer.** Pick an option, or write **your own words** in the comment box.
   Your words become part of the answer the agent reads and folds, so say what
   you mean there, not only which button you pressed. A round of questions
   opens as a form: ←/→ to move, 1–9 to pick.
3. **Lock.** A locked answer is a decision; an unlocked one is a draft. Use
   **Lock all N answers** on an item, or **Lock all & process** at the end of a
   round. You can change a locked answer later and lock it again. Each answer
   records what must stay true for it to stand. When that stops holding, the
   answer shows **stale**, and **Why stale?** says what changed.
4. **"Process".** Press **Answers are in: process them** on an item. That rings
   the doorbell, and the waiting session (or the next one to start) folds what
   you locked and answers your messages.
5. **Deliberate** when you want reviewers before deciding, or a second look
   after deciding:
   - **⑂ Deliberate (full round)** on an item sends it to the committee
     (architect, UX, security and the project's own audit by default). Its
     findings come back as new lockable questions on the same item.
   - **Next step ▾** beside a locked answer offers **Follow up** (one to three
     seats you pick look at that one answer), **Refine** or **Drill** (the
     project's own skills), and **Roar** (a three-round panel, once per
     lock). The locked answer stands unless you supersede it.
   - **⑂ Deliberate before answering** under a question that is still
     **open** (unanswered, or answered and not locked): pick one to three
     seats, a mode (explore by default) and an optional note. The form shows
     the cost before you send, about 100k tokens per seat. The seats reply on
     that question with a ★ recommendation, its reasons and what they found;
     only if they show the options themselves are wrong do they ask a
     replacement question, and the reply names it. Answering and locking stay
     yours (CONSOLE-kit/Q18).
6. **Chat** for anything that is not a question. It wakes the listening
   session, which answers in the same thread. **Request a visual** on an item
   to get a diagram or a static mock back.

## The agent side

Most of this is driven by the plugin's skills. Knowing the shape helps you
read what your sessions do.

- **Sessions name themselves.** Type `/console-kit:as agent-5` in a session,
  or start it as `CONSOLE_KIT_AGENT=agent-5 claude`. The console then shows
  who asked, replied or drew what.
- **Stewards.** The doorbell has one cursor, so with several sessions on one
  console, name one **steward** in your registry
  (`agent.py --state <state> steward agent-5`). Only the steward may `watch`,
  `synced` and fold. Every session may still `ask`, `reply` and `working`, and
  the plugin redirects other sessions' live `AskUserQuestion` to
  `agent.py ask`, so their questions reach your inbox. It keeps cooperating
  sessions from racing; it is not a security boundary.
- **ask / reply / working.** `console-ask` posts the decisions a session finds
  as lockable questions, each citing evidence in the repository (`file:lines`).
  `reply` answers a thread. `working ITEM…` shows you "agent active" on the
  items a session has picked up.
- **watch / synced.** The steward runs `agent.py watch` in the background and
  wakes when you press "process", send a chat, ask for a deliberation or
  request a visual. `console-process` handles what is waiting, then records
  `synced --through SEQ`, so nothing is handled twice and nothing is skipped.
- **Fold through a PR.** `console-fold` exports the locked answers, previews
  the fold, writes them into the project's record through the adapter, and
  lands that in a pull request. The console's store is never the project's
  record; the merged PR is.

## Spending fewer tokens

Console reads can be large. Measured on a live console with 461 store records
(spec `console-kit-multiproject.md`, §8.1 and Appendix A):

- `agent.py view` printed **644,064 bytes** (≈161,000 tokens). Re-serialised
  compact JSON is still **459,082 bytes**. The fields `console-process`
  actually uses come to about **6 KB** (6,023 bytes).
- `inbox` after the cursor is often **0 bytes**, and `health` 130.
- `answers --item ITEM` is about 7 KB, against about 76 KB for all answers.
- A deliberation seat's `fork-context` bundle is capped at 64 KiB (about
  16–26 KB in practice).

So:

- **Prefer the scoped reads.** Use `todo` (what is waiting: forks not done,
  threads and the chat awaiting the agent, visual requests, inbox qids),
  `view --item ITEM` (one item and everything under it), `inbox` (since the
  cursor), `answers --item ITEM`, `check`, `health` and `fork-context FORK`.
  `view --since SEQ` and `answers --since SEQ` print only what changed after a
  store seq: a question when anything recorded on it (asked, answered, locked,
  re-anchored) is newer, plus every stale question, because a file changing
  has no seq. A question that turned valid again with no new record is not
  shown; `check` and the full read still are.
- **If a read exits 4**, run the narrower command it names on stderr. Do not
  retry the same command, and do not add `--full`.
- **Reads are capped.** `todo`, `view` and `answers` refuse to print more
  than 64 KiB without `--full`: they print nothing, name the narrower command
  and exit 4. JSON is compact when the output is not a terminal.
- **Give each project its own console and state.** A store that only grows
  makes every `view` grow with it.
- **Keep deliberations small.** Pick one to three follow-up seats instead of a
  full round when one answer is in doubt. Each seat reads its own bundle.
- **Cite excerpts, not whole files.** A question anchored on an excerpt stays
  valid through unrelated edits. A whole-file hash goes stale, and every stale
  answer is another round trip.

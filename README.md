# console-kit

**Answer your agents' questions from a web page.** An agent working in a
project asks you structured questions: options, a recommended pick (★), what
each costs, and the evidence behind it. You answer from any browser, behind
Cloudflare Access. The next Claude session in that project is told what
arrived, acts on it, and folds your locked answers into the project's own
record.

This README describes the kit as it is today. [CHANGELOG.md](CHANGELOG.md)
says what each release added.

## Download

Get **`console-kit-<version>.zip`** from
[Releases](https://github.com/MikeHeid/console-kit/releases). Unzip it
somewhere it can stay (it makes a `console-kit/` folder). Then, in Claude
Desktop's **Code** tab or in `claude`:

    /plugin marketplace add ~/console-kit
    /plugin install console-kit@console-kit-local

Then, in your project:

    /console-kit:console-onboard

It asks for the project name and your Cloudflare values, and tells you what
to run next. `INSTALL.md` (also in the zip) has the whole guide.

| Guide | For |
|---|---|
| [docs/USER-GUIDE.md](docs/USER-GUIDE.md) | Start here: what the console is, the pinned install, your day and the agents' day, and tips for spending fewer tokens. |
| [INSTALL.md](INSTALL.md) | Installing the plugin and onboarding a project. |
| [docs/MIGRATION.md](docs/MIGRATION.md) | Upgrading. Covers moving a vendored kit onto one pinned install, and the one-off steps each release needs. |
| [docs/CLOUDFLARE.md](plugin/kit/docs/CLOUDFLARE.md) | The Access application, the tunnel and DNS. |
| [docs/ADAPTER.md](plugin/kit/docs/ADAPTER.md) | Connecting the console to your project's work items and decision log. |
| [docs/DEPLOY.md](plugin/kit/docs/DEPLOY.md) | Installing, upgrading and rolling back the systemd services. |
| [docs/RUNBOOK.md](plugin/kit/docs/RUNBOOK.md) | Symptoms, checks and fixes, starting with the loopback `/health` check. |

## What you can do

### Answer, lock, and ask for a round

- **Answer and lock.** Each question shows its options, the ★ and whose
  recommendation it is, and evidence rows. The evidence shows the cited lines
  as they are now, and says whether they changed since the question was asked.
  Your own words on an answer travel with it. Locking turns an answer into a
  ruling. A round's questions open as one form: ←/→ to move, 1–9 to pick, then
  **Lock all & process**.
- **Lock with one tap, undo for five seconds.** **Lock this answer** starts a
  countdown with **Undo** focused. Nothing is sent until it ends. Undo, opening
  another item, closing the panel or leaving the page cancels it, and nothing
  is written.
- **A round moves on by itself.** Pick a single-choice option and, after a
  moment, the form moves to the next question still without a pick, and says
  so. It stays put for multiple choice, for a question you are writing words
  on, and for ↑/↓ through the options. It never moves onto the Lock page.
- **Answer these together.** Loose questions on one item, asked by one named
  agent session within five minutes of each other, are grouped in the inbox
  and open as one form. Unnamed sessions are never grouped, because nothing
  shows they are one agent's.
- **Send to agent.** Once you have answered something, a bar under the panel
  header shows "N answered · M left" and sends the agent the same "process
  them" signal as before. It goes once sent and comes back with the next
  answer.
- **Locked questions roll up** to one line (question, pick, state). Click or
  press Enter to open one. Open and stale questions stay whole. The slides and
  fades run only when your system does not ask for reduced motion.
- **Deliberate before answering.** On an open question, pick one to three
  seats (DevOps, UX, adversarial, security, architect, analyst, or one you
  name). They reply with a ★ and their reasons, and never answer or lock for
  you. The cost is shown first, at about 100k tokens a seat.
- **Next step ▾** beside every locked answer:
  - **Follow up**: chosen seats look at that answer again.
  - **Roar**: a three-round panel, at most once per lock.
  - **Refine** or **Drill**: runs the skill your project names, by default
    the plugin's own `console-kit:refine` and `console-kit:drill`.

  Each step comes back as questions for you to lock. Nothing is written into
  the project before you lock.
- **Status flowchart on every item.** Each item view carries a collapsible
  **Status flowchart** that the server builds from your questions and rounds:
  the item at the left, open/stale/locked questions coloured, rounds as their
  own shape, dotted edges where a newer question supersedes an older one or
  a round follows up on an earlier one. It is lazy (nothing loads until you
  expand it) and renders inside the same sandboxed Mermaid frame as a
  requested visual. **Click a node** to jump straight to that question's card
  (or open an item if it is an item node).
- **Project map** at the top of the Inbox: a tree of every item, parent to
  child, each node coloured by its own questions' roll-up (open > stale >
  answered-but-unlocked > locked) with a short tally. Click an item to open
  it. A chip row narrows the map to one state; ancestors stay drawn muted so
  the tree is still a tree. Lazy, like the per-item chart.
- **Save SVG** on every chart and Mermaid visual. The rendered SVG is sent
  back to the parent and downloaded as a plain file; nothing goes back to
  the server.
- **Request a visual** on an item. The agent answers with a Mermaid diagram
  (rendered right there inside a sandboxed frame — the vendored lib runs in an
  opaque origin and cannot touch the console's cookies, storage or network;
  **View source** still shows the raw `.mmd`), or with an HTML mock shown the
  same way, scripts off entirely.
- **A short chime when a visual arrives**, the drawn item surfaces at the top
  of the Inbox under **New visuals**, and the Feed row says "Visual drawn".
  The badge on the inbox button and the Feed tab counts it too, until you look.
- **Star what matters.** Tap ☆ on a visual or an item (the item view's title
  bar, the Inbox row, the New visuals row). The **Favorite** tab lists them,
  grouped by item, newest first. Only the owner can star.
- **Chat**, for a message not tied to a question. It wakes the watching
  session.

### Keep rulings honest as the code moves

A question says what must stay true for its answer to stand (`valid_if`):
cited text (an `excerpt`), a whole file (`file_sha256`), or an item's status.
When that stops holding, the answer reads **stale** and **Why stale?** says
which check failed.

- **Still holds: re-lock…** keeps your answer and re-checks it against the
  files as they are now.
- **`agent.py reanchor`** (the steward runs it, `--dry-run` first) turns a
  stale whole-file hash into an excerpt only with evidence: the question names
  a line range in that file, the steward's pushed history holds the version
  it was locked against, and those lines are still in the file exactly once.
  Anything else stays stale, with the reason listed.
- **Withdraw…**: the ruling no longer applies, and you give a reason.
- **Keep, stop checking…**: the ruling stands, but is no longer checked
  against the files.
- **Confirm a proposed anchor.** The steward proposes new lines for the
  ruling to rest on, the server reads them itself, and nothing changes until
  you press Confirm.
- **Replace.** A new question is linked to the stale one. The old ruling
  stands until you lock the new answer.

Only the owner can do any of these, and only on a stale answer. Nothing is
deleted: each act is recorded in a file beside the store, and the fold records the
outcome in your project. New questions are refused a whole-file hash where an
excerpt fits, which is what made most rulings go stale.

### Your dashboard, published by you

The console can sit inside your project's own dashboard page. An agent
stages a page from a merged commit (`agent.py page-snapshot`). The console
then shows it as a proposal, with its ref, commit, size and a sandboxed
preview. It is served only after you press **Use this page**. No agent can
publish, and the "reviewed" label is shown as the agent's claim, because the
server cannot check it.

### See what's happening

- The page updates itself, with no reload. A box you're typing in is never
  redrawn under you.
- The **Inbox** shows what waits for you, with an unread count. The **Feed**
  shows every question, answer, lock and reply, newest first.
- The status bar says whether an agent session is listening right now.
  Optionally, a footer shows your Claude usage for the 5-hour and 7-day
  windows.
- **Several agents, one console.** Sessions can be named (`/console-kit:as
  agent-6`). One of them, the **steward**, named in your own registry, is the
  only one that processes your requests and folds your answers. The others
  post their questions to your inbox instead of asking you directly.
- The **PRs** tab lists the project's open pull requests, then those
  merged or closed in the last 30 days (`--days` changes it), with checks, draft and merged
  badges, a link to each on GitHub, and an **Open** button for any item or
  question a title or branch names. The steward pushes the list
  (`agent.py prs-push`), and the tab says when it last did.
- The **Favorite** tab lists everything you starred, grouped by item.
- A **Back** control sits at the top of every item view, so **discuss** from
  the dashboard always has a one-tap way home.
- **Direction stays visible.** Each item in the Inbox carries a collapsible
  "N answered on this item" under its open questions, showing the last few
  locked rulings on it. Items whose questions are all locked within the last
  day stay on the list under **Recently answered**, so you can see what was
  asked and what you answered without leaving the Inbox.

## How it fits together

```
 browser ──Access login──▶ Cloudflare ──tunnel──▶ 127.0.0.1:PORT  server.py
                                                     │ checks the Access JWT (team keys + AUD)
                                                     ▼
                                              state dir: store + doorbell
                                                     ▲
 Claude session ── SessionStart hook reads the doorbell (registered projects only)
                └─ skills: console-process / console-fork / console-fold / console-ask / console-visual ── agent.py
```

- **`server.py`** serves the console, inside your published dashboard page if
  you have one. Every request without a valid Access token is refused, loopback
  included. **It runs no project code and no git.** Your work items arrive by
  `agent.py items-push`, git history by `agent.py history-push`, pull
  requests by `agent.py prs-push` (`gh` runs in the steward, never in the
  server), and the page by `agent.py page-snapshot`. The steward runs all
  four in its own process,
  and each sends data the server checks against a closed schema.
- **`agent.py`** is the agent's side. Its commands:

  | Purpose | Commands |
  |---|---|
  | Reading | `inbox`, `todo`, `view`, `answers` (capped at 64 KiB, with a narrower command named when a read is too big), `health` |
  | Asking and answering | `ask`, `reply`, `working`, `synced`, `watch` |
  | Deliberation | `fork-context`, `transcript`, `visual`, `visual-export`, `next-step`, `costs` |
  | Stale answers | `check`, `reanchor`, `propose-anchor` |
  | Pushing data | `items-push`, `history-push`, `prs-push`, `page-snapshot` |
  | Owner only | `register`, `steward`, `server add`, `server token rotate` |

  `--as NAME` names the session.
- **`fold.py`** writes locked answers, and what became of them, into your
  project through its adapter, in the steward's process.
- **`plugin/`** holds the Claude Code plugin: the hook, the skills, and the
  committee agents. Its skills include `roar`, `refine`, `drill` and
  `deliberate`, which a console round uses and which also run on their own
  (`/console-kit:roar` and so on).
- **`onboard.py`** and **`deploy/`** onboard a project and install the
  services.

### What a project token protects

One console server can host several projects. Each project's agents reach it
with that project's own token. The token is kept in
`~/.config/console-kit/tokens/NAME` (mode 0600, outside every repository);
`server.json` holds only its hash. `agent.py` picks the project from the
directory it runs in. `agent.py server token rotate NAME` (which you run)
refuses the old token from the next request.

- **What it stops:** cross-talk and mistakes. A wrong `--state`, a skill bug,
  or an agent carrying another project's habits cannot read or write another
  project's console through the kit.
- **What it does not stop:** an agent running as the same OS user that sets
  out to cross over. It can read `tokens/` and the state directories directly.
  Real isolation needs a sandbox or a separate user.
- **An old per-project server is a way around it.** While one still listens on
  `STATE/agent.sock`, that socket answers without a token. Stop it before
  relying on the token.

## Windows + WSL2 (optional)

The server, the tunnel and the systemd units are POSIX; a convenient layout
on a Windows host is to run them in WSL2 while a Claude Code session lives on
either side. A few traps worth knowing:

- **CRLF.** `git config core.autocrlf=true` on Windows gives every file in
  the plugin cache CRLF endings. `install.sh` then fails at once
  (`set: pipefail: invalid option name`), and systemd unit files with a `\r`
  silently misbehave. Install the kit from a copy stripped of CR (`sed -i
  's/\r$//'` on each `*.sh`, `*.in`, `*.py`, `*.js`, `*.css`, `*.html`) or
  clone with `core.autocrlf=false`.
- **Default branch.** `page-snapshot --from-ref origin/main` is the default
  and fails on a repository whose default is anything else. Pass
  `--from-ref HEAD --unreviewed` while bootstrapping, or point `--from-ref`
  at your actual branch.
- **Page path.** `onboard.py` defaults the page to `.console-kit/page.html`,
  which `page-snapshot` refuses (leading dot). Move the page under
  `docs/console/page.html` or similar.
- **Items.** The server never runs your adapter. After onboarding, run
  `agent.py --state STATE items-push --adapter PATH` once (and whenever the
  item list changes); until then `ask` on any item but `PROJECT` is refused.
- **Reaching the server from Windows.** The agent socket lives in WSL and
  Windows cannot open a WSL2 Unix socket. Options: run the agent-side session
  inside WSL (`wsl -e bash` wrapper), or use the one-server HTTP door with a
  project token on loopback.
- **Linger.** Systemd user units stop when WSL shuts down. Run
  `loginctl enable-linger $USER` in WSL; a Windows process that keeps WSL
  alive still helps when nothing else holds it.

## Trust

- The plugin's hook runs in every project you open, but acts only in projects
  listed in `~/.config/console-kit/projects.json`, which only `agent.py
  register` and `agent.py steward` write. A cloned repository's
  `.console-kit.json` is never trusted by itself, and cannot name a steward.
- Anything an agent can write — the adapter, `.git/config`, the dashboard file
  in the working tree — is never run or read by the server.
- The kit never opens `~/.cloudflared/cert.pem` or a tunnel credentials file.
  It checks that the file exists, and nothing more.
- Nothing onboarding writes is a secret. The AUD tag and team domain are what
  tokens are *checked against*; no token can be made from them.

## Development

    python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
    .venv/bin/python test_kit.py && .venv/bin/python test_server.py && .venv/bin/python test_onboard.py
    .venv/bin/python test_build.py
    CONSOLE_KIT_BROWSER=1 .venv/bin/python test_browser.py   # needs playwright + browsers

`test_server.py`'s syscall tests need `strace`, and FAIL when it is missing,
so they cannot quietly not run. On a machine without it, set
`CONSOLE_KIT_NO_STRACE=1` to skip them, and each is then reported, by name, as
not run.

To build the release zip:

    python3 build_zip.py --deny-file ~/my-deployment-values.txt

It runs `claude plugin validate --strict` on the result. The deny file lists
strings from your own deployment (hostnames, AUD tags, team domain, home
path), and the build refuses if any of them appears in the zip.

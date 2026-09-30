# console-kit

**Answer your agents' questions from a web page.** An agent working in a
project asks the owner structured questions: options, a recommended pick, and
what each costs. The owner answers from any browser, behind Cloudflare Access,
and can:

- lock an answer;
- change it;
- ask for a **deliberation round** (a committee of reviewer agents looks at
  one item and comes back with decisions to lock).

The next Claude session in that project is told what arrived, processes it,
and folds locked answers into the project's own record.

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

- **[INSTALL.md](INSTALL.md)**: install the plugin in Claude Desktop or the
  CLI, and onboard a project.
- **[docs/CLOUDFLARE.md](plugin/kit/docs/CLOUDFLARE.md)**: the Access application, the
  tunnel, DNS.
- **[docs/ADAPTER.md](plugin/kit/docs/ADAPTER.md)**: connect the console to your
  project's work items and decision log.
- **[docs/DEPLOY.md](plugin/kit/docs/DEPLOY.md)**: install, upgrade and roll back a
  kit vendored into a project and served by a systemd user unit, and check a
  vendored copy against a release (`kit/tools/verify_vendor.py`).
- **[docs/RUNBOOK.md](plugin/kit/docs/RUNBOOK.md)**: symptoms, checks and fixes,
  starting with the loopback `/health` check.

The console's status bar says whether an agent session is **listening** on the
doorbell right now, idle since a time, or has never listened, from the
heartbeat `agent.py watch` writes while it waits (0.6.0).

### A live console (0.7.0)

- **It updates itself.** An open page long-polls the server (`/api/wait`, at
  most 25 s a request, behind the same Access gate), so a new question, reply
  or lock appears without a reload. The poll pauses while the tab is hidden and
  backs off (2 s up to 60 s) when the server does not answer. A box you are
  typing in is never redrawn under you: a note offers "Show" instead.
- **An unread chip** on the Inbox button counts what an agent wrote since you
  last had the inbox open. "Last looked" is kept in this browser's
  localStorage, so each browser keeps its own; a new browser starts at zero.
- **Feed tab**: every question, answer, lock, re-anchor, deliberation request,
  process request, reply and chat message, newest first, filterable by kind and
  by item. Folds and PR merges are not in the console's store, so they are not
  in the Feed.
- **A round is one form.** A deliberation round's questions open as a form, one
  question per step: ←/→ move, 1–9 pick, a comment per question becomes your
  answer's own words. Picks are drafts (kept in this browser) until the review
  page's **Lock all & process**, which locks each and sends one process
  request. If the server would refuse any of them it locks none and says which;
  if a write fails part way, pressing again finishes the rest.
- **Evidence on a question**: rows citing `path:start-end`, with the command
  run and what it printed. The form shows the cited lines as they are now and
  says whether they changed since the question was asked.
- **Chat tab**: a message not tied to a question. It wakes whichever session
  is watching, which answers in the same thread. Six a minute, sixty an hour,
  4000 characters each.
- Progress rings on items and rounds, and gentle transitions; all motion is off
  under reduced motion.

### Next steps, roar and visuals (0.8.0)

- **"Next step ▾"** beside every locked answer and every round's answers
  offers three kinds of fork: **Follow up** (seats you pick, as before),
  **Refine** and **Drill**. Refine and drill run the project's own skill,
  named in `.console-kit.json`'s `next_step`, on that answer or round. Neither
  writes into the project before you lock: what they find comes back as
  questions, specs land by pull request, and log entries at merge time.
- **Roar** is a seat in the follow-up picker: a three-round panel
  (independent reads, deliberation, synthesis) on one locked answer, **at most
  once per lock**; the server refuses a second on the same lock, naming the
  first, and a superseded-and-re-locked answer may roar again. Its
  transcript (at most 48 KiB, refused whole if larger, never cut) shows
  collapsed on the round and on each question it produced.
- **Suggested next steps**: small chips beside each question and round say
  which step fits and why (the reason is the chip's accessible text):
  *refine* when a locked answer cites a spec under `specs_dir` that has not
  been edited since the lock, *drill* when your own words name something (any
  `backticked` term or Capitalised word, less common words like "The") that
  no spec and no item title mentions (or a proposed item has no spec),
  *deliberate* when an answer
  is stale or went against the ★. The server works them out by rule
  (`console_kit/tags.py`); they start nothing.
- **Request a visual** on an item: an agent answers with a Mermaid diagram
  (shown as its source text) or a static HTML mock (shown **only** inside
  `<iframe sandbox="">`, served under a `sandbox` Content-Security-Policy, so
  no script in it runs, even opened on its own), plus a short doc. The server
  stores both in its state directory, beside `store.jsonl`, and **never writes
  into the project's working tree** (0.8.1). To land a visual in the
  repository, an agent runs `agent.py visual-export --project <its own
  worktree>`, which copies it under `visuals_dir` there and regenerates that
  folder's `INDEX.md`; the agent then opens a pull request. The checkout the server runs
  from keeps following main with a plain `git merge --ff-only`.

`docs/ADAPTER.md` documents `specs_dir`, `visuals_dir` and `next_step`.

### Several agents, one console (0.8.2)

- **Agent names.** A session may say who it is: `agent.py --as agent-6`, or
  `CONSOLE_KIT_AGENT=agent-6`. The console shows the name wherever it shows
  an agent as the author: "asked by agent-6" on a question, on replies and
  chat messages, visuals, roar transcripts and Feed rows. `fold.py export`
  carries it as `asked_by_agent`, so a ruling can say which agent asked. A
  name is 1 to 32 lowercase letters, digits and single hyphens, and anything
  else is refused by name. With no name, everything reads as in 0.8.1.
- **Names live beside the store, not in it** (`<state>/names.jsonl`). The
  store, its records and `SCHEMA_VERSION` are unchanged, so a 0.8.1 kit still
  starts on a store 0.8.2 wrote; it would refuse a record carrying a field it
  does not know.
- **Each agent's own "agent active" marks.** `working` marks are kept per
  name, and a session's `synced` clears only its own. Unnamed sessions share
  one set, as before. The doorbell cursor is still one for all sessions.
- `agent.py visual-export` exits **3** when it wrote files but refused a
  visual (each refusal named), and 1 only when nothing was written. Its
  directory walk holds each folder open as it goes (`O_DIRECTORY|O_NOFOLLOW`
  with `dir_fd`), so a folder swapped for a symlink mid-export cannot redirect
  a write.

### One steward, many sessions (0.8.3)

Owner decision, 2026-09-30: *"Lock + hook others ★"*.

- **The steward.** The doorbell has one cursor, so one session holds it: the
  steward, named in **your own registry**, never by the repository:

      python3 <kit>/agent.py --state <state> steward agent-5     # or: register ... --steward agent-5
      python3 <kit>/agent.py --state <state> steward --clear     # back to 0.8.2: no lock

  It is set on every project registered on that state (several checkouts of
  one project share one console, so they share its steward).
- **Start the steward session with its name in the environment**:
  `CONSOLE_KIT_AGENT=agent-5 claude`. Its `agent.py` calls then carry the
  name, and the question hook recognises it. Start the others with names of
  their own (`CONSOLE_KIT_AGENT=agent-6 claude`).
- **The lock.** With a steward set, `agent.py watch`, `agent.py synced` and
  `fold.py` (export and fold) refuse, exit 1, any session whose name
  (`--as`, else `CONSOLE_KIT_AGENT`) is not the steward's, and the refusal
  names the steward. `ask`, `reply` and `working` stay open to every
  session, named or not.
- **The question hook.** In a registered project with a steward, the plugin
  blocks `AskUserQuestion` in every session but the steward's, and tells it
  the full `agent.py ask` command instead, so its question reaches your
  inbox. The steward may still ask you live, and mirrors each live answer
  to the console (it posts the question and replies with your answer). The
  hook fails open: if it cannot read your registry, the question goes
  through.
- **A guardrail, not a lock against an attacker.** Every session runs as you,
  on the same socket and files; one that sets `CONSOLE_KIT_AGENT` to the
  steward's name is the steward. It keeps your own cooperating sessions from
  racing for the cursor, and nothing more.
- With no steward set, everything is exactly as in 0.8.2.
- **Before going back to 0.8.2, run `agent.py --state <state> steward
  --clear`.** Once a steward is set, a 0.8.2 SessionStart hook reads the
  `steward` key as a malformed entry and reports "Owner console: not
  checked" (the owner's requests are not listed) until the steward is
  cleared (`kit/docs/DEPLOY.md`, "Rollback", step 7).

### Name a session from inside it (0.8.4)

- **`/console-kit:as NAME`.** Type it in a Claude Code session (e.g.
  `/console-kit:as agent-5`) instead of starting the session as
  `CONSOLE_KIT_AGENT=agent-5 claude`, which still works as the fallback. The
  plugin's UserPromptSubmit hook records the session's id against the name in
  `<state>/sessions.jsonl` (mode 0600, rewritten to its newest lines before it
  passes 64 KiB) for the registered project the session works in. The skill
  itself is user-only (`disable-model-invocation: true`): Claude cannot run it.
- **It lasts as long as the session id.** `claude --resume` and `--continue`
  keep the id, so the name; `/clear` and `--fork-session` start a new session,
  which is named again. After `/clear`, forks and restarts the SessionStart
  note says "This session is not named" when the console has a steward.
- **Everything reads it the same way.** The question hook, the SessionStart
  note, `agent.py` and `fold.py` take this session's name as: `--as` (the two
  scripts), then the name recorded for this session, then
  `CONSOLE_KIT_AGENT`. `agent.py` and `fold.py` find the session through
  `CONSOLE_KIT_SESSION`, which the SessionStart hook exports into the
  session's Bash environment (`CLAUDE_ENV_FILE`).
- **The file only names sessions; your registry still names the steward.** A
  stale or damaged line cannot make a session the steward the registry does
  not name, and never locks the steward out: the last well-formed line for a
  session wins, so typing the command again fixes it, and a file that cannot
  be read gives no name (the hooks then fall back to `CONSOLE_KIT_AGENT`).
- **Still a guardrail.** Any prompt submitted in the session fires the hook:
  one you type, or one another plugin's SessionStart hook injects
  (`initialUserMessage`). Claude calling a tool does not.
- **Messages keep their line breaks.** Owner messages and agent replies in an
  item's thread now render their newlines (`white-space: pre-wrap`), as chat
  messages already did. The text is still set as text, never as HTML.

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

In a registered project the hook also tells every session to post the
questions you must decide to the console (the **console-ask** skill), so a
question an agent writes in a document still reaches your inbox.

Each locked answer has a **Next step ▾** menu beside it, whose **Follow up**
(0.4.0; under the menu since 0.8.0) works like this. Pick one to three
seats (DevOps, UX, adversarial, security, architect, analyst, or a seat you
name), a mode and an optional note, and those seats look at that one answer
and bring any follow-up questions back to the same item. The locked answer
stands; a seat that finds its premise false says so in a question, and only
you supersede it.

### When a locked answer goes stale

A question says what must stay true for its answer to stand (`valid_if`):

- an **`excerpt`**: the text the question cites, which must still be in the file;
- a **`file_sha256`**: the whole file, byte for byte;
- an **`item_status`**: an item's status on your task list.

An `excerpt` is found wherever it sits in the file. Moving it, or re-wrapping
it, is not a change. Editing or deleting it is. A whole-file hash of a big file
that keeps changing goes stale after almost any unrelated edit, so questions
should cite an `excerpt` instead.

On a stale answer, **Why stale?** says which check failed and why, in plain
words. For cited text, it also shows the closest text now in the file.

**Still holds: re-lock…** keeps your answer word for word and locks it again.
The new lock is checked against the files as they are now: the server
re-hashes whole files, and keeps each cited excerpt that is still there. The
page never supplies these checks. Changing your answer and locking it also
re-anchors it.

For locks made before 0.5.0, run `agent.py reanchor --dry-run`, then
`agent.py reanchor`. It turns a stale whole-file hash into an excerpt only
with evidence:

1. the question names a line range in that file;
2. git history holds the exact version the answer was locked against;
3. those lines, as they were then, are still in the file now, exactly once.

Anything else stays stale and is listed with the reason. Each re-anchor is a
new record in the store, and nothing already written changes.

- **`server.py`** serves one page (yours) with the console injected. It
  refuses every request without a valid Access token, loopback included.
- **`agent.py`** is the agent's side: `view`, `ask` (one question or a batch), `reply`, `inbox`,
  `working` (shows you "agent active" on the items it has picked up),
  `synced`, `fork-context`, `check` (why each stale answer is stale), `reanchor`,
  `transcript` (a roar's transcript), `visual` (an answer to a visual request),
  `visual-export`, and `register` and `steward`, which **only you** run.
  `--as NAME` names the session (0.8.2).
- **`fold.py`** writes locked answers into your project through the adapter.
- **`plugin/`** holds the Claude Code plugin: the hook, the skills and the
  committee agents.
- **`onboard.py`** and **`deploy/`** onboard a project and install the
  services.

## Trust

- The plugin's hook runs in every project you open. It acts only in projects
  listed in `~/.config/console-kit/projects.json`, which only
  `agent.py register` and `agent.py steward` write. A cloned repository's
  `.console-kit.json` is never trusted by itself, and cannot name a steward.
- The kit never opens `~/.cloudflared/cert.pem` or a tunnel credentials file.
  It checks that the file exists, and nothing more.
- Nothing onboarding writes is a secret. The AUD tag and team domain are what
  tokens are *checked against*, and no token can be made from them.

## Development

    python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
    .venv/bin/python test_kit.py && .venv/bin/python test_server.py && .venv/bin/python test_onboard.py
    .venv/bin/python test_build.py
    CONSOLE_KIT_BROWSER=1 .venv/bin/python test_browser.py   # needs playwright + browsers

To build the release zip:

    python3 build_zip.py --deny-file ~/my-deployment-values.txt

It runs `claude plugin validate --strict` on the result. The deny file lists
strings from your own deployment, such as your hostnames, AUD tags, team
domain and home path. The build refuses if any of them appears in the zip.

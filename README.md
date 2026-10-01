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

- **[docs/USER-GUIDE.md](docs/USER-GUIDE.md)**: start here if you are new:
  what the console is, the pinned install, and the owner's and the agents'
  day, with tips for spending fewer tokens.
- **[INSTALL.md](INSTALL.md)**: install the plugin in Claude Desktop or the
  CLI, and onboard a project.
- **[docs/MIGRATION.md](docs/MIGRATION.md)**: move a project that vendors the
  kit (e.g. `tools/console-kit`) onto one user-level pinned install, in an
  order that keeps its console up.
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

### Back to the inbox, and one lock for many answers (0.8.5)

- **"← Inbox".** An item opened from the inbox has a button back to the inbox,
  which returns focus to that item's row.
- **"Lock all N answers".** An item with more than one answered, unlocked
  question offers one button. It shows every answer it will lock, locks them
  in order, and stops at the first refusal, naming the question and how many
  were locked. The message can be dismissed.

### A usage footer (0.8.6)

- **What it shows.** A thin bar at the bottom of the console: Claude usage for
  the 5-hour and 7-day windows with their reset times, how old that snapshot
  is, and the signed-in account's email. It polls `GET /api/usage` every 45 s
  while the tab is visible.
- **Off unless you ask for it.** The server reads nothing for the footer until
  it is started with `--usage-file` or `--account-file` (absolute paths), and
  a page talking to an older server shows no footer.
- **`--usage-file`** is a status line's usage snapshot (for claude-hud, the
  file named by `display.externalUsageWritePath`): `updated_at`, and
  `five_hour` / `seven_day` each with `used_percentage` and `resets_at`. The
  status line writes it only while a Claude Code session runs, so with none
  open the footer says how old the numbers are, in italics once they pass
  10 minutes.
- **`--account-file`** is Claude Code's settings file. Only
  `oauthAccount.emailAddress` is read from it; the file also holds other
  account details and project history, and none of that reaches the page.
- **Checked, then forwarded.** Both files are read with a size cap, parsed,
  and reduced to those fields; a percentage must be 0-100 and a timestamp must
  carry a time zone. A problem is shown as a short sentence, never as the
  file's contents.

### Claude Code's own status line input as the usage file (0.8.7)

- **No plugin needed.** Claude Code hands its status line command a JSON
  object on stdin; a status line that saves it to a file (for example
  `printf '%s' "$input" > ~/.claude/usage/statusline-latest.json`, written
  through a temporary file and `mv`) makes a usage file `--usage-file` can
  read. The windows sit under `rate_limits`, reset times are whole epoch
  seconds, and there is no `updated_at`: the file's own write time stands in.
  The status line rewrites the file on every redraw, so that time says a
  session is running, not when the limits were last fetched; with no session
  open it goes stale. A write time more than a minute ahead of the server's
  clock is named as a problem, never shown as fresh, and input from before
  the session's first answer (no `rate_limits` yet) says so.
- **Nothing else in it is forwarded.** That input also carries the session id,
  paths and cost; only the two windows and the write time reach the page.
- **claude-hud's snapshot still works.** A file with `five_hour` / `seven_day`
  at the top level is read exactly as in 0.8.6, and only from there.

### Deliberate before answering (0.8.8)

- **A committee before you answer.** A question you have not locked yet has a
  **⑂ Deliberate before answering** button: pick one to three seats, a mode and
  an optional note, and the form shows the cost first (about 100k tokens a
  seat). The round never answers or locks; it replies on the question with
  `Result: ★ <option>` and its reasons, or, only when the seats show the
  options themselves are wrong, posts a replacement question and replies
  `Result: replaced by <qid>`. Roar, refine and drill still need a locked
  answer.
- **One rule for "done".** A deliberation is finished when a `Result:` reply to
  it exists, or, for every kind except a before-answering round, when its
  questions exist, so rounds finished before 0.8.8 stay finished. A progress
  note never closes a round, and a crash between a replacement question and
  its result leaves the round waiting rather than lost.
- **Your picks survive a refresh.** Seat ticks, the other seat and the mode are
  kept across a re-render, in this form and in "Follow up on this answer".

### Slim reads and a cost sidecar (0.8.9)

- **`agent.py todo`.** Prints only what waits for the agent: forks, threads,
  chat, visuals, inbox and seq. It is computed from the view's own rules, so it
  can never disagree with `view`. Measured once on a 116-question console:
  `todo` printed 184 bytes where `view` would have printed 501,167.
- **Narrower reads.** `view --item ID` prints one item and everything under it
  (`@chat` for the chat). `--since SEQ` on `view` and `answers` keeps only
  questions touched after that seq; a later lock or re-anchor counts as a
  touch, and stale questions are always kept. JSON is compact when stdout is
  not a terminal.
- **A 64 KiB cap.** A `todo`, `view` or `answers` read over 64 KiB exits 4,
  prints nothing on stdout, and names the narrower command; `--full` prints it
  anyway. The console skills say what to do on exit 4.
- **Newer client, older server.** `todo`, `view --item` and `answers` work
  against a 0.8.8 server. `--since` refuses in one line until the server is
  restarted on 0.8.9, because it needs a field only 0.8.9 sends.
- **`agent.py costs collect | show --fork ID`.** Reads token usage from Claude
  Code's own transcripts, only this project's, and only the usage, message id
  and agent type, plus the first 64 characters of a subagent's description to
  find its `ck-fork:<id>` tag. Writes `costs.jsonl` beside the store, never
  into it. `collect` is steward-only; `show` only reads. Symlinks are
  refused, and lines and files are size-capped.

### The server starts no git (0.8.10)

- **Why.** Git obeys the repository's own `.git/config`, which an agent can
  write, and some keys make git run a program. The console server runs
  outside every agent's jail, so it no longer runs git at all. A list of
  forbidden config keys was rejected: git has no switch to ignore repository
  config, so a new key would defeat any list.
- **What you see.** Anything git history fed now says "unavailable (no git in
  the server)" rather than going blank. `check` can't show which version a
  whole-file hash was locked against. `reanchor` leaves locks stale and says
  why. Refine chips show a spec's file time in place of its last commit.
  These return once an agent-side step computes and supplies them.
- **How it is held.** `console_kit/gitseam.py` is the only place a process
  can be started, and the server closes it before reading the project. A test
  runs the real server under an audit hook and fails if any route starts a
  process. A second test walks every kit module's syntax tree and fails on
  any other way to start one.

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

A question that is still open (unanswered, or answered and not locked) has a
**⑂ Deliberate before answering** button instead. The same seat picker, with
explore as the default mode and the cost shown before you send (about 100k
tokens per seat). The seats reply on that question with a ★ recommendation and
their reasons, and ask a replacement question only when the options
themselves are wrong. A roar, refine or drill still needs a locked answer, and
the answer and the lock stay yours.

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

**The console server starts no git (owner ruling CONSOLE-kit/Q23).** Git obeys
the repository's own `.git/config`, which an agent can write, and some keys
make git run a program; the server runs outside every agent's jail. So until
an agent-side step supplies it, everything git history fed says
"unavailable (no git in the server)" instead: `check` cannot show which version
a whole-file hash was locked against, `reanchor` re-anchors nothing (rule 2
cannot be checked) and lists each lock with that reason, and a refine chip
shows the spec's file time and says the last-commit time is unavailable.

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

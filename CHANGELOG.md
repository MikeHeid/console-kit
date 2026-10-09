# Changelog

What each overture release added, oldest first. These sections moved
word for word from the README, where each was written as its release
shipped. The README itself describes the kit as it is today.

## A live console (0.7.0)

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

## Next steps, roar and visuals (0.8.0)

- **"Next step ▾"** beside every locked answer and every round's answers
  offers three kinds of fork: **Follow up** (seats you pick, as before),
  **Refine** and **Drill**. Refine and drill run the project's own skill,
  named in `.overture.json`'s `next_step`, on that answer or round. Neither
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
  (`overture/tags.py`); they start nothing.
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

## Several agents, one console (0.8.2)

- **Agent names.** A session may say who it is: `agent.py --as agent-6`, or
  `OVERTURE_AGENT=agent-6`. The console shows the name wherever it shows
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

## One steward, many sessions (0.8.3)

Owner decision, 2026-09-30: *"Lock + hook others ★"*.

- **The steward.** The doorbell has one cursor, so one session holds it: the
  steward, named in **your own registry**, never by the repository:

      python3 <kit>/agent.py --state <state> steward agent-5     # or: register ... --steward agent-5
      python3 <kit>/agent.py --state <state> steward --clear     # back to 0.8.2: no lock

  It is set on every project registered on that state (several checkouts of
  one project share one console, so they share its steward).
- **Start the steward session with its name in the environment**:
  `OVERTURE_AGENT=agent-5 claude`. Its `agent.py` calls then carry the
  name, and the question hook recognises it. Start the others with names of
  their own (`OVERTURE_AGENT=agent-6 claude`).
- **The lock.** With a steward set, `agent.py watch`, `agent.py synced` and
  `fold.py` (export and fold) refuse, exit 1, any session whose name
  (`--as`, else `OVERTURE_AGENT`) is not the steward's, and the refusal
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
  on the same socket and files; one that sets `OVERTURE_AGENT` to the
  steward's name is the steward. It keeps your own cooperating sessions from
  racing for the cursor, and nothing more.
- With no steward set, everything is exactly as in 0.8.2.
- **Before going back to 0.8.2, run `agent.py --state <state> steward
  --clear`.** Once a steward is set, a 0.8.2 SessionStart hook reads the
  `steward` key as a malformed entry and reports "Owner console: not
  checked" (the owner's requests are not listed) until the steward is
  cleared (`kit/docs/DEPLOY.md`, "Rollback", step 7).

## Name a session from inside it (0.8.4)

- **`/overture:as NAME`.** Type it in a Claude Code session (e.g.
  `/overture:as agent-5`) instead of starting the session as
  `OVERTURE_AGENT=agent-5 claude`, which still works as the fallback. The
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
  `OVERTURE_AGENT`. `agent.py` and `fold.py` find the session through
  `OVERTURE_SESSION`, which the SessionStart hook exports into the
  session's Bash environment (`CLAUDE_ENV_FILE`).
- **The file only names sessions; your registry still names the steward.** A
  stale or damaged line cannot make a session the steward the registry does
  not name, and never locks the steward out: the last well-formed line for a
  session wins, so typing the command again fixes it, and a file that cannot
  be read gives no name (the hooks then fall back to `OVERTURE_AGENT`).
- **Still a guardrail.** Any prompt submitted in the session fires the hook:
  one you type, or one another plugin's SessionStart hook injects
  (`initialUserMessage`). Claude calling a tool does not.
- **Messages keep their line breaks.** Owner messages and agent replies in an
  item's thread now render their newlines (`white-space: pre-wrap`), as chat
  messages already did. The text is still set as text, never as HTML.

## Back to the inbox, and one lock for many answers (0.8.5)

- **"← Inbox".** An item opened from the inbox has a button back to the inbox,
  which returns focus to that item's row.
- **"Lock all N answers".** An item with more than one answered, unlocked
  question offers one button. It shows every answer it will lock, locks them
  in order, and stops at the first refusal, naming the question and how many
  were locked. The message can be dismissed.

## A usage footer (0.8.6)

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

## Claude Code's own status line input as the usage file (0.8.7)

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

## Deliberate before answering (0.8.8)

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

## Slim reads and a cost sidecar (0.8.9)

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

## The server starts no git (0.8.10)

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
- **How it is held.** Among the modules the server runs,
  `overture/gitseam.py` is the only place a process can be started, and
  the server closes it before reading the project. (`agent.py` and `tools/`
  still run git, in the agent's or the operator's own process; the server
  never imports them.) A test runs the real server under an audit hook and
  fails if any route starts a process. A second test walks the syntax tree of
  every kit module except those named exceptions, and fails on any other way
  to start one.

## Git history returns, from the steward (0.8.11)

- **What returns.** The panels 0.8.10 marked "unavailable (no git in the
  server)": `check` naming the version a whole-file hash was locked against,
  `reanchor`, and refine chips showing a spec's last commit.
- **How, with still no git in the server.** The steward runs git in its own
  process and pushes what it found:

      python3 <kit>/agent.py --state <state dir> history-push

  run from inside the project's checkout. It sends file contents keyed by
  their SHA-256. The server keeps a pushed file only when its hash is one a
  current lock names, checks it again on every read, and works out the diff
  and the cited lines itself. A commit id the steward reports is shown
  "(from the steward)": the server cannot check it.
- **When nothing has been pushed**, or a key no longer matches, each panel
  falls back to the 0.8.10 label rather than going blank.

## The server runs no project code (0.8.11, owner ruling CONSOLE-kit/Q24)

- **Why.** The project's adapter is Python an agent can write. A server that
  imported it ran that code at its next restart, outside every agent's jail.
- **What changed.** The console server never imports or runs the adapter, at
  start or later. The steward runs it in its own process and sends the
  result, as data:

      python3 <kit>/agent.py --state <state dir> items-push --project <project> --adapter <adapter path>

  Run it from inside the project's checkout, with `--adapter` inside that
  project. The command picks its server from the directory it runs in, and
  refuses an adapter outside the project.

  The server checks what arrives against a closed schema (each item is
  `title`, `parent`, `status`; unknown keys are refused). It keeps the last
  push in the state dir, so a restart shows it. The single server and the
  one server (`server.py --all`) take the same push, refuse in the same words,
  and show the same items.
- **What you see.** Until the first push, the inbox says the items appear when
  the steward pushes them. It never shows a blank or invented items. Push
  again whenever the register changes. `--adapter` is still accepted, so an
  existing unit keeps starting, but it is ignored. If the stored items cannot
  be read, the single server still starts. It shows no items, the inbox says
  why, and `health` answers 200 with its register marked "error" until the
  next `items-push` replaces the file.
  The one server refuses just that project.
- **How it is held.** A test runs the real server under an audit hook, with an
  adapter configured whose import leaves a marker. The test fails if the
  server imports, opens, compiles or runs that file.

## The page is a reviewed snapshot you publish (0.8.12, owner rulings CONSOLE-kit/Q28 and Q29)

- **Why.** The server used to read your page from the project's checkout on
  every request and put the console into it. Any agent that could edit that
  file could put a script in your browser, where it could answer and lock as
  you.
- **What changed.** No server reads a page from a project any more: not
  `--page`, and not the one server's `page` entry in `server.json`. The page it
  serves is a snapshot kept in the console's state dir, and only the steward
  writes it, from a commit:

      python3 <kit>/agent.py --state <state dir> page-snapshot --path <page path in the repository>

  Run it from inside the project's checkout, after a `git fetch`. It reads the
  page from `origin/main` (or `--from-ref <ref>`) with git, never from the
  working tree, so an uncommitted or unmerged edit never reaches your browser.
  It refuses a commit that is not in `origin/main`, unless you pass
  `--unreviewed`. On the one server, `--path` defaults to the project's `page`
  in `server.json`. The server checks what arrives (at most 4 MiB, exactly one
  `</body>`, no console block already in it) and keeps it as data. It never
  runs git.
- **You publish it (owner ruling CONSOLE-kit/Q29).** What `page-snapshot`
  sends is only *proposed*: nothing you are served changes. The console shows
  a strip, "Proposed dashboard: <ref> @ <commit> · <size> · reviewed (agent's
  claim, not checked by the server)" (or "unreviewed"), with a preview and a
  **Use this page** button. The page is
  served only after you press it, through a route behind your Access login.
  No agent can publish, because the agent socket has no route that does.
  **The cost is one click per dashboard update.** The button sends the commit
  you were shown. If a newer page was staged after your page loaded, it is
  refused, naming both commits. Reload, look, and press again. Publishing
  checks the page again (size, schema, the console injection). It cannot
  check that the commit is in `origin/main`, because the server runs no git.
  "reviewed" is only what the sending agent's command reported, so judge the
  commit and the preview, not the label. Any process that can reach the
  console's agent socket can replace the staged page, but a press on a page
  that has since been replaced is refused, naming both commits.
- **The preview never runs the proposed page's scripts.** It is shown in an
  `<iframe sandbox="">` with no allowances. The route it loads from also
  answers under its own `sandbox; default-src 'none'` policy, the one stored
  visuals use. Each layer alone stops the page's scripts, so the preview shows
  the page's markup and styles, never its behaviour. Its scripts first run
  when you publish it.
- **What you see.** A line at the end of the page names where it came from:
  "Dashboard page from origin/main @ <commit> (staged by the steward,
  published by you)", or "(staged from an unreviewed ref, published by you)"
  for a page sent with `--unreviewed`. Until you publish one, the server
  serves the console alone, with a note saying to run `agent.py page-snapshot`
  and then press **Use this page**. The console works either way. Your
  page's own scripts still run once published. Stage and publish again
  whenever the page's merged version changes. `--page` is still accepted, so
  an existing unit keeps starting, but it is never read, and the server logs
  one line saying so.
- **How it is held.** A test runs the real server under strace with `--page`
  naming a page in the project, and with it naming a symlink to a page
  outside. No system call names either path. Another test edits the page in
  the working tree, stages it and runs `page-snapshot` again, and the edit
  never reaches the page served. A test drives every agent route, and none
  changes the served page. A browser test shows that the proposed page's
  script does not run in the preview and does run once published.

## Settling a stale answer (0.8.13, owner rulings CONSOLE-kit/Q30, Q31 and Q32)

- **Why.** A stale answer used to have one way out: re-lock it as it stands.
  A ruling that no longer applies, one whose anchor was the wrong check, or
  one that needs a new question stayed in the inbox for good.
- **What you can do now.** Each act is yours alone, and each works only on a
  stale answer. On anything else it is refused, naming why.
  - **Withdraw…** says the ruling no longer applies. It leaves the inbox and
    reads *withdrawn*, with the reason you give. A reason is required.
  - **Keep, stop checking…** says the ruling stands but is no longer checked
    against the files. It leaves the inbox and reads *locked*, with a note
    saying it is no longer checked. A reason is optional.
  - **Confirm a proposed anchor.** The steward may propose new lines that the
    ruling depends on (`agent.py propose-anchor QID --cite PATH:A-B --basis
    TEXT`). The server reads those lines itself, so the steward names lines and
    never supplies the text. The banner shows what failed beside what is
    proposed. Nothing changes until you press **Confirm**. The server reads the
    file again at that moment, and a proposal that no longer holds is refused,
    naming why. The steward never re-anchors a ruling to a passage it chose.
    (`agent.py reanchor` is unchanged.) A proposal may hold about 40 KB of
    text, so whole `view` and `answers` reads name it by its cites and a
    digest. `view --item` and your page show the text.
  - **Replace.** The steward asks a new question with `"replaces": "<old
    qid>"` in its file. The old ruling stays in force, still stale and linked
    to the new question, until you lock the new one. Then it reads
    *superseded*. One open replacement per ruling. If the question is stored
    but its link is not, the ask fails naming its `nonce`. Sending the same
    ask again with that nonce writes the link, while nobody has answered it.

  Neither withdraw nor keep is a default; the page offers both side by side.
- **Nothing is deleted.** These acts are written to `refactor.jsonl`, beside
  `store.jsonl` in the console's state dir. The store is not changed, and the
  old lock, answer and anchor stay readable. Every act names the lock you were
  shown. A page loaded before a re-lock is refused rather than settling a
  ruling you never saw.
- **The fold records each outcome.** The export carries `outcome` (withdrawn,
  untracked or superseded, with who, when, and the reason or the replacing
  question), and `replaces` on a replacement. fold passes the entry to the
  adapter again when an outcome arrives for a lock it already folded. An
  adapter that does not declare `RECORDS_OUTCOMES = True` is refused such an
  entry and nothing is folded, so a ruling never silently vanishes from your
  project's record. The starter `adapter_template.py` declares it. To update
  an existing adapter, see `docs/MIGRATION.md`.
- **New asks: no whole-file hash where an excerpt fits.** Most answers that go
  stale were anchored to a hash of a whole file that kept changing. A new
  question is refused a `file_sha256` check when its `source` or an evidence
  cite names lines of that same file (the refusal shows the `excerpt` to use
  instead), and on any file over 100 lines. Questions already in the store
  are not affected.
- **If `refactor.jsonl` is damaged** (an edited or torn line, a link, not a
  plain file), the console keeps running and names the problem on the page.
  Withdraw, keep, confirm, propose and replace are refused until the file is
  fixed or moved aside, and every answer reads as if the file were empty, so a
  settled answer reads stale again, never fresh.
- **Rolling back.** An older kit never opens `refactor.jsonl` and reads the
  store as it always did. Every withdrawn, kept or superseded answer reads
  stale again, and every confirmed anchor reads as the old anchor. Nothing is
  lost. Upgrade again and the outcomes return.
- **How it is held.** A test drives every agent route with each act's body,
  and no answer's state changes. Another shows a proposal confirmed after its
  file changed is refused. A test moves the sidecar aside and the answer reads
  stale. A test shows fold refuses an adapter without `RECORDS_OUTCOMES`.

## Skills the plugin ships (0.8.13)

The plugin carries the skills the console's rounds lean on, so a fresh
install needs nothing else: **`roar`** (a three-seat panel on the plugin's
own `overture:architect`, `overture:ux` and `overture:other` sitting
as advisor), **`refine`**, **`drill`** and **`deliberate`**. Run them as
`/overture:roar` and so on. Onboarding now writes `next_step` as
`{"refine": "overture:refine", "drill": "overture:drill"}` when a
project names none; a project's own entry is kept. Inside a console round
none of them writes into the project before the owner locks. The lookup's
trust rules are unchanged: installed user or plugin skills only, never a
skill inside the repository.

## Pull requests on the console (0.8.13)

- **What you can do.** The inbox has a **PRs** tab: the project's open pull
  requests first, then those merged or closed in the last 30 days. Each shows
  its number, title, branch, author, checks (pass, fail, running or none), a
  draft badge, and when it was opened, merged or closed, and links to the PR
  on GitHub. A title or branch that names an item or a question the console
  knows gets an **Open** button for that item. A line says when the steward
  last pushed the list, so a stale list reads as stale.
- **How, with no GitHub call in the server.** The steward runs `gh` in its
  own process and sends the result, as data:

      python3 <kit>/agent.py --state <state dir> prs-push

  run from inside the project's checkout (`--days`, default 30; `--limit`,
  default 50 of each list). The server checks it against a closed schema:
  unknown keys, more than 200 PRs, a title over 1024 characters, a branch
  over 255, and any link other than that PR's own
  `https://github.com/<repo>/pull/<number>` are refused by name. It keeps the
  last push in the state dir (`prs.json`), stamped with when it arrived and
  which agent sent it. Only the agent door takes a push; the owner door can
  only read it. Titles and branches are shown as text, never as HTML.
- **Before the first push** the tab says the steward has not pushed pull
  requests yet. It never shows a blank or invented list.
- **Not in the Feed.** The Feed lists the console's own records, paged by
  their sequence number; a pull request has none, so the Feed's footer points
  to the PRs tab instead.

## Footer version and the stale-board bar (0.8.14)

- **The footer names the kit version** (owner, 2026-10-01: "version number
  should be in footer"). It reads "overture 0.8.13", say, taken from the
  page's config, which the server fills from `overture.__version__`: the
  same value `/health` reports, never typed into `console.js`. It shares the
  usage footer's bar, so the page keeps one bottom bar that already makes
  room for itself, and the bar now shows even when the usage footer is off or
  has nothing to say. It is set as text, never markup.
- **The stale-board bar no longer offers Reload** (owner, 2026-10-01: "the
  'board has changed since page loaded' message is not going away"). The page
  is a published snapshot, so a reload served the same page and the bar came
  straight back. It now says the dashboard has changed since the page was
  published, so its live numbers are paused, and names what fixes it: when a
  page is staged and waiting, the proposal's "Use this page", with a button
  that takes you to it; otherwise the steward staging a new page
  (`agent.py page-snapshot`). It stays a status message fixed at the bottom
  and cannot be dismissed, because polling has stopped and it is the only
  sign the numbers on the page are frozen.

## Reviewing a new dashboard page in a dialog (0.8.15)

- **A staged page is reviewed in a modal dialog** (owner, 2026-10-02: "can
  you create a modal rather than place on bottom (button hides under
  header)"). The proposal is no longer a strip at the end of the page. A
  small button fixed at the bottom-left, "New dashboard page waiting ·
  Review", shows whenever a page is staged. It sits above the host page's
  sticky header in the stacking order and away from the top edge, so no
  header covers it at any scroll position. When the stale-board bar is also
  showing, the bar moves up to sit above it.
- **The dialog** is a native `<dialog>` opened with `showModal()`, so focus
  moves into it and stays there, Esc closes it, and the page behind gets a
  backdrop. It is named by its heading, and focus goes back to the button
  that opened it. It shows the ref, commit, size and provenance line, the
  same sandboxed preview (`<iframe sandbox="">` on `/api/page-staged`, under
  the same CSP, unchanged), **Use this page** and **Not now**. It fades and
  scales in only under `prefers-reduced-motion: no-preference`.
- **It never opens by itself.** It opens only from the waiting button or the
  stale-board bar's button, now named "Review the new page". Opening it,
  closing it and pressing Esc publish nothing. Publishing is still only
  **Use this page**, through the same `POST /api/page-publish` with the
  commit the page showed, so a page staged after the owner looked is still
  refused by name.
- **Without the console script** the proposal is still shown. The server
  renders the dialog without `open`, and until `console.js` marks it ready,
  the stylesheet lays it out inline at the end of the page, as the strip
  was. The waiting button and "Not now" stay hidden there, because they
  would do nothing without the script.

## A calmer inbox (0.8.16, owner rulings CONSOLE-kit/Q33 to Q38)

- **One tap locks, after a countdown you can undo.** Tapping Lock starts a
  short visible countdown with an Undo button; nothing is sent at the tap. The
  countdown is cancelled, and the Lock button comes back, on Undo, on opening
  another question or item, on closing the panel, on hiding the tab and on
  leaving the page (including a page kept in the back-forward cache).
- **A pick moves you on.** After a single-choice pick the form moves to the
  next question you have not answered, and says so. It never moves on a
  multi-choice question, while you walk the options with the arrow keys, or
  past the last step.
- **Answer together.** Questions one named agent asked about one item within
  a few minutes are shown as a group. Locking the group still goes through
  your own answer and lock, one question at a time, then sends one process
  request; if one is refused it stops and says "Locked k of n".
- **A send bar you can always see.** Once answers are in, a "send to agent"
  bar appears as a row under the item panel's header, and the panel sits
  above the page's own sticky header, so nothing covers it. It comes from the
  data, so it returns when there is something to send.
- **Locked answers roll up** to one line (question, pick, state); open the
  line for the rest. A stale answer stays whole.
- **Motion** (panels sliding, rows settling) runs only under
  `prefers-reduced-motion: no-preference`.

## Smaller fixes (0.8.16)

- A retry that reuses a nonce but names a different `replaces` (or none) is
  refused by name (409), instead of being taken as the earlier ask. An
  identical retry still succeeds. One case cannot be checked: when the
  earlier ask's link was never written, nothing kept the `replaces` it named,
  so the retry's link is written as the recovery.
- The slim read's pointer says what it left out and names the reads that
  print it: `view --item X` or `answers --item X --json`.
- The unit template no longer passes `--page` or `--adapter`; the server read
  neither. Units installed earlier still start: both flags are accepted and
  ignored.

## Scan for a resolve (0.8.17, owner rulings CONSOLE-kit/Q40 and Q41)

- **Ask the steward to look again.** Every stale answer has a **Scan for a
  resolve** button, and the inbox has **Scan all stale (N)**. A press asks the
  steward to find out whether the ruling still applies: where its cited text
  went, whether its premise still holds, and what replaced it. "Scan all" is
  one request naming every stale ruling at the moment you press it; one that
  went stale after the page loaded is refused by name rather than missed.
- **One scan at a time.** While a scan is open the buttons give way to its
  status ("Scan asked …: k of n rulings still wait"), and a second scan is
  refused, naming the open one.
- **The steward answers each ruling one way:** a proposed new anchor (old and
  new side by side, as before), a replacement question, or new **advice**: a
  ★ Withdraw or Keep recommendation with its evidence, shown beside your own
  Withdraw and Keep buttons. Nothing changes until you press one of them; the
  steward proposes, you decide.
- **A scan is done when records say so, never the files.** Each ruling is done
  once the steward answered it after the scan, or you settled it (withdraw,
  keep, confirm, a replacement, a new answer or a re-lock). A done scan never
  reopens. A ruling whose cited text came back while the scan was open still
  gets an answer: the steward may advise on it (most often Keep), but only
  while that scan is open and waiting on it.
- **Where it is kept.** Scans and advice are two new record kinds in
  `refactor.jsonl`, beside the other stale-answer records; `store.jsonl` is
  untouched, so an older kit still starts on the same state. An older kit
  reads `refactor.jsonl` as unreadable, so it shows every settled answer as
  stale again until it is upgraded. Nothing is lost on disk.
- **For the steward:** `agent.py advise QID --star withdraw|keep --evidence
  TEXT`; a scan rings the doorbell, and `todo` lists open scans with what each
  still waits on. A scan's doorbell line carries `rx`, the refactor seq, and
  keeps waking `watch` until `synced --rx-through RX`. The console-process
  skill's step 4b says how to answer one.

## Every stale answer reaches the inbox (0.8.18)

- **A stale answer from a deliberation round now has its own inbox row.**
  Before this, a locked answer that came out of a round and later went stale
  was still filed under that round. A round lists only the questions still
  waiting for an answer, so the stale one showed as "0 questions", and its
  stale banner and **Confirm** button could only be reached from the item
  page. A question now goes under its round only while it is open; otherwise
  it is an ordinary row that opens its card.
- **Rows say which question and what waits.** A stale row shows the question
  number (the full id is in its title and its accessible name) and says when
  "a new anchor is proposed" or "advice is waiting". Two stale answers on one
  item no longer look the same. Clicking a row scrolls to that question's card.
- **"Answer these together" groups open questions only**, so a stale answer
  is never offered inside a group whose form would skip it.
- Rounds, their forms, the server and the store are unchanged.

## Rendered visuals, favorites, direction in the inbox, and charting (0.9.0)

- **Mermaid visuals are rendered as diagrams**, inside an iframe the console
  grants `allow-scripts` but NOT `allow-same-origin`: the vendored lib runs in
  an opaque origin, so it cannot touch parent cookies, storage or network. The
  response's own CSP allows only the vendored `/api/mermaid.js` and the init
  script that carries the request's nonce. **View source** still shows the raw
  `.mmd`. HTML mocks keep their earlier, scripts-off sandbox.
- **A visual drawn by an agent surfaces at once.** A short chime plays, the
  item appears at the top of the Inbox under **New visuals** with a ◫ badge,
  the Feed row says "Visual drawn", and the unread count rises until you look.
  The badge is seq-gated per browser, like unread elsewhere.
- **Favorite tab** (between PRs and Chat). Tap ☆ on a visual, on the item
  view's title bar, or on an Inbox row to star it. The tab lists starred
  visuals grouped under their items. Favorites are per console (not per
  browser), kept in `STATE/favorites.json`; only the owner door writes to it.
- **Direction stays visible in the Inbox.** Each item with open questions
  carries a collapsible "N answered on this item" under its rows, listing the
  last few locked rulings with the picked option. Items whose questions are
  all locked within the last 24 hours stay on the list under **Recently
  answered**, so what was asked and what you answered sit side by side without
  leaving the Inbox.
- **A Back control at the top of every item view.** The ← Back button no
  longer hides at desktop widths, so **discuss** from the dashboard always has
  a one-tap way home.
- **Status flowchart on every item** (collapsible). The server builds a
  Mermaid flowchart from the item's own questions, rounds, `forked_from`,
  `supersedes` and `follow_up_of`: the item at the left, question nodes
  state-coloured, round nodes shaped apart, dotted edges where a newer
  question supersedes an older one or a round follows up. Lazy-loaded; up to
  40 questions shown, past that an ellipsis names how many more exist.
  Served through `GET /api/item-chart?item=X`, same sandboxed Mermaid frame
  as `/api/visual-render`.
- **Nodes click back.** A chart node emits `postMessage` from inside its
  sandboxed iframe; the parent scrolls to that question's card (opening the
  item first when the owner is not already in it), or opens an item when it
  is an item node. Mermaid runs at `securityLevel: 'antiscript'` for charts,
  so the text in nodes is still sanitised but `click` directives fire.
- **Project map** on the Inbox tab (collapsible, above every other section).
  `GET /api/project-chart` builds a tree of every item, parent → child, with
  each node coloured by its own questions' roll-up (awaiting_you > stale >
  unlocked > locked > nothing) and a short tally like `? 2 · o 3`. A chip row
  above it narrows the map to one state (`?state=awaiting_you|stale|unlocked|locked`
  on the endpoint); ancestors of matching items are kept, drawn muted, so the
  tree stays connected.
- **Clicking a chart node focuses the card**, not just scrolls: tabindex is
  added when missing so keyboard follow-through lands where the pointer did.
- **Save SVG** on every chart and every Mermaid visual. The iframe serialises
  its rendered SVG and posts it back to the parent, which triggers a plain
  browser download (`item-foo-chart.svg`, `project-map.svg`,
  `project-map-stale.svg`, `visual-<rid>.svg`). No same-origin needed; nothing
  reaches the server.
- **Documentation.** The README now has a **Windows + WSL2** section covering
  the CRLF trap (`core.autocrlf`), the `origin/main` default in
  `page-snapshot`, the leading-dot page path, the first `items-push`, reaching
  the agent socket from Windows, and `loginctl enable-linger`.

## Code fixes for the Windows + WSL2 workarounds (0.9.1)

Follow-up to 0.9.0: the README notes stay useful, but a fresh install no
longer needs any of the workarounds they describe.

- **`.gitattributes` pins LF.** Every text file ships as LF everywhere;
  `*.sh`, `*.in` and `*.py` are explicit so `install.sh` and the systemd
  unit templates do not pick up CR on a Windows checkout (`core.autocrlf`
  default). The committed blobs already were LF; this makes the working-tree
  guarantee explicit, so a WSL copy from the plugin cache no longer needs
  the dos2unix dance.
- **A dotfile page path is accepted.** `serverfile.PAGE` now allows a
  single leading dot on the first path segment, so `.overture/page.html`
  (the onboarding default) passes. `.`, `..`, `./foo` and empty components
  are still refused by `_page_problem`.
- **`page-snapshot` reads origin's default branch.** `--from-ref` defaults
  to the ref `git symbolic-ref --short refs/remotes/origin/HEAD` returns
  (`origin/main`, `origin/dev`, `origin/claude/foo-bar`, …), with
  `origin/main` as the fallback when that is not set. The error messages
  and `--unreviewed` wording follow suit. A project whose default branch is
  not `main` no longer needs `--from-ref HEAD --unreviewed` just to bootstrap.
- **`items-push` is called out.** `onboard.py` printed next-steps now
  include an `agent.py … items-push --adapter …` line (step 6), and the
  server's refusal when a write names an unknown item names the push to
  run. The brief defect "item X is not in the project's item list" without
  a next-step is gone.

## Fix /health version drift and automate the bump (0.9.2)

- **`overture.__version__` is in step with VERSION again.** Across 0.9.0
  and 0.9.1 the package's `__version__` stayed at `0.8.18`, so `/health`,
  the dashboard footer, and the `overture` block the server embeds all
  reported the wrong version. The code itself was 0.9.1; only the label
  lied. 0.9.2 brings every version-holding file into sync and reports
  `0.9.2`.
- **`release.py <version>` bumps all four files in one shot.** `VERSION`,
  `plugin/.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`
  and `plugin/kit/overture/__init__.py` are updated from one invocation,
  so the next release cannot drift. `test_build.py::LayoutTests` already
  asserts the four stay equal; the script just makes passing that test the
  default.

## Scaffold a dashboard, keep it live (0.9.3)

- **`agent.py scaffold-dashboard`** writes a working dashboard page and
  injects a matching `board()` into the project's adapter. The page carries
  Header / Items / Rollout / Engine / Spec / Footer sections, each wrapped
  in `<!-- scaffold:<name> start/end -->` markers so re-running adds
  missing sections without clobbering hand edits. Items / Rollout / Footer
  use `data-live="..."` attributes so values refresh from the adapter's
  `board()` without a new snapshot. The sections mirror the shape a
  mature console (dashboard, rollout, engine, spec) uses, so a fresh
  install does not look bare.
- **`agent.py items-watch`** polls the project's adapter,
  `.overture/items.json`, `.overture.json` and any extra `--watch`
  paths and re-runs `items-push` whenever any changes. A plain stat loop
  (no file-watcher dependency); `--interval` clamps to 1..300 s. Pair
  with the scaffolded `board()` to keep the dashboard fresh "as the
  project evolves".
- **New plugin skill** `scaffold-dashboard` so an agent can offer the
  scaffold on an onboarded project.
- **`docs/ADAPTER.md`** gains an "A live board" section covering `board()`,
  the `data-live-*` contract, and the three fresh-keeping patterns:
  manual push, `items-watch`, or a git `post-commit` hook.

## Marketplace manifest schema + install/upgrade notice (0.9.4)

- **`.claude-plugin/marketplace.json`** moves the marketplace description
  from `metadata.description` to top-level `description`, matching the
  shape `build_zip.py` has emitted for the zip-marketplace since 0.8.12.
  Users on the GitHub-source install (`/plugin marketplace add
  MikeHeid/overture`) can now actually upgrade: Claude Code reads the
  top-level `description` as the spec expects, so `/plugin marketplace
  update overture` picks up the new version and Desktop ungrays Update.
- **A one-time install / upgrade notice**, printed by the SessionStart
  hook the first time a session runs in a registered project after a
  version change. A short banner followed by the commands most useful at
  that moment: `items-push`, `items-watch`, `scaffold-dashboard`,
  `page-snapshot`, `prs-push`, `inbox`. The last-seen version is kept in
  `STATE/last_seen_kit_version`, so the note prints once per project per
  bump.

## Fix 'Mermaid is not defined' in the sandboxed iframe (0.9.5)

- **The vendored lib's `<script>` tag now carries the per-request nonce**
  as well as `src="/api/mermaid.js"`. In an iframe sandboxed with
  `sandbox="allow-scripts"` but no `allow-same-origin`, Chrome rejects a
  `script-src 'self'` match because the document's effective origin is
  opaque, so the lib never ran and `mermaid.initialize` fell into the
  "Mermaid failed to load: mermaid is not defined" fallback. The nonce
  path is honoured whichever way the browser resolves `'self'`.
- **Init is gated on the lib's `load` event** so a slow fetch never races
  ahead of `mermaid.initialize`, and the error UI now names the file to
  check (`plugin/kit/overture/vendor/mermaid.min.js`) rather than only
  the exception.

## Fix Mermaid rendering as text, not SVG (0.9.6)

- **The DOM scan is now explicit.** Mermaid v10's `startOnLoad: true`
  only hooks `DOMContentLoaded`, which has already fired by the time our
  `load`-gated init runs; `<pre class="mermaid">` then stayed as text
  on the Project map, the item Status flowchart, and every rendered
  Mermaid visual. The shared `_mermaid_wrapper` now passes
  `startOnLoad: false` and calls `mermaid.run()` after `initialize`, so
  the diagram renders whether the lib arrives before or after the DOM
  is parsed. Any render error from `mermaid.run()`'s promise is caught
  and surfaced in the same error UI.
- **The wrapper iframes get a real stylesheet** served from
  `/api/wrapper.css` (with the per-request nonce on the `<link>` tag,
  so the sandboxed iframe can load it even when Chrome refuses
  `style-src 'self'`). It matches the console's palette, honours
  `prefers-color-scheme`, caps `.mermaid svg` width to the iframe, and
  tightens the Save SVG button. One stylesheet backs all three
  wrappers — visuals, item chart, project map — so editing it changes
  every rendered diagram at once.

## Fix: Access blocked Mermaid + CSS subresources (0.9.7)

The chart iframes stayed as unrendered text and the new 0.9.6 styles did
not apply. Root cause: an iframe sandboxed with `sandbox="allow-scripts"`
but no `allow-same-origin` has an **opaque origin**, so its subresource
requests (`/api/mermaid.js`, `/api/wrapper.css`) go out WITHOUT the
parent's Cloudflare Access cookies. Access challenged every one of them,
the iframe could not follow the login redirect, and both loads died
silently. Hard refresh didn't help because the top-level wrapper HTML
loaded fine — it was its own navigation and carried the cookie.

- **The vendored `mermaid.min.js` and the wrapper CSS are now inlined**
  into the wrapper HTML (nonce-protected). No subresource requests, so
  no Access challenge. CSP tightens to `script-src 'nonce-...'` and
  `style-src 'nonce-...' 'unsafe-inline'` (the `'self'` legs were only
  useful for the subresource pattern that is gone).
- The response is ~3.3 MB per iframe (the lib dominates). The parsed JS
  is cached in-process (`_mermaid_js_bytes`), so disk is hit once per
  server lifetime, not per request.
- `/api/mermaid.js` and `/api/wrapper.css` endpoints stay for direct
  debugging and for any caller same-origin to the console.
- Defensive escape: `</script>` and `<!--` inside the inlined lib are
  replaced with their JS-safe forms; the current lib has neither, but a
  future bump might.

## Fix: Cross-Origin-Resource-Policy blocked everything served to a sandbox (0.9.8)

0.9.7's inlining dodged the subresource problem for Mermaid, but every
raw response (`_send_raw`: visuals, charts, mocks, the inlined wrapper
HTML itself) carried `Cross-Origin-Resource-Policy: same-origin`. A
sandbox-opaque-origin iframe is not same-origin to the server, so the
browser refused the response with a CORP violation — the same symptom
family as the Access-cookie trap: subresources silently fail to render.

- `_send_raw` now sets `Cross-Origin-Resource-Policy: cross-origin`.
  These responses are specifically designed to be consumed by sandboxed
  iframes with opaque origins; the data (static Mermaid wrapper HTML,
  inlined lib, inlined CSS, stored visuals the owner requested) is not
  sensitive, and the Cloudflare Access tunnel still gates every byte at
  the perimeter.
- JSON responses (`_send`) keep the default and remain protected: they
  carry the owner's view data and are only consumed by the same-origin
  top-level page.

## Dark theme fallback, loading bar, pan + zoom on charts (0.9.9)

- **`console.css` carries a fallback palette** inside `@layer
  console-fallbacks`, so a dashboard page that does not define
  `--c-fg`, `--c-surface`, `--c-border` etc. still renders the console
  with visible text, surfaces, borders and hovers. A
  `prefers-color-scheme: dark` block covers OS-dark users: before, the
  console panel on a dark host looked all black because every token
  resolved to transparent. The host page's own `:root` (unlayered)
  continues to win because unlayered rules beat layered rules in the
  cascade, so no project with its own tokens is affected.
- **A loading bar replaces the flash of unrendered Mermaid source.** The
  `.mermaid` element stays `visibility: hidden` until Mermaid flips it
  to `data-processed="true"`; a thin animated bar + "Rendering…" label
  sits in the viewport until `mermaid.run()` resolves, then hides.
- **Pan + zoom on every Mermaid wrapper.** The diagram lives inside a
  `.ck-stage` viewport with a `.ck-pan` transform wrapper. Mouse wheel
  zooms toward the cursor; drag pans; `+` / `−` / reset buttons sit in
  the wrapper bar. Reset returns to centred, scale 1. Clickable chart
  nodes still fire their click handler because the drag threshold is
  pointer-based and routes clicks through `click` directives first.
  Reduced-motion viewers get no animation on either the loading bar or
  the pan transition.

## Fix: a re-anchor proposal could never be confirmed when a kept excerpt went non-unique (0.9.10)

Reported from a user project (AB-findanchors/Q1, lock `3f1e5e31…`): a
ruling with two excerpts went stale because excerpt #1 was removed; the
steward proposed a new anchor for #1; `propose_anchor` kept excerpt #2
(still holds) in the proposal; Confirm 409'd forever because excerpt
#2's text had become non-unique in its file (3 occurrences). The owner
had no path to re-anchor and had to use `advise --star keep`.

- **`proposal_problem(anchors, tree, strict_excerpts=None)`** gains a
  `strict_excerpts` parameter. When set, exactly-once is enforced only
  on the excerpts in that list (the steward's newly cited anchors); any
  carried-over condition is checked by `tree.holds()` only, matching
  its lock-time invariant. `_confirm_check` computes
  `strict_excerpts = proposal.anchors - proposal.base`, so kept
  conditions can be ambiguous without blocking the confirm. The default
  (no `strict_excerpts`) preserves the old exactly-once-everywhere
  behaviour for other callers.
- **The owner now sees the 409 message in the UI**, not just a silent
  "409 conflict". `renderProposal`'s Confirm button shows the server's
  refusal in an inline alert below itself, in addition to announcing it.
  Styled with the kit's blocked tokens so it stands out against the
  normal confirmation box.

## Lane-board palette alignment and complete tag coverage (0.9.11)

0.9.9 shipped fallback tokens so the console rendered on a host page that
defined no `:root` custom properties. 0.9.11 brings the fallback values
in line with a mature lane-board palette (GitHub Primer family), adds
the tokens the lane board uses but the kit was missing, and completes
the tag styling for every state the view emits.

- **New / changed fallback tokens** (inside `@layer console-fallbacks`):
  `--c-bg` surface warmed to `#f4f6f8`; `--c-accent` → `#0969da`;
  `--c-open` → `#57606a` (muted gray, not accent blue — tags for an
  item in "proposed/open" no longer compete with the accent).
  **New**: `--c-deferred`, `--c-deferred-bg`, `--c-bar-bg`,
  `--c-bar-fill`, `--tree-line`. `--radius-sm` → `6px`, `--radius-md`
  → `10px`. Dark values follow the Primer dark theme. `color-scheme`
  declared on `:root` so native scrollbars and form controls match.
  A `:root[data-theme="dark"]` block honours an explicit toggle too.
- **Status chips now cover every state the view emits.** Previously
  only `awaiting_you / unlocked / locked / stale / agent_active /
  visual` had colour; `awaiting_agent / withdrawn / superseded`
  inherited the base chip look and were hard to tell apart.
  `awaiting_agent` picks up the accent-tint; `withdrawn` and
  `superseded` use the new `--c-deferred` pair.
- **The Mermaid wrapper CSS** mirrors the same palette (`--wrap-*`
  tokens), so an opened chart iframe looks consistent with the console
  panel in both light and dark.
- A host page's own `:root` still wins (unlayered rules beat layered),
  so projects that already define Primer-aligned tokens see no change;
  projects that define a different palette see their own values.

## Dashboard section links, Direction strip, immediate lock (0.9.12)

- **Dashboard section links.** `.overture.json` gains an optional
  `sections` map: `{"AB-2": ["#features/rollout", "#features/timeline"]}`.
  Each item id maps to one or more `#anchor` fragments on the dashboard
  page. The item view renders them as "Dashboard: #features/rollout →"
  chips that break out of the console frame to the dashboard page. Up
  to 16 anchors per item; schema refuses bad shapes by name at server
  start. The server does not resolve them against the page HTML — it is
  pass-through.
- **A `[data-ck-item="X"]` on the host page gets a live badge** injected
  by `console.js` on every live wake: `"X  ◐ 2  ◑ 1  ◌ 3  ○ 4"` (short
  tallies by state). Click the badge to open the panel on that item.
  The badge updates in place on each live wake.
- **Direction strip under each question's text.** Four kinds of chip,
  each wearing a direction glyph: `↑ from round <id>` (forked_from),
  `← supersedes <qid>` (what this ruling replaces), `→ superseded by
  <qid>` (what replaced it), `↳ <path>` (every file an evidence row
  cites, de-duplicated). The supersedes/superseded-by chips are
  clickable and scroll to that question's card in the panel. All
  chip data is already in the view JSON; no new endpoint.
- **The 5-second lock countdown is gone.** `Lock this answer` now sends
  immediately. The countdown apparatus (`renderLockCountdown`,
  `tickLock`, `setCountdown`, `undoLock`, `sendLock`, `pendingLocks`,
  `.ck-lock-countdown` CSS) is removed; `dropAllLocks` /
  `dropLocksNotOnShow` are no-op stubs so the existing pagehide /
  visibilitychange listeners don't need changing. Server-side
  lock-processing is unchanged.

## Auto-insert item sections into the dashboard tree (0.9.13)

- **`agent.py sync-dashboard --page <path>`** inserts a
  `<details id="item-X" data-ck-item="X"><summary>…</summary></details>`
  stub into the dashboard page for every item not already there, nested
  under its parent via per-item `<!-- ck:item X start/end -->` markers
  with a `<!-- ck:children X -->` insertion point inside each parent's
  `<details>`. Idempotent: re-runs only add new items, and hand-edits
  inside each node survive.
- **`agent.py items-push --sync-dashboard <path>`** chains the two
  steps so new lanes/phases/waves land on the dashboard in the same
  command as the push. The agent still commits + runs page-snapshot;
  no server-side mutation of the project.
- Items whose parent is not (yet) in the tree are inserted at the top
  of the ck:items block. Smoke-tested: 5-item tree with grandchildren,
  idempotent re-sync, add-child-under-existing-parent, hand-edit
  preservation, and orphan placement.

## Zoom-in further, drag anywhere, inbox default open, README updates (0.9.14)

- **Mermaid wrappers**: zoom clamp raised to 0.1x..20x so you can
  browse into a dense diagram. Drag now works anywhere in the stage
  (nodes included); a 5px click-vs-drag threshold means a tap on a
  node still fires its handler, and a drag that reaches the threshold
  swallows the next click so the pointer-up doesn't trigger a
  navigation you didn't ask for.
- **Inbox opens by default on docked (wide) screens** after the first
  view loads. Closing the panel once sticks for the session
  (`sessionStorage: ck-inbox-closed`).
- **README additions**: a "Name the session, bootstrap the dashboard"
  section with `/overture:as`, the full `scaffold-dashboard →
  sync-dashboard → page-snapshot → items-watch` chain, and the CSS
  custom-property surface for re-skinning. A new "Hooks the plugin
  ships" section documents `SessionStart` (the doorbell),
  `UserPromptSubmit` (`name_session.py`) and `PreToolUse`
  (`ask_guard.py`). Replaced the stale "undo for five seconds" bullet
  to match 0.9.12's immediate lock.

## Delegate — orchestrate from the Inbox (0.10.0)

The owner can already start a round, request a visual, or send a chat
from the item view. 0.10.0 makes the same primitives one click from
the Inbox header, so orchestration doesn't require opening an item
first. A collapsed **⚑ Delegate** bar above the tabs expands into a
quick form: pick kind (round / visual / chat), target item, mode +
focus (for rounds), and a free-text brief. Submits to the existing
`/api/message` endpoint — no new server routes.

This is step 1 of the "ultimate web orchestrator" arc. On deck:
0.11 playbooks (codified multi-step delegations), 0.12 triggers
(cron + webhooks that fire delegations), 0.13 cross-project overview,
0.14 impact graph (Cytoscape).

## Playbooks — codified multi-step delegations (0.11.0)

A playbook is `.overture/playbooks/<slug>.json` with a `description`
and an ordered list of `steps`. Each step is `{"kind": "round"|"visual"
|"chat", "item": "...", "text": "...", mode/focus (round only)}`.
Picked from the Delegate bar's "Run a playbook" option.

- **`playbooks.py`** loads, validates, caps (128 files, 24 steps,
  32 KiB). A malformed or badly-named file is skipped silently so one
  bad playbook never hides the rest. `view.playbooks` carries
  `[{name, description, steps}]`.
- **`POST /api/playbook`** fans each step out as an owner `message`
  write (same path the Delegate bar posts individually; same checks,
  same doorbell rings, same view updates). Owner-only. Returns
  `{records, skipped}` so the UI can report partial runs.
- **Delegate bar** gets a fourth option "Run a playbook" with a picker.
  Hidden/empty when no files are present (with a hint pointing at the
  folder). Submit calls the new endpoint and reports step-by-step
  skips if any item is missing from the register.
- No new agent-side work: a step is just an owner message with the
  usual intent (`fork` / `visual` / `chat`); the steward's `watch`
  picks each up in order.

## Webhook triggers — external events fire playbooks (0.12.0)

Codify an external event → playbook mapping in `.overture/triggers.json`:

    {"triggers": {"pr-merged": {"playbook": "security-review",
                                "token_sha256": "<sha256 of the bearer token, hex>"}}}

A caller sends `POST /api/trigger/pr-merged` with
`Authorization: Bearer <token>`. Two gates:

- **Cloudflare Access service token** at the perimeter (configured on the
  Access application).
- **Per-trigger bearer token** at the server: it hashes the received token
  with SHA-256 and compares (constant-time) to the configured
  `token_sha256`. The server **never** stores the plaintext token.

Rate-limited to one firing per trigger per 10 s (in-process). The firing
runs the trigger's named playbook (same path as the Delegate bar's "Run
a playbook"); `{records, skipped}` returns.

- **`agent.py trigger-token`** mints a `token` + its `sha256` so the
  operator can paste the sha into `triggers.json` and hand the plaintext
  to the sender.
- `view.triggers` carries the public shape (`name`, `playbook`) — never
  the token hash — so the UI can list what is wired.
- Smoke-tested: mint round-trip, header extraction, rate limiter,
  config validation for bad names / bad playbook / bad hash.

GitHub webhooks use HMAC-SHA256, not bearer tokens — a 10-line Worker or
small shim can bridge them (document in RUNBOOK). cron.io, cloud
functions, and custom scripts work out of the box.

On deck: 0.13 cross-project overview, 0.14 impact graph. Also 0.12.1 for
in-server cron if the webhook path proves solid.

## Portfolio — one view across every registered console (0.13.0)

- **`agent.py portfolio`** reads `~/.config/overture/projects.json`
  and, for each registered project, opens its `STATE/store.jsonl`
  directly. No HTTP calls, no running server required: it tallies
  open-questions-awaiting-you, unlocked, locked, waiting visuals, and
  the last-locked timestamp per project, then prints a compact table.
  `--json` emits the full rows for programmatic use.
- A single malformed project's state surfaces as `"(error: ...)"` on
  its row; the other projects still render.
- The one-server variant was always able to host many projects
  (`agent.py server add`); this makes the owner's cross-project
  backlog visible in one place without opening N browser tabs.
- Browser portfolio UI is deferred (per-project origins + Access
  cookies make a cross-origin aggregator thorny). The CLI covers 90 %
  of the "where should I spend my time" ask today.

## Impact graph — Cytoscape (0.14.0)

The fifth and last step on the orchestrator arc. Each item view gains an
**Impact graph** collapsible beside the Status flowchart. Nodes: the
item + its ancestors, every question on it, mapped dashboard sections,
cited files. Edges: `parent`, `fork`, `supersedes`, `in_section`,
`cites`. Click a node to highlight everything downstream; click again
to clear. Clicking a question or item node also opens that card in the
panel (same `postMessage` channel as the Mermaid charts).

- **`cytoscape.min.js`** vendored (~425 KB) alongside `mermaid.min.js`;
  both inlined into the wrapper response so the opaque-origin iframe
  does not need Access-authenticated subresources.
- **`impact.py`** builds the Cytoscape-shaped graph; capped at 120
  nodes.
- **`GET /api/impact-graph?item=X`** returns an HTML wrapper with the
  lib + JSON data + a self-contained init. CSP + sandbox match the
  Mermaid wrapper exactly.
- Default layout is `breadthfirst, directed`; wheel-zoom clamped
  0.1x–4x. Drag nodes freely; the layout keeps edges live.

## Cron triggers — the clock fires playbooks (0.15.0)

Companion to 0.12.0's webhooks: triggers can now carry a `cron`
expression and fire on the server's clock, with no external POST.

    {"triggers": {"monday-triage": {"playbook": "inbox-triage",
                                    "cron": "0 9 * * MON"}}}

- 5-field cron: `minute hour day-of-month month day-of-week`. Accepts
  `*`, `a-b`, `a,b,c`, `a-b/n`, `*/n`. Named months (`JAN..DEC`) and
  days-of-week (`SUN..SAT`); 7 is a Sunday alias.
- A background thread sleeps until the top of each minute (+ 0.2 s so
  firings don't race the boundary); fires every matching trigger exactly
  once per (name, minute). A clock jump backward doesn't double-fire;
  a `run_playbook` nonce derived from `name + minute` makes the server
  side idempotent too.
- A trigger can carry `token_sha256` (webhook), `cron` (schedule), or
  both. A webhook POST to a cron-only trigger returns 405 with "cron-
  only; it fires on its schedule".
- `view.triggers[*].kinds` lists `["webhook"]`, `["cron"]`, or both
  so the UI can tell them apart. The cron expression itself stays
  server-side.
- Smoke-tested: 6 valid specs parse, 6 bad shapes refuse by name,
  Mon/Sun/Sat alignments, step matching, cron-only config loads,
  trigger-with-neither refuses the start.

The orchestrator arc now folds the clock in: external HTTP, scheduled
tick, or owner click all share the same playbook execution path.

## Trigger log in the Inbox (0.16.0)

Without visibility, webhook and cron firings felt opaque. 0.16.0 shows
them in the Inbox under a **Triggers** collapsible beside Delegate:

- The configured triggers (name → playbook, with its kinds: webhook,
  cron, or both).
- The last 20 firings, newest first: timestamp, name, kind, step and
  skip counts, and any error.

The server keeps the last 100 firings in an in-process ring; older
ones drop off on restart. `view.trigger_log` carries the slice;
`view.triggers[*].kinds` carries webhook/cron so the row says which
flavour the trigger is armed for.

Both `fire_trigger` and `fire_cron` wrap the playbook call in a
try/except and log the outcome before re-raising, so a failing playbook
still writes a log row with the error.

## Impact graph filter chips (0.17.0)

Dense impact graphs got hard to read. A chip row above the Cytoscape
canvas now toggles visibility by:

- **Kind**: item, question, round, section, file.
- **State** (applied only to question nodes): awaiting_you, unlocked,
  locked, stale.

An empty selection in a group means "all shown"; selecting one or more
narrows the group to just those. Multi-select across groups is AND
(matching nodes in both). A **reset** chip clears everything. Hidden
nodes drag their edges along via a `.ck-hidden` display:none rule.

All filter state lives inside the sandboxed iframe; nothing round-trips
to the server. The hint ("drag · wheel to zoom · click to highlight
downstream") moved to the bottom of the stage so the chip row has
breathing room.

## `/overture:playbook` — run a playbook from any Claude session (0.18.0)

The browser was the only way to run a playbook; now any Claude session
on the project's machine can.

- New plugin skill **`playbook`** (`/overture:playbook <slug>`): the
  agent reads `view.playbooks`, confirms the slug and the step count,
  dispatches, and reports `{records, skipped}` from the server.
- New CLI **`agent.py playbook <name>`** POSTs through the console's
  agent socket.
- New agent-door route **`POST /playbook`** calls the same
  `run_playbook` as the owner door. The agent socket is a user-only
  Unix socket (0600 in a 0700 dir) — the same trust surface
  `items-push`, `prs-push`, and `page-snapshot` already use for
  owner-privileged writes.
- The nonce is minted from `playbook-slug + current second` so a retry
  within the same second dedupes on the server. Slash-command runs are
  not currently logged in the trigger log (that stays scoped to
  webhook + cron firings — slash-command dispatch is closer to a
  Delegate-bar click).

## Browser Portfolio (0.19.0)

For an owner across several projects, one browser tab now shows every
console at a glance and alerts the OS when a sibling needs them.

- **Portfolio tab** between **Favorite** and **Chat**: one card per
  configured peer plus the local console, with the state tallies
  (`?you ~unl !stale ○lock`, plus `◫vis` when a visual is waiting),
  relative last-activity, and a click that opens that peer's console
  in a new tab. Peers that are unreachable show the error on the card
  rather than disappearing.
- **Desktop notifications (opt-in)**: a bell button asks the browser
  for Notification permission; thereafter a peer's `?you` or `!stale`
  going up fires one OS notification per peer, deduped at 60 s, with
  click-to-open. A Snooze button sets a 1-hour DND lid, so a push
  during off-hours stays silent.
- **Portfolio tab note**: `N ?you` sums the `awaiting_you` across all
  peers plus self, so the roll-up is visible from any other tab.
- **How peers talk to the home server**: `.overture/portfolio.json`
  committed in the repo lists each peer's URL, Cloudflare Access
  service-token `client_id`, and the **sha256** of its secret. The
  plaintext secret lives in `STATE/portfolio-secrets/<peer>.json`
  (0600, out of the repo). The home server reads it, hashes it,
  compares, and uses the pair to call each peer's new
  `GET /api/portfolio-slim` (owner-gated, Access-authed) through the
  owner's own `GET /api/portfolio`, which the browser then renders. A
  rotated secret whose sha drifts from the committed config fails
  loudly rather than silently. Peer rows carry only counts and
  timestamps; no owner view content leaves a peer.
- **New CLI** `agent.py portfolio-token` prints the two-file layout
  the owner fills in by hand.
- **Caching + timeouts**: peer fetches are capped at 4 s and cached
  in-process for 5 s, so one slow peer never blocks the aggregator;
  the browser also polls softly (30 s) while the tab is open.

## Playbook branching with `when` predicates (0.20.0)

A playbook step may now carry an optional **`when`** object. The
server evaluates it against the current view and item register at the
start of the firing; a step whose `when` does not pass is skipped with
`{index, why: "when:<reason>"}`, and the rest still run as owner
messages. One playbook can now be the "backlog triage" for every
state the project might be in.

The predicate set is closed — no eval, no user expressions, no
file-system or network reach — so a bad config fails at load, not at a
firing:

- `project_has_state` — the project has at least `min` questions in
  one of `awaiting_you`, `unlocked`, `locked`, `stale`.
- `item_has_state` — the same, scoped to one item id.
- `item_exists` — a specific item is in the register.
- `has_section` — a `.overture.json` section is configured.
- `not` — negates another predicate (nested up to three levels so a
  typo cannot build a loop).

Example — a release-safety playbook that only drafts the chat
heads-up when there is unanswered work:

    {
      "description": "Pre-release triage.",
      "steps": [
        {"kind": "chat", "text": "Any blockers in the air?",
         "when": {"kind": "project_has_state", "state": "awaiting_you", "min": 1}},
        {"kind": "round", "item": "RELEASE-7", "mode": "tighten",
         "text": "Confirm the rollout gates.",
         "when": {"kind": "not", "of": {"kind": "project_has_state", "state": "awaiting_you", "min": 1}}}
      ]
    }

A trigger (webhook or cron) and the Delegate bar and the
`/overture:playbook` slash command all go through the same
`run_playbook`, so the `when` behaviour is the same wherever a
playbook fires. The browser's Delegate bar shows which steps were
skipped in the firing's result row, with the predicate reason.

## Keyboard shortcuts (0.21.0)

An owner across many consoles needs to move fast. Every page now
carries a global keyboard layer; `?` prints the full list in a
modal overlay:

- **`g i / f / p / s / o / c`** — Inbox, Feed, PRs, Favorite, Portfolio, Chat.
- **`g b`** — close the panel (back to the board).
- **`.`** — focus the Delegate bar (opens it if collapsed).
- **`j / k`** — next / previous focusable row inside the panel.
- **Enter / Space** on a focused row — open it (same as click).
- **`?`** — show the help overlay; **Esc** closes it.

Rules the layer follows:

- Never fires in a text input, textarea, select, or
  `contenteditable` — so typing into a comment never hijacks a
  letter.
- `g` is a sticky prefix for 1500 ms; a second key completes the
  pair, anything else cancels it.
- Modifier keys (Ctrl / Meta / Alt) suppress every shortcut, so a
  browser-level binding wins without conflict.

No server changes; the whole layer is in `console.js` and `console.css`.

## Command palette (0.22.0)

**Ctrl+K / Cmd+K** on any page opens a search box over every surface
the console knows about. One keystroke, type a few characters, press
Enter.

- **Items** — type an id or title fragment; Enter opens the item panel.
- **Questions** — their qid plus the first slice of the text; Enter
  opens the owning item.
- **Playbooks** — their slug plus description or step count; Enter runs
  the playbook (same path as the Delegate bar; `when` predicates still
  gate each step).
- **Peers** — portfolio cards by their project name; Enter opens the
  peer's console in a new tab.
- **Tabs** — hop to Inbox, Feed, PRs, Favorite, Portfolio, Chat.
- **Actions** — close the panel, open this help, focus the Delegate
  bar (same bindings as the shortcut layer, so the palette is a
  mouse-and-touch alternative).

The filter is a case-insensitive subsequence match ranked by how
early the query lands in the label; ties break by kind priority
(item → question → playbook → peer → tab → action) so typing an id
wins. The palette is browser-only: no new server routes, nothing
sent on a filter — only one `/api/playbook` POST when you Enter a
playbook row (the same call the Delegate bar makes).

↑/↓ walk the results, Enter runs the selected row, Esc or a click on
the backdrop closes.

## Portfolio peek + status LED (0.23.0)

A peer card now carries real triage context, not just counts, so the
owner can see what is awaiting across every project without opening
every tab.

- **Peek**: the top three `awaiting_you` questions per peer, newest
  first — qid plus the first 60 characters of the question text and
  a relative time. The server includes it in `/api/portfolio-slim`,
  and the aggregator shapes it defensively (bounded to 3 entries,
  120 chars each) before letting it reach the home browser.
- **Status LED** per card:
  - **green** — nothing awaiting;
  - **yellow** — awaiting under an hour;
  - **orange** — awaiting over an hour;
  - **red** — awaiting over 24 hours;
  - **grey/red-outline** — the peer is unreachable.
  The dot's title gives the oldest-awaiting age.
- The peek is `peek: [{qid, item, ts, text}]` on the slim shape; the
  text is already the owner's view, so showing it here is the same
  trust boundary as the question itself. The home console still
  never sees locked answers or chat content.

## Cross-project priority ribbon (0.24.0)

The Inbox tab now opens with a ribbon of the oldest awaiting
questions across every console (self + each portfolio peer). One
glance says what to touch first.

- Up to six rows, sorted by `ts` (oldest first = most urgent).
- Each row: a traffic-light dot (yellow < 1 h, orange < 24 h, red
  > 24 h), the project name, the qid, a 60-char slice of the
  question text, and a relative time.
- Click a row — if it's local, the item panel opens; if it's a
  peer, that peer's console opens in a new tab.
- The summary line names the total and the number of projects
  spanned ("4 of 11 awaiting you across 3 projects"), so the
  owner can tell at a glance whether a backlog is one project or
  many.
- The ribbon self-starts the portfolio fetch: an owner who never
  opens the Portfolio tab still gets the cross-project view the
  first time they open the Inbox. Peer data flows through the
  same aggregator (5 s cache, 4 s timeout), so one slow peer
  never slows the ribbon.
- Nothing to show when every project is quiet; the ribbon
  simply does not render.

## Deep links + Copy link (0.25.0)

The URL hash now activates one view on load, and the hash
updates as the owner navigates:

- `#inbox | #feed | #prs | #favorite | #portfolio | #chat` — open
  the panel on that tab.
- `#item=<id>` — open the item panel.
- `#qid=<id>/Q<n>` — open the owning item.

A deep-link in the URL beats the "open the Inbox by default"
rule, so a shared bookmark lands the receiver exactly where the
sender was. `history.replaceState` is used (not pushState) so a
tab-hop isn't a navigation entry; `hashchange` keeps the view in
sync when the owner edits the bar or hits Back.

The command palette gained **Alt+Enter** on any row: copies a
shareable link to the clipboard — a `#item=…` or `#qid=…`
fragment for local rows, a `#<tab>` for tabs, or the peer's URL
for a portfolio row. The shortcut-help overlay lists this.

Nothing crosses to another browser or another owner — the hash is
only interpreted inside the same console. Peers still require
their own Access login, same as before.

## Snooze a question (0.26.0)

Not every awaiting question is answerable right now. A small ⌛
button on each Priority-ribbon row offers three snooze presets:

- **1h** — one hour from now.
- **til tmrw** — tomorrow at 9am local time.
- **1w** — one week from now.

A snoozed question leaves the Priority ribbon immediately and
reappears when its time comes. The Inbox tab bottom carries a
collapsible **Snoozed · N** section listing every live snooze
with its wake time and a one-click **Unsnooze** button.

Snoozes live in this browser's `localStorage` keyed by
`<project>#<qid>`, so a snooze made in one window does not reach
another and a private window may refuse it — the UX works
without storage. Expired entries are pruned on read; no server
work is involved.

## Trigger replay (0.27.0)

The Triggers section of the Inbox now carries a **↻ Replay**
button on every configured trigger and on every past firing in
the log (as long as the trigger still exists).

- One click POSTs `/api/trigger-replay` with a fresh nonce; the
  server runs the trigger's playbook via the same path
  `fire_cron` and `fire_trigger` already use.
- The firing is appended to the trigger log with
  `kind: "replay"`, so an audit shows manual replays alongside
  webhook and cron firings.
- Replay is owner-only (gated by the Access-authed owner door)
  and bypasses the webhook token + rate limit — the owner
  already proved themselves.
- A playbook step that no longer passes its `when` predicate is
  skipped as usual; the Delegate-bar-style surface shows ran vs.
  skipped counts, with the first skip's reason.

## Export today's rulings as Markdown (0.28.0)

The Feed tab's filter row gains a **⤓ Export today** button that
opens a modal with every ruling locked since local midnight as
Markdown, ready to paste into a standup, a weekly review or a
sprint retro.

Shape of the output:

    # Rulings — 2026-10-08

    From <project> · N locks across M items.

    ## AB-7 · <item title>

    - **AB-7/Q1** — 09:14 UTC
    - **AB-7/Q2** (re-lock) — 11:02 UTC

    ## XX-9 · …

- Browser-only: reads `/api/feed?kind=lock&limit=200`, filters by
  `ts >= start-of-today-local`, groups by item, sorts within an
  item by time.
- The modal has **Copy** (clipboard write; falls back to selecting
  the text if the browser refuses), **Close**, and the standard
  Esc / backdrop close.
- Nothing is sent anywhere else — the generated Markdown stays in
  this browser until the owner pastes it.

## Overture — the 1.0 rename (1.0.0)

**console-kit is now Overture.** The product name, the GitHub repo,
the plugin manifest, the Python package, the slash-command prefix,
the service file, and the on-disk paths all rename at once.
Semantically nothing else changes in 1.0 — every feature from 0.9.6
through 0.28.0 keeps working as it did, under new names.

- **GitHub:** `MikeHeid/console-kit` → `MikeHeid/overture`.
- **Plugin manifest:** `name: overture` (slash commands now
  `/overture:*`, e.g. `/overture:playbook`, `/overture:as`).
- **Python package:** `console_kit` → `overture`.
- **Env vars:** `CONSOLE_KIT_*` → `OVERTURE_*`.
- **Config paths:** `.console-kit.json` → `.overture.json`;
  `.console-kit/` → `.overture/` (playbooks, triggers,
  portfolio.json live under there).
- **State / share dirs:**
  `~/.config/console-kit/`     → `~/.config/overture/`,
  `~/.local/share/console-kit/`→ `~/.local/share/overture/`,
  `~/.local/state/console-kit/`→ `~/.local/state/overture/`.
- **Service / socket:** `console-kit.service` → `overture.service`;
  `console-kit-agent.sock` → `overture-agent.sock`.

**Register-after-upgrade reminder.** A new SessionStart hook,
`install_notice.py`, prints an ASCII Overture banner and the
agent-registration commands exactly once per installed version,
then writes a per-version flag so it stays silent on later
sessions. The flag path encodes the version, so the next upgrade
fires it again. Linux/macOS/WSL users get the direct `agent.py
register` + `systemctl --user start overture.service` pair;
Windows users get the WSL-wrapped variants (`wsl -d Ubuntu-24.04
-- …`), because the server itself only runs on POSIX.

**Upgrading from console-kit 0.28.0 or earlier.** The 1.0
rename is deliberate; it needs a fresh registration. After the
upgrade, run (Linux/macOS/WSL):

    python3 ~/.local/share/overture/kit/plugin/kit/agent.py register

Then start the user service:

    systemctl --user start overture.service

The hook will remind you if you forget — once per installed
version, and it goes quiet after.

## Inbox filter chips (1.1.0)

The Inbox tab opens with a row of four toggle chips above the
sections, so a dense inbox can be trimmed to what you want to look
at right now:

- **? you** — rounds + loose questions awaiting the owner.
- **~ unl** — questions with a draft answer that is not locked yet.
- **! stale** — rulings whose cited evidence has moved.
- **○ lock** — the "Recently answered" footers that nest under each item.

All chips are on by default. Each chip's state is held in this
project's `localStorage`, so the choice sticks per project and
per browser without ever reaching the server. Chips mirror the
Portfolio-card tally glyphs and the Priority ribbon's compact
header, so one visual language carries the whole triage layer.

## Theme toggle (1.2.0)

A compact theme button in every panel header cycles **auto → light
→ dark**:

- `◐ auto` — follows the OS `prefers-color-scheme` (the default).
- `☀ light` — pins the light Primer palette.
- `☾ dark` — pins the dark Primer palette.

The CSS already carried both palettes; the toggle adds the user
override. Choice is held in this project's `localStorage` and
applied by setting `document.documentElement.dataset.theme`, which
the `console-fallbacks` layer keys off. A screen reader hears the
new state (`announce` fires on cycle); the button's `title`
tooltip names the current mode.

## Playbook preview (1.3.0)

Before firing a playbook, see exactly what will happen. A new
**Preview** button in the Delegate bar (visible when the Delegate
form is in `playbook` mode) and a **⌕** button next to each
configured trigger's Replay open a modal that lists:

- Every step with its kind, item, and the first 80 chars of its text.
- Whether the step **would run** (▶ green) or **would skip** (▢
  muted), with the exact reason — a failing `when` predicate, a
  missing item id, etc.
- A header line summarising "would run N of M steps".
- A **Run now** button that fires through the same path as the
  Delegate bar; **Close** / Esc / backdrop closes.

A new owner-only route `GET /api/playbook-plan?name=<slug>`
evaluates the predicates against the current view and item
register and writes nothing. It never fires the playbook; a
"Preview" is always safe to click. The route is route-gated like
the owner door, so a Preview without authentication fails the
same way as `/api/view`.

## Orchestration tree: section field on items (1.4.0)

An item in `items.json` can now carry an optional **`section`**
field — a slash-separated hierarchical slug like
`wave-1/phase-2/lane-ui`. The scaffold materialises every path
segment as a nested `<details data-ck-section="…">` on the
dashboard, and a new item drops into its already-visible
wave/phase/lane node without the owner editing the page.

- **Schema:** `section` is validated at push time:
  up to 5 slash-separated slug segments, each 1–64 chars from
  `[a-z0-9][a-z0-9._-]*`. A bad shape refuses the push with the
  offending segment named.
- **Scaffold:** `sync_items_block` creates missing section nodes
  in order (shallowest first) so each new item sees its full
  parent chain. Hand-edits inside each `<details data-ck-section>`
  are preserved across re-syncs — only the content inside
  `<!-- ck:section-children slug -->` markers is written.
- **Parent still wins:** an item with a `parent` already on the
  page nests under the parent; otherwise it drops into its
  section. Items with neither fall to the top of the items block,
  so no item ever disappears.
- **Order:** parents before children, roots sorted by section
  depth, so a push of 40 items at once produces the right tree in
  one pass.
- **Adapter note:** `plugin/kit/docs/ADAPTER.md` documents the
  new optional field; existing adapters keep working — `section`
  defaults to absent.

The dashboard scaffold ships enough markup for a project's own
CSS to style sections however it likes; data attributes
(`data-ck-section`) make the tree selectable without parsing the
slug.

## `/overture:add-skill` — scaffold a new skill (1.5.0)

A new slash command: **`/overture:add-skill <skill-name> [category]`**
walks you through creating a Claude Code skill end-to-end.

The flow:

1. **Name** — the first argument is the slug (`[a-z0-9][a-z0-9._-]*`,
   up to 64 chars). A missing or malformed slug is refused; a slug
   that already exists is refused rather than overwritten.
2. **Category** — if the second argument is absent or not one of the
   four known values, the skill prompts with `AskUserQuestion`:
   - **agent-review** — reviewer-style skill (code / design / security).
   - **visualizer** — Mermaid or HTML-mock skill.
   - **documentation** — doc-writing skill.
   - **other** — generic skill.
3. **Target** — picks `plugin/skills/<slug>/SKILL.md` when the CWD is
   the Overture plugin repo itself (there is a `plugin/skills/` next
   door); otherwise `.claude/skills/<slug>/SKILL.md` under the
   project.
4. **Scaffold** — writes a `SKILL.md` with the category-appropriate
   template: frontmatter (name, description, argument-hint,
   disable-model-invocation) plus a body stub for the chosen kind.
5. **Confirm** — tells you the path, the category, and that the
   file is a starting point.

The skill touches nothing else — no git, no install, no plugin
reload. A fresh skill becomes available next session (or on next
plugin install when the file landed under `plugin/skills/`).

## Fragment buttons + viewer modal (1.6.0)

When an agent's reply (chat, answer note, fork message, round
message, or message card) contains a **fenced code block**, the
block renders as a compact **📄 button** instead of inline
walls-of-code; clicking opens a monospace viewer modal with
**Copy** + **Close**.

Any language works: `md`, `txt`, `py`, `go`, `rs`, `js`, `ts`,
`json`, `yaml`, `html`, `css`, `sh`, `sql`, … — the button icon
picks a per-language glyph; the lang defaults to `text` when none
is given. A fence may name a file: `` ```md:plan.md `` makes the
button title `plan.md`.

- A **named fence** (`` ```lang:name.ext ``) is always a
  fragment, regardless of length.
- An **anonymous fence** must be ≥ 40 characters so a 10-char
  inline snippet does not turn into a button.
- The viewer modal shows the raw content preformatted (no
  rendering); Copy writes to the clipboard with a
  select-fallback. Esc / backdrop closes.
- Applied uniformly to every surface where agent text renders:
  Chat log, round messages, follow-up messages, locked-answer
  notes, and the sheet view.

## Onboarding + docs cleanup (1.6.1)

Three bug fixes and a doc pass surfaced from a Windows + WSL2
install review:

- **`agent.py scaffold-dashboard --name "Project Name With Spaces"`**
  no longer refuses with "steward: agent name '…' is refused". The
  argument now writes to `project_name_display` instead of `name`,
  so the shared agent-name validator doesn't touch it. `--name` is
  kept as a hidden alias — scripts that use the old flag still
  work.
- **Onboarding auto-adds `.overture/console.env` to `.gitignore`**
  when it is not already listed. The file carries the AUD tag and
  team domain — not secret by the kit's account, but shaped like
  credentials, so Claude Code's safety check used to block the
  commit mid-onboarding. Idempotent; swallows OSErrors into a
  REVIEW line.
- **`ADAPTER.md`** updated: the "The page" section now says the
  server serves the `page-snapshot` from STATE, not
  `CONSOLE_PAGE`; the legacy flag has been ignored since the Q28
  ruling.
- **`INSTALL.md`** gained a WSL2 note (`loginctl enable-linger
  $USER`) and a one-liner about SessionStart hooks loading only in
  a new session, so an owner who installs mid-session knows to
  reopen Claude.

## Numbered item refs — phase 1 (1.7.0)

Items can now carry a **dotted-number ref** like `1`, `1.2`,
`1.2.1`. The owner uses these in chat, on calls and in notes to
point at specific work. The kit owns their assignment, so
numbers do not drift or collide across pushes.

### Fields

- **`ref`** — optional in `items.json`. Dotted non-zero segments,
  `^[1-9][0-9]*(\.[1-9][0-9]*)*$`. The ref must sit exactly one
  segment under its parent's ref (`1.2` under `1`); a top-level
  item has a single-segment ref.
- **`kind`** — `"topic"` | `"item"` (default `"item"`). A topic may
  have no parent even when `require_parent` is on.

### `.overture.json` options

    "items": {
      "auto_ref": true,          // default true — assign missing refs on items-push
      "require_parent": false    // default false — refuse an item with no parent and no "topic"
    }

### Behaviour

- **Store owns the numbers.** `STATE/refs.json` carries
  `{"assignments": {id: ref}, "retired": {ref: last-id}}`. On every
  `items-push`:
  - Items with a stored ref keep it; a push that disagrees refuses
    the whole snapshot with the stored value in the message.
  - Items with no stored ref AND `auto_ref` on get the next free
    number under their parent (walking parents-first so each
    child sees its parent's ref).
  - Items that have left the register move to `retired`;
    retired numbers are **never reused**.
- **Returned to the caller.** `/api/items-push` adds a `refs`
  object to its 200 so the pushing agent sees every assigned ref.
- **Everywhere in the view.** Each item in the live payload
  carries its ref in `items[id].ref`, so a UI update can show
  `1.1 · <title>` beside the id without touching the view
  pipeline.
- **No cycle checks, no `item-move` yet.** Those come in 1.8.0
  along with the browser surfaces (ref beside each title in the
  Inbox, Priority ribbon and command palette).

## Numbered item refs — phase 2 (1.8.0)

Browser surfaces for the refs that 1.7.0 taught the kit to assign.

- **Ref pill beside every item id.** New visual rows, cluster
  cards, loose rows, favorites item headers all show the item's
  ref as a small accent-coloured pill (`1.2`) before the id.
  Items without a ref (older project, auto_ref off) render as
  before — the pill is omitted, not blank.
- **Command palette shows the ref.** An item's row label now
  starts with its ref: `1.2 · AB-2 · <title>`.
- **Palette search by ref.** Typing a dotted-number query (`1.2`)
  treats it as a ref lookup: exact matches rank first,
  descendants of a prefix (`1` matches `1.1`, `1.2`, …) rank
  next. Non-ref queries still filter by label as before.
- Browser-only; no server changes beyond what 1.7.0 already
  carries. `item-move` (the move-vs-alias open question) stays
  deferred.

## Items tree as a live value (1.9.0)

The scaffold page gets a kit-computed, always-current tree of
every item — no `board()` code needed.

- **Server-side.** `Console.board()` now injects an
  `items_tree` key into whatever the project's adapter pushed as
  `values`, unless the project already set that key. The tree is
  plain text, one item per line (`<ref> <title>`), indented two
  spaces per depth, sorted by ref (`1.10` after `1.2`, items
  without a ref last). Orphans whose parent left the register
  show at root.
- **Page.** New scaffolds get a single `<pre
  class="scaf-items-tree" data-live="items_tree">` block in
  place of the old `items_rows` HTML table (which never worked —
  `data-live` sets `textContent`, not inner HTML; brief 1 #17
  closed).
- **Backward compat.** Existing scaffolded pages keep whatever
  markup they have; the `items_tree` value is only applied where
  `data-live="items_tree"` exists. A project that already pushes
  `items_tree` in its own `board()` is untouched.
- **No new routes, no new fields.** The tree is derived at
  render time from `items.json`'s existing shape; no snapshot
  change needed.

## Clickable refs in agent text (1.10.0)

A dotted-number mention like `1.2` or `1.2.1` in agent-authored
text (chat replies, round messages, follow-ups, locked notes,
sheet view, cluster cards) now renders as a clickable pill that
opens that item.

- **Only real refs link.** The browser builds a reverse map
  `ref → id` from the current items view and matches on exact
  equality. A dotted number that nobody has as a ref stays
  plain.
- **Guards against false positives.** A character class
  blocks the lookbehind/lookahead when the token is adjacent to
  letters, digits, dot, underscore, hyphen, or slash — so
  `AB-1`, `x_1.2`, `0.9.3`, `path/1.2`, `1.2.html` never
  wrongly link.
- **Keyboard reachable.** The pill is a real `<button>`;
  Tab-focus and Enter open the item like any other focusable
  row.
- Browser-only; same `renderAgentText` entry point as the
  fragment viewer, so every surface that already shows agent
  text picks this up automatically.

## Scaffold append sets `data-live-shape` (1.11.0)

A scaffold run that appends sections to an existing page now
patches the page's `<body>` tag so the live loop can find it.

- Before: `_page_write` only wrote `<body data-live-shape="…">`
  on a brand-new page. Appending sections to a hand-made page
  left the attribute off, so none of the `data-live="…"`
  elements updated. (Brief 1 #16.)
- Now: `_needs_body_shape` / `_add_body_shape` detect a `<body>`
  without the attribute and inject it next to the existing
  attributes. Idempotent (a page that already has the attr is
  untouched). Case-insensitive — `<BODY class="x">` works too.
- Pages that already carry a different `data-live-shape` are
  left alone (so a project with its own shape is not
  overwritten). Pages with no `<body>` tag are left alone (the
  scaffold is additive, not restorative).

## Topic status rollup in `items_tree` (1.12.0)

Each line of the `items_tree` live value now carries a
rolled-up count tail:

    1 Rollout
      1.1 Rollout plan · ?2 !1
      1.2 Deploy script · ~1
    2 Audit
      2.1 Security pass · ?1

- Three states are counted: `?` = awaiting_you, `~` = unlocked,
  `!` = stale.
- Each item's tail is the **sum of its own open questions plus
  every descendant's**, so a topic shows at a glance what sits
  under it.
- Zero segments are omitted; an item with no open work shows no
  tail. Locked and withdrawn states are not counted (the tree
  is a now-what view, not a history).
- Browser-only consumers see the richer text via the same
  `<pre data-live="items_tree">` block — no page change needed.
- Projects that compute their own `items_tree` in `board()` are
  still unaffected: the kit only writes when the project did
  not.

## Markdown rendering for `md` fragments (1.13.0)

The fragment viewer now **renders** Markdown fragments
(`md` / `markdown` / `mkd` / `mdown`) with real headings,
lists, bold, italic, inline code, blockquotes, horizontal
rules, and `[text](https://…)` links. A **Raw** button toggles
to the preformatted source; other-language fragments (`py`,
`js`, `json`, …) still open preformatted.

- Hand-rolled renderer, no library. Every element is built
  with `document.createElement`; **no HTML pass-through**, so
  a `<script>` or `<img onerror>` in the body cannot inject.
- Supported block structures: `# ## ### #### ##### ######`,
  `- * +` lists, `1. 2.` lists, `>` blockquotes, `---` rules,
  ``` ``` ``` fenced code blocks, paragraphs that fold adjacent
  lines.
- Supported inline: `` `code` ``, `**bold**` / `__bold__`,
  `*italic*` / `_italic_`, `[label](url)` (http(s) only — other
  schemes render as literal text).
- The viewer defaults to **rendered** for Markdown and raw for
  everything else; the toggle re-paints the same body container.
- Copy still copies the **raw** body regardless of what's shown.

## Web skins (1.14.0)

Three palette overlays beyond the Primer default — the owner
cycles the skin independently of the auto/light/dark theme and
each choice persists per project.

- **Primer** (default) — the existing GitHub-aligned palette.
- **Solarized** — warm, cream/mustard base with teal / rust
  accents. Light + dark variants.
- **Nord** — cool blue-grey base with muted accents. Light +
  dark variants.
- **Contrast** — accessibility-forward: pure black-on-white
  (or white-on-black) with saturated accents.

Mechanism:

- `document.documentElement.dataset.skin` is set to the chosen
  key (`solarized` / `nord` / `contrast`); unset for Primer.
- CSS keys off `:root[data-skin="…"]` with the same
  `prefers-color-scheme: dark` + `:root[data-theme="dark"]`
  pattern as the base palette.
- A new `◉`/`S`/`N`/`C` button in the panel header cycles the
  skins; the theme (sun/moon/auto) stays its own button
  alongside.
- Choice persists in `localStorage` as `skin`; it reaches
  nothing outside the browser.

## PR title ref linking (1.15.0)

The PRs tab's "Open" buttons already match **item ids** and
**qids** found in a PR's title or branch; now **dotted-number
refs** in those same strings also become open-item buttons.

- A PR titled "Refactor 1.2 to use typed deps" now surfaces a
  button labelled `1.2 · AB-2` that opens item AB-2 (whose ref
  is `1.2`).
- Guards are the same as 1.10.0's clickable refs in agent
  text: lookbehind + lookahead block adjacent
  letters/digits/dot/underscore/hyphen/slash, so a PR title
  mentioning `v0.9.3` or `/path/1.2.html` never wrongly links.
- Honours the existing `PR_MAX_LINKS` cap and de-duplicates
  against id/qid matches, so a title that mentions both an id
  and its ref produces one button, not two.
- Browser-only; no server changes.

## "What's new since last look" banner (1.16.0)

The Inbox tab opens with a dismissible banner summarising what
has arrived since the panel opened — questions, answers,
visuals, chat — so the owner sees the delta without scanning
every section.

- Counts the four event classes from `view.questions`,
  `view.visuals`, `view.threads` against the `seenAtOpen`
  baseline (frozen at the moment the panel last opened), not
  the live `seen` cursor — so arrivals during the current
  reading stay in the banner.
- One click on **✓ Caught up** advances both the persisted
  `seen` and the panel baseline, so the banner stays dismissed
  until something arrives next.
- Rendered inside the Inbox tab, above the Priority ribbon;
  hidden when nothing is new.
- Browser-only; no server changes.

## `agent.py item-move` — re-parent intent (1.17.0)

Closes the remaining open piece from the numbered-refs brief
(Brief 2 §4.3) with the simpler semantics: **the ref stays, the
move is a signal the adapter picks up next.**

### New owner CLI

    agent.py item-move AB-5 --to AB-2         # move AB-5 under AB-2
    agent.py item-move XX-9 --to-root         # move XX-9 to top level
    agent.py item-move AB-5 --to AB-2 --text "needed under the new epic"

### What writes

- A `message` record on the item's thread with the new intent
  **`move`** and a `move_to` field (target parent id, or `null`
  for root).
- The project's adapter stays the source of truth for parents;
  the move is a documented intent, not an override.
- The message surfaces in the item's thread and the Feed; the
  steward picks it up on next watch.

### Guards

- Target parent must exist in the register (`404` otherwise).
- A move that would form a cycle through pushed parents is
  refused by name.
- An item moving to itself is refused.
- The chat thread never accepts `intent: move` (the schema
  check named-refuses the mismatch).
- Fields validated at the schema layer: `move_to` must be an
  item id or `null`; the fork-only fields (`focus`, `mode`,
  `roles`, …) stay refused on a move; a move on CHAT_ITEM is
  refused.

### Trust surface

- The agent-door `/item-move` route is user-only (0600 Unix
  socket in a 0700 dir) — same trust surface as `items-push`,
  `prs-push`, `page-snapshot`, `playbook`.
- The owner-door `/api/item-move` is Access-authed like every
  other owner route.

## Palette full-text search (1.18.0)

The command palette (`Ctrl+K` / `Cmd+K`) now searches **inside**
item titles, question bodies, and chat messages — not just the
label slice.

- Each row carries a `search` payload with lowercased full
  content; the filter checks label first (startswith /
  substring / subsequence) and falls back to body-text
  substring hits one score tier lower.
- A new **`chat`** row kind indexes the newest 50 chat
  messages. Each row's label is `You: …` / `<agent>: …` with
  the first 80 chars of text; Enter opens the Chat tab (which
  scrolls to its end on render).
- Chat rows only appear once the owner types; an empty palette
  stays short (items / tabs / playbooks / peers / actions).
- Alt+Enter on a chat row copies a `#chat` deep link.
- No new routes — everything comes from the existing view
  (`view.questions`, `view.threads[CHAT_ITEM]`).

## Draft autosave to server (1.19.0)

Unfinished compose text — chat messages half-typed, answer
drafts, visual briefs, follow-up seat picks — now mirrors
across the owner's browsers. Write at home, finish at the office.

- **Server state**: new `STATE/drafts.json`, dir-relative /
  O_NOFOLLOW, same path as `items.json` and `refs.json`. One
  entry per shaped key: `{text, ts}`. Capped at 500 keys and
  1 MiB total; a key's text is capped at 20 000 characters
  (matching the schema's message cap). Over-cap entries are
  pruned oldest-first on write.
- **New owner-only route** `POST /api/draft` with
  `{key, text, nonce}`. Empty text removes the entry.
- **View payload** now carries `view.drafts: {key: text}` so a
  fresh page load merges server drafts into the in-session
  map.
- **Browser side**: a `setDraft(key, value)` helper replaces
  every direct `draftTexts[key] = …` write (17 sites). The
  helper updates the in-session map and schedules a debounced
  server save (1500 ms after the last keystroke); a `clear`
  path is the same helper with an empty string.
- **Merge policy**: on view fetch, server-only drafts (where
  the local map has no key) are copied into the local map.
  Local text that hasn't synced yet never gets stomped by an
  old server value.
- **Trust**: route is Access-authed, same as every other owner
  route. Drafts are per-owner; peers and agents cannot read or
  write them.

## Security + reliability patch (1.19.1)

Two independent reviews surfaced issues worth blocking the next
feature ship on. All fixed in this patch.

### CRITICAL

- **Agent-door `/view` leaked owner drafts.** `payload()`
  added `view.drafts` since 1.19.0, and `AgentHandler.do_GET`
  serves `payload()` straight through. An agent calling
  `/view` on the user-only socket could read the owner's
  half-typed answers and chat text. Fix: drafts now attach in
  `page_payload()` only (the owner door's path). Agents see no
  `drafts` key.
- **Markdown renderer could hang the tab.** `renderMarkdown`
  used a strict fence regex but a loose paragraph exclusion:
  lines like `` ```c++ foo `` or `` ```json {"a":1} `` matched
  neither, so `i` never advanced and `<p>` elements piled up
  forever. CRLF text with a heading hit the same case because
  `.` does not match `\r`. Fix: normalise CRLF; the paragraph
  branch now unconditionally consumes at least one line, so a
  malformed fence falls through as literal text.

### HIGH

- **Access service-token could leak via redirect.**
  `urllib.urlopen` follows 30x redirects AND re-sends custom
  headers, including `CF-Access-Client-Secret`. A crafted peer
  (or MITM) could 302 to `http://attacker/` and receive the
  secret. Fix: `peers._fetch_slim_http` now builds an opener
  with a redirect handler that raises on any redirect; the
  request fails with `HTTP 30x`.
- **Private-host peers refused.** The `peers.py` URL regex
  accepted `https://localhost`, `.local`, `.internal`, and
  RFC1918 ranges. Combined with an in-repo config the agent
  can edit, this could point the Access secret at a host the
  owner never intended. Fix: `_is_private_host` refuses
  loopback / `.local` / `.internal` / `.lan` / `.home` /
  `.corp` / `127.*` / `10.*` / `172.16-31.*` / `192.168.*` /
  `169.254.*` at config load time.
- **Agent-door `/item-move` forged owner authorship.**
  `item_move` always wrote `by: "owner"`. An agent writing to
  the user-only socket could post as the owner. Fix: pass a
  `by` argument through; the agent-door path writes a plain
  advisory message under the agent's own name, and `intent:
  "move"` stays reserved for the owner door.
- **Draft clear could resurrect from a server round-trip.**
  `setDraft(key, '')` deleted locally, but a view fetch
  before the debounced server save landed would see the key
  absent locally and re-add the old server value. Fix: a
  `draftClearedPending` set tracks keys whose clear hasn't
  acked; `syncServerDrafts` skips them. `pagehide` flushes
  pending saves with `sendBeacon`.
- **Peer aggregator could 503 on a bad peer.**
  `_fetch_slim_http` caught `OSError` + `URLError`, but
  `http.client.HTTPException` (and its `BadStatusLine`,
  `LineTooLong`, `IncompleteRead` subclasses) escaped and
  faulted the whole aggregator. Fix: catch `HTTPException`
  explicitly.
- **`refs._topological` recursed.** A 1 000-item parent chain
  hit Python's recursion limit. Fix: iterative walk.
- **`save_draft` had no lock.** Two concurrent saves could
  lose updates on `ThreadingHTTPServer`. Fix: wrap in
  `self._lock`. `OSError` on write now surfaces as 500 with a
  server-log entry rather than an unhandled traceback.
- **`replay_trigger` turned 404s into 500s.** A missing
  playbook on a configured trigger returned 500 instead of
  propagating the inner error's code. Fix: preserve
  `e.code` from the caught `RequestError`.

### MEDIUM

- **`refs.py` reused retired numbers on an explicit push.**
  Uniqueness was checked only against `assignments`. Fix:
  pushing a retired ref is refused by name, unless it is the
  same id getting its own number back. A retired id that
  returns now gets its old ref restored.
- **`drafts.save` could inflate on non-ASCII and silently
  push past `MAX_FILE`.** Fix: `ensure_ascii=False`.
- **`KEY_RE` anchor `$` allowed a trailing newline in a draft
  key.** Fix: `\Z`.

## Test-regression patch (1.19.2)

Follow-up on the parallel reviews. Full-suite agent surfaced four
real regressions in test_kit and test_server; all fixed. The
remaining ~38 server failures are shape drifts from feature
additions (items_tree, auto-ref, sections, drafts) and need
test-fixture updates in the matching test file, not code fixes.

- **`triggers.load` and `peers.load` treated a missing config file
  as a hard fault.** On a project with no `.overture/triggers.json`
  or no `.overture/portfolio.json`, the broad `except OSError`
  swallowed `FileNotFoundError` into a `TriggerError`/`PeersError`,
  which `multiserver.open_project` then surfaced as a project-wide
  refusal. One project with no triggers would 503 everything on
  the one-server. Fix: catch `FileNotFoundError` first and return
  `{}` — "not configured" is not an error.
- **Old-kit regression tests (`v0.8.*`) failed to import `overture`.**
  Pre-1.0 kits still ship the package as `console_kit`; the
  rename sweep updated the OLD_BOOT and start-old shim scripts to
  `from overture import server` without a fallback. Fix: both
  shims try `overture.server` and fall back to `console_kit.server`
  via `importlib.import_module`.
- **`agent.py register` left pre-1.0 hooks orphaned.** The 1.0
  rename moved the registry to `~/.config/overture/projects.json`.
  A user who still had a pre-1.0 plugin install alongside the new
  one saw its SessionStart hook read the empty legacy path and go
  silent. Fix: `register()` dual-writes to
  `~/.config/console-kit/projects.json` as a mirror. The new path
  stays the source of truth.

## Open-items close-out (1.20.0)

Four remaining items from the parallel review backlog, all fixed.

### Peer URL ↔ secret binding

An agent with write access to `.overture/portfolio.json` could
previously change a peer's `url` without re-signing: the drift
check hashed `secret` alone, so a URL swap kept the same hash. The
1.19.1 patch mitigated with redirect refusal + private-host
blocks; **this closes the gap** by binding the hash to the URL:

    token_sha256 = sha256(secret + 0x00 + url).hexdigest()

- `load_secret` prefers the URL-bound form.
- The pre-1.20 shape `sha256(secret)` is still accepted with a
  one-line stderr warning naming the peer and the fix.
- `agent.py portfolio-token` prints the new formula.
- A URL swap without re-signing now fails the drift check with
  the same error the owner sees for a rotated-but-not-updated
  secret.

### `refs.json` unbounded growth

The `retired` map was append-only. A project with high id churn
could grow `refs.json` past `MAX_FILE` and brick `items-push`
("refs.json is not JSON" once the file crosses the read cap). Fix:
`MAX_RETIRED = 5000`; oldest retirements are pruned FIFO when the
cap is hit (Python 3.7+ insertion-ordered dicts). Common
"returning id" case still works for recent retirements; very old
ones get a fresh ref.

### Markdown italic hits `snake_case`

`foo_bar_baz` was rendering as `foo<em>bar</em>baz`. A snake_case
guard in `renderMdInline` detects an underscore-wrapped match
adjacent to a word char on either side and emits the first
underscore as literal text, retrying the rest. `_italic_` on its
own and `_italic_` between non-word chars still work.

## Full test-suite green (1.20.1)

The parallel review's test agent flagged ~38 server-test
failures as "shape drifts from feature additions, not code bugs."
This patch cleans them up and the full suite now passes.

### Code change

- **Refs no longer fold into `items.json` or into
  `view.items`.** Items stay as the project pushed them, a
  shape-stability contract. The sibling `view.refs: {id: ref}`
  map carries the kit's assignments. Browser `itemRef(id)`,
  palette ref search, PR title ref linking, and clickable refs
  in agent text all read from `view.refs` first (fall back to
  legacy `items[id].ref` so an older project upgrading still
  shows refs).
- `items-push` response is shape-stable: `{"items": N,
  "seeds_added": [...]}`. The pushing agent reads assigned refs
  via `/view` instead.
- `MS.POST_ROUTES` catalog grew to include `/playbook` and
  `/item-move`.

### Test-fixture change

- `test_server.test_the_view_carries_tags_and_the_project_dirs`
  accepts `view.config` with `sections: {}`.
- `test_server.test_board_is_behind_the_same_gate` tolerates the
  kit's `items_tree` key in `values`.
- `test_server.test_ac33_a_fixture_copy_keeps_its_bytes_...`
  whitelists `refs.json` alongside `items.json` + `server.lock`.
- `test_server.test_an_older_kit_reads_the_side_file_as_...`
  archives the pre-1.0 package path (`console_kit`), with a
  probe script that imports either module name.
- `test_server.AGENT_POSTS` includes `/playbook` and
  `/item-move`.
- `test_onboard` tests expect one more output line (the
  gitignore append) + idempotent `kept` on rerun.
- `test_vendor` fixture matches sorted order.

### Results

- `test_kit.py`: 303 pass / 3 skip
- `test_server.py`: **285 pass** (was 42 failing)
- `test_onboard.py`: 31 pass
- `test_vendor.py`: 11 pass

## Concurrent peer fetch + total-time cap (1.21.0)

Closes the two peer-aggregator reliability items from the parallel
review.

- **Concurrent fan-out.** `Aggregator.fan_out(peers)` fetches
  every peer in a bounded `ThreadPoolExecutor` (up to
  `MAX_WORKERS = 16`). A deployment with 32 peers no longer takes
  32× `TIMEOUT_S` wall time in the worst case — just one slow
  peer's worth. Order of the returned rows matches the input
  order so the Portfolio grid stays stable across refreshes.
- **Overall deadline.** `OVERALL_DEADLINE_S = 8.0` caps the whole
  fan-out. Peers whose futures are still pending at the deadline
  yield a `"aggregator deadline exceeded"` error row, so one slow
  peer can never block the browser's `/api/portfolio` fetch past
  that cap.
- **Per-peer body-read deadline.** `_fetch_slim_http` enforces a
  total-time cap on the body read (`body_deadline = TIMEOUT_S`),
  reading in chunks against a monotonic clock. A peer that
  trickles one byte right before every socket timeout — which
  used to be able to stall the per-op `TIMEOUT_S` indefinitely —
  now fails with `"body read exceeded"`.
- **One bad peer never faults the aggregator.** `fan_out`'s
  per-future `except Exception` catches everything (not just
  `OSError` + `HTTPException`) so a protocol-level surprise from
  one peer yields an error row for that peer alone.

Smoke-tested: 5 peers return 5 ordered rows in ~17ms (all error
rows under test; a real peer run would see concurrent round-trips).

## Mobile viewport polish (1.22.0)

The console was designed around a docked wide screen (≥1024 px);
below 640 px the Portfolio grid overflowed, the six tabs crowded
off-screen, modals (palette, viewer, help) spilled past the
viewport, and some tap targets were under the ergonomic 36 px
floor. A `@media (max-width: 640px)` block fixes all of this
without touching desktop behaviour.

- **Full-width panel + safe-area insets** on notched phones.
- **36 px tap-target floor** on every button, chip, tab, toggle,
  skin cycle, ref link, snooze preset, inbox filter chip, caught-up
  dismiss, etc.
- **Horizontal-scroll tab bar** with scroll-snap and hidden
  scrollbar — six tabs no longer wrap or clip.
- **Portfolio grid stacks** to one card per row.
- **Modals fit the viewport** at `calc(100vw - 20px)` with 4-6
  vh top padding and 60-70 vh max-height on scrollable bodies so
  the virtual keyboard doesn't eat the content.
- **Shortcut help stacks** key chips over labels (single-column
  list) and gets a position-fixed sheet with `inset: 10px`.
- **Priority ribbon + Inbox rows wrap** so a long title no
  longer pushes the pill off-screen.
- **Snooze menu left-anchors** instead of right-anchoring,
  avoiding right-edge overflow.
- No desktop change: the breakpoint is `max-width: 640px` only;
  the existing `min-width: 1024px` dock rules still rule the
  owner's workstation experience.

### Trust

- The store is append-only in spirit: assignments are permanent
  per id; retired numbers block their own number from being
  given out again.
- `assign_refs` checks shape, parent prefix, and uniqueness
  before writing anything; a conflict leaves the file untouched.
- `STATE/refs.json` is written whole, dir-relative, O_NOFOLLOW —
  same path as `items.json`.

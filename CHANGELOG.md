# Changelog

What each console-kit release added, oldest first. These sections moved
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

## Several agents, one console (0.8.2)

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

## One steward, many sessions (0.8.3)

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

## Name a session from inside it (0.8.4)

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
  `console_kit/gitseam.py` is the only place a process can be started, and
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

## Settling a stale answer (unreleased, owner rulings CONSOLE-kit/Q30, Q31 and Q32)

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


# Console runbook

Symptom, then checks, then the fix. Placeholders are the ones in `DEPLOY.md`
(`<name>`, `<state>`, `<vendored>` or the installed kit, `<port>`). Nothing here
needs a secret; never paste `console.env` or a token into a ticket.

## First: health

    python3 <vendored>/agent.py --state <state> health; echo rc=$?

| Result | Meaning | Go to |
|---|---|---|
| `rc=0`, `"ok": true` | server up, register readable | the symptom's own section |
| `rc=1`, `"register": "error"` | server up, the adapter's `items()` fails | the journal (below); fix the register file |
| `rc=2`, `cannot reach the console server` | server down, or `<state>` is wrong | "Console 502" |

`"agent": "listening"` means a session is waiting on the doorbell now;
`"idle"` means none is (the last one stopped or went quiet); `"never"` means
none ever has on this state dir. The console's status bar shows the same thing.

The journal names every start-up error and every failed register read:

    journalctl --user -u <name>-console.service -n 50 --no-pager

## Console 403

    curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:<port>/api/view    # 403 = server up and gated

The 403's JSON body names the reason (the journal logs only the request line,
never a header). Read it in the browser:

- **`no Access token`**: the request did not come through Access. Check that
  the Access application's domain is exactly the tunnel's hostname, and that
  its policy includes you.
- **`Access token refused (InvalidAudienceError)`**: `CONSOLE_AUD` is not the
  Access application's AUD tag. Correct it in the env file and restart.
- **`(InvalidIssuerError)`**: `CONSOLE_TEAM_DOMAIN` is not your team domain.
- **`(ExpiredSignatureError)`**: log in again; if it repeats, check the
  machine's clock (`timedatectl`).
- **`(PyJWKClientError)`** or similar: the server cannot fetch the team's keys.
  Check outbound HTTPS from the machine to `https://<team>/cdn-cgi/access/certs`.
- **A POST fails with `a write from another site, or with no Origin`**:
  `CONSOLE_HOSTNAME` is not the hostname in the browser's address bar.

## Console 502 (or Cloudflare 1033 / 530)

Cloudflare reached the tunnel but not the server, or not the tunnel at all.

    systemctl --user status <name>-console.service <name>-console-tunnel.service --no-pager
    ss -ltn | grep ':<port> '

- Server not running: read the journal. A `StoreError` on start is "Store
  damaged" below. `the agent socket path is N bytes` means `--state` is too
  long a path; shorten it.
- Server running, tunnel not: restart the tunnel unit, then check that its own
  config (`~/.cloudflared/config-<tunnel>.yml`) has ingress
  `http://127.0.0.1:<port>`, and that `<port>` is `CONSOLE_PORT`.
- Both running: the DNS record may point at another tunnel. Route it again
  with `--config` for this tunnel (`CLOUDFLARE.md`).

## The doorbell does not wake the session

1. Is anything listening? The status bar, or `health`, says `listening`.
   If not, no session is running `agent.py watch`: start one (the
   `console-process` skill re-arms it after each run).
2. Did the owner's action ring? Only "Answers are in" (`process`), a chat message (`chat`) and a
   deliberation request (`fork`) wake a watch; answers, locks and messages
   do not.

       tail -n 3 <state>/inbox.jsonl
       python3 <vendored>/agent.py --state <state> inbox --all | tail -n 3

3. Has the agent already passed it? The watch starts after the agent's cursor.

       cat <state>/agent-cursor.json

   If `through` is at or past the wake line's `seq`, that line was already
   processed. `inbox --since <seq-1>` shows it again.
4. Same state dir? The watch, the server and the registry must name the same
   `<state>`: compare the unit's `--state` with
   `~/.config/console-kit/projects.json`.
5. `... is not a regular file`: something replaced the doorbell or a state
   file with a FIFO or device. Remove it; the server recreates the doorbell
   on the next owner write.

"Agent active" that never clears lapses on its own after an hour, or at once
on `agent.py synced`.

## The console does not update by itself (0.7.0)

An open tab keeps one long poll to `/api/wait`; each answers within 25 s.

1. Reload once. A tab opened before the server was upgraded has the old page.
2. In the browser's network panel, `/api/wait` should show one request at a
   time, each lasting up to 25 s. A `403` means the Access session expired:
   reload to log in again. A `429` means more than 16 tabs are polling; close
   some. A `404` means the server is older than 0.7.0 (`health` shows the
   version); the page then stops polling and **Refresh** still works.
3. A hidden tab does not poll; it catches up the moment it is shown.
4. While you type in the panel, a change shows a **New activity · Show**
   note instead of redrawing the box you are typing in. The chat is the
   exception: replies appear in its log as you type.
5. The server log shows one `GET /api/wait` line per poll. Several a second
   from one tab is a bug: note the browser and version.

## "Lock all & process" says nothing was locked (0.7.0)

It checks the whole batch first and writes nothing if the store would refuse
any of it; each question's line says why. The usual cause is a question
locked from another tab or card since you drafted it: re-open the round (it
shows as already locked) and lock the rest. To change a locked answer, use
**Change my answer** on its card, which asks for a reason.

**"Stopped part way"** means a write failed (a full disk, say) after some
questions were locked. Those stay locked; the rest were not written and no
process request was sent. Fix the cause (`df -h <state>`), then press **Lock
all & process** again: already-locked questions are skipped and one process
request is sent.

## A chat message is not answered (0.7.0)

1. The Chat tab says whether an agent is listening. If not, the message waits
   for the next session; nothing is lost.
2. A watch started before 0.7.0 does not wake on chat: restart it (the
   `console-process` skill re-arms it).
3. `tail -n 3 <state>/inbox.jsonl` shows a line with `"intent": "chat"` and
   `"item": "@chat"` for each message.
4. **429 "at most 6 chat messages a minute"** (or 60 an hour): wait and send
   again. What you typed stays in the box.

## The server will not start: `.console-kit.json` (0.8.0)

The journal says `console: .console-kit.json: specs_dir ...` (or
`visuals_dir`, `next_step`). The server refuses a folder that is not a plain
relative path at least one folder deep, one that climbs out or resolves
outside the project through a symlink, a `visuals_dir` inside `specs_dir`, and
a `next_step` that is not `{"refine"|"drill": "<skill name>"}`. Fix the key,
then `systemctl --user restart <name>-console.service`.

## A visual does not show (0.8.0)

- **"Not shown: ... changed since the agent stored it"** or a blank frame with
  a 409 in the network log: someone edited the file under `visuals_dir` after
  it was stored, or swapped it for a symlink. The console shows a visual only
  while its sha256 matches the store. Restore the file from the pull request
  that landed it, or ask for the visual again.
- **"the file is not there any more"**: the server's checkout lost it (a
  checkout, a clean). Restore it from its pull request.
- **An HTML mock shows unstyled or with gaps**: it loads something from
  outside itself. The mock's policy allows only inline styles and `data:`
  images; the agent should inline them (console-visual skill, step 2).
- **A script or form in a mock does nothing**: by design. A mock runs in
  `<iframe sandbox="">` and is served with a `sandbox` Content-Security-Policy,
  so no script runs, even if its URL is opened in its own tab.
- **`agent.py visual` says "this project sets no visuals_dir"**: add
  `visuals_dir` to `.console-kit.json` and restart the server.
- **"INDEX.md was not written by the kit"**: a hand-written `INDEX.md` sits in
  `visuals_dir`. The visual was still stored; move that file aside and the next
  visual regenerates the index.

## Roar is refused (0.8.0)

**"LANE/Q3 already had its roar: fork <id>"**: by design, a roar runs at most
once per question (each costs about six agent runs). Follow up with chosen
seats instead, or supersede the answer and ask a new question. A transcript
is refused **"transcript is N bytes; the limit is 49152. It is refused, not
truncated"**: the agent shortens its round summaries and sends it again.

## The suggested-next-step chips look wrong (0.8.0)

The chips are rules, not judgement, and each says its reason (hover, or a
screen reader reads it). `console_kit/tags.py` holds the exact rules.

- **No refine chip ever**: no `specs_dir` in `.console-kit.json`, or the locked
  question cites no file under it, or the spec's last commit (or, uncommitted,
  its mtime) is after the lock. In a checkout where specs are only ever edited
  and never committed, every edit counts by mtime.
- **A drill chip for something a spec covers**: the term is spelled
  differently in the spec (a plural, a hyphen) or is a proper name. It is a
  suggestion; ignore it.
- **No chips at all, and the server log says `console tags: ...`**: the tags
  failed for that page view (a spec caught mid-write, git slow); the page
  still works and the next view tries again.

## An answer shows stale

    python3 <vendored>/agent.py --state <state> check

It names each failed condition and why: the text moved, changed or
disappeared, or a status changed.

- **The cited text is still there but the lock is on a whole-file hash**
  (locks from before 0.5.0):

      python3 <vendored>/agent.py --state <state> reanchor --dry-run
      python3 <vendored>/agent.py --state <state> reanchor

  It writes only what git history supports, and lists the rest with a reason.
- **The text really changed**: the owner reads "Why stale?" and either
  re-locks ("Still holds: re-lock…") or answers again. Never write a lock or
  anchor by hand.
- **Goes stale after every unrelated edit**: the question hashes a busy file.
  Ask it again citing an `excerpt`.

## The fold refuses

The fold is all or nothing: one refused file refuses the lot, and each reason
is named. Always run it from the project root, `--dry-run` first.

| Refusal | Fix |
|---|---|
| `schemaVersion ... is not ...` | export and fold with the same kit version as the store |
| `file name does not match qid` | a file was renamed; export again, never rename |
| `item ... does not resolve to a project item` | the item left the register; restore it, or leave that question unfolded |
| `not locked` | the answer is not locked yet; lock it in the console first |
| `must be a relative path inside the project` | run from the project root; fix `--locked`, `--ledger` or `--adapter` |
| `cannot load adapter` / `lacks ...()` | the adapter file is missing or broken on this branch |

Re-export (`fold.py export --store <state>/store.jsonl --out <dir>`) rather
than editing an exported file.

## Store damaged

`store.jsonl` is append-only. The server checks every line's JSON, schema,
`seq` and `id` on start, and will not start past a bad line. Back it up first,
under the same lock the server writes with:

    systemctl --user stop <name>-console.service
    flock <state>/store.jsonl cp -p <state>/store.jsonl <state>/store.jsonl.bak-$(date +%Y%m%d%H%M%S)

- **`line N has no newline, so a write was cut short`**: a crash mid-write.
  That write was never acknowledged, so its sender retries it with the same
  nonce. Drop only the torn last line:

      n=$(wc -l < <state>/store.jsonl)            # counts complete lines only
      head -n "$n" <state>/store.jsonl > <state>/store.jsonl.new
      mv <state>/store.jsonl.new <state>/store.jsonl
      chmod 600 <state>/store.jsonl

- **`not JSON`, `seq or id does not match the line; the file was edited`**, or a
  validation error on a line: the file was edited or damaged. Do not repair
  lines by hand, since an `id` is a hash of its record. Restore the newest
  backup that loads, and name what was lost after it.
- **`schemaVersion ...; this kit reads only ...`**, or `unknown field(s)`: the
  store was written by a newer kit. Upgrade the kit, and never trim the store
  (`DEPLOY.md`, "Rollback").

Then start the server and confirm:

    systemctl --user start <name>-console.service
    python3 <vendored>/agent.py --state <state> health

## The vendored copy drifted

    python3 <kit-clone>/plugin/kit/tools/verify_vendor.py --kit <kit-clone> --ref <tag> --vendored <vendored>

Each `DRIFT` line names a file that differs from `<tag>`, is not a kit file,
or is missing. Re-copy it from `<tag>` (`DEPLOY.md`, "Upgrade"); a local fix
belongs in the kit repository, as a release.

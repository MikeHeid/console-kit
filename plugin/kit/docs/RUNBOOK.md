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
2. Did the owner's action ring? Only "Answers are in" (`process`) and a
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

# Deploying the console

How a kit that is **vendored into a project** is installed, upgraded and rolled
back, when a systemd **user** unit serves it behind Cloudflare Access. For the
first install from the zip, see `INSTALL.md`. For what to do when something is
wrong, see `RUNBOOK.md`.

Placeholders used below:

| Placeholder | Meaning |
|---|---|
| `<project>` | the project checkout the unit serves (its `WorkingDirectory`) |
| `<vendored>` | the kit's copy inside it, e.g. `<project>/tools/console-kit` |
| `<name>` | the unit's name: `<name>-console.service` |
| `<state>` | the server's `--state` directory (store, doorbell, agent socket) |
| `<kit-clone>` | a local git clone of console-kit, with its tags fetched |
| `<tag>` | the kit release the copy should be, e.g. `v0.6.0` |
| `<port>` | the owner port (`--port`); `<hport>` an optional health port |

## How it runs

- The unit runs `python <vendored>/server.py` **from the project checkout**. What
  runs is whatever that checkout holds at the last restart. Switching its branch
  and restarting changes the running kit.
- The server binds `127.0.0.1:<port>` only. Every HTTP request needs a valid
  `Cf-Access-Jwt-Assertion`, loopback included. The tunnel's ingress points at
  `<port>`.
- Agents talk to `<state>/agent.sock` (mode 0600, in a 0700 directory). It
  needs no token; file permissions are the check.
- `/health` is on the agent socket always, and on `127.0.0.1:<hport>` only if
  the unit passes `--health-port <hport>` (see "Health" below).

Read the live unit before changing anything:

    systemctl --user cat <name>-console.service
    systemctl --user status <name>-console.service --no-pager

## Upgrade

1. Get the release, offline from then on:

       git -C <kit-clone> fetch --tags origin
       git -C <kit-clone> rev-parse <tag>

2. On a branch of the project, copy the release in. The mapping is the table
   in `kit/tools/verify_vendor.py` (`RULES`): `plugin/kit/console_kit/*` to
   `console_kit/`; `plugin/kit/{agent,fold,publish,server}.py` and
   `requirements.txt` to the top; `plugin/hooks/`, `plugin/agents/`,
   `plugin/skills/<each skill you take>/` and
   `plugin/.claude-plugin/plugin.json` to the same paths under `plugin/`.
   Your own adapted files (tests, marketplace entry) are merged by hand.

       mkdir -p /tmp/kit-<tag> && git -C <kit-clone> archive <tag> plugin | tar -x -C /tmp/kit-<tag>
       # copy per the table, then:
       python3 <kit-clone>/plugin/kit/tools/verify_vendor.py \
           --kit <kit-clone> --ref <tag> --vendored <vendored>

   It exits 0 only when every vendored file is byte-identical to `<tag>`, and 1
   naming each file that differs, is stray, or is missing. Run the same line in
   CI to catch a copy that drifts later.

3. Run the project's tests of the kit (the vendored `test_*.py`), then land the
   branch through the project's PR process.
4. After the merge, update the served checkout and restart:

       git -C <project> pull --ff-only
       systemctl --user restart <name>-console.service
       python3 <vendored>/agent.py --state <state> health    # exit 0, "version": the new one

5. Reload any open console tab. A session with a running `agent.py watch`
   keeps the old code until its watch next exits; that is harmless, except
   that a watch started before 0.7.0 does not wake on a chat message. Restart
   it (the console-process skill re-arms it) so chat reaches a session.

From 0.8.0 the server reads `specs_dir`, `visuals_dir` and `next_step` from
the project's `.console-kit.json` when it starts (`kit/docs/ADAPTER.md`). A
bad value stops it with `console: .console-kit.json: <key> ...` in the
journal; fix the key and restart. The server writes visuals into
`<project>/<visuals_dir>/`, so the unit's user must be able to write there;
nothing else in the project is ever written. Its `/api/visual` route is behind
the same Access gate as every other owner route.

From 0.7.0 an open tab keeps one long poll (`/api/wait`, 25 s) open to the
server. The tunnel needs nothing for it: it is a plain GET behind Access, well
inside Cloudflare's 100 s response limit. The server holds at most 16 such
polls at once and answers a 17th with 429, which the page backs off from.

A kit installed by `deploy/install.sh` instead (copied to
`~/.local/share/console-kit/kit`) is upgraded by re-running
`install.sh --project <project> --start`, which copies the new kit and restarts.

## Rollback

1. Revert the vendoring merge on a branch, land it, then:

       git -C <project> pull --ff-only
       systemctl --user restart <name>-console.service
       python3 <vendored>/agent.py --state <state> health

2. Before rolling back **below 0.5.0**, check the store. A kit older than 0.5.0
   refuses a store holding any 0.5.0 record and will not start:

       grep -cE '"type":"anchor"|"anchors":|"kind":"excerpt"' <state>/store.jsonl

   Anything but `0` means the store needs 0.5.0 or later. The store is
   append-only and nothing may be edited out of it, so the only way back is a
   kit at 0.5.0 or later. Rolling 0.6.0 back to 0.5.0 is always safe: 0.6.0
   adds no store record. It adds only `<state>/watch.json`, which older kits
   ignore.

3. Before rolling 0.7.0 back to **0.6.0 or older**, check the store the same
   way. 0.7.0 adds no record kind, but a 0.6.0 kit refuses, by name, a chat
   message (`intent 'chat'`, item `@chat`) and a question carrying `evidence`,
   and will not start:

       grep -cE '"intent":"chat"|"item":"@chat"|"evidence":' <state>/store.jsonl

   Anything but `0` means the store needs 0.7.0 or later.

4. Before rolling 0.8.0 back to **0.7.0 or older**, check the store once more.
   0.8.0 adds two record kinds and three message shapes, and a 0.7.0 kit
   refuses each, by name (`unknown record type 'transcript'` or `'visual'`,
   `intent 'visual'`, `role(s) ['roar']`, `unknown field(s) step`), and will
   not start:

       grep -cE '"type":"(transcript|visual)"|"intent":"visual"|"roar"|"step":' <state>/store.jsonl

   Anything but `0` means the store needs 0.8.0 or later. Files the server
   wrote under `visuals_dir` stay where they are; a 0.7.0 server ignores them.

5. Never "roll back" by editing `store.jsonl`. Every line's `seq` and `id` are
   checked on load, and an edited line stops the server.

## Health

Two ways to ask, neither of which goes near the tunnel:

    python3 <vendored>/agent.py --state <state> health          # exit 0 ok, 1 not ok, 2 unreachable
    curl -s --unix-socket <state>/agent.sock http://localhost/health

For a monitor that cannot use a Unix socket, add `--health-port <hport>` to the
unit's `ExecStart` (a port that is not `<port>`), then `systemctl --user
daemon-reload` and restart. Then:

    curl -s http://127.0.0.1:<hport>/health

The answer is `{"ok", "version", "store_seq", "register", "agent",
"agent_listening"}`: HTTP 200 when `ok`, 503 when the register cannot be read.
It holds no owner text and no secret.

The health port refuses (403) any request that carries a header a proxy or the
Cloudflare edge adds (`Cf-Connecting-Ip`, `Cf-Ray`, `X-Forwarded-For`,
`Forwarded` and others), whatever the method, a `Host` that is not
`127.0.0.1` or `localhost`, and any peer that is not loopback. So pointing the tunnel at it by
mistake exposes nothing. Never put `<hport>` in the tunnel's ingress anyway.
The owner port still answers `/health` with 403 without a token.

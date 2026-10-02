# Moving a project from a vendored console-kit to the standalone pinned install

Until 0.8.7, a project could run the console from a copy of the kit **vendored
into its own repository** (for example `<project>/tools/console-kit`, checked
with `kit/tools/verify_vendor.py`). This guide moves such a project onto **one
user-level install of a pinned kit release**, outside every project, and then
removes the vendored copy.

Why move:

- **One copy per machine.** Each repository vendoring its own copy means N
  projects may run N kit versions. A pinned install is one version, chosen by
  you, used by every project on the machine.
- **No checkout coupling.** A vendored kit runs whatever commit the project's
  service checkout is on. A branch switch or a merge to that project changes
  the console. A pinned install changes only when you move the pin.
- **It is where the kit is going.** The owner's ruling (CONSOLE-kit/Q13) is one
  user-level install of a pinned release, with one server that each project's
  install links into through an API and a per-project token. **That server is
  not built yet.** It is specified in the multi-project spec
  (`console-kit-multiproject.md`, kept with the owner's architect docs). Today each project still
  runs its own server process, and this guide only changes *which copy of the
  kit* that process runs. When the one-server build ships, it will start from
  the pinned install this guide sets up.

The order of the steps matters. **The vendored copy is deleted last**, after the
server, the registry and the Claude Code plugin all run from the pinned install
and you have checked them. Step 6 explains why.

---

## 0. Before you start

You need:

- a local clone of `MikeHeid/console-kit` with its tags (`git fetch --tags origin`);
- the release your vendored copy is, confirmed byte for byte:

      python3 <kit-clone>/plugin/kit/tools/verify_vendor.py \
          --kit <kit-clone> --ref <tag> --vendored <project>/<vendored>

  Exit 0 means the copy is that release exactly. **If it exits 1, stop.** Each
  file it names is a change your project made to the kit. Get it into a kit
  release upstream first; otherwise the move silently drops it. (Files listed
  as "adapted", such as the project's own tests and marketplace entry, are not
  compared and are not part of the kit.)

Write down what you have now. You will need it to roll back:

    systemctl --user cat <name>-console.service
    cat ~/.config/console-kit/projects.json
    systemctl --user list-timers --all | grep <name>

## Placeholders

| Placeholder | Meaning | Example |
|---|---|---|
| `<kit-clone>` | your development clone of console-kit | `~/dev/projects/console-kit` |
| `<tag>` | the pinned release | `v0.8.7` |
| `<pins>` | where pinned releases live | `~/.local/share/console-kit/releases` |
| `<current>` | a symlink to the pinned release in use | `~/.local/share/console-kit/current` |
| `<project>` | the checkout the unit serves | `~/src/acme` |
| `<vendored>` | the vendored copy, relative to `<project>` | `tools/console-kit` |
| `<name>` | the unit name, `<name>-console.service` | `acme` |
| `<state>` | the server's `--state` directory | `~/.local/state/acme-console` |
| `<venv>` | a Python with the kit's pinned PyJWT and cryptography | `~/.local/share/acme-console/venv` |

`~/.local/share/console-kit/kit` and `~/.local/share/console-kit/venv` belong to
`deploy/install.sh` (the zip route in `INSTALL.md`). The pinned releases go in
`releases/` beside them, so neither ever overwrites the other.

---

## 1. Create the pinned install

    git -C <kit-clone> fetch --tags origin
    mkdir -p <pins>
    git -C <kit-clone> worktree add --detach <pins>/<tag> <tag>
    ln -sfn <pins>/<tag> <current>

**Why a detached worktree, and not the clone itself.** The clone is where kit
development happens: its branch moves, it gets dirty, tests write into it. A
server running from it would change under you exactly as a vendored copy
does. A worktree is a second checkout of the same repository, **fixed at the
tag**, that nothing moves unless you do:

- `git -C <pins>/<tag> describe --tags --exact-match` prints the tag, and
  `git -C <pins>/<tag> status --porcelain` prints nothing. Those two lines
  prove the running kit is the release, untouched, with no separate
  `verify_vendor.py` step. (Python's `__pycache__` is in `.gitignore`, so
  running the kit does not make the worktree dirty.)
- It costs no second download: it shares the clone's objects.

Two things a worktree does **not** survive: deleting the clone, and moving it
without telling git. If you move the clone, run `git -C <new-clone-path>
worktree repair <pins>/<tag>`. If you would rather the pin not depend on the
clone at all, make it a standalone clone instead; everything below is the
same:

    git clone --branch <tag> --depth 1 https://github.com/MikeHeid/console-kit <pins>/<tag>

**Why `<current>`.** The systemd unit and the Claude Code marketplace point at
`<current>`, not at `<pins>/<tag>`. An upgrade or a rollback is then one
`ln -sfn` and a restart, not an edit of the unit and the settings.

**Python.** The server needs the two packages in
`<pins>/<tag>/plugin/kit/requirements.txt`, in a venv of their own (PEP 668
hosts refuse `pip --user`). A venv the vendored install already built can be
reused when it holds the same pins:

    <venv>/bin/pip install -q -r <current>/plugin/kit/requirements.txt
    <venv>/bin/python -c 'import jwt, cryptography; print(jwt.__version__, cryptography.__version__)'

For a new machine, make one: `python3 -m venv ~/.local/share/console-kit/venv-pinned`
and use that as `<venv>`.

**Rollback:** nothing uses the pin yet. `git -C <kit-clone> worktree remove
<pins>/<tag>` and `rm <current>`.

## 2. Point the registry at the pinned kit

The registry (`~/.config/console-kit/projects.json`) tells the plugin's hook
and skills which `agent.py` to run for a project. `register` records the
directory of the `agent.py` you run it with, so run the **pinned** one.

**`register` replaces the project's whole entry.** It drops any existing
entry for that root (`projects.pop(root)` in `console_kit/registry.py`) and
writes a new one. Back the registry up first, and note the steward if one is
set:

    cp ~/.config/console-kit/projects.json ~/.config/console-kit/projects.json.bak-before-pin
    python3 <current>/plugin/kit/agent.py --state <state> steward      # prints the steward, if any

Then register with the **pinned** `agent.py`, passing `--steward NAME` again if
the line above printed one:

    python3 <current>/plugin/kit/agent.py --state <state> register --project <project> [--steward NAME]

The current code carries a steward over from another entry on the same state,
but re-passing it makes the result not depend on that. It prints
`registered <project>: state <state>, kit <pins>/<tag>/plugin/kit`. The path is
resolved, so the registry names the release itself, not `<current>`.

Check it:

    cat ~/.config/console-kit/projects.json        # "kit" is <pins>/<tag>/plugin/kit
    python3 <current>/plugin/kit/agent.py --state <state> health

**Rollback:** restore the backup
(`cp ~/.config/console-kit/projects.json.bak-before-pin ~/.config/console-kit/projects.json`),
or register again with the vendored `agent.py` (and `--steward NAME`):
`python3 <project>/<vendored>/agent.py --state <state> register --project <project>`.

## 3. Run the server from the pinned kit

Add a systemd **drop-in** that replaces only `ExecStart`. The unit file the
project's installer rendered stays as it is, so re-running that installer
later does not undo this; the drop-in wins.

Keep every flag the current `ExecStart` has. Change only the script path, from
`<vendored>/server.py` to `<current>/plugin/kit/server.py`. In a drop-in,
`ExecStart=` on its own line first clears the old command; without it systemd
refuses a second `ExecStart` for a `Type=simple` service.

    systemctl --user edit --drop-in=pinned-kit <name>-console.service

or, to write it without an editor, put the same text in
`~/.config/systemd/user/<name>-console.service.d/pinned-kit.conf`. Then:

    systemctl --user daemon-reload
    systemctl --user restart <name>-console.service

`WorkingDirectory` can stay the project checkout. The server reads only the
paths it is given (`--root`, `--page`, `--state`, and the optional usage and
account files, which must be absolute), and finds its own modules beside its
`server.py`. Make sure each of those flags is an absolute path in your unit.

The server never runs the project's adapter (owner ruling CONSOLE-kit/Q24).
An `--adapter` flag still in the unit is accepted and ignored, so the unit
keeps starting. The items come from the steward instead, which runs the
adapter in its own process and pushes the result. Run this once the server
is up, and again whenever the register changes:

    python3 <current>/plugin/kit/agent.py --state <state> items-push --project <project> --adapter <adapter path>

Run it from inside the project's checkout, with an `--adapter` path inside
that project. The command picks the server to send to from the directory it
runs in, and refuses an adapter outside the project.

Until then the inbox says that items appear when the steward pushes them.

If the stored items cannot be read (a damaged `items.json` in the state dir,
or something other than a plain file in its place), the single server still
starts. It logs one line, shows no items, and the inbox says the stored items
cannot be read. `health` still answers 200, since the process is up and a
restart would not fix the file, with its register marked "error" and a note
saying to push again. This covers a file the server may not open, too. To recover, run
`items-push` again: it replaces the file, and the board comes back without a
restart. A directory found there is renamed aside to `.items.json.set-aside.*`,
never deleted. The one server (`--all`) still refuses just that project and
keeps serving the others.

Check it:

    systemctl --user status <name>-console.service --no-pager | head -5   # active (running)
    systemctl --user show <name>-console.service -p DropInPaths           # names pinned-kit.conf
    tr '\0' ' ' < /proc/$(systemctl --user show -p MainPID --value <name>-console.service)/cmdline; echo
                                                   # .../console-kit/current/plugin/kit/server.py ...
    python3 <current>/plugin/kit/agent.py --state <state> health   # exit 0, "ok": true, "version": "<tag without v>"
    curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:<port>/   # 403: no Access token, as it must be
    python3 <current>/plugin/kit/agent.py --state <state> inbox --all | head

Then open the console's hostname in a browser: the Access login, then the
page, with your inbox.

`/health` answers on the agent socket only (or on `--health-port`, if your
unit passes one). The owner port refuses every request without an Access
token, loopback included, so `curl` there can only show the 403.

**Rollback:**

    rm ~/.config/systemd/user/<name>-console.service.d/pinned-kit.conf
    systemctl --user daemon-reload && systemctl --user restart <name>-console.service

## 4. A follow-main timer, if your project has one

Some projects keep the service checkout fast-forwarded to `main`, so merged
work reaches the console, and restart the server when the files it runs
change. Once the server runs from the pinned install, **kit changes no longer
arrive through the project's `main`**; they arrive when you move the pin
(see "Upgrading", below). Two consequences:

- the timer's list of "files the server runs" should drop `<vendored>`. Until
  it does, a change there only causes a harmless restart of the pinned kit;
- the timer keeps doing its other job (pages, adapter, project config), and
  the drop-in survives its restarts.

Change nothing in the timer now. Its edit goes in the same project change that
deletes the vendored copy (step 6).

## 5. Point the Claude Code plugin at the pinned install

The plugin (hooks, skills, committee agents) comes from a **marketplace**.
A vendored project usually registered its vendored directory as one. Point the
marketplace of the same name at `<current>` instead. `<current>` is a
marketplace: it holds `.claude-plugin/marketplace.json`, named `console-kit`,
whose plugin is `./plugin`, and `plugin/kit/` is inside it.

**A directory marketplace's plugin loads in place.** Claude Code runs it from
the marketplace's folder, not from a copy: an edit there applies at the next
session start or `/reload-plugins`
([plugins: in-place and copied plugins](https://code.claude.com/docs/en/plugins/loading.md#in-place-and-copied-plugins)).
So once the marketplace points at `<current>`, every session loads the skills
and hooks from the pin, and until it does, they load from the vendored folder.

1. **First, the settings entry.** If `~/.claude/settings.json` declares the
   marketplace under `extraKnownMarketplaces`, change that entry's `"path"` to
   `<current>`, written as an absolute path (JSON does not expand `~`).
   `marketplace remove` uninstalls the marketplace's plugins and clears their
   `enabledPlugins` entries, but the docs do not say it clears
   `extraKnownMarketplaces`
   ([install: manage marketplaces](https://code.claude.com/docs/en/plugins/install.md#manage-marketplaces)),
   so an entry left on the old path could bring it back.
2. **Then repoint the marketplace**, in a shell (absolute path):

       claude plugin marketplace remove console-kit
       claude plugin marketplace add <current>
       claude plugin marketplace list        # console-kit, at <current>

3. **Then reinstall and reload**, in a `claude` session:

       /plugin install console-kit@console-kit
       /reload-plugins

If the plugin asks for its two optional defaults again (team domain, default
zone), give the same values, or set them later with
`/plugin configure console-kit@console-kit`.

**Nothing in the hooks names a path.** The SessionStart note and the skills
build the `agent.py` command from the registry's `kit`. After step 2, a new
session is told `python3 <pins>/<tag>/plugin/kit/agent.py --state <state>`.

Check it, in a new session in the project:

- `/plugin` lists `console-kit` at the new version, from marketplace `console-kit`;
- the SessionStart note's `agent.py` path is under `<pins>/<tag>/plugin/kit`;
- `cat ~/.claude/plugins/known_marketplaces.json` shows `console-kit` at `<current>`.

**Rollback:** the same three steps with the vendored directory in place of
`<current>`, including the old `extraKnownMarketplaces` path.

## 6. Only then: remove the vendored copy from the project

When steps 1 to 5 check out, delete `<vendored>` in a normal project change
(a pull request) and update everything that names it.

**Why last.** A project whose service checkout follows `main` pulls a merged
deletion within minutes, with nobody watching:

- if the unit still ran `<vendored>/server.py`, the follow timer's restart (or
  the next crash or reboot) starts a server whose script is gone. Systemd
  retries it in a loop, and the console is down;
- if the registry still named `<vendored>`, every session's SessionStart note
  and every skill would run an `agent.py` that no longer exists;
- if the marketplace still pointed at `<vendored>`, every session would lose
  console-kit's skills and hooks at its next start or `/reload-plugins`: a
  directory marketplace's plugin loads in place from that folder, with no copy
  to fall back on (step 5).

Done in this order, the deletion changes nothing that runs.

What the project change usually touches, besides `<vendored>` itself:

- the project's unit template and installer, if it renders its own units
  (the script path, and the `requirements.txt` the venv installs from);
- the follow timer's restart list (step 4);
- CI that ran the vendored tests, `verify_vendor.py`, `publish.py check` or
  `fold.py` from `<vendored>`. It should check out the pinned release (a tag
  and its commit sha) and run them from there;
- docs and tests that cite kit files by their vendored path.

**Rollback:** revert that change. Steps 1 to 5 do not depend on it.

---

## What a project token protects

Once projects move onto the one console server, each project's agents reach
it with that project's own token (`tokens/NAME` beside `server.json`, mode
0600; the server keeps only a hash). The token stops cross-talk through the
kit and by mistake: a wrong `--state` or a skill bug cannot reach another
project's console. It is **not** isolation between agents running as the same
user: such an agent can read `tokens/` and every other project's state
directory directly. Real isolation needs a sandbox or a separate user covering
both.

Two consequences for the cut-over:

- **Stop the old per-project servers first.** While a hosted project's old
  server still listens on `STATE/agent.sock`, that socket is an
  unauthenticated route to its store, whatever the tokens say. The cut-over
  must stop and disable those servers before the token is relied on.
- **A broken `server.json` blocks every `agent.py` command**, read-only ones
  included, until it is fixed.

## Upgrading the pinned install

    git -C <kit-clone> fetch --tags origin
    git -C <kit-clone> worktree add --detach <pins>/<new-tag> <new-tag>
    <venv>/bin/pip install -q -r <pins>/<new-tag>/plugin/kit/requirements.txt
    ln -sfn <pins>/<new-tag> <current>
    python3 <current>/plugin/kit/agent.py --state <state> register --project <project>   # each project
    systemctl --user restart <name>-console.service                                       # each project
    python3 <current>/plugin/kit/agent.py --state <state> health                          # the new version
    python3 <current>/plugin/kit/agent.py --state <state> items-push --project <project> --adapter <adapter path>
    /reload-plugins            # the plugin loads in place from <current>

**The steward pushes the items after an upgrade.** The server no longer runs
the adapter (CONSOLE-kit/Q24). So after the first upgrade past that change,
the board has no items until the `items-push` line above runs. The inbox says
so instead of going blank. The push is kept in the state dir, so later
restarts keep the items. Run the push from inside the project's checkout,
with `--adapter` inside that project.

Read the release's notes in `INSTALL.md` ("Upgrading to …") first. Some
releases ask for a step of their own, such as `reanchor` or a
`.console-kit.json` key. Keep the old worktree until the new one has run for
a while: it is your rollback.

## Rolling back to the previous pin

    ln -sfn <pins>/<old-tag> <current>
    <venv>/bin/pip install -q -r <current>/plugin/kit/requirements.txt
    python3 <current>/plugin/kit/agent.py --state <state> register --project <project>
    systemctl --user restart <name>-console.service
    /reload-plugins

Check `INSTALL.md`'s "Going back to an older kit" first. An older kit refuses
by name a store record it does not know, and a steward must be cleared before
going back below 0.8.3.

To retire an old pin: `git -C <kit-clone> worktree remove <pins>/<old-tag>`.

---

## A worked example

A project keeps its own worked example, with its real paths, unit flags and the
list of files its deletion PR touches, in its own repository. This repository
names no consumer project.

# Worked example: Gradiance, from `tools/console-kit` (0.8.7) to the pinned v0.8.7

The steps of [MIGRATION.md](MIGRATION.md), with Gradiance's values filled in. Read that
file for why each step is there and how to roll it back.

Measured 2026-10-01 at Gradiance `020441c`:

- `verify_vendor.py --kit ~/dev/projects/console-kit --ref v0.8.7 --vendored
  ~/dev/projects/gradiance/tools/console-kit` exits 0: **42 files match, no
  drift**. The four adapted files (`.claude-plugin/marketplace.json`,
  `test_kit.py`, `test_server.py`, `test_browser.py`) differ from upstream only
  in Gradiance's flat layout (`KIT = HERE`), its committed lane-board page and
  its own adapter. None of that is kit code that needs upstreaming.
- The venv `~/.local/share/gradiance-console/venv` (Python 3.11.8) holds
  PyJWT 2.15.0 and cryptography 50.0.1, the exact v0.8.7 pins, so it is reused.
- A detached worktree at v0.8.7 was tried in a scratch directory: its
  `agent.py health` and `inbox` answered from the live console's socket, its
  `server.py --help` ran under that venv, and its `git status` stayed clean.

Run these as yourself, in order. Each block ends with its check.

**1. The pin.**

    git -C ~/dev/projects/console-kit fetch --tags origin
    mkdir -p ~/.local/share/console-kit/releases
    git -C ~/dev/projects/console-kit worktree add --detach ~/.local/share/console-kit/releases/v0.8.7 v0.8.7
    ln -sfn ~/.local/share/console-kit/releases/v0.8.7 ~/.local/share/console-kit/current
    git -C ~/.local/share/console-kit/current describe --tags --exact-match     # v0.8.7
    git -C ~/.local/share/console-kit/current status --porcelain                # (nothing)
    ~/.local/share/gradiance-console/venv/bin/pip install -q -r ~/.local/share/console-kit/current/plugin/kit/requirements.txt

**2. The registry.** (No steward is set today. `register` keeps one if one is set.)

    python3 ~/.local/share/console-kit/current/plugin/kit/agent.py \
        --state ~/.local/state/gradiance-console register --project ~/dev/projects/gradiance
    cat ~/.config/console-kit/projects.json
    # "kit": "<your home>/.local/share/console-kit/releases/v0.8.7/plugin/kit"

**3. The server.** Every flag is the live unit's, unchanged. Only the script path moves.
Copy this block as it is: the heredoc's closing `EOF` must start its line.

```bash
mkdir -p ~/.config/systemd/user/gradiance-console.service.d
cat > ~/.config/systemd/user/gradiance-console.service.d/pinned-kit.conf <<'EOF'
# Run the console from the user-level pinned console-kit, not gradiance/tools/console-kit.
# See console-kit docs/MIGRATION.md. Remove this file to go back.
[Service]
ExecStart=
ExecStart=%h/.local/share/gradiance-console/venv/bin/python %h/.local/share/console-kit/current/plugin/kit/server.py \
  --root %h/dev/projects/gradiance \
  --page %h/dev/projects/gradiance/docs/dashboard/index.html \
  --state %h/.local/state/gradiance-console \
  --adapter %h/dev/projects/gradiance/scripts/register/console_adapter.py \
  --team-domain ${CONSOLE_TEAM_DOMAIN} \
  --aud ${CONSOLE_AUD} \
  --hostname ${CONSOLE_HOSTNAME} \
  --port ${CONSOLE_PORT} \
  --usage-file %h/.claude/usage/statusline-latest.json \
  --account-file %h/.claude.json \
  --project gradiance
EOF
systemctl --user daemon-reload
systemctl --user restart gradiance-console.service
systemctl --user status gradiance-console.service --no-pager | head -5
tr '\0' ' ' < /proc/$(systemctl --user show -p MainPID --value gradiance-console.service)/cmdline; echo
python3 ~/.local/share/console-kit/current/plugin/kit/agent.py --state ~/.local/state/gradiance-console health
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:4793/      # 403
```

Then open the console's hostname (`CONSOLE_HOSTNAME` in `console/deploy/console.env`)
and check the inbox.
`EnvironmentFile`, `WorkingDirectory` and `Restart` come from the base unit and
are unchanged. `gradiance-console-tunnel.service` points at port 4793 and needs
nothing.

**4. The follow timer.** Nothing to do yet. `console/deploy/follow-main.sh`
restarts the server when `tools/console-kit` changes on `main`
(`RESTART_PATHS`). With the drop-in in place, such a restart runs the pinned
kit, so it is harmless. The edit that drops `tools/console-kit` from that list
goes in the step 6 PR.

**5. The plugin.** Today `~/.claude/settings.json` (`extraKnownMarketplaces`)
and `~/.claude/plugins/known_marketplaces.json` both name
`~/dev/projects/gradiance/tools/console-kit`. The installed plugin
is recorded as **0.1.0** (`~/.claude/plugins/installed_plugins.json`, cache
`~/.claude/plugins/cache/console-kit/console-kit/0.1.0`, whose hooks differ
from v0.8.7's), so this step also brings the install up to date:

1. In `~/.claude/settings.json`, change
   `extraKnownMarketplaces."console-kit".source.path` to
   `~/.local/share/console-kit/current`
   (written out in full: JSON does not expand `~`).
2. In `claude`:

       /plugin marketplace remove console-kit
       /plugin marketplace add ~/.local/share/console-kit/current
       /plugin install console-kit@console-kit
       /reload-plugins

3. Start a new session in `~/dev/projects/gradiance`. `/plugin` shows
   console-kit 0.8.7, and the SessionStart note names
   `python3 <home>/.local/share/console-kit/releases/v0.8.7/plugin/kit/agent.py --state <home>/.local/state/gradiance-console`.

**6. The Gradiance PR, only after 1 to 5 check out.** Delete `tools/console-kit`
and update what names it (`git grep -l tools/console-kit` at `020441c`):

- `console/deploy/gradiance-console.service.in`: the `ExecStart` script path;
- `console/deploy/install.sh`: line 20 installs `tools/console-kit/requirements.txt`
  (with `set -e`, re-running the installer fails once the directory is gone);
- `console/deploy/follow-main.sh`: drop `tools/console-kit` from `RESTART_PATHS`;
  `console/deploy/test_follow_main.py` uses that path as its fixture;
- `.github/workflows/owner-console.yml`: it already checks out the release
  (`.kit-release`, pinned to v0.8.7's sha). Run the kit tests, `publish.py check`
  and the `fold.py` dry run from there, and drop the vendor-drift step;
- `scripts/register/test_dashboard.py` reads `tools/console-kit/console_kit/console.js`;
- `scripts/docs-currency-check.py`, `scripts/register/console_adapter.py`
  (docstring), and the specs that cite kit lines by vendored path
  (`architect/40-specs/owner-console.md`, `console-kit-multiproject.md`, and
  others the citation gate names). Gradiance's citation gate fails on a cited
  file that is gone, so these must move in the same PR.

The PR merges like any other. Within two minutes of the merge the follow timer
fast-forwards the service checkout, sees `tools/console-kit` change, and
restarts the server, which by then runs from the pin.

**Rollback, Gradiance.** Undo in reverse order: revert the step 6 PR; repeat
step 5 with the old path; delete `pinned-kit.conf`, `daemon-reload`, restart;
register with `python3 ~/dev/projects/gradiance/tools/console-kit/agent.py
--state ~/.local/state/gradiance-console register --project
~/dev/projects/gradiance`. The pin can stay; it is inert once nothing points
at it.

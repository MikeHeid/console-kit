# Installing overture

overture comes as one zip, `overture-<version>.zip`. It holds:

- a **Claude Code plugin**: a SessionStart note, skills to onboard a project and
  to process, fork and fold the owner's answers, and the committee agents;
- the **server kit** the plugin sets up (in `plugins/overture/kit/`).

You need Python 3.11 or newer, `systemd --user` (Linux or WSL2) to keep the
server running, and `cloudflared` with a Cloudflare account to reach it from
outside.

**WSL2 users:** run `loginctl enable-linger $USER` **once** on first install
so `systemd --user` units keep running after you close every terminal (the
default is to tear them down with your last session). WSL itself can still
stop when no Windows process uses it; a `wsl.exe -d <distro> echo ok` from a
Windows shell wakes it on demand.

**After plugin install or upgrade,** Claude's SessionStart hooks only load
the next time a session starts: open a fresh session (or `claude` CLI
invocation) to see the install-notice banner and the owner-console
SessionStart note. New skills and committee agents are available at once; it's
the hooks that need a session restart.

## 1. Add the plugin to Claude (Desktop or CLI)

Claude installs plugins from a **marketplace**, and the unzipped folder is one.

1. Unzip it somewhere it can stay; it makes a `overture/` folder, e.g. `~/overture/`. Claude
   reads the plugin from here, so do not delete the folder afterwards.
2. In Claude Desktop's **Code** tab, or in `claude` in a terminal, run:

       /plugin marketplace add ~/overture
       /plugin install overture@overture-local

3. Claude then asks for two **optional machine-wide defaults**. Both can be
   left empty and given per project later:
   - **Cloudflare Zero Trust team domain**, `<team>.cloudflareaccess.com`;
   - **Default domain for console hostnames**, e.g. `example.com`.
     Onboarding then suggests `<project>-console.example.com`.

   Neither is a secret. If Claude does not ask, or to change them later, run
   `/plugin configure overture@overture-local`.
4. Restart the session so the SessionStart hook loads.

**Or straight from GitHub**, with no zip, if your git can reach the repository:

    /plugin marketplace add MikeHeid/overture
    /plugin install overture@overture

Both routes install the same plugin folder, and the kit is inside it at
`kit/`. Use this plugin in a **local** session: remote/cloud sessions do not
load plugins ("Plugins aren't available in this environment").

## 2. Onboard a project

Open a session in the project's folder and run:

    /overture:console-onboard

It asks for:

| Value | Example | From |
|---|---|---|
| Project name | `acme` | you; it names the services and the state folder |
| Team domain | `acme.cloudflareaccess.com` | Zero Trust settings (step 1's default, if set) |
| AUD tag | 64 hex characters | the Access application's Overview (`kit/docs/CLOUDFLARE.md`) |
| Hostname | `acme-console.example.com` | a name in a zone on your Cloudflare account |
| Port | `4793` | any free port on 127.0.0.1 |

It then writes the project's settings (`.overture/console.env` and
`.overture.json`, none of them secret), plus a starter page and adapter.
Finally it prints the next commands. **You run those yourself**: they install
services, tell your Claude sessions to trust this project, and put a hostname
on the internet. The skill never runs them for you.

The same steps without Claude:

    python3 ~/overture/plugins/overture/kit/onboard.py write --project . \
        --name acme --team-domain acme.cloudflareaccess.com --aud <AUD> \
        --hostname acme-console.example.com

## 3. What the printed steps do

1. `deploy/install.sh --project . --start` copies the kit to
   `~/.local/share/overture/kit`, builds its venv (PyJWT, cryptography) and
   renders two `systemd --user` units, `<name>-console` and
   `<name>-console-tunnel`. It starts **only** the loopback server.
2. `agent.py register` adds this project to
   `~/.config/overture/projects.json`. The plugin's hook acts only in
   projects listed there. **A repository cannot register itself.**
3. You create the Access application in Cloudflare.
4. You create the tunnel, render its config with `onboard.py tunnel --id`,
   route DNS **with `--config`**, and start the tunnel unit.
5. Check it. `curl` on loopback must say **403**: the server refuses anything
   without an Access token. Then the hostname shows the Access login, then the
   page.

`kit/docs/CLOUDFLARE.md` walks through the Cloudflare screens and lists the usual
failures. `kit/docs/ADAPTER.md` explains how to connect the console to your
project's own task list. `kit/docs/DEPLOY.md` covers upgrades and rollback, and
`kit/docs/RUNBOOK.md` what to check when something is wrong.

## Updating

Delete the old `~/overture/` folder, unzip the new zip in its place (same
path), then:

    /plugin marketplace update overture-local
    /plugin update overture@overture-local

Restart the session. Then, for each onboarded project, re-run
`bash ~/overture/plugins/overture/kit/deploy/install.sh --project <dir> --start`.
It copies the new kit and restarts that project's server.

**Upgrading to 0.5.0.** Answers locked before 0.5.0 still go stale whenever
the whole file they hash changes. To re-anchor the ones git history can
justify, run this in each project, from a session:

    python3 <kit>/agent.py --state <state dir> reanchor --dry-run   # lists what would change, writes nothing
    python3 <kit>/agent.py --state <state dir> reanchor

`agent.py check` shows why each answer that is left is still stale.

**Upgrading to 0.6.0.** Nothing to migrate: 0.6.0 adds no store record, so a
0.5.0 kit still reads a 0.6.0 store. It adds `/health` (on the agent socket,
and on a loopback port only if you pass `--health-port`), the "agent listening"
line in the status bar, and `kit/tools/verify_vendor.py`. `kit/docs/DEPLOY.md`
and `kit/docs/RUNBOOK.md` cover running it.

**Upgrading to 0.7.0.** Nothing to migrate, and no new record kind. Two new
record shapes, both optional: a chat message (an owner `message` on the
reserved thread `@chat` with intent `chat`) and a question's `evidence`
rows. The page starts its live loop by itself; restart the server so it
serves the new page and routes (`/api/wait`, `/api/feed`, `/api/evidence`,
`/api/lock-all`), all behind the Access gate. A session watching with
`agent.py watch` now also wakes on a chat message; update the plugin so its
console-process skill answers them.

**Upgrading to 0.8.0.** Nothing to migrate. Two new record kinds, both
written by the agent (`transcript`, a roar panel's transcript; `visual`, an
agent-drawn Mermaid block or HTML mock), and three new message shapes (intent
`visual`, the seat `roar`, and `step: refine|drill`). Add the optional
`specs_dir`, `visuals_dir` and `next_step` keys to each project's
`.overture.json` (`kit/docs/ADAPTER.md` explains them), then restart the
server: it reads them at start, and refuses a bad one by name. A session's
`agent.py watch` now also wakes on a visual request; update the plugin so its
skills (console-fork's roar, refine and drill sections, and the new
console-visual skill) answer them.

**Upgrading to 0.8.1.** The server no longer writes visuals into the
project's checkout: it stores them in its state directory
(`<state>/visuals/<item>/`), and `visuals_dir` is only where an agent lands
them by pull request, with `agent.py visual-export --project <its own
worktree>`. The store is unchanged (schema 1, no new record kind). If a 0.8.0
server already wrote visuals into the checkout, **the 0.8.1 server ignores
them** and never deletes them: move them once, as `kit/docs/RUNBOOK.md`
("Upgrading to 0.8.1") says, then restart. Update the plugin too, so the
console-visual skill exports instead of copying from the checkout. A 0.8.1
server also runs every git command in the project with
`--no-optional-locks`, so reading it never rewrites `.git/index`.

**Upgrading to 0.8.2.** Nothing to migrate: the store, its schema (1) and
every record shape are unchanged. Sessions may now name themselves
(`agent.py --as agent-6`, or `OVERTURE_AGENT=agent-6`), and the console
shows the name on what each writes; update the plugin so the console-process
skill says how. Each session's "agent active" marks are now its own, so one
session's `synced` no longer clears another's; a `working.json` a 0.8.1
server wrote is read as the unnamed sessions' marks. `agent.py
visual-export` now exits 3, not 1, when it wrote files but refused a visual.
A 0.8.1 kit reads a store 0.8.2 wrote without a change: the names are kept
beside it, in `<state>/names.jsonl`, and a 0.8.1 kit shows every agent as
"agent" (`kit/docs/DEPLOY.md`, "Rollback").

**Upgrading to 0.8.3.** Nothing to migrate, and nothing changes until you
name a steward: `agent.py --state <state> steward agent-5` (CHANGELOG, "One
steward, many sessions"). Then only the session started with
`OVERTURE_AGENT=agent-5` may `watch`, `synced` or fold, and the plugin's
new PreToolUse hook blocks `AskUserQuestion` in every other session of that
project, pointing it at `agent.py ask`. Update the plugin for the hook and
the skills. Every id-shaped field (item ids, qids, record ids, nonces,
option ids, sources, cites and the like) now also refuses a trailing
newline, which 0.8.2 let through; no honest client sends one. **Before going
back to 0.8.2, clear the steward** (`agent.py steward --clear`): a 0.8.2
SessionStart hook reads an entry with a `steward` key as malformed and says
"not checked" instead of listing the owner's requests.

**Upgrading to 0.8.13.** The store is unchanged. Three additions:

- **Settling stale answers.** The owner's acts are kept in `refactor.jsonl`
  beside `store.jsonl`; back them up together. Fold refuses an answer that
  carries an outcome until the project's adapter sets
  `RECORDS_OUTCOMES = True` and writes the outcome
  (`docs/MIGRATION.md`).
- **Pull requests.** The PRs tab stays empty until the steward runs
  `agent.py prs-push` from the project's checkout, with `gh` logged in.
- **Plugin-shipped skills.** Nothing to migrate.

The plugin now ships `roar`, `refine`, `drill` and `deliberate`, so
Refine and Drill no longer need a skill installed by hand. An existing
project may switch its `.overture.json` to the plugin's names, by pull
request, then restart the server:

    "next_step": {"refine": "overture:refine", "drill": "overture:drill"}

A project that keeps `{"refine": "refine", "drill": "drill"}` keeps working
for as long as those user-level skills are installed: the lookup and its
rules are unchanged (installed user or plugin skills only, never a skill
inside the repository). Re-running onboarding adds the plugin's names only
for a kind the config does not already name. Before going back to an older
kit, switch `next_step` back to user-level skills: an older plugin ships no
`refine` or `drill`, so once the newer plugin is no longer in Claude's plugin
cache, `overture:refine` resolves to nothing and Refine and Drill refuse,
naming the skill. While a newer copy is still cached they keep resolving to
it.

**Going back to an older kit.** A 0.8.0 kit reads a store 0.8.1 wrote, but
it looks for each visual 0.8.1 stored in its own checkout: it refuses the
path by name (`not under visuals_dir`), or, if its `visuals_dir` is
`visuals`, shows it only when a file there still has the stored sha256.
Nothing else changes meaning. A 0.7.0 kit refuses a store that holds any
0.8.0 record, and names it (`unknown record type 'transcript'`, `unknown
record type 'visual'`, `intent 'visual' is not one of fork, process, chat`,
`role(s) ['roar'] are not one of ...`, `message: unknown field(s) step`). The
first such record is written when you request a visual or pick Roar, Refine
or Drill; after that, going back means 0.8.0 or later.
A 0.6.0 kit refuses a store that holds a
0.7.0 chat message or a question with evidence, and names what it cannot
read (`intent 'chat' is not one of fork, process`, `item '@chat' is not an
item id`, `question: unknown field(s) evidence`). The first such record is
written when you send a chat message or an agent asks a question with
evidence; after that, going back means 0.7.0 or later.
A 0.4.0 kit refuses a store that holds a
0.5.0 record, and names the line and the field (for example `lock: unknown
field(s) anchors`). It never drops a field it does not know. The first such
record appears when you re-lock an answer and it is re-anchored, or when
`reanchor` or a question with an `excerpt` is written. After that, going back
means moving to 0.5.0 or later again, not to 0.4.0.

## Removing

    systemctl --user disable --now <name>-console <name>-console-tunnel
    /plugin uninstall overture@overture-local

Then delete the project's entry from `~/.config/overture/projects.json`.
Delete the tunnel and its DNS record in Cloudflare yourself.

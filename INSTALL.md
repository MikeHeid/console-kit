# Installing console-kit

console-kit comes as one zip, `console-kit-<version>.zip`. It holds:

- a **Claude Code plugin**: a SessionStart note, skills to onboard a project and
  to process, fork and fold the owner's answers, and the committee agents;
- the **server kit** the plugin sets up (in `plugins/console-kit/kit/`).

You need Python 3.11 or newer, `systemd --user` (Linux or WSL2) to keep the
server running, and `cloudflared` with a Cloudflare account to reach it from
outside.

## 1. Add the plugin to Claude (Desktop or CLI)

Claude installs plugins from a **marketplace**, and the unzipped folder is one.

1. Unzip it somewhere it can stay; it makes a `console-kit/` folder, e.g. `~/console-kit/`. Claude
   reads the plugin from here, so do not delete the folder afterwards.
2. In Claude Desktop's **Code** tab, or in `claude` in a terminal, run:

       /plugin marketplace add ~/console-kit
       /plugin install console-kit@console-kit-local

3. Claude then asks for two **optional machine-wide defaults**. Both can be
   left empty and given per project later:
   - **Cloudflare Zero Trust team domain**, `<team>.cloudflareaccess.com`;
   - **Default domain for console hostnames**, e.g. `example.com`.
     Onboarding then suggests `<project>-console.example.com`.

   Neither is a secret. If Claude does not ask, or to change them later, run
   `/plugin configure console-kit@console-kit-local`.
4. Restart the session so the SessionStart hook loads.

## 2. Onboard a project

Open a session in the project's folder and run:

    /console-kit:console-onboard

It asks for:

| Value | Example | From |
|---|---|---|
| Project name | `acme` | you; it names the services and the state folder |
| Team domain | `acme.cloudflareaccess.com` | Zero Trust settings (step 1's default, if set) |
| AUD tag | 64 hex characters | the Access application's Overview (`docs/CLOUDFLARE.md`) |
| Hostname | `acme-console.example.com` | a name in a zone on your Cloudflare account |
| Port | `4793` | any free port on 127.0.0.1 |

It then writes the project's settings (`.console-kit/console.env` and
`.console-kit.json`, none of them secret), plus a starter page and adapter.
Finally it prints the next commands. **You run those yourself**: they install
services, tell your Claude sessions to trust this project, and put a hostname
on the internet. The skill never runs them for you.

The same steps without Claude:

    python3 ~/console-kit/plugins/console-kit/kit/onboard.py write --project . \
        --name acme --team-domain acme.cloudflareaccess.com --aud <AUD> \
        --hostname acme-console.example.com

## 3. What the printed steps do

1. `deploy/install.sh --project . --start` copies the kit to
   `~/.local/share/console-kit/kit`, builds its venv (PyJWT, cryptography) and
   renders two `systemd --user` units, `<name>-console` and
   `<name>-console-tunnel`. It starts **only** the loopback server.
2. `agent.py register` adds this project to
   `~/.config/console-kit/projects.json`. The plugin's hook acts only in
   projects listed there. **A repository cannot register itself.**
3. You create the Access application in Cloudflare.
4. You create the tunnel, render its config with `onboard.py tunnel --id`,
   route DNS **with `--config`**, and start the tunnel unit.
5. Check it. `curl` on loopback must say **403**: the server refuses anything
   without an Access token. Then the hostname shows the Access login, then the
   page.

`docs/CLOUDFLARE.md` walks through the Cloudflare screens and lists the usual
failures. `docs/ADAPTER.md` explains how to connect the console to your
project's own task list.

## Updating

Delete the old `~/console-kit/` folder, unzip the new zip in its place (same
path), then:

    /plugin marketplace update console-kit-local
    /plugin update console-kit@console-kit-local

Restart the session. Then, for each onboarded project, re-run
`bash ~/console-kit/plugins/console-kit/kit/deploy/install.sh --project <dir> --start`.
It copies the new kit and restarts that project's server.

## Removing

    systemctl --user disable --now <name>-console <name>-console-tunnel
    /plugin uninstall console-kit@console-kit-local

Then delete the project's entry from `~/.config/console-kit/projects.json`.
Delete the tunnel and its DNS record in Cloudflare yourself.

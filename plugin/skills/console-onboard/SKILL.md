---
name: console-onboard
description: Use when the user wants to set up the owner console for the current project (onboard, install, connect it to Cloudflare), or asks how to get the console running. Asks for the project name and the Cloudflare values, writes the project's non-secret config with the kit's onboard.py, and hands the user the commands only they may run.
---

# Onboard this project onto the owner console

The owner console is a web page, behind Cloudflare Access, where the owner
answers an agent's questions. Onboarding a project means five values, one
script run, and a short list of commands **the user runs themselves**.

## What you must never do here

- **Never open, print or copy** `~/.cloudflared/cert.pem` or any
  `~/.cloudflared/*.json`. They are Cloudflare secrets. The kit only ever
  checks that a credentials file exists.
- **Never run** `deploy/install.sh` (with or without `--start`),
  `agent.py register`, `cloudflared tunnel create`,
  `cloudflared tunnel route dns`, or `systemctl --user enable`/`start`. The
  installer sets up a service that **imports and runs the project's
  `.console-kit/adapter.py` as Python**. Registering tells every Claude
  session to trust this project's console. The tunnel and DNS make it
  reachable from the internet. They are the user's decisions, so you print
  them and the user runs them.
- **Never pass `--force`** unless the user has seen the file it would replace
  and said to. **Never delete or replace a symbolic link** that `onboard.py`
  refused; tell the user what it points at instead.
- **Show every `REVIEW` line** `onboard.py` prints. It means an adapter or
  page was already in the project, and the server will run it.
- **Never invent a value.** An AUD tag or team domain you did not get from the
  user is a console nobody can log in to.

## 1. Find the kit

The kit ships inside this plugin, at `${CLAUDE_SKILL_DIR}/../../kit`, however the
plugin was installed (from the release zip or straight from the repository).
Call that folder `KIT`.

`KIT/onboard.py` must exist. If it does not, stop and say the plugin is
incomplete.

## 2. Ask for the values

Ask with AskUserQuestion where it helps, and in plain text otherwise. Offer
these defaults, and let the user change every one:

| Value | Default | Notes |
|---|---|---|
| Project name | the project directory's name, lowercased, spaces to hyphens | 2-32 of a-z, 0-9, hyphen; it names the services and the state folder |
| Team domain | `${user_config.team_domain}` if set | `<team>.cloudflareaccess.com` |
| AUD tag | none; the user pastes it | 64 hex characters, from the Access application (step 3 below) |
| Hostname | `<name>-console.${user_config.zone}` if a zone is set | must be in a zone on their Cloudflare account |
| Port | 4793 | the script checks it is free on 127.0.0.1 |

- If a default in the table above is empty or still reads as a literal
  `${user_config...}`, the user has not set it in the plugin's settings. Just
  ask for it.
- **The AUD tag comes from an Access application that may not exist yet.** If
  the user has none, walk them through `KIT/docs/CLOUDFLARE.md`, section
  "Access application", first. Onboarding can be re-run later with the real
  tag.
- Optional: an **audit seat**, the project's own review, which joins the
  deliberation committee (see `KIT/README.md`). Skip it unless the user has one.

## 3. Write the config

Run from the project root:

    python3 "KIT/onboard.py" write --project . --name NAME --team-domain TEAM \
        --aud AUD --hostname HOST --port PORT

- **Refused (exit 2):** the message names the bad value and says what it
  should be. Ask for that one value again. Never edit the files by hand to
  get past a refusal.
- **"exists and was not written by onboard.py":** show the user the file. Pass
  `--force` only if they say to replace it.
- **Success:** it writes `.console-kit/console.env`, merges
  `.console-kit.json`, and adds a starter `.console-kit/adapter.py` and
  `.console-kit/page.html` when the project has none. Then it prints the
  next steps.

## 4. Hand over the next steps

Show the user the commands the script printed, **verbatim and in order**, and
say plainly that each is theirs to run:

1. `install.sh --start`: the loopback server;
2. `agent.py register`: trust this project's console;
3. create the Access application;
4. create the tunnel, `onboard.py tunnel --id`, route DNS **with `--config`**,
   then start the tunnel unit;
5. check it: 403 on loopback without a token, then an Access login at the
   hostname.

Point them at `KIT/docs/CLOUDFLARE.md` for the Cloudflare screens. Offer to
explain any step. Offer, too, to replace the starter page and adapter with the
project's own (`KIT/docs/ADAPTER.md`).

When they have run step 2, the SessionStart note and the `console-process`
skill work in this project from the next session.

# console-kit

**Answer your agents' questions from a web page.** An agent working in a
project asks the owner structured questions: options, a recommended pick, and
what each costs. The owner answers from any browser, behind Cloudflare Access,
and can:

- lock an answer;
- change it;
- ask for a **deliberation round** (a committee of reviewer agents looks at
  one item and comes back with decisions to lock).

The next Claude session in that project is told what arrived, processes it,
and folds locked answers into the project's own record.

## Download

Get **`console-kit-<version>.zip`** from
[Releases](https://github.com/MikeHeid/console-kit/releases). Unzip it
somewhere it can stay (it makes a `console-kit/` folder). Then, in Claude
Desktop's **Code** tab or in `claude`:

    /plugin marketplace add ~/console-kit
    /plugin install console-kit@console-kit-local

Then, in your project:

    /console-kit:console-onboard

It asks for the project name and your Cloudflare values, and tells you what
to run next. `INSTALL.md` (also in the zip) has the whole guide.

- **[INSTALL.md](INSTALL.md)**: install the plugin in Claude Desktop or the
  CLI, and onboard a project.
- **[docs/CLOUDFLARE.md](plugin/kit/docs/CLOUDFLARE.md)**: the Access application, the
  tunnel, DNS.
- **[docs/ADAPTER.md](plugin/kit/docs/ADAPTER.md)**: connect the console to your
  project's work items and decision log.

## How it fits together

```
 browser ──Access login──▶ Cloudflare ──tunnel──▶ 127.0.0.1:PORT  server.py
                                                     │ checks the Access JWT (team keys + AUD)
                                                     ▼
                                              state dir: store + doorbell
                                                     ▲
 Claude session ── SessionStart hook reads the doorbell (registered projects only)
                └─ skills: console-process / console-fork / console-fold ── agent.py
```

- **`server.py`** serves one page (yours) with the console injected. It
  refuses every request without a valid Access token, loopback included.
- **`agent.py`** is the agent's side: `view`, `ask`, `reply`, `inbox`,
  `working` (shows you "agent active" on the items it has picked up),
  `synced`, `fork-context`, and `register`, which **only you** run.
- **`fold.py`** writes locked answers into your project through the adapter.
- **`plugin/`** holds the Claude Code plugin: the hook, the skills and the
  committee agents.
- **`onboard.py`** and **`deploy/`** onboard a project and install the
  services.

## Trust

- The plugin's hook runs in every project you open. It acts only in projects
  listed in `~/.config/console-kit/projects.json`, which only
  `agent.py register` writes. A cloned repository's `.console-kit.json` is
  never trusted by itself.
- The kit never opens `~/.cloudflared/cert.pem` or a tunnel credentials file.
  It checks that the file exists, and nothing more.
- Nothing onboarding writes is a secret. The AUD tag and team domain are what
  tokens are *checked against*, and no token can be made from them.

## Development

    python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
    .venv/bin/python test_kit.py && .venv/bin/python test_server.py && .venv/bin/python test_onboard.py
    .venv/bin/python test_build.py
    CONSOLE_KIT_BROWSER=1 .venv/bin/python test_browser.py   # needs playwright + browsers

To build the release zip:

    python3 build_zip.py --deny-file ~/my-deployment-values.txt

It runs `claude plugin validate --strict` on the result. The deny file lists
strings from your own deployment, such as your hostnames, AUD tags, team
domain and home path. The build refuses if any of them appears in the zip.

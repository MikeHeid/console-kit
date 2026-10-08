#!/usr/bin/env python3
"""Onboard one project onto the owner console: validate its answers, write its
non-secret config, and print the commands the OWNER runs.

    python3 onboard.py write  --project DIR --name acme --team-domain acme.cloudflareaccess.com \
                              --aud <64 hex> --hostname acme-console.example.com [--port 4793] ...
    python3 onboard.py tunnel --project DIR --id <tunnel uuid>

`write` creates, inside the project:

    .overture/console.env     the server's settings (none of them a secret)
    .overture.json            fold paths, the adapter, the audit seat, the next-step skills
                                 (merged, never clobbered)
    .overture/adapter.py      a starter adapter, only when the project has none
    .overture/page.html       a starter page, only when the project has none

`tunnel` renders ~/.cloudflared/config-<tunnel>.yml once `cloudflared tunnel
create` has given the tunnel its id.

What it never does, on purpose:
- it never opens a Cloudflare credentials file or cert.pem. It checks only that
  the credentials file EXISTS, by path, so the config it writes points at a real
  file;
- it never creates a tunnel, writes DNS, starts a service or registers the
  project. Those make the console reachable, or tell your Claude sessions to
  trust this project's doorbell, so they are commands printed for you to run;
- it never overwrites a file it did not write before, unless --force is passed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
from pathlib import Path

KIT = Path(__file__).resolve().parent

NAME = re.compile(r"^[a-z][a-z0-9-]{0,30}[a-z0-9]\Z")
TEAM = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.cloudflareaccess\.com\Z")
AUD = re.compile(r"^[0-9a-f]{64}\Z")
LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
HOST = re.compile(rf"^(?:{LABEL}\.)+[a-z]{{2,63}}\Z")
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
REL = re.compile(r"^[A-Za-z0-9_.][A-Za-z0-9_./-]{0,200}\Z")
MARK = "# written by overture onboard.py"
# The skills the plugin ships for the two next-step forks (resolved as an installed plugin's
# `<plugin>:<name>`, never from the repository: overture/projectcfg.py `resolve_skill`).
DEFAULT_NEXT_STEP = {"refine": "overture:refine", "drill": "overture:drill"}


class OnboardError(ValueError):
    pass


def check(answers: dict) -> dict:
    """Every answer, validated and normalised; the first bad one is refused by name."""
    a = {k: (v.strip() if isinstance(v, str) else v) for k, v in answers.items()}
    for key in ("name", "team_domain", "hostname", "tunnel", "aud"):
        if isinstance(a.get(key), str):
            a[key] = a[key].lower()
    if not a.get("tunnel") and isinstance(a.get("name"), str):
        a["tunnel"] = f"{a['name']}-console"
    rules = [
        ("name", NAME, "2-32 lowercase letters, digits and hyphens, starting with a letter (e.g. acme)"),
        ("team_domain", TEAM, "your Zero Trust team domain, <team>.cloudflareaccess.com"),
        ("aud", AUD, "the Access application's AUD tag: 64 hex characters"),
        ("hostname", HOST, "a public hostname in a zone on your Cloudflare account, e.g. acme-console.example.com"),
        ("tunnel", NAME, "a tunnel name made like the project name (default <name>-console)"),
    ]
    for key, rx, want in rules:
        if not isinstance(a.get(key), str) or not rx.match(a[key]):
            raise OnboardError(f"{key}: {a.get(key)!r} is not valid: expected {want}")
    if a["hostname"].endswith(".cloudflareaccess.com"):
        raise OnboardError("hostname: that is the team domain; give the console's own hostname")
    port = a.get("port", 4793)
    try:
        port = int(port)
    except (TypeError, ValueError):
        raise OnboardError(f"port: {port!r} is not a number") from None
    if not 1024 <= port <= 65535:
        raise OnboardError(f"port: {port} is outside 1024-65535")
    a["port"] = port
    for key, default in (("page", ".overture/page.html"), ("adapter", ".overture/adapter.py")):
        v = a.get(key) or default
        if not isinstance(v, str) or not REL.match(v) or ".." in Path(v).parts:
            raise OnboardError(f"{key}: {v!r} must be a plain relative path inside the project")
        a[key] = v
    return a


def port_free(port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _target(base: Path, rel: str) -> Path:
    """`base/rel`, refused when it or any directory on the way is a symbolic link, or it lands outside `base`.

    A cloned repository can ship a DANGLING link at a path onboarding writes
    (`.overture/adapter.py -> ~/.bashrc`): `exists()` follows it, says no,
    and a plain write would create the link's target wherever it points. So
    every component is checked with lstat, never followed, and `--force`
    does not override this.
    """
    p = base
    for part in Path(rel).parts:
        p = p / part
        if p.is_symlink():
            raise OnboardError(f"{p} is a symbolic link; onboarding writes only real files inside {base}")
    if not p.resolve().is_relative_to(base.resolve()):
        raise OnboardError(f"{p} resolves outside {base}")
    return p


def _write(path: Path, text: str, force: bool) -> str:
    if path.is_symlink():  # the last line of defence; _target refuses these first
        raise OnboardError(f"{path} is a symbolic link; refusing to write through it")
    if path.exists():
        old = path.read_text(encoding="utf-8", errors="replace")
        if old == text:
            return f"unchanged {path}"
        if MARK not in old and not force:
            raise OnboardError(f"{path} exists and was not written by onboard.py; pass --force to replace it")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return f"wrote     {path}"


def env_text(a: dict) -> str:
    return "\n".join([
        MARK,
        "# The owner console's settings for this project. None of these is a secret: the AUD",
        "# tag and team domain are what the server CHECKS a token against; a token cannot be",
        "# made from them. The tunnel's credentials file is never named or read here.",
        f"CONSOLE_NAME={a['name']}",
        f"CONSOLE_TEAM_DOMAIN={a['team_domain']}",
        f"CONSOLE_AUD={a['aud']}",
        f"CONSOLE_HOSTNAME={a['hostname']}",
        f"CONSOLE_PORT={a['port']}",
        f"CONSOLE_TUNNEL={a['tunnel']}",
        f"CONSOLE_PAGE={a['page']}",
        f"CONSOLE_ADAPTER={a['adapter']}",
        "",
    ])


def config_json(project: Path, a: dict) -> str:
    path = project / ".overture.json"
    cfg = {}
    if path.exists():
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as e:
            raise OnboardError(f"{path} is not valid JSON ({e}); fix or remove it first") from None
        if not isinstance(cfg, dict):
            raise OnboardError(f"{path} must hold a JSON object")
    fold = cfg.setdefault("fold", {})
    if not isinstance(fold, dict):
        raise OnboardError(f"{path}: \"fold\" must be an object")
    fold.setdefault("locked", ".overture/locked")
    fold.setdefault("ledger", ".overture/folded.txt")
    fold["adapter"] = a["adapter"]
    # Refine and drill run a skill the plugin itself ships, so a fresh install has them. A kind
    # already named is kept as it is: a project that installed its own skill keeps using it.
    steps = cfg.setdefault("next_step", {})
    if not isinstance(steps, dict):
        raise OnboardError(f"{path}: \"next_step\" must be an object")
    for kind, skill in DEFAULT_NEXT_STEP.items():
        steps.setdefault(kind, skill)
    if a.get("audit_seat"):
        cfg["audit"] = {"seat": a["audit_seat"], "brief": a.get("audit_brief") or ""}
    return json.dumps(cfg, indent=2) + "\n"


def state_dir(name: str) -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    return base / "overture" / name


def write(project: Path, answers: dict, force: bool = False, check_port: bool = True) -> list[str]:
    project = project.resolve()
    if not project.is_dir():
        raise OnboardError(f"project: {project} is not a directory")
    a = check(answers)
    if check_port and not port_free(a["port"]):
        raise OnboardError(f"port: {a['port']} is already in use on 127.0.0.1; pick another with --port")
    # Every target is checked BEFORE anything is written, so a refusal leaves the project untouched.
    env, cfg = _target(project, ".overture/console.env"), _target(project, ".overture.json")
    starters = [(_target(project, a[k]), t) for k, t in (("adapter", "adapter_template.py"), ("page", "demo/index.html"))]
    new_cfg = config_json(project, a)  # parse the old config BEFORE writing anything
    out = [_write(env, env_text(a), force)]
    if not cfg.exists() or cfg.read_text(encoding="utf-8") != new_cfg:
        cfg.write_text(new_cfg, encoding="utf-8")  # a merge: every key already there is kept
        out.append(f"wrote     {cfg}")
    for target, template in starters:
        if not target.exists():
            out.append(_write(target, (KIT / template).read_text(encoding="utf-8"), force))
        else:
            # The server imports the adapter and runs it as Python, and serves the page with the
            # console in it. One already here may have come with a checkout, and no marker in it
            # can prove otherwise (a hostile file would carry the marker too): always say so.
            out.append(f"REVIEW    {target}: already here, so it was left as it is. The server runs it: "
                       "know what it holds before you run install.sh.")
    # 1.6.1: ensure `.overture/console.env` is gitignored. The file holds the AUD tag + team domain —
    # not secret by the kit's account, but shaped like credentials and Claude Code's safety check
    # treats them that way, so a commit mid-onboarding gets blocked. Adding one line to .gitignore
    # resolves that cleanly for the owner.
    gi_line = ".overture/console.env"
    out.append(_ensure_gitignore(project, gi_line))
    return out


def _ensure_gitignore(project: Path, line: str) -> str:
    """Append `line` to the project's root .gitignore if it is not already there. Idempotent.

    Creates .gitignore when absent. Reports what happened as one output line. Any read/write OSError
    is swallowed into a REVIEW line, so a missing or unwritable .gitignore does not fail onboarding.
    """
    path = project / ".gitignore"
    try:
        current = path.read_text(encoding="utf-8") if path.exists() else ""
    except OSError as e:
        return f"REVIEW    {path}: could not read ({e}); add `{line}` yourself if you track {line} content"
    needle = line.strip()
    for existing in current.splitlines():
        if existing.strip() == needle:
            return f"kept      {path}: already lists {line}"
    sep = "" if (current == "" or current.endswith("\n")) else "\n"
    try:
        path.write_text(current + sep + line + "\n", encoding="utf-8")
    except OSError as e:
        return f"REVIEW    {path}: could not write ({e}); add `{line}` yourself"
    return f"wrote     {path}: appended `{line}`"


def next_steps(project: Path, a: dict) -> str:
    project = project.resolve()
    cfg = Path.home() / ".cloudflared" / f"config-{a['tunnel']}.yml"
    cert = Path.home() / ".cloudflared" / "cert.pem"
    login = "" if cert.exists() else "      cloudflared tunnel login        # once per machine; writes cert.pem\n"
    return f"""
Next, run these yourself, in order. Each one is yours to approve:

 1. Install the server (a venv, the kit, systemd user units; starts only the loopback server):
      bash "{KIT}/deploy/install.sh" --project "{project}" --start
 2. Let your Claude sessions trust this project's console. It is a trust decision, so a
    session never runs it for you:
      python3 ~/.local/share/overture/kit/agent.py --state "{state_dir(a['name'])}" register --project "{project}"
 3. Create the Access application in Cloudflare Zero Trust for https://{a['hostname']}
    (docs/CLOUDFLARE.md, "Access application"). Its AUD tag is the one you gave.
 4. Create the tunnel, and route DNS WITH --config, or the record can land on another tunnel:
{login}      cloudflared tunnel create {a['tunnel']}
      python3 "{KIT}/onboard.py" tunnel --project "{project}" --id <the tunnel id it printed>
      cloudflared tunnel --config "{cfg}" route dns --overwrite-dns <tunnel id> {a['hostname']}
      systemctl --user enable --now {a['name']}-console-tunnel
 5. Check it:
      curl -s -o /dev/null -w '%{{http_code}}\\n' http://127.0.0.1:{a['port']}/api/view   # 403: no token, refused
      then open https://{a['hostname']}: an Access login, then the page
 6. Push the project's items so an agent can `ask` on anything but the starter PROJECT item.
    The server never runs your adapter (CONSOLE-kit/Q24); run this now and whenever the item
    list or the adapter changes:
      python3 ~/.local/share/overture/kit/agent.py --state "{state_dir(a['name'])}" items-push --adapter "{a['adapter']}"
 7. (Optional, for the dashboard page) Send the page from a merged commit, then press
    "Use this page" in the console. Reads the default branch from origin/HEAD (0.9.1):
      python3 ~/.local/share/overture/kit/agent.py --state "{state_dir(a['name'])}" page-snapshot --path "{a['page']}"
"""


def read_env(path: Path) -> dict:
    if not path.exists():
        raise OnboardError(f"{path} is missing: run `onboard.py write` first")
    env = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    keys = {"name": "CONSOLE_NAME", "team_domain": "CONSOLE_TEAM_DOMAIN", "aud": "CONSOLE_AUD",
            "hostname": "CONSOLE_HOSTNAME", "port": "CONSOLE_PORT", "tunnel": "CONSOLE_TUNNEL",
            "page": "CONSOLE_PAGE", "adapter": "CONSOLE_ADAPTER"}
    a = check({k: env.get(v) for k, v in keys.items()})
    # Only what was validated leaves this function, in its normalised form: an
    # unknown key in the file is ignored, never passed on to a unit file.
    return {v: str(a[k]) for k, v in keys.items()}


def tunnel(project: Path, tunnel_id: str, force: bool = False) -> str:
    env = read_env(project.resolve() / ".overture/console.env")
    tid = tunnel_id.strip().lower()
    if not UUID.match(tid):
        raise OnboardError(f"id: {tunnel_id!r} is not a tunnel id (a UUID, as `cloudflared tunnel create` prints)")
    creds = Path.home() / ".cloudflared" / f"{tid}.json"
    if not creds.exists():  # existence only: the file is a secret and is never opened
        raise OnboardError(f"no credentials file at {creds}: run `cloudflared tunnel create` on THIS machine first")
    text = (KIT / "deploy/cloudflared.yml.in").read_text(encoding="utf-8")
    for key, value in {"@TUNNEL_ID@": tid, "@CREDENTIALS@": str(creds), "@HOSTNAME@": env["CONSOLE_HOSTNAME"],
                       "@PORT@": env["CONSOLE_PORT"], "@NAME@": env["CONSOLE_NAME"]}.items():
        text = text.replace(key, value)
    return _write(_target(Path.home() / ".cloudflared", f"config-{env['CONSOLE_TUNNEL']}.yml"), MARK + "\n" + text, force)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Onboard a project onto the owner console.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("write", help="validate the answers and write the project's config")
    w.add_argument("--project", type=Path, default=Path.cwd())
    w.add_argument("--name", required=True)
    w.add_argument("--team-domain", required=True)
    w.add_argument("--aud", required=True)
    w.add_argument("--hostname", required=True)
    w.add_argument("--tunnel")
    w.add_argument("--port", type=int, default=4793)
    w.add_argument("--page")
    w.add_argument("--adapter")
    w.add_argument("--audit-seat")
    w.add_argument("--audit-brief")
    w.add_argument("--force", action="store_true")
    w.add_argument("--no-port-check", action="store_true", help="skip the free-port check (re-running on a live port)")
    s = sub.add_parser("show", help="print the project's settings, re-validated (install.sh reads this)")
    s.add_argument("--project", type=Path, default=Path.cwd())
    t = sub.add_parser("tunnel", help="render the cloudflared config once the tunnel exists")
    t.add_argument("--project", type=Path, default=Path.cwd())
    t.add_argument("--id", required=True)
    t.add_argument("--force", action="store_true")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "write":
            answers = {k: v for k, v in vars(a).items()
                       if k not in ("cmd", "project", "force", "no_port_check") and v is not None}
            for line in write(a.project, answers, a.force, not a.no_port_check):
                print(line)
            print(next_steps(a.project, check(answers)))
        elif a.cmd == "show":
            for k, v in sorted(read_env(a.project.resolve() / ".overture/console.env").items()):
                print(f"{k}={v}")
        else:
            print(tunnel(a.project, a.id, a.force))
    except OnboardError as e:
        print(f"onboard: {e}", file=sys.stderr)
        return 2
    except OSError as e:  # a read-only mount, a full disk, a permission: named, never a traceback
        print(f"onboard: {e.filename or ''}: {e.strerror or e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""The one console server's file: which projects it hosts, and where (K3, spec §3.4, §3.5).

    ${XDG_CONFIG_HOME:-~/.config}/console-kit/server.json
    {"team_domain": "team.example.cloudflareaccess.com",
     "projects": {"<name>": {"state": "<abs dir>", "root": "<abs dir>", "page": "<path under root>",
                             "hostname": "<bare DNS name>", "aud": "<Access AUD>", "port": N,
                             "slugs": ["<one directory name>", ...]}}}

It sits beside the user's registry and, like it, is written only by a command
the user runs (`agent.py server add`), never by a session and never from a
repository. It is a SEPARATE file on purpose (§1's trap): the registry's
entries keep their exact shape and bytes, so every installed plugin copy and
SessionStart hook still reads them. This module reads `projects.json` and
never writes it.

A project's name is the user's word, given on the command line: it is never
read from the repository's `.console-kit.json`, whatever keys that file holds.

The cost collector (K2) reads `slugs` from here too, through `slugs_for_state`:
this is the one reader of the file. Keys it does not know are ignored, so K4
can add each project's token hash without a reader change.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

from . import names as N
from . import registry as R

FILE = "server.json"
MAX_FILE = 1 << 20
SLUG = re.compile(r"^[A-Za-z0-9-]{1,255}\Z")   # one directory name: never "/", "." or ".."
# A bare DNS name: lowercase labels of letters, digits and inner hyphens, dot-separated. No scheme, port,
# path, "@", whitespace or trailing dot; it is put into an Origin check and a deep link, never parsed.
HOSTNAME = re.compile(r"^(?=.{1,253}\Z)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+\Z")
AUD = re.compile(r"^[A-Za-z0-9_-]{1,256}\Z")
TEAM = HOSTNAME
PAGE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]{0,511}\Z")


class ServerFileError(ValueError):
    pass


def location() -> Path:
    return R.location().parent / FILE


def _page_problem(page: object) -> str | None:
    if not isinstance(page, str) or not PAGE.match(page):
        return f"page {page!r} is not a relative path of plain characters"
    if any(part in ("", ".", "..") for part in page.split("/")):
        return f"page {page!r} must name a file under the root: no '.', '..' or empty component"
    return None


def entry_problems(name: object, e: object) -> list[str]:
    """Why a hosted project's entry is unusable; [] when it is well formed. Unknown keys are ignored."""
    if N.problem(name):
        return [f"project name: {N.problem(name)}"]
    if not isinstance(e, dict):
        return [f"{name}: an entry is an object"]
    out = []
    for k in ("state", "root"):
        if not (isinstance(e.get(k), str) and R.PLAIN_PATH.match(e[k])):
            out.append(f"{name}: {k} {e.get(k)!r} is not an absolute path of plain characters")
    why = _page_problem(e.get("page"))
    if why:
        out.append(f"{name}: {why}")
    if not (isinstance(e.get("hostname"), str) and HOSTNAME.match(e["hostname"])):
        out.append(f"{name}: hostname {e.get('hostname')!r} is not a bare DNS name")
    if not (isinstance(e.get("aud"), str) and AUD.match(e["aud"])):
        out.append(f"{name}: aud is not an Access AUD tag")
    port = e.get("port")
    if not (isinstance(port, int) and not isinstance(port, bool) and 1 <= port <= 65535):
        out.append(f"{name}: port {port!r} is not 1-65535")
    slugs = e.get("slugs", [])
    if not isinstance(slugs, list) or not all(isinstance(s, str) and SLUG.match(s) for s in slugs):
        out.append(f"{name}: slugs must be a list of single directory names")
    return out


def load(path: Path | None = None) -> dict:
    """The whole file as {"team_domain": str | None, "projects": {...}}; empty when absent. Raises on bad JSON."""
    p = path or location()
    raw = R.read_regular(p, MAX_FILE)
    if raw is None:
        return {"team_domain": None, "projects": {}}
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise ServerFileError(f"{p} is not JSON: {e}") from None
    projects = doc.get("projects") if isinstance(doc, dict) else None
    if not isinstance(projects, dict):
        raise ServerFileError(f"{p} needs a \"projects\" object")
    team = doc.get("team_domain")
    return {"team_domain": team if isinstance(team, str) else None, "projects": projects}


def slugs_for_state(state: str, path: Path | None = None) -> list[str]:
    """The extra transcript slugs listed for the console at `state` (an absolute real path); K2 reads these."""
    p = path or location()
    out = []
    for name, e in load(p)["projects"].items():
        if not isinstance(e, dict) or not isinstance(e.get("state"), str):
            continue
        if os.path.realpath(e["state"]) != state:
            continue
        slugs = e.get("slugs", [])
        if not isinstance(slugs, list) or not all(isinstance(s, str) and SLUG.match(s) for s in slugs):
            raise ServerFileError(f"{p}: project {name!r}: slugs must be a list of single directory names")
        out.extend(slugs)
    return out


def _write(p: Path, doc: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".server.", suffix=".tmp")   # 0600 from creation
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def add(name: str, state: Path, hostname: str, aud: str, port: int, team_domain: str,
        root: Path | None = None, page: str = "index.html", slugs: list[str] | None = None,
        registry: Path | None = None, path: Path | None = None) -> dict:
    """Host `name` on the one server. Only the user runs this. Writes server.json; never the registry.

    Refuses: a name not in the name shape; a state no registry entry names; a
    root that is not registered on that state (and, with no --root, more than
    one root to choose from); a hostname that is not a bare DNS name; another
    project already holding this state, port or hostname. Re-adding a name
    replaces its entry, keeping its slugs (and any key K4 added) unless given.
    """
    problems = []
    if N.problem(name):
        problems.append(f"project name: {N.problem(name)}")
    s = os.path.realpath(state)
    projects = R.load(registry)
    roots = sorted(r for r, e in projects.items() if isinstance(e, dict) and e.get("state") == s)
    r = ""
    if not roots:
        problems.append(f"no project is registered on the console at {s}; run `agent.py --state {s} register "
                        f"--project DIR` first")
    elif root is not None:
        r = os.path.realpath(root)
        if r not in roots:
            problems.append(f"root {r} is not registered on the console at {s} (registered: {', '.join(roots)})")
    elif len(roots) > 1:
        problems.append(f"several roots are registered on the console at {s}; name the main one with --root "
                        f"({', '.join(roots)})")
    else:
        r = roots[0]
    if not (isinstance(team_domain, str) and TEAM.match(team_domain)):
        problems.append(f"team domain {team_domain!r} is not a bare DNS name")
    p = path or location()
    doc = load(p)
    old = doc["projects"].get(name) if isinstance(doc["projects"].get(name), dict) else {}
    e = {**old, "state": s, "root": r, "page": page, "hostname": hostname, "aud": aud, "port": port,
         "slugs": list(slugs) if slugs is not None else list(old.get("slugs", []))}
    problems += [x for x in entry_problems(name, e)
                 if not x.startswith("project name:") and not (not r and " root " in x)]
    if doc["team_domain"] not in (None, team_domain):
        problems.append(f"the server's team domain is {doc['team_domain']}; one server serves one Access team")
    for other, oe in doc["projects"].items():
        if other == name or not isinstance(oe, dict):
            continue
        if oe.get("state") == s:
            problems.append(f"project {other} already holds the console at {s}")
        if oe.get("port") == port:
            problems.append(f"project {other} already holds port {port}")
        if oe.get("hostname") == hostname:
            problems.append(f"project {other} already holds hostname {hostname}")
    if problems:
        raise ServerFileError("; ".join(problems))
    doc["projects"][name] = e
    _write(p, {"team_domain": team_domain, "projects": doc["projects"]})
    return e

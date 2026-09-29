"""Which projects this user has switched the owner console on for (spec §7.7, the trust anchor).

The plugin is installed per user, and its hook and skills act in whatever
project a session opens. So what they execute, and which doorbell they read,
must come from the USER, never from the repository: a clone could otherwise
ship its own `agent.py` and a fake doorbell and have a session run it. The
user's registry is one file,

    ${XDG_CONFIG_HOME:-~/.config}/console-kit/projects.json
    {"projects": {"<absolute project root>": {"state": "<absolute dir>", "kit": "<absolute dir>"}}}

written only by `agent.py register`, which the user runs. A project that is not
in it hears nothing from the plugin. The repository's `.console-kit.json` holds
data only (fold paths, the audit seat), never a path anything executes from.

The SessionStart hook carries its own copy of the reading rules (it may import
nothing from a project); the kit's tests hold the two to one fixture.
"""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from pathlib import Path

FILE = "projects.json"
MAX_REGISTRY = 1 << 20
# A path the plugin prints into a command and a session runs: absolute, and only
# characters no shell treats specially. Anything else is refused, never quoted around.
PLAIN_PATH = re.compile(r"^/[A-Za-z0-9_./\-]{0,511}$")


class RegistryError(ValueError):
    pass


def location() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "console-kit" / FILE


def read_regular(path: Path, cap: int) -> bytes | None:
    """The bytes of a regular file, or None when absent. A FIFO or device is refused without blocking."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise RegistryError(f"{path} is not a regular file")
        if st.st_size > cap:
            raise RegistryError(f"{path} is {st.st_size} bytes, over the {cap}-byte limit")
        return os.read(fd, cap + 1)[:cap]
    finally:
        os.close(fd)


def entry_problems(e: object) -> list[str]:
    if not isinstance(e, dict) or set(e) != {"state", "kit"}:
        return ["an entry is exactly {\"state\": ..., \"kit\": ...}"]
    return [f"{k} {e[k]!r} is not an absolute path of plain characters"
            for k in ("state", "kit") if not (isinstance(e[k], str) and PLAIN_PATH.match(e[k]))]


def load(path: Path | None = None) -> dict[str, dict]:
    raw = read_regular(path or location(), MAX_REGISTRY)
    if raw is None:
        return {}
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise RegistryError(f"the registry is not JSON: {e}") from None
    projects = doc.get("projects") if isinstance(doc, dict) else None
    if not isinstance(projects, dict):
        raise RegistryError("the registry needs a \"projects\" object")
    return projects


def lookup(project: Path, path: Path | None = None) -> dict | None:
    """This project's entry, or None when the user never registered it."""
    e = load(path).get(os.path.realpath(project))
    if e is None:
        return None
    problems = entry_problems(e)
    if problems:
        raise RegistryError("; ".join(problems))
    return e


def register(project: Path, state: Path, kit: Path, path: Path | None = None) -> dict:
    """Switch the console on for `project`. Only the user runs this."""
    root = os.path.realpath(project)
    e = {"state": os.path.realpath(state), "kit": os.path.realpath(kit)}
    problems = entry_problems(e) + ([] if PLAIN_PATH.match(root) else [f"project {root!r} is not a plain path"])
    if not (Path(e["kit"]) / "agent.py").is_file():
        problems.append(f"kit {e['kit']} has no agent.py")
    if problems:
        raise RegistryError("; ".join(problems))
    p = path or location()
    projects = load(p)
    projects[root] = e
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".projects.", suffix=".tmp")  # 0600 from creation
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"projects": projects}, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, p)
    return e

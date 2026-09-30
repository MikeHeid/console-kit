"""Which projects this user has switched the owner console on for (spec §7.7, the trust anchor).

The plugin is installed per user, and its hook and skills act in whatever
project a session opens. So what they execute, and which doorbell they read,
must come from the USER, never from the repository: a clone could otherwise
ship its own `agent.py` and a fake doorbell and have a session run it. The
user's registry is one file,

    ${XDG_CONFIG_HOME:-~/.config}/console-kit/projects.json
    {"projects": {"<absolute project root>": {"state": "<absolute dir>", "kit": "<absolute dir>",
                                              "steward": "<agent name, optional>"}}}

written only by `agent.py register` and `agent.py steward`, which the user runs.
A project that is not in it hears nothing from the plugin. The repository's
`.console-kit.json` holds data only (fold paths, the audit seat), never a path
anything executes from, and never the steward.

**The steward (0.8.3).** Several sessions may share one console, but its
doorbell has one cursor. The optional `steward` names the one session that may
`watch`, mark `synced` and fold; every other session asks, replies and marks its
work (`ask`, `reply`, `working`) and leaves the doorbell to it. The steward is a
property of the CONSOLE (its state directory): several checkouts of one project
registered on one state share it, so `register --steward` and `steward` write it
to every entry on that state. With no steward anywhere, everything is as in
0.8.2. It is a guardrail between the owner's own cooperating sessions, not a
defence: any process running as the user can edit this file, or the cursor.

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

from . import names as N

FILE = "projects.json"
REQUIRED = frozenset({"state", "kit"})
STEWARD = "steward"
MAX_REGISTRY = 1 << 20
# A path the plugin prints into a command and a session runs: absolute, and only
# characters no shell treats specially. Anything else is refused, never quoted around.
PLAIN_PATH = re.compile(r"^/[A-Za-z0-9_./\-]{0,511}\Z")


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
    if not isinstance(e, dict) or not REQUIRED <= set(e) <= REQUIRED | {STEWARD}:
        return ["an entry is exactly {\"state\": ..., \"kit\": ...}, with an optional \"steward\": NAME"]
    problems = [f"{k} {e[k]!r} is not an absolute path of plain characters"
                for k in ("state", "kit") if not (isinstance(e[k], str) and PLAIN_PATH.match(e[k]))]
    if STEWARD in e:
        why = N.problem(e[STEWARD])
        if why:
            problems.append(f"steward: {why}")
    return problems


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


def _write(p: Path, projects: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".projects.", suffix=".tmp")  # 0600 from creation
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"projects": projects}, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, p)


def _on_state(projects: dict, state: str) -> list[str]:
    """The registered roots whose console is `state` (an absolute real path)."""
    return sorted(root for root, e in projects.items() if isinstance(e, dict) and e.get("state") == state)


def _stewards(projects: dict, state: str) -> set[str]:
    return {projects[r][STEWARD] for r in _on_state(projects, state) if projects[r].get(STEWARD)}


def register(project: Path, state: Path, kit: Path, path: Path | None = None, steward: str | None = None) -> dict:
    """Switch the console on for `project`. Only the user runs this.

    `steward` (0.8.3) names the session that alone may watch, sync and fold
    this console; it is written to every entry on the same state. Without it,
    the entry keeps the steward its console already has, if any.
    """
    root = os.path.realpath(project)
    e = {"state": os.path.realpath(state), "kit": os.path.realpath(kit)}
    problems = entry_problems(e) + ([] if PLAIN_PATH.match(root) else [f"project {root!r} is not a plain path"])
    if not (Path(e["kit"]) / "agent.py").is_file():
        problems.append(f"kit {e['kit']} has no agent.py")
    if steward is not None and N.problem(steward):
        problems.append(f"steward: {N.problem(steward)}")
    if problems:
        raise RegistryError("; ".join(problems))
    p = path or location()
    projects = load(p)
    held = _stewards(projects, e["state"])   # this entry's own old steward counts only on the same state
    projects.pop(root, None)
    if steward is None and len(held) > 1:
        raise RegistryError(_conflict(e["state"], held))
    name = steward if steward is not None else next(iter(held), None)
    if name is not None:
        e[STEWARD] = name
        for r in _on_state(projects, e["state"]):
            projects[r] = {**projects[r], STEWARD: name}
    projects[root] = e
    _write(p, projects)
    return e


def _conflict(state: str, names: set[str]) -> str:
    return (f"the registry names more than one steward for the console at {state}: {', '.join(sorted(names))}; "
            f"set one with `agent.py --state {state} steward NAME`")


def set_steward(state: Path, name: str | None, path: Path | None = None) -> list[str]:
    """Name (or with None, clear) the steward of the console at `state`, on every entry using it (0.8.3).

    It writes nothing but the `steward` key: state and kit stay as the user
    registered them. Returns the project roots it changed.
    """
    if name is not None and N.problem(name):
        raise RegistryError(f"steward: {N.problem(name)}")
    p = path or location()
    projects = load(p)
    s = os.path.realpath(state)
    roots = _on_state(projects, s)
    if not roots:
        raise RegistryError(f"no project is registered on the console at {s}; run `agent.py --state {s} register "
                            f"--project DIR` first")
    for r in roots:
        e = {k: v for k, v in projects[r].items() if k != STEWARD}
        projects[r] = {**e, STEWARD: name} if name is not None else e
    _write(p, projects)
    return roots


def steward_for_state(state: Path, path: Path | None = None) -> str | None:
    """The steward of the console at `state`, None when it has none (or nothing is registered on it).

    The lock `agent.py watch`, `synced` and `fold.py` check. Raises
    RegistryError when the registry cannot be read or names two stewards.
    """
    projects = load(path)
    s = os.path.realpath(state)
    for r in _on_state(projects, s):
        if STEWARD in projects[r]:
            problems = entry_problems(projects[r])
            if problems:
                raise RegistryError(f"{r}: " + "; ".join(problems))
    held = _stewards(projects, s)
    if len(held) > 1:
        raise RegistryError(_conflict(s, held))
    return next(iter(held), None)

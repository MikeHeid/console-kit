"""The console server's reading of the project's `.console-kit.json` (0.8.0).

The file is the repository's DATA, never a path anything executes from
(`registry.py`). The server reads three optional keys from it:

    specs_dir    where the project keeps its specs, e.g. "architect/40-specs/".
                 Read only: the "refine" and "drill" tags look in it.
    visuals_dir  where agent-made visuals are written, e.g. "architect/visuals/".
                 The kit writes the visual files and a generated INDEX.md there.
    next_step    {"refine": "<skill>", "drill": "<skill>"}: the skill a session
                 runs for each kind of fork. The server never reads it; the
                 console-fork skill does, and it is checked here only so a typo
                 is named when the server starts rather than in the middle of a fork.

Each directory is jailed like an evidence path: relative, plain characters, at
least one real folder (never "." or the root itself), no `..`, never a
secrets directory, and it must resolve inside the project root (no symlink
out). A key that breaks a rule is refused BY NAME at start-up, so a server
never runs half-configured.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import schema as S
from .registry import RegistryError, read_regular

FILE = ".console-kit.json"
MAX_FILE = 64 * 1024
DIR = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]*(/[A-Za-z0-9_][A-Za-z0-9_.\-]*)*/?$")
SKILL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:\-]{0,79}$")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ProjectConfig:
    specs_dir: str | None = None     # normalised: no trailing slash
    visuals_dir: str | None = None
    next_step: dict = field(default_factory=dict)


def _dir(root: Path, v: object, key: str) -> str:
    if not isinstance(v, str) or not DIR.match(v) or len(v) > S.MAX_LINE:
        raise ConfigError(f"{FILE}: {key} {v!r} must be a relative folder of plain characters, e.g. "
                          f"'architect/40-specs/'")
    rel = v.rstrip("/")
    parts = rel.split("/")
    if ".." in parts or "." in parts:
        raise ConfigError(f"{FILE}: {key} {v!r} must stay inside the project: no '.' or '..'")
    if S.secret_path(rel + "/x"):
        raise ConfigError(f"{FILE}: {key} {v!r} is under {S.secret_path(rel + '/x')}")
    top = Path(root).resolve()
    p = (top / rel).resolve()
    if top not in p.parents:
        raise ConfigError(f"{FILE}: {key} {v!r} resolves outside the project (a symlink out?)")
    return rel


def load(root: Path) -> ProjectConfig:
    """The project's console keys; an absent file or key is simply unset."""
    try:
        raw = read_regular(Path(root) / FILE, MAX_FILE)
    except RegistryError as e:
        raise ConfigError(str(e)) from None
    if raw is None:
        return ProjectConfig()
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise ConfigError(f"{FILE} is not JSON: {e}") from None
    if not isinstance(doc, dict):
        raise ConfigError(f"{FILE} must be a JSON object")
    specs = _dir(root, doc["specs_dir"], "specs_dir") if doc.get("specs_dir") is not None else None
    visuals = _dir(root, doc["visuals_dir"], "visuals_dir") if doc.get("visuals_dir") is not None else None
    steps = doc.get("next_step") or {}
    if not isinstance(steps, dict) or set(steps) - set(S.STEPS) or not all(
            isinstance(v, str) and SKILL.match(v) for v in steps.values()):
        raise ConfigError(f"{FILE}: next_step maps {' and '.join(S.STEPS)} to a skill name "
                          f"(letters, digits, '_.:-', up to 80), e.g. {{\"refine\": \"refine\"}}")
    if specs and visuals and (visuals == specs or visuals.startswith(specs + "/")):
        raise ConfigError(f"{FILE}: visuals_dir {visuals!r} is inside specs_dir {specs!r}; keep them apart, "
                          f"or every visual would read as a spec")
    return ProjectConfig(specs_dir=specs, visuals_dir=visuals, next_step=dict(steps))

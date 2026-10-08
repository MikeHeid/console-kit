"""The console server's reading of the project's `.overture.json` (0.8.0).

The file is the repository's DATA, never a path anything executes from
(`registry.py`). The server reads three optional keys from it:

    specs_dir    where the project keeps its specs, e.g. "architect/40-specs/".
                 Read only: the "refine" and "drill" tags look in it.
    visuals_dir  where agent-made visuals LAND by pull request, e.g. "architect/visuals/".
                 A destination only (0.8.1): the server validates it and never writes
                 there. It stores visuals in its STATE directory; `agent.py
                 visual-export` copies them, and a generated INDEX.md, into the
                 agent's own worktree under this path. Without it a visual is still
                 requested, stored and shown; only landing it needs this key.
    next_step    {"refine": "<skill>", "drill": "<skill>"}: the skill a session
                 runs for each kind of fork. The server never reads it; the
                 console-fork skill does, and it is checked here only so a typo
                 is named when the server starts rather than in the middle of a fork.
                 The name is resolved ONLY against the user's installed skills (the
                 user-level skills folder or an installed plugin), NEVER against a
                 skill folder inside the repository, and a name that is not an
                 installed user skill is refused by name (`resolve_skill`, run by
                 `agent.py next-step`).

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

from . import rootfs as RF
from . import schema as S
from .registry import RegistryError, read_regular

FILE = ".overture.json"
MAX_FILE = 64 * 1024
DIR = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]*(/[A-Za-z0-9_][A-Za-z0-9_.\-]*)*/?\Z")
SKILL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:\-]{0,79}\Z")
# A section anchor is a URL fragment the dashboard page's HTML carries on an id or data-ck-item. Shape it
# the way a browser accepts a fragment: letters, digits, `-_./:` and a required leading `#`, at most 128
# chars. Not resolved by the server; passed to the console script to turn into deep links.
SECTION = re.compile(r"^#[A-Za-z0-9][A-Za-z0-9._/:\-]{0,127}\Z")
MAX_SECTIONS_PER_ITEM = 16


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ProjectConfig:
    specs_dir: str | None = None     # normalised: no trailing slash
    visuals_dir: str | None = None
    next_step: dict = field(default_factory=dict)
    # {item_id: [section_anchor, ...]}; the console turns these into deep-links from the item view to the
    # dashboard page's sections (0.9.12). Validated here only; the server does not resolve them against the
    # page (it never reads the page's HTML).
    sections: dict = field(default_factory=dict)
    # 1.7.0: refs options under `.overture.json`'s "items" key.
    # - auto_ref: assign a dotted number to a pushed item that has none. Default True.
    # - require_parent: refuse an item that has no parent and no `kind: "topic"`. Default False
    #   (so an existing project upgrading does not refuse its first push).
    items_auto_ref: bool = True
    items_require_parent: bool = False


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
        if RF.confined():   # the one console server (K3): beneath the held root, no symlink followed
            try:
                raw = RF.read(Path(root), FILE, MAX_FILE)
            except FileNotFoundError:
                raw = None
        else:
            raw = read_regular(Path(root) / FILE, MAX_FILE)
    except (RegistryError, RF.Refused, ValueError) as e:
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
    sections = _sections(doc.get("sections"))
    items_opts = doc.get("items") or {}
    if not isinstance(items_opts, dict) or set(items_opts) - {"auto_ref", "require_parent"}:
        raise ConfigError(f"{FILE}: items takes only {{auto_ref: bool, require_parent: bool}}")
    for k in ("auto_ref", "require_parent"):
        if k in items_opts and not isinstance(items_opts[k], bool):
            raise ConfigError(f"{FILE}: items.{k} must be true or false")
    return ProjectConfig(specs_dir=specs, visuals_dir=visuals, next_step=dict(steps), sections=sections,
                         items_auto_ref=items_opts.get("auto_ref", True),
                         items_require_parent=items_opts.get("require_parent", False))


def _sections(v: object) -> dict:
    """Parse an optional `sections` map: {item_id: [#anchor, ...]}; refused by name on any bad entry.

    Returns {} when absent. Each item id must pass `S.ITEM_ID`; each anchor must match SECTION (a URL
    fragment of plain characters), with at most MAX_SECTIONS_PER_ITEM anchors per item. The server never
    resolves the anchors against the page itself: they are only passed through to the console script.
    """
    if v is None:
        return {}
    if not isinstance(v, dict):
        raise ConfigError(f"{FILE}: sections must be an object mapping item ids to lists of #anchors")
    out: dict[str, list[str]] = {}
    for item_id, anchors in v.items():
        if not isinstance(item_id, str) or not S.ITEM_ID.match(item_id):
            raise ConfigError(f"{FILE}: sections key {item_id!r} is not an item id")
        if not isinstance(anchors, list) or not anchors:
            raise ConfigError(f"{FILE}: sections[{item_id!r}] must be a non-empty list of #anchors")
        if len(anchors) > MAX_SECTIONS_PER_ITEM:
            raise ConfigError(f"{FILE}: sections[{item_id!r}] has {len(anchors)} anchors; at most "
                              f"{MAX_SECTIONS_PER_ITEM}")
        checked: list[str] = []
        for a in anchors:
            if not isinstance(a, str) or not SECTION.match(a):
                raise ConfigError(f"{FILE}: sections[{item_id!r}] anchor {a!r} must look like '#features/rollout'")
            if a not in checked:
                checked.append(a)
        out[item_id] = checked
    return out


def user_config_dir() -> Path:
    """Claude Code's user-level folder: $CLAUDE_CONFIG_DIR, else ~/.claude."""
    import os
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude"))


def resolve_skill(name: str, root: Path, config_dir: Path | None = None) -> Path:
    """The SKILL.md a `next_step` name means, looked up ONLY among the user's installed skills.

    `name` comes from the repository, so the repository must not also choose
    what it runs. A plain name is `<config>/skills/<name>/SKILL.md`; a
    `plugin:skill` name is an installed plugin's `.../<plugin>/<version>/skills/<skill>/SKILL.md`
    under `<config>/plugins/cache/`. A skill folder inside the project
    (`.claude/skills/...`) is never consulted, and a user-level entry whose real
    path lands inside the project (a symlink into the repository) is refused.
    Raises ConfigError naming why.
    """
    if not isinstance(name, str) or not SKILL.match(name):
        raise ConfigError(f"next_step skill {name!r} is not a skill name")
    cfg = Path(config_dir) if config_dir is not None else user_config_dir()
    if ":" in name:
        plugin, _, skill = name.partition(":")
        if not plugin or not skill or ":" in skill:
            raise ConfigError(f"next_step skill {name!r} must be <name> or <plugin>:<name>")
        found = sorted((cfg / "plugins" / "cache").glob(f"*/{plugin}/*/skills/{skill}/SKILL.md"))
    else:
        p = cfg / "skills" / name / "SKILL.md"
        found = [p] if p.exists() else []
    if not found:
        raise ConfigError(f"next_step skill {name!r} is not an installed user skill (looked in {cfg}/skills and "
                          f"{cfg}/plugins/cache); a skill inside the repository is never used for it")
    path = found[-1]
    real = path.resolve()
    top = Path(root).resolve()
    if real == top or top in real.parents:
        raise ConfigError(f"next_step skill {name!r} resolves into the project ({real}); a repository skill is "
                          f"never used for it")
    if not real.is_file():
        raise ConfigError(f"next_step skill {name!r}: {path} is not a regular file")
    return real

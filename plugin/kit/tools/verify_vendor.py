#!/usr/bin/env python3
"""Check that a project's vendored copy of the kit is exactly one released kit version.

    verify_vendor.py --kit KIT_CLONE --ref REF --vendored DIR [--adapted PATH ...] [--json]

REF is a tag or commit in a LOCAL clone of the kit; nothing is fetched, so this
runs offline (in CI, check out the kit at the tag first). Every file under DIR
is mapped to the kit file it was copied from (`RULES`, below), and compared byte
for byte with that file AT REF (`git cat-file`, so no checkout, no filters).

Exit codes: 0 the copy is REF exactly; 1 drift, each difference named;
2 a usage or git problem (an unknown REF, a KIT that is not a git clone).

Drift is any of:
- a vendored file whose bytes differ from its kit file at REF;
- a vendored file no rule maps and not declared ADAPTED (a stray or renamed file);
- a kit file a COMPLETE rule covers that the copy lacks (a module added in a
  release and never vendored).

ADAPTED files are the project's own edits of kit files (its tests point at a
flat layout, its marketplace entry describes the project): listed, never
compared. Pass `--adapted` for more.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Rule:
    vendored: str        # a file, or a directory prefix ending in "/"
    kit: str             # the same, in the kit repository
    complete: str = ""   # "" a single file must be present; "all" every kit file under the prefix;
    #                      "subdir" every kit file under each first-level folder the copy holds


# How gradiance vendors the kit, flat under tools/console-kit. A project that
# vendors differently copies this script and edits this table, nothing else.
RULES = (
    Rule("console_kit/", "plugin/kit/console_kit/", "all"),
    Rule("agent.py", "plugin/kit/agent.py"),
    Rule("fold.py", "plugin/kit/fold.py"),
    Rule("publish.py", "plugin/kit/publish.py"),
    Rule("server.py", "plugin/kit/server.py"),
    Rule("requirements.txt", "plugin/kit/requirements.txt"),
    Rule("plugin/.claude-plugin/plugin.json", "plugin/.claude-plugin/plugin.json"),
    Rule("plugin/hooks/", "plugin/hooks/", "all"),
    Rule("plugin/agents/", "plugin/agents/", "all"),
    # A project may take some skills and not others (gradiance has no console-onboard),
    # but a skill it takes, it takes whole.
    Rule("plugin/skills/", "plugin/skills/", "subdir"),
)
ADAPTED = (".claude-plugin/marketplace.json", "test_kit.py", "test_server.py", "test_browser.py")
IGNORED_PARTS = ("__pycache__", ".pytest_cache")
IGNORED_SUFFIXES = (".pyc",)


class GitError(Exception):
    pass


def _git(kit: Path, *args: str) -> bytes:
    try:
        r = subprocess.run(["git", "-C", str(kit), *args], capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise GitError(f"git {' '.join(args)}: {e}") from None
    if r.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {r.stderr.decode('utf-8', 'replace').strip()}")
    return r.stdout


def resolve(kit: Path, ref: str) -> tuple[str, list[str]]:
    """The commit REF names, and the tags that point at it."""
    if not ref or ref.startswith("-"):
        raise GitError(f"{ref!r} is not a tag or commit")
    _git(kit, "rev-parse", "--git-dir")  # not a clone at all: git's own words say so
    try:
        commit = _git(kit, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").decode().strip()
    except GitError:
        raise GitError(f"no tag or commit {ref!r} in {kit} (fetch its tags first; nothing is fetched here)") from None
    tags = _git(kit, "tag", "--points-at", commit).decode().split()
    return commit, tags


def kit_files(kit: Path, commit: str) -> set[str]:
    return set(_git(kit, "ls-tree", "-r", "-z", "--name-only", commit).decode("utf-8").split("\0")) - {""}


def vendored_files(root: Path) -> list[str]:
    out = []
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if any(part in IGNORED_PARTS for part in rel.split("/")) or rel.endswith(IGNORED_SUFFIXES):
            continue
        if p.is_symlink() or not p.is_file():
            if p.is_symlink():
                out.append(rel)  # compared below, and never followed: a link is drift
            continue
        out.append(rel)
    return out


def map_path(rel: str) -> tuple[Rule, str] | None:
    for r in RULES:
        if r.vendored.endswith("/") and rel.startswith(r.vendored):
            return r, r.kit + rel[len(r.vendored):]
        if rel == r.vendored:
            return r, r.kit
    return None


def verify(kit: Path, ref: str, vendored: Path, adapted: tuple[str, ...] = ()) -> dict:
    commit, tags = resolve(kit, ref)
    in_kit = kit_files(kit, commit)
    adapted_set = set(ADAPTED) | set(adapted)
    have = vendored_files(vendored)
    report = {"ref": ref, "commit": commit, "tags": tags, "matching": 0,
              "differs": [], "unexpected": [], "missing": [], "adapted": []}
    for rel in have:
        if rel in adapted_set:
            report["adapted"].append(rel)
            continue
        hit = map_path(rel)
        if hit is None:
            report["unexpected"].append(rel)
            continue
        src = hit[1]
        if src not in in_kit:
            report["unexpected"].append(f"{rel} (no {src} at {ref})")
            continue
        p = vendored / rel
        if p.is_symlink():
            report["differs"].append(f"{rel} (a symlink, not the file)")
            continue
        if p.read_bytes() != _git(kit, "cat-file", "blob", f"{commit}:{src}"):
            report["differs"].append(rel)
        else:
            report["matching"] += 1
    have_set = set(have)
    for r in RULES:
        if not r.vendored.endswith("/"):
            if r.vendored not in have_set and r.kit in in_kit:
                report["missing"].append(r.vendored)
            continue
        under = sorted(f[len(r.kit):] for f in in_kit if f.startswith(r.kit))
        if r.complete == "subdir":
            taken = {h[len(r.vendored):].split("/", 1)[0] for h in have_set if h.startswith(r.vendored)}
            under = [u for u in under if u.split("/", 1)[0] in taken]
        elif r.complete != "all":
            under = []
        report["missing"] += [r.vendored + u for u in under if r.vendored + u not in have_set]
    report["ok"] = not (report["differs"] or report["unexpected"] or report["missing"])
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--kit", type=Path, required=True, help="a local git clone of console-kit")
    ap.add_argument("--ref", required=True, help="the released tag (or commit) the copy should be")
    ap.add_argument("--vendored", type=Path, required=True, help="the project's vendored kit directory")
    ap.add_argument("--adapted", action="append", default=[], help="another project-edited path, not compared")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not a.vendored.is_dir():
        print(f"verify_vendor: {a.vendored} is not a directory", file=sys.stderr)
        return 2
    try:
        rep = verify(a.kit, a.ref, a.vendored, tuple(a.adapted))
    except GitError as e:
        print(f"verify_vendor: {e}", file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps(rep, indent=2))
    else:
        tag = f" (tag {', '.join(rep['tags'])})" if rep["tags"] else " (no tag points at it)"
        print(f"kit {rep['ref']} = {rep['commit'][:12]}{tag}: {rep['matching']} file(s) match")
        for key, words in (("differs", "differs from the kit"), ("unexpected", "is not a kit file"),
                           ("missing", "is in the kit but not vendored")):
            for rel in rep[key]:
                print(f"DRIFT {rel}: {words}")
        for rel in rep["adapted"]:
            print(f"adapted {rel}: the project's own edit, not compared")
        print("OK: the copy is this kit version exactly" if rep["ok"] else "FAIL: the copy has drifted",
              file=sys.stderr)
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Bump the kit's version in every file that holds it, in one shot.

    python3 release.py 0.9.2

Writes the new version to:
  VERSION
  plugin/.claude-plugin/plugin.json          (top-level `version`)
  plugin/kit/overture/__init__.py         (`__version__`, which /health and the footer report)
  .claude-plugin/marketplace.json            (plugins[0].version; what Claude Code reads from the GitHub route)

Prints what changed and the next steps. Does not touch CHANGELOG.md, commit,
build or push: the operator still writes release notes and runs test_build.py.

Added in 0.9.2 after /health kept showing 0.8.18 across the 0.9.0 and 0.9.1
releases: `__version__` and the repo-marketplace manifest had drifted from
VERSION. `test_build.py::test_the_version_matches_everywhere_kit_is_installed`
asserts they stay equal; this script makes it easy to keep them equal.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

VERSION_RE = re.compile(r"^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)(?:[-+].+)?$")


def set_version(version: str) -> list[str]:
    """Update every file that carries the kit's version. Returns a list of files changed."""
    if not VERSION_RE.match(version):
        raise ValueError(f"{version!r} is not a semver string (X.Y.Z[-tag])")

    changed: list[str] = []

    def write(path: Path, data: str) -> None:
        if path.read_text(encoding="utf-8") != data:
            path.write_text(data, encoding="utf-8")
            changed.append(str(path.relative_to(ROOT).as_posix()))

    # VERSION — trailing newline matches the existing convention.
    write(ROOT / "VERSION", version + "\n")

    # plugin.json — plain top-level "version"; keep key order and indentation.
    plugin_json = ROOT / "plugin" / ".claude-plugin" / "plugin.json"
    pj = json.loads(plugin_json.read_text(encoding="utf-8"))
    pj["version"] = version
    write(plugin_json, json.dumps(pj, indent=2) + "\n")

    # marketplace.json — plugins[0].version is what Claude Code reads for the GitHub route.
    market_json = ROOT / ".claude-plugin" / "marketplace.json"
    mj = json.loads(market_json.read_text(encoding="utf-8"))
    if not isinstance(mj.get("plugins"), list) or not mj["plugins"]:
        raise ValueError(f"{market_json}: plugins[] is empty; cannot bump version")
    mj["plugins"][0]["version"] = version
    write(market_json, json.dumps(mj, indent=2) + "\n")

    # overture/__init__.py — /health and the footer read this.
    init_py = ROOT / "plugin" / "kit" / "overture" / "__init__.py"
    text = init_py.read_text(encoding="utf-8")
    new = re.sub(r'(__version__\s*=\s*)"[^"]*"', lambda m: f'{m.group(1)}"{version}"', text, count=1)
    if '__version__' not in new or f'"{version}"' not in new:
        raise ValueError(f"{init_py}: __version__ assignment not found; expected `__version__ = \"...\"`")
    write(init_py, new)

    return changed


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        changed = set_version(argv[1])
    except (ValueError, OSError) as e:
        print(f"release: {e}", file=sys.stderr)
        return 1
    if not changed:
        print(f"release: already at {argv[1]}; nothing changed.")
        return 0
    print(f"release: bumped to {argv[1]}; files updated:")
    for p in changed:
        print(f"  {p}")
    print()
    print("Next:")
    print("  1. Add a CHANGELOG entry for the new version.")
    print("  2. python3 test_build.py              # asserts the four files stay equal")
    print("  3. git switch -c release-" + argv[1])
    print("  4. git add -A && git commit -m 'release: " + argv[1] + "'")
    print("  5. git push -u origin release-" + argv[1] + " && gh pr create ...")
    print("  6. After merge: build zip (in WSL for LF endings) and `gh release create`.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

#!/usr/bin/env python3
"""Build dist/console-kit-<version>.zip: a local Claude Code plugin marketplace
holding the console-kit plugin and, inside it, the server kit.

    python3 build_zip.py [--deny-file FILE] [--no-validate]

Claude Desktop installs plugins from a marketplace, not from a bare zip: unzip
it, then in the Code tab run `/plugin marketplace add <unzipped folder>` and
`/plugin install console-kit@console-kit-local` (INSTALL.md).

The zip is deterministic (sorted entries, fixed timestamps and modes), so the
same tree always gives the same bytes and the printed sha256 identifies it.

It REFUSES to build when a file holds anything shaped like a secret, or any
string from --deny-file (one per line: your own hostnames, AUD tags, team
domain, home path), so one project's values never ride out in the kit.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MARKET = "console-kit-local"
# What the installed kit needs at run time. Tests, dist/ and git metadata stay out.
KIT_PARTS = ["console_kit", "deploy", "demo", "docs", "agent.py", "fold.py", "onboard.py", "publish.py",
             "server.py", "adapter_template.py", "requirements.txt", "README.md", "INSTALL.md", "VERSION"]
PLUGIN_PARTS = [".claude-plugin", "hooks", "skills", "agents"]
SKIP = re.compile(r"(^|/)(__pycache__|\.pytest_cache|\.DS_Store)(/|$)|\.pyc$")
SECRET = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----|\"TunnelSecret\"|\"AccountTag\"|"
                    r"\bghp_[A-Za-z0-9]{30,}|\bsk-ant-[A-Za-z0-9_-]{20,}|\bAKIA[0-9A-Z]{16}\b")
SECRET_NAMES = re.compile(r"(^|/)(cert\.pem|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.json"
                          r"|\.env|console\.env)$")
FIXED = (1980, 1, 1, 0, 0, 0)
EXECUTABLE = {"onboard.py", "agent.py", "fold.py", "publish.py", "server.py"}


class BuildError(Exception):
    pass


def version() -> str:
    return (ROOT / "VERSION").read_text(encoding="utf-8").strip()


def files(base: Path, parts: list[str]) -> list[Path]:
    out = []
    for part in parts:
        p = base / part
        if not p.exists():
            raise BuildError(f"missing {p}")
        for f in ([p] if p.is_file() else sorted(p.rglob("*"))):
            rel = f.relative_to(base).as_posix()
            if f.is_file() and not SKIP.search(rel):
                out.append(f)
    return out


def stage(tmp: Path) -> Path:
    ver = version()
    # Unversioned on purpose: the marketplace is added by path, so an update must
    # land at the same path for `/plugin marketplace update` to find it.
    top = tmp / "console-kit"
    plugin = top / "plugins/console-kit"
    for f in files(ROOT / "plugin", [p for p in PLUGIN_PARTS if (ROOT / "plugin" / p).exists()]):
        dst = plugin / f.relative_to(ROOT / "plugin")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, dst)
    for f in files(ROOT, KIT_PARTS):
        dst = plugin / "kit" / f.relative_to(ROOT)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, dst)
    manifest = json.loads((plugin / ".claude-plugin/plugin.json").read_text(encoding="utf-8"))
    if manifest.get("version") != ver:
        raise BuildError(f"plugin.json version {manifest.get('version')!r} != VERSION {ver!r}")
    (top / ".claude-plugin").mkdir(parents=True)
    (top / ".claude-plugin/marketplace.json").write_text(json.dumps({
        "name": MARKET,
        "owner": manifest.get("author", {"name": "console-kit"}),
        "description": "A local marketplace holding the console-kit plugin, unzipped from its release zip.",
        "plugins": [{"name": "console-kit", "source": "./plugins/console-kit", "version": ver,
                     "description": manifest["description"]}],
    }, indent=2) + "\n", encoding="utf-8")
    shutil.copy2(ROOT / "INSTALL.md", top / "INSTALL.md")
    return top


def scan(top: Path, deny: list[str]) -> None:
    problems = []
    for f in sorted(top.rglob("*")):
        if not f.is_file():
            continue
        rel = f.relative_to(top).as_posix()
        if SECRET_NAMES.search(rel):
            problems.append(f"{rel}: a file with a secret's or a project's config name")
        text = f.read_text(encoding="utf-8", errors="replace")
        if SECRET.search(text):
            problems.append(f"{rel}: holds something shaped like a secret")
        for d in deny:
            if d.lower() in text.lower():
                problems.append(f"{rel}: holds a denied string ({d[:4]}...)")
    if problems:
        raise BuildError("refusing to build:\n  " + "\n  ".join(problems))


def validate(top: Path) -> str:
    claude = shutil.which("claude")
    if not claude:
        return "validate: skipped, `claude` is not on PATH"
    for target in (top, top / "plugins/console-kit"):
        r = subprocess.run([claude, "plugin", "validate", "--strict", str(target)],
                           capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            raise BuildError(f"claude plugin validate --strict {target.name} failed:\n{r.stdout}{r.stderr}")
    return "validate: marketplace and plugin pass `claude plugin validate --strict`"


def pack(top: Path, out: Path) -> str:
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(p for p in top.rglob("*") if p.is_file()):
            info = zipfile.ZipInfo(f.relative_to(top.parent).as_posix(), FIXED)
            executable = f.suffix == ".sh" or f.name in EXECUTABLE
            info.external_attr = (0o100755 if executable else 0o100644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, f.read_bytes())
    return hashlib.sha256(out.read_bytes()).hexdigest()


def build(deny: list[str], check: bool = True, out_dir: Path | None = None) -> tuple[Path, str, str]:
    with tempfile.TemporaryDirectory() as td:
        top = stage(Path(td))
        scan(top, deny)
        note = validate(top) if check else "validate: skipped (--no-validate)"
        out = (out_dir or ROOT / "dist") / f"console-kit-{version()}.zip"
        return out, pack(top, out), note


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--deny-file", type=Path, help="strings that must not appear in the zip, one per line")
    ap.add_argument("--no-validate", action="store_true")
    ap.add_argument("--out-dir", type=Path)
    a = ap.parse_args(argv)
    deny = []
    if a.deny_file:
        deny = [line.strip() for line in a.deny_file.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.startswith("#")]
    try:
        out, digest, note = build(deny, not a.no_validate, a.out_dir)
    except BuildError as e:
        print(f"build_zip: {e}", file=sys.stderr)
        return 1
    print(note)
    print(f"wrote {out}\nsha256 {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

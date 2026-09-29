"""build_zip.py: the right files, a valid marketplace, the same bytes twice, and no secrets.

    python3 test_build.py
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import build_zip as B


class Base(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.tmp = Path(td.name)

    def built(self, deny=(), sub="out"):
        out, digest, _ = B.build(list(deny), check=False, out_dir=self.tmp / sub)
        return out, digest


class LayoutTests(Base):
    def test_the_zip_is_a_marketplace_holding_the_plugin_and_the_kit(self):
        out, _ = self.built()
        self.assertEqual(out.name, f"console-kit-{B.version()}.zip")
        names = set(zipfile.ZipFile(out).namelist())
        for must in ("console-kit/.claude-plugin/marketplace.json", "console-kit/INSTALL.md",
                     "console-kit/plugins/console-kit/.claude-plugin/plugin.json",
                     "console-kit/plugins/console-kit/hooks/hooks.json",
                     "console-kit/plugins/console-kit/hooks/session_start.py",
                     "console-kit/plugins/console-kit/skills/console-onboard/SKILL.md",
                     "console-kit/plugins/console-kit/skills/console-process/SKILL.md",
                     "console-kit/plugins/console-kit/agents/security.md",
                     "console-kit/plugins/console-kit/kit/onboard.py",
                     "console-kit/plugins/console-kit/kit/agent.py",
                     "console-kit/plugins/console-kit/kit/server.py",
                     "console-kit/plugins/console-kit/kit/deploy/install.sh",
                     "console-kit/plugins/console-kit/kit/console_kit/console.js",
                     "console-kit/plugins/console-kit/kit/requirements.txt"):
            self.assertIn(must, names)
        for n in names:
            with self.subTest(entry=n):
                self.assertFalse(Path(n).name.startswith("test_"), n)
                self.assertNotIn("__pycache__", n)
                self.assertNotIn("/.git/", n)
                self.assertNotIn("/dist/", n)

    def test_the_marketplace_points_at_the_plugin_at_the_same_version(self):
        z = zipfile.ZipFile(self.built()[0])
        market = json.loads(z.read("console-kit/.claude-plugin/marketplace.json"))
        plugin = json.loads(z.read("console-kit/plugins/console-kit/.claude-plugin/plugin.json"))
        self.assertEqual(market["name"], B.MARKET)
        [entry] = market["plugins"]
        self.assertEqual(entry["name"], plugin["name"])
        self.assertEqual(entry["source"], "./plugins/console-kit")
        self.assertEqual(entry["version"], plugin["version"])
        self.assertEqual(plugin["version"], B.version())

    def test_scripts_are_executable_and_the_rest_is_not(self):
        z = zipfile.ZipFile(self.built()[0])
        mode = {i.filename: (i.external_attr >> 16) & 0o777 for i in z.infolist()}
        self.assertEqual(mode["console-kit/plugins/console-kit/kit/deploy/install.sh"], 0o755)
        self.assertEqual(mode["console-kit/plugins/console-kit/kit/onboard.py"], 0o755)
        self.assertEqual(mode["console-kit/INSTALL.md"], 0o644)

    def test_the_same_tree_gives_the_same_bytes(self):
        (_, a), (_, b) = self.built(sub="a"), self.built(sub="b")
        self.assertEqual(a, b)


class RefusalTests(Base):
    def test_a_denied_string_refuses_the_build_and_writes_no_zip(self):
        with self.assertRaisesRegex(B.BuildError, "holds a denied string"):
            self.built(deny=["CLOUDFLAREACCESS.com"])      # case-insensitive
        self.assertFalse((self.tmp / "out").exists())

    def test_a_version_mismatch_is_refused(self):
        with mock.patch.object(B, "version", return_value="9.9.9"):
            with self.assertRaisesRegex(B.BuildError, "plugin.json version"):
                self.built()

    def test_secret_shaped_content_and_names_are_refused(self):
        cases = {
            "a.txt": "-----BEGIN PRIVATE KEY-----\nMII...\n",
            "b.json": '{"AccountTag": "x", "TunnelSecret": "y"}',
            "0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b.json": "{}",
            "cert.pem": "x",
            ".console-kit/console.env": "CONSOLE_NAME=x\n",
            "c.md": "token ghp_" + "a" * 36,
        }
        for rel, text in cases.items():
            with self.subTest(file=rel):
                top = self.tmp / rel.replace("/", "_") / "top"
                p = top / rel
                p.parent.mkdir(parents=True)
                p.write_text(text)
                with self.assertRaises(B.BuildError):
                    B.scan(top, [])

    def test_a_clean_tree_passes_the_scan(self):
        top = self.tmp / "clean"
        (top / "docs").mkdir(parents=True)
        (top / "docs/CLOUDFLARE.md").write_text("never open ~/.cloudflared/cert.pem or <id>.json")
        B.scan(top, ["example-deploy.net"])


@unittest.skipUnless(shutil.which("claude"), "`claude` is not on PATH")
class ValidateTests(Base):
    def test_claude_validates_the_marketplace_and_the_plugin_strictly(self):
        _, _, note = B.build([], check=True, out_dir=self.tmp / "v")
        self.assertIn("pass `claude plugin validate --strict`", note)


@unittest.skipUnless(shutil.which("claude"), "`claude` is not on PATH")
class InstalledPluginTests(Base):
    """Install for real, into a throwaway Claude config, by BOTH routes, and look inside.

    The repository's own marketplace once pointed at a plugin folder without the
    kit: installing from GitHub gave skills that stopped at "the plugin is
    incomplete", while the zip (a different marketplace) was fine. Only an
    install shows what a user gets.
    """

    def install(self, marketplace_dir: Path, plugin_id: str) -> Path:
        cfg = self.tmp / f"cfg-{plugin_id.split('@')[1]}"
        env = {**os.environ, "CLAUDE_CONFIG_DIR": str(cfg)}
        for argv in (["plugin", "marketplace", "add", str(marketplace_dir)], ["plugin", "install", plugin_id]):
            r = subprocess.run(["claude", *argv], env=env, capture_output=True, text=True, timeout=120,
                               stdin=subprocess.DEVNULL)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        [skill] = cfg.glob("plugins/cache/*/console-kit/*/skills/console-onboard/SKILL.md")
        return skill.parent

    def assert_complete(self, skill_dir: Path):
        kit = (skill_dir / "../../kit").resolve()     # where SKILL.md looks
        for rel in ("onboard.py", "agent.py", "server.py", "deploy/install.sh", "docs/CLOUDFLARE.md",
                    "adapter_template.py", "demo/index.html", "console_kit/console.js", "requirements.txt"):
            self.assertTrue((kit / rel).is_file(), f"installed plugin lacks kit/{rel}")
        # Every KIT/<path> a skill tells a session to read must be in the installed kit.
        for skill in (skill_dir / "..").resolve().glob("*/SKILL.md"):
            for rel in re.findall(r"`KIT/([\w./-]+)`", skill.read_text(encoding="utf-8")):
                self.assertTrue((kit / rel).exists(), f"{skill.parent.name} cites KIT/{rel}, not in the installed kit")

    def test_an_install_from_the_repository_carries_the_kit(self):
        self.assert_complete(self.install(B.ROOT, "console-kit@console-kit"))

    def test_an_install_from_the_release_zip_carries_the_kit(self):
        out, _ = self.built()
        with zipfile.ZipFile(out) as z:
            z.extractall(self.tmp / "unzipped")
        self.assert_complete(self.install(self.tmp / "unzipped/console-kit", "console-kit@console-kit-local"))


if __name__ == "__main__":
    unittest.main()

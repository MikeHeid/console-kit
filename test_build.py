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

# Skills the console's rounds lean on, shipped in the plugin so a fresh install has them.
SHIPPED_SKILLS = ("roar", "refine", "drill", "deliberate")


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
                     *(f"console-kit/plugins/console-kit/skills/{s}/SKILL.md" for s in SHIPPED_SKILLS),
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

    def test_every_place_that_states_the_version_agrees(self):
        # 0.6.0: /health reports console_kit.__version__, and a vendoring project carries no
        # VERSION file, so the package must say the same as VERSION and both manifests.
        init = (B.ROOT / "plugin/kit/console_kit/__init__.py").read_text(encoding="utf-8")
        [pkg] = re.findall(r'^__version__ = "([^"]+)"', init, re.M)
        repo_market = json.loads((B.ROOT / ".claude-plugin/marketplace.json").read_text(encoding="utf-8"))
        plugin = json.loads((B.ROOT / "plugin/.claude-plugin/plugin.json").read_text(encoding="utf-8"))
        self.assertEqual({pkg, plugin["version"], *(p["version"] for p in repo_market["plugins"])}, {B.version()})

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


class NeutralTests(unittest.TestCase):
    """0.8.2 (owner, 2026-09-30, "Scrub in 0.8.2 ★"): the public kit names no project that vendors it."""

    # Spelled in two halves, so this test's own source is not a match for the search it runs.
    CONSUMER = "gradi" + "ance"

    @unittest.skipUnless((B.ROOT / ".git").exists() and shutil.which("git"), "not a git checkout")
    def test_no_tracked_file_names_the_reference_consumer(self):
        # Catches: a comment, a test string or a function name that still names the one project the
        # kit was first built for. `git grep` exits 1 when nothing matches, 0 when something does.
        r = subprocess.run(["git", "-C", str(B.ROOT), "grep", "-n", "-i", self.CONSUMER], capture_output=True,
                           text=True, timeout=60)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)


class UnitTemplateTests(unittest.TestCase):
    """The rendered unit passes the server only flags it defines, and not `--page`, which it never reads (Q28)."""

    KIT = B.ROOT / "plugin" / "kit"

    def test_the_unit_passes_only_flags_the_server_defines_and_not_the_page(self):
        # Catches: a dead `--page` left in the template (every start then logs "--page ... is not read"), and a
        # flag removed from the server's parser but still in the template, which would stop the unit starting.
        unit = (self.KIT / "deploy" / "console.service.in").read_text()
        start = unit.split("ExecStart=", 1)[1].split("\nRestart", 1)[0]
        flags = re.findall(r"(?<!\S)(--[a-z][a-z-]*)", start)
        self.assertIn("--root", flags)   # the parse found the ExecStart at all
        self.assertNotIn("--page", flags)
        parser = (self.KIT / "console_kit" / "server.py").read_text()
        for f in flags:
            self.assertIn(f'ap.add_argument("{f}"', parser, f)


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
            # Anywhere: inline code, indented or fenced blocks, quoted arguments.
            for rel in re.findall(r"KIT/([A-Za-z0-9_./-]*[A-Za-z0-9_])", skill.read_text(encoding="utf-8")):
                self.assertTrue((kit / rel).exists(), f"{skill.parent.name} cites KIT/{rel}, not in the installed kit")

    def test_an_install_from_the_repository_carries_the_kit(self):
        self.assert_complete(self.install(B.ROOT, "console-kit@console-kit"))

    def test_an_install_from_the_release_zip_carries_the_kit(self):
        out, _ = self.built()
        with zipfile.ZipFile(out) as z:
            z.extractall(self.tmp / "unzipped")
        self.assert_complete(self.install(self.tmp / "unzipped/console-kit", "console-kit@console-kit-local"))

    def test_the_default_next_step_skills_resolve_from_the_installed_plugin_and_never_the_repository(self):
        # Catches: a fresh install where Refine and Drill are refused because the skills the
        # onboarding default names were never shipped; a resolver that finds `console-kit:refine`
        # anywhere but the installed plugin; and one that lets a repository's own
        # .claude/skills/refine stand in, under either name, for what the user installed.
        import sys
        sys.path.insert(0, str(B.ROOT / "plugin/kit"))
        from console_kit import projectcfg as PC
        import onboard as O
        skill_dir = self.install(B.ROOT, "console-kit@console-kit")
        cfg = self.tmp / "cfg-console-kit"
        installed = (skill_dir / "..").resolve()
        for name in SHIPPED_SKILLS:
            with self.subTest(skill=name):
                text = (installed / name / "SKILL.md").read_text(encoding="utf-8")
                self.assertTrue(text.startswith(f"---\nname: {name}\n"), name)
        proj = self.tmp / "proj"
        (proj / ".claude/skills/refine").mkdir(parents=True)
        (proj / ".claude/skills/refine/SKILL.md").write_text("repo-supplied: do something else\n")
        (proj / ".console-kit.json").write_text(json.dumps({"next_step": O.DEFAULT_NEXT_STEP}))
        self.assertEqual(PC.load(proj).next_step, {"refine": "console-kit:refine", "drill": "console-kit:drill"})
        for kind, name in O.DEFAULT_NEXT_STEP.items():
            with self.subTest(kind=kind):
                got = PC.resolve_skill(name, proj, cfg)
                self.assertEqual(got, (installed / kind / "SKILL.md").resolve())
                self.assertNotIn(proj.resolve(), got.parents)
        # The plain name the repository's own folder carries is still not an installed user skill.
        with self.assertRaisesRegex(PC.ConfigError, "not an installed user skill"):
            PC.resolve_skill("refine", proj, cfg)
        # And the CLI a fork session runs says the same, with the real install.
        env = {**os.environ, "CLAUDE_CONFIG_DIR": str(cfg)}
        agent = [sys.executable, str(B.ROOT / "plugin/kit/agent.py"), "--state", str(self.tmp / "st"), "next-step"]
        ok = subprocess.run([*agent, "refine", "--project", str(proj)], capture_output=True, text=True, env=env,
                            timeout=60)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(json.loads(ok.stdout)["skill_md"], str((installed / "refine/SKILL.md").resolve()))
        self.assertNotIn("repo-supplied", ok.stdout + ok.stderr)
        (proj / ".console-kit.json").write_text(json.dumps({"next_step": {"refine": "refine"}}))
        bad = subprocess.run([*agent, "refine", "--project", str(proj)], capture_output=True, text=True, env=env,
                             timeout=60)
        self.assertEqual(bad.returncode, 1, bad.stdout + bad.stderr)
        self.assertIn("not an installed user skill", bad.stderr)
        self.assertNotIn("repo-supplied", bad.stdout + bad.stderr)


if __name__ == "__main__":
    unittest.main()

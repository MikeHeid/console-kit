#!/usr/bin/env python3
"""Tests for plugin/kit/tools/verify_vendor.py (0.6.0): a vendored copy must be one kit release, exactly.

Each test builds a throwaway kit repository with git, tags it, vendors it the
way a project that vendors the kit flat does, and then breaks the copy one way at a time.

    python3 -m unittest test_vendor
"""
from __future__ import annotations

import contextlib
import io
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "plugin" / "kit" / "tools"))
import verify_vendor as VV  # noqa: E402

KIT_FILES = {
    "plugin/kit/overture/__init__.py": "v = 1\n",
    "plugin/kit/overture/server.py": "server\n",
    "plugin/kit/overture/console.js": "js\n",
    "plugin/kit/agent.py": "agent\n",
    "plugin/kit/fold.py": "fold\n",
    "plugin/kit/publish.py": "publish\n",
    "plugin/kit/server.py": "shim\n",
    "plugin/kit/requirements.txt": "PyJWT==2\n",
    "plugin/kit/onboard.py": "not vendored by the flat layout\n",
    "plugin/.claude-plugin/plugin.json": '{"version": "1.0.0"}\n',
    "plugin/hooks/hooks.json": "{}\n",
    "plugin/hooks/session_start.py": "hook\n",
    "plugin/agents/ux.md": "ux\n",
    "plugin/skills/console-fold/SKILL.md": "fold skill\n",
    "plugin/skills/console-onboard/SKILL.md": "onboard skill\n",
    "test_kit.py": "KIT = HERE / 'plugin' / 'kit'\n",
}


def vendor_map(kit_path: str) -> str | None:
    """The inverse of RULES, for building a correct copy (the test's own table, not the script's)."""
    for src, dst in (("plugin/kit/overture/", "overture/"), ("plugin/kit/", ""), ("plugin/", "plugin/")):
        if kit_path.startswith(src):
            rel = dst + kit_path[len(src):]
            return None if rel in ("onboard.py", "plugin/skills/console-onboard/SKILL.md") else rel
    return None


class VerifyVendorTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        d = Path(self._td.name)
        self.kit, self.copy = d / "kit", d / "project" / "tools" / "overture"
        for rel, text in KIT_FILES.items():
            (self.kit / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.kit / rel).write_text(text)
        self.git("init", "-q")
        self.git("add", "-A")
        self.git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "1.0.0")
        self.git("tag", "v1.0.0")
        for rel in KIT_FILES:
            dst = vendor_map(rel)
            if dst:
                (self.copy / dst).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(self.kit / rel, self.copy / dst)
        # What a flat vendoring project adapts: its tests point at a flat layout.
        (self.copy / "test_kit.py").write_text("KIT = HERE\n")
        (self.copy / "overture" / "__pycache__").mkdir()
        (self.copy / "overture" / "__pycache__" / "server.cpython-312.pyc").write_bytes(b"\0")

    def git(self, *args):
        subprocess.run(["git", "-C", str(self.kit), *args], check=True, capture_output=True)

    def run_cli(self, *extra, ref="v1.0.0"):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = VV.main(["--kit", str(self.kit), f"--ref={ref}", "--vendored", str(self.copy), *extra])
        return rc, out.getvalue() + err.getvalue()

    def test_an_exact_copy_passes_and_names_its_tag(self):
        rc, out = self.run_cli()
        self.assertEqual(rc, 0, out)
        self.assertIn("tag v1.0.0", out)
        self.assertIn("adapted test_kit.py", out)
        rep = VV.verify(self.kit, "v1.0.0", self.copy)
        self.assertEqual(rep["matching"], 13)  # every vendored kit file was compared, caches skipped

    def test_one_changed_byte_is_drift(self):
        # Catches: a check by name or size only.
        p = self.copy / "overture" / "server.py"
        p.write_text("servex\n")
        rc, out = self.run_cli()
        self.assertEqual(rc, 1)
        self.assertIn("DRIFT overture/server.py: differs", out)

    def test_a_line_ending_change_is_drift(self):
        (self.copy / "agent.py").write_bytes(b"agent\r\n")
        self.assertEqual(self.run_cli()[0], 1)

    def test_a_stray_file_is_drift(self):
        # Catches: a project file dropped into the kit's folder, or a kit file renamed.
        (self.copy / "overture" / "local_patch.py").write_text("x\n")
        (self.copy / "notes.txt").write_text("x\n")
        rc, out = self.run_cli()
        self.assertEqual(rc, 1)
        self.assertIn("DRIFT overture/local_patch.py", out)
        self.assertIn("DRIFT notes.txt", out)

    def test_a_module_the_release_added_but_the_copy_lacks_is_drift(self):
        (self.copy / "overture" / "console.js").unlink()
        (self.copy / "plugin" / "hooks" / "hooks.json").unlink()
        (self.copy / "fold.py").unlink()
        rep = VV.verify(self.kit, "v1.0.0", self.copy)
        self.assertEqual(sorted(rep["missing"]), ["fold.py", "overture/console.js", "plugin/hooks/hooks.json"])
        self.assertFalse(rep["ok"])

    def test_skills_may_be_left_out_but_one_taken_is_taken_whole(self):
        # A vendoring project may leave out console-onboard: that is allowed. A skill it has must be complete.
        self.assertEqual(VV.verify(self.kit, "v1.0.0", self.copy)["missing"], [])
        (self.kit / "plugin/skills/console-fold/reference.md").write_text("more\n")
        self.git("add", "-A")
        self.git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "1.1.0")
        self.git("tag", "v1.1.0")
        self.assertEqual(VV.verify(self.kit, "v1.1.0", self.copy)["missing"],
                         ["plugin/skills/console-fold/reference.md"])

    def test_a_copy_of_another_release_is_drift(self):
        # Catches: comparing against the working tree instead of the named release.
        (self.kit / "plugin/kit/agent.py").write_text("agent 2\n")
        self.git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qam", "1.1.0")
        self.git("tag", "v1.1.0")
        self.assertEqual(self.run_cli(ref="v1.0.0")[0], 0)
        rc, out = self.run_cli(ref="v1.1.0")
        self.assertEqual(rc, 1)
        self.assertIn("DRIFT agent.py", out)
        # The kit's checkout is not what is read: dirty it, and v1.0.0 still passes.
        (self.kit / "plugin/kit/fold.py").write_text("dirty\n")
        self.assertEqual(self.run_cli(ref="v1.0.0")[0], 0)

    def test_an_adapted_file_is_listed_not_compared_and_more_can_be_declared(self):
        (self.copy / "overture" / "local_patch.py").write_text("x\n")
        self.assertEqual(self.run_cli("--adapted", "overture/local_patch.py")[0], 0)

    def test_a_symlink_is_never_followed(self):
        # Catches: a link to the kit's own file passing as a vendored copy.
        p = self.copy / "publish.py"
        p.unlink()
        p.symlink_to(self.kit / "plugin/kit/publish.py")
        rc, out = self.run_cli()
        self.assertEqual(rc, 1)
        self.assertIn("publish.py (a symlink", out)

    def test_an_unknown_ref_or_a_non_clone_is_a_usage_error(self):
        rc, out = self.run_cli(ref="v9.9.9")
        self.assertEqual(rc, 2)
        self.assertIn("no tag or commit 'v9.9.9'", out)
        self.assertEqual(self.run_cli(ref="--upload-pack=x")[0], 2)
        shutil.rmtree(self.kit / ".git")
        self.assertEqual(self.run_cli()[0], 2)

    def test_the_real_kit_maps_every_file_a_flat_vendor_takes(self):
        # The table must cover this repository's own layout: every kit file a rule names exists here.
        for r in VV.RULES:
            with self.subTest(rule=r.kit):
                self.assertTrue((HERE / r.kit).exists(), r.kit)


if __name__ == "__main__":
    unittest.main(verbosity=1)

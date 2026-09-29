"""onboard.py: every answer validated by name, nothing foreign overwritten, no secret opened.

    python3 test_onboard.py
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import socket
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import onboard as O

AUD = "0123456789abcdef" * 4
GOOD = {"name": "acme", "team_domain": "acme.cloudflareaccess.com", "aud": AUD,
        "hostname": "acme-console.example.com", "port": 4793}
TID = "0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b"


class Base(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.root = Path(td.name)
        self.project = self.root / "proj"
        self.project.mkdir()
        self.home = self.root / "home"
        self.home.mkdir()
        # Never the real ~/.cloudflared or ~/.local/state.
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home), "XDG_STATE_HOME": str(self.home / "st")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, **over):
        return O.write(self.project, {**GOOD, **over}, check_port=False)


class CheckTests(Base):
    def test_good_answers_are_normalised(self):
        a = O.check({**GOOD, "name": " Acme ", "hostname": "ACME-Console.Example.com"})
        self.assertEqual(a["name"], "acme")
        self.assertEqual(a["hostname"], "acme-console.example.com")
        self.assertEqual(a["tunnel"], "acme-console")        # the default tunnel name
        self.assertEqual(a["page"], ".console-kit/page.html")
        self.assertEqual(a["adapter"], ".console-kit/adapter.py")

    def test_each_bad_answer_is_refused_by_its_own_name(self):
        cases = {
            "name": ["a", "1acme", "acme_", "acme-", "x" * 33, "ac me"],
            "team_domain": ["acme.example.com", "acme.cloudflareaccess.com.evil.net", ""],
            "aud": ["abc", "g" * 64, AUD + "0", AUD[:-1]],
            "hostname": ["localhost", "http://acme.example.com", "acme.example.com/x", "a b.example.com"],
            "port": [80, 70000, "x"],
            "page": ["/etc/passwd", "../outside.html", "a/../../b"],
            "adapter": ["/tmp/a.py", "..", "~/a.py"],
            "tunnel": ["Bad Tunnel", "-x"],
        }
        for key, values in cases.items():
            for v in values:
                with self.subTest(key=key, value=v):
                    with self.assertRaises(O.OnboardError) as e:
                        O.check({**GOOD, key: v})
                    self.assertTrue(str(e.exception).startswith(key + ":"), e.exception)

    def test_the_team_domain_is_not_accepted_as_the_hostname(self):
        with self.assertRaisesRegex(O.OnboardError, "^hostname: that is the team domain"):
            O.check({**GOOD, "hostname": "acme.cloudflareaccess.com"})


class WriteTests(Base):
    def test_writes_env_config_adapter_and_page(self):
        out = self.write()
        env = (self.project / ".console-kit/console.env").read_text()
        self.assertTrue(env.startswith(O.MARK))
        for line in ("CONSOLE_NAME=acme", f"CONSOLE_AUD={AUD}", "CONSOLE_HOSTNAME=acme-console.example.com",
                     "CONSOLE_PORT=4793", "CONSOLE_TUNNEL=acme-console"):
            self.assertIn(line + "\n", env)
        cfg = json.loads((self.project / ".console-kit.json").read_text())
        self.assertEqual(cfg["fold"]["adapter"], ".console-kit/adapter.py")
        self.assertTrue((self.project / ".console-kit/adapter.py").is_file())
        self.assertIn(O.MARK.lstrip("# "), (self.project / ".console-kit/page.html").read_text())
        self.assertEqual(len(out), 4, out)

    def test_a_rerun_with_the_same_answers_changes_nothing(self):
        self.write()
        before = {p: p.read_bytes() for p in self.project.rglob("*") if p.is_file()}
        out = self.write()
        self.assertEqual(out, [f"unchanged {self.project.resolve() / '.console-kit/console.env'}"])
        self.assertEqual(before, {p: p.read_bytes() for p in self.project.rglob("*") if p.is_file()})

    def test_a_rerun_with_new_answers_rewrites_its_own_file(self):
        self.write()
        self.write(hostname="other.example.com")
        self.assertIn("CONSOLE_HOSTNAME=other.example.com\n", (self.project / ".console-kit/console.env").read_text())

    def test_a_file_it_did_not_write_is_refused_unless_forced(self):
        env = self.project / ".console-kit/console.env"
        env.parent.mkdir()
        env.write_text("CONSOLE_NAME=hand-made\n")
        with self.assertRaisesRegex(O.OnboardError, "was not written by onboard.py"):
            self.write()
        self.assertEqual(env.read_text(), "CONSOLE_NAME=hand-made\n")
        O.write(self.project, GOOD, force=True, check_port=False)
        self.assertIn("CONSOLE_NAME=acme", env.read_text())

    def test_an_existing_adapter_and_page_are_left_alone(self):
        (self.project / "board.html").write_text("<p>mine</p>")
        (self.project / "tools").mkdir()
        (self.project / "tools/adapter.py").write_text("# mine\n")
        self.write(page="board.html", adapter="tools/adapter.py")
        self.assertEqual((self.project / "board.html").read_text(), "<p>mine</p>")
        self.assertEqual((self.project / "tools/adapter.py").read_text(), "# mine\n")

    def test_the_config_is_merged_never_replaced(self):
        (self.project / ".console-kit.json").write_text(json.dumps(
            {"keep": 1, "fold": {"locked": "custom/locked", "other": True}}))
        self.write(audit_seat="determinism", audit_brief="check replay")
        cfg = json.loads((self.project / ".console-kit.json").read_text())
        self.assertEqual(cfg["keep"], 1)
        self.assertEqual(cfg["fold"], {"locked": "custom/locked", "other": True,
                                       "ledger": ".console-kit/folded.txt", "adapter": ".console-kit/adapter.py"})
        self.assertEqual(cfg["audit"], {"seat": "determinism", "brief": "check replay"})

    def test_a_broken_config_is_refused_before_anything_is_written(self):
        (self.project / ".console-kit.json").write_text("{not json")
        with self.assertRaisesRegex(O.OnboardError, "is not valid JSON"):
            self.write()
        self.assertFalse((self.project / ".console-kit").exists())

    def test_a_bad_answer_writes_nothing(self):
        with self.assertRaises(O.OnboardError):
            self.write(aud="nope")
        self.assertEqual(list(self.project.iterdir()), [])

    def test_a_port_in_use_is_refused(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            s.listen()
            port = s.getsockname()[1]
            if port < 1024:
                self.skipTest("the OS gave a privileged port")
            with self.assertRaisesRegex(O.OnboardError, f"^port: {port} is already in use"):
                O.write(self.project, {**GOOD, "port": port})

    def test_next_steps_route_dns_with_its_own_config(self):
        steps = O.next_steps(self.project, O.check(GOOD))
        self.assertIn("register --project", steps)
        route = next(line for line in steps.splitlines() if "route dns" in line)
        self.assertIn("--config", route)
        self.assertIn("config-acme-console.yml", route)


class ReadEnvTests(Base):
    def test_show_returns_only_validated_keys(self):
        self.write()
        env = self.project / ".console-kit/console.env"
        env.write_text(env.read_text() + "EVIL=$(rm -rf ~)\nExecStartPre=/bin/sh\n")
        got = O.read_env(env)
        self.assertEqual(set(got), {"CONSOLE_NAME", "CONSOLE_TEAM_DOMAIN", "CONSOLE_AUD", "CONSOLE_HOSTNAME",
                                    "CONSOLE_PORT", "CONSOLE_TUNNEL", "CONSOLE_PAGE", "CONSOLE_ADAPTER"})

    def test_a_hand_edited_bad_value_is_refused_on_read(self):
        self.write()
        env = self.project / ".console-kit/console.env"
        env.write_text(env.read_text().replace("CONSOLE_NAME=acme", "CONSOLE_NAME=acme;touch /tmp/x"))
        with self.assertRaisesRegex(O.OnboardError, "^name:"):
            O.read_env(env)

    def test_show_on_a_project_never_onboarded(self):
        with self.assertRaisesRegex(O.OnboardError, "run `onboard.py write` first"):
            O.read_env(self.project / ".console-kit/console.env")


class TunnelTests(Base):
    def setUp(self):
        super().setUp()
        self.write()
        self.cf = self.home / ".cloudflared"
        self.cf.mkdir()

    def test_a_bad_id_is_refused(self):
        for bad in ("acme-console", "../../etc/passwd", TID + "x"):
            with self.subTest(bad=bad), self.assertRaisesRegex(O.OnboardError, "^id:"):
                O.tunnel(self.project, bad)

    def test_no_credentials_file_is_refused(self):
        with self.assertRaisesRegex(O.OnboardError, "no credentials file"):
            O.tunnel(self.project, TID)

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root can read a mode-000 file")
    def test_the_credentials_file_is_never_opened(self):
        creds = self.cf / f"{TID}.json"
        creds.write_text('{"TunnelSecret": "SENTINEL-SECRET"}')
        creds.chmod(0)                       # any open() of it would raise PermissionError
        self.addCleanup(creds.chmod, stat.S_IRUSR | stat.S_IWUSR)
        O.tunnel(self.project, TID.upper())
        cfg = (self.cf / "config-acme-console.yml").read_text()
        self.assertNotIn("SENTINEL", cfg)
        self.assertIn(TID, cfg)
        self.assertIn(str(creds), cfg)
        self.assertIn("acme-console.example.com", cfg)
        self.assertIn("127.0.0.1:4793", cfg)

    def test_a_foreign_tunnel_config_is_not_overwritten(self):
        (self.cf / f"{TID}.json").write_text("{}")
        (self.cf / "config-acme-console.yml").write_text("tunnel: someone-else\n")
        with self.assertRaisesRegex(O.OnboardError, "was not written by onboard.py"):
            O.tunnel(self.project, TID)


class CliTests(Base):
    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = O.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_a_refusal_exits_2_and_names_the_value(self):
        rc, out, err = self.run_main("write", "--project", str(self.project), "--name", "Bad Name",
                                     "--team-domain", GOOD["team_domain"], "--aud", AUD,
                                     "--hostname", GOOD["hostname"], "--no-port-check")
        self.assertEqual(rc, 2)
        self.assertTrue(err.startswith("onboard: name:"), err)
        self.assertEqual(out, "")

    def test_write_then_show(self):
        rc, out, _ = self.run_main("write", "--project", str(self.project), "--name", "acme",
                                   "--team-domain", GOOD["team_domain"], "--aud", AUD,
                                   "--hostname", GOOD["hostname"], "--no-port-check")
        self.assertEqual(rc, 0)
        self.assertIn("Next, run these yourself", out)
        rc, out, _ = self.run_main("show", "--project", str(self.project))
        self.assertEqual(rc, 0)
        self.assertIn("CONSOLE_NAME=acme\n", out)


if __name__ == "__main__":
    unittest.main()

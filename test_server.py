"""Tests for the owner console server (spec §4.4, §4.5; slice P3's ACs).

The Access check runs for real: tokens are signed with a throwaway RSA key and
verified by PyJWT through `access_verifier`. Only the key lookup is replaced,
so no test reaches the network.

    python3 tools/overture/test_server.py
"""

from __future__ import annotations

import contextlib
import hashlib
import http.client
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

HERE = Path(__file__).resolve().parent
KIT = HERE / "plugin" / "kit"  # the kit ships inside the plugin, so every install carries it
sys.path.insert(0, str(KIT))
# 0.8.3: never read or write the user's real registry (`watch`, `synced` and the fold read it).
if not os.environ.get("OVERTURE_TEST_CONFIG"):
    os.environ["OVERTURE_TEST_CONFIG"] = os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="ck-cfg-")
os.environ.pop("OVERTURE_AGENT", None)
from overture import server as SV  # noqa: E402
from overture import multiserver as MS  # noqa: E402
from overture import items as IT  # noqa: E402
from overture import pagesnap as PS  # noqa: E402
from overture import prs as PR  # noqa: E402
from overture import publish as P  # noqa: E402
from html import escape as html_escape  # noqa: E402

TEAM = "team.example.cloudflareaccess.com"
AUD = "a" * 64
HOSTNAME = "console.example.com"
ITEMS = {
    "LANE": {"title": "a lane", "parent": None, "status": "open"},
    "LANE.1": {"title": "a phase", "parent": "LANE", "status": "open"},
}
SEED = [{"qid": "LANE.1/Q1", "item": "LANE.1", "text": "Which?", "kind": "single",
         "options": [{"id": "a", "label": "A"}, {"id": "b", "label": "B ★"}], "star": "b",
         "valid_if": [], "source": "architect/40-specs/owner-console.md:1", "by": "agent",
         "nonce": "seednonce0001"}]
PAGE = "<!doctype html><html><body><p>board</p></body></html>\n"

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def token(key=KEY, **over) -> str:
    now = int(time.time())
    claims = {"aud": [AUD], "iss": f"https://{TEAM}", "email": "owner@example.com",
              "iat": now, "exp": now + 600, "sub": "owner"}
    claims.update(over)
    for k in [k for k, v in claims.items() if v is None]:
        del claims[k]
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "k1"})


class FakeAdapter:
    def items(self):
        return dict(ITEMS)

    def seed_questions(self):
        return [dict(q) for q in SEED]

    def record(self, entries, dry_run):
        return []


def seam_closed(test) -> None:
    """Close the git seam for one test, as `serve` closes it for the server process (CONSOLE-kit/Q23).

    The in-process servers these tests build never run `serve`, so without this
    they would read git where the served console does not. Reopened after the test.
    """
    from unittest import mock
    from overture import gitseam as G
    p = mock.patch.object(G, "_OPEN", False)
    p.start()
    test.addCleanup(p.stop)


def seam_open():
    """The seam as an agent-side caller finds it: open, so git is read."""
    from unittest import mock
    from overture import gitseam as G
    return mock.patch.object(G, "_OPEN", True)


class EarlyRefusalDrainTests(unittest.TestCase):
    """A refusal sent before the body is read must not cut off a client still sending it (the BrokenPipe flake)."""

    def test_a_body_sent_after_the_refusal_is_read_not_cut_off(self):
        # Catches: closing the connection right after an early 403/404 with the declared body unread. Python's
        # http.client sends headers and body in two writes; on a Unix socket the second then fails with EPIPE.
        # Here the body is sent only AFTER the whole refusal arrived, which makes that race certain, not rare.
        import socket as so
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name)
        (d / "page.html").write_text(PAGE)
        cfg = SV.Config(root=d, page=d / "page.html", state=d / "state", adapter=d / "unused.py",
                        team_domain="t.example.com", aud=AUD, hostname=HOSTNAME, port=0)
        cfg.state.mkdir()
        (d / "unused.py").write_text("def items():\n    return {}\ndef seed_questions():\n    return []\n"
                                     "def record(entries, dry_run):\n    return []\n")
        console = SV.Console(cfg, IT.SnapshotAdapter(cfg.state))
        srv = SV.UnixHTTPServer(str(d / "a.sock"), type("H", (SV.AgentHandler,), {"console": console}))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        body = b'{"x": 1}'
        s = so.socket(so.AF_UNIX, so.SOCK_STREAM)
        s.settimeout(10)
        s.connect(str(d / "a.sock"))
        s.sendall(b"POST /no-such-route HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                  b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n")
        got = b""
        while b"\r\n\r\n" not in got or not got.endswith(b"}"):
            chunk = s.recv(4096)
            if not chunk:
                break
            got += chunk
        self.assertTrue(got.startswith(b"HTTP/1.0 404") or got.startswith(b"HTTP/1.1 404"), got[:40])
        time.sleep(0.2)                                   # the server has answered; is it still listening?
        s.sendall(body)                                   # EPIPE here when it closed without reading
        s.close()

    # -- the drain is bounded (K4 security review): by size, and by ONE total deadline ------------

    def server(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name)
        (d / "page.html").write_text(PAGE)
        cfg = SV.Config(root=d, page=d / "page.html", state=d / "state", adapter=d / "unused.py",
                        team_domain="t.example.com", aud=AUD, hostname=HOSTNAME, port=0)
        cfg.state.mkdir()
        (d / "unused.py").write_text("def items():\n    return {}\ndef seed_questions():\n    return []\n"
                                     "def record(entries, dry_run):\n    return []\n")
        console = SV.Console(cfg, IT.SnapshotAdapter(cfg.state))
        srv = SV.UnixHTTPServer(str(d / "a.sock"), type("H", (SV.AgentHandler,), {"console": console}))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return d / "a.sock"

    def refused_then_eof(self, sock, declared: int, feed=None) -> float:
        """Send headers declaring `declared` body bytes to an unknown route, read the 404, then time to EOF."""
        import socket as so
        s = so.socket(so.AF_UNIX, so.SOCK_STREAM)
        s.settimeout(15)
        s.connect(str(sock))
        self.addCleanup(s.close)
        s.sendall(b"POST /no-such-route HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                  b"Content-Length: " + str(declared).encode() + b"\r\n\r\n")
        got = b""
        while not got.endswith(b"}"):
            chunk = s.recv(4096)
            self.assertTrue(chunk, "the connection closed before the refusal arrived")
            got += chunk
        self.assertIn(b" 404 ", got.split(b"\r\n", 1)[0])
        t0 = time.monotonic()
        stop = threading.Event()
        if feed is not None:
            threading.Thread(target=feed, args=(s, stop), daemon=True).start()
        try:
            self.assertEqual(s.recv(1), b"")              # EOF: the server closed
        except (ConnectionResetError, BrokenPipeError):
            pass                                          # closed with bytes unread: an RST, also a close
        finally:
            stop.set()
        return time.monotonic() - t0

    def test_the_agent_door_serves_one_request_per_connection(self):
        # Pins the premise /history-blob's per-INSTANCE max_body rests on (lane 4 review, LOW): HTTP/1.0, so a
        # handler object never serves a second request. Catches: a move to HTTP/1.1 keep-alive, under which the
        # raised limit would carry over to every later route on the same connection.
        self.assertEqual(SV.AgentHandler.protocol_version, "HTTP/1.0")

    def test_a_body_nested_past_the_decoders_depth_is_refused_as_not_json(self):
        # Catches (lane 4 review, LOW): json.loads raising RecursionError, which `except ValueError` lets escape,
        # so the handler dies and the client gets a dropped connection instead of the normal refusal.
        import http.client
        import socket as so
        sock = self.server()
        deep = ("[" * 100_000 + "]" * 100_000).encode()

        class Conn(http.client.HTTPConnection):
            def connect(self):
                self.sock = so.socket(so.AF_UNIX, so.SOCK_STREAM)
                self.sock.connect(str(sock))

        for route in ("/cursor", "/history-blob", "/history-specs"):
            c = Conn("localhost", timeout=10)
            c.request("POST", route, body=deep, headers={"Content-Type": "application/json"})
            r = c.getresponse()
            self.assertEqual((r.status, json.loads(r.read()).get("error")), (400, "the body is not JSON"), route)
            c.close()

    def test_a_trickled_body_is_refused_at_one_total_deadline(self):
        # Catches (lane 4 review, LOW): a body read under the 30 s socket timeout alone, which is per read and
        # restarts with every byte, so a client sending a byte every 0.2 s holds a handler thread for as long as
        # it likes. Here 100 declared bytes at that pace would take 20 s; the deadline (patched to 1 s) ends it.
        import socket as so
        from unittest import mock
        sock = self.server()
        s = so.socket(so.AF_UNIX, so.SOCK_STREAM)
        s.settimeout(15)
        s.connect(str(sock))
        self.addCleanup(s.close)
        stop = threading.Event()

        def trickle():
            try:
                while not stop.wait(0.2):
                    s.sendall(b" ")
            except OSError:
                pass

        with mock.patch.object(SV, "BODY_SECONDS", 1.0):
            t0 = time.monotonic()
            s.sendall(b"POST /cursor HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\nContent-Length: 100\r\n\r\n")
            threading.Thread(target=trickle, daemon=True).start()
            got = b""
            try:
                while not got.endswith(b"}"):
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    got += chunk
            finally:
                stop.set()
            took = time.monotonic() - t0
        self.assertIn(b" 408 ", got.split(b"\r\n", 1)[0], got[:80])
        self.assertIn(b"the body did not arrive within 1 seconds", got)
        self.assertLess(took, 3.0)

    def test_a_body_larger_than_max_body_is_not_read_or_waited_for(self):
        # Catches: draining whatever length is declared (an 8 MiB claim must not be read, nor waited on).
        took = self.refused_then_eof(self.server(), SV.AGENT_MAX_BODY * 4)
        self.assertLess(took, 1.0)

    def test_a_client_that_declares_a_body_and_stops_is_cut_off_at_the_deadline(self):
        from unittest import mock
        with mock.patch.object(SV, "DRAIN_SECONDS", 0.5):
            took = self.refused_then_eof(self.server(), 1000)
        self.assertLess(took, 2.0)                        # not the 30 s per-read timeout

    def test_a_trickling_client_cannot_stretch_the_deadline(self):
        # Catches: a per-read timeout standing in for a total one. A byte every 0.2 s restarts any per-read
        # timer for ever; the total deadline still closes the connection on time.
        from unittest import mock

        def trickle(s, stop):
            while not stop.is_set():
                try:
                    s.send(b" ")
                except OSError:
                    return
                time.sleep(0.2)
        with mock.patch.object(SV, "DRAIN_SECONDS", 0.5):
            took = self.refused_then_eof(self.server(), 100_000, feed=trickle)
        self.assertLess(took, 2.0)

    @staticmethod
    def late_then_silent(s, stop):
        """Bytes at 0, 0.3, 0.6 and 0.9 s, then nothing: the last read starts just before a 1 s deadline."""
        for _ in range(4):
            try:
                s.send(b" ")
            except OSError:
                return
            if stop.wait(0.3):
                return

    def test_each_read_waits_only_for_what_is_left_of_the_drain_deadline(self):
        # Catches (lane 4 review, LOW gap a): each read given the WHOLE budget (settimeout(seconds)) instead of
        # what is left (settimeout(remaining)). A read that starts at 0.9 s of a 1 s deadline then waits until
        # 1.9 s; the loop's own check runs only between reads, so it cannot stop that. The trickle tests above
        # stay under their bound either way; this one does not.
        from unittest import mock
        with mock.patch.object(SV, "DRAIN_SECONDS", 1.0):
            took = self.refused_then_eof(self.server(), 100_000, feed=self.late_then_silent)
        self.assertLess(took, 1.45)

    def test_each_read_waits_only_for_what_is_left_of_the_body_deadline(self):
        # The same, for the body read (`_body` shares `_read_within`): the 408 arrives at the deadline, not a
        # whole budget after the last byte.
        import socket as so
        from unittest import mock
        sock = self.server()
        s = so.socket(so.AF_UNIX, so.SOCK_STREAM)
        s.settimeout(15)
        s.connect(str(sock))
        self.addCleanup(s.close)
        stop = threading.Event()
        with mock.patch.object(SV, "BODY_SECONDS", 1.0):
            s.sendall(b"POST /cursor HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\nContent-Length: 100\r\n\r\n")
            t0 = time.monotonic()
            threading.Thread(target=self.late_then_silent, args=(s, stop), daemon=True).start()
            got = b""
            try:
                while not got.endswith(b"}"):
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    got += chunk
            finally:
                stop.set()
            took = time.monotonic() - t0
        self.assertIn(b" 408 ", got.split(b"\r\n", 1)[0], got[:80])
        self.assertLess(took, 1.45)


def steward_push(console, root) -> dict:
    """What `agent.py history-push` does, in-process: the server says what it wants, the steward reads git
    (the seam open, as in the agent's process) for exactly that, and pushes it through the console's own door."""
    from overture import stewardgit as SG
    want = console.history_wants()
    with seam_open():
        blobs, specs = SG.collect(root, want)
    for b in blobs:
        console.push_history_blob(b)
    return {"want": want, "blobs": blobs, "specs": console.push_history_specs({"specs": specs})}


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        (d / "page.html").write_text(PAGE)
        self.cfg = SV.Config(root=d, page=d / "page.html", state=d / "state", adapter=d / "unused.py",
                             team_domain=TEAM, aud=AUD, hostname=HOSTNAME, port=0, project="test")
        self.console = SV.Console(self.cfg, FakeAdapter())
        self.console.seed()
        verify = SV.access_verifier(TEAM, AUD, key_for=lambda _t: KEY.public_key())
        self.owner = SV.owner_server(self.console, verify, 0)
        self.port = self.owner.server_address[1]
        threading.Thread(target=self.owner.serve_forever, daemon=True).start()
        self.agent = SV.agent_server(self.console)
        threading.Thread(target=self.agent.serve_forever, daemon=True).start()

    def tearDown(self):
        self.owner.shutdown()
        self.owner.server_close()
        self.agent.shutdown()
        self.agent.server_close()
        self.tmp.cleanup()

    def req(self, method, path, body=None, tok=None, headers=None, origin=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = dict(headers or {})
        if method == "POST" and origin:  # what a browser on the console's own page sends
            h.setdefault("Origin", f"https://{HOSTNAME}")
            # 1.27.0: CSRF double-submit — the browser carries the server's boot-scoped
            # token in its HTML config block and echoes it on every POST. Tests read the
            # token off the live Console instance and set it the same way.
            if hasattr(self, "console") and getattr(self.console, "_csrf", None):
                h.setdefault("X-Overture-CSRF", self.console._csrf)
        if tok is not None:
            h["Cf-Access-Jwt-Assertion"] = tok
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            h.setdefault("Content-Type", "application/json")
        conn.request(method, path, body=data, headers=h)
        r = conn.getresponse()
        raw = r.read()
        conn.close()
        try:
            return r.status, json.loads(raw)
        except ValueError:
            return r.status, raw.decode()

    def answer(self, **over):
        b = {"qid": "LANE.1/Q1", "picks": ["a"], "own_text": "", "nonce": "ownernonce01"}
        b.update(over)
        return b

    def doorbell(self):
        p = self.cfg.inbox
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    # -- the Access gate (P3 ACs) ---------------------------------------------

    # -- /api/board: live values (AB-2/Q4) ---------------------------------------

    def with_board(self, fn):
        """Give the console an adapter whose board() is `fn`, counting its calls."""
        calls = []

        class BoardAdapter(FakeAdapter):
            def board(self):
                calls.append(1)
                return fn()
        self.console.adapter = BoardAdapter()
        return calls

    def test_board_is_behind_the_same_gate(self):
        # Catches: the new route answered before _gate(), leaking register state
        # to anyone who can reach the port.
        calls = self.with_board(lambda: {"shape": "s", "values": {"pct": "5%"}})
        self.assertEqual(self.req("GET", "/api/board")[0], 403)
        self.assertEqual(self.req("GET", "/api/board", tok=token(key=OTHER))[0], 403)
        self.assertEqual(calls, [])  # refused before the register is read at all
        code, body = self.req("GET", "/api/board", tok=token())
        # 1.20.1: the server adds `items_tree` to values (kit-computed from items.json); the test
        # asserts on the project's own `pct` plus the shape, and tolerates the extra kit key.
        self.assertEqual(code, 200)
        self.assertEqual(body["shape"], "s")
        self.assertEqual(body["values"].get("pct"), "5%")
        self.assertEqual(len(calls), 1)

    def test_a_stored_items_file_that_fails_its_check_is_503_on_both_views_and_names_no_path(self):
        # Catches (lane 5 review, MEDIUM): payload() raising StoreError straight out of /view and /api/view, so
        # the caller sees a dropped connection; and a 503 whose body carries the server's file path. A real
        # SnapshotAdapter (the one server's, which refuses rather than tolerates) over an items.json that is not
        # JSON; after a push the same views answer 200 again.
        import io
        from unittest import mock
        state = Path(self.cfg.state, "items-state")
        state.mkdir()
        (state / IT.ITEMS).write_bytes(b"{not json")
        self.console.adapter = IT.SnapshotAdapter(state)
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            code_owner, owner = self.req("GET", "/api/view", tok=token())
            code_agent, agent = SV.agent_request(self.cfg.socket, "GET", "/view")
        for route, code, body in (("/api/view", code_owner, owner), ("/view", code_agent, agent)):
            with self.subTest(route=route):
                self.assertEqual(code, 503, body)
                self.assertEqual(set(body), {"error"})
                self.assertIn("items-push", body["error"])
                self.assertNotIn(str(state), json.dumps(body))
                self.assertNotIn(IT.ITEMS, json.dumps(body))
        self.assertEqual(err.getvalue().count("console view: "), 2, err.getvalue())   # the path goes here instead
        IT.store_snapshot(state, {"items": {}, "seed_questions": []})
        self.assertEqual(self.req("GET", "/api/view", tok=token())[0], 200)
        self.assertEqual(SV.agent_request(self.cfg.socket, "GET", "/view")[0], 200)

    def test_a_stored_items_file_that_cannot_be_opened_is_503_on_both_views_and_names_no_path(self):
        # Lane 5 re-review (MEDIUM): on the refusing adapter (the one server's), a mode-0000 items.json raised a
        # bare PermissionError past _view, which catches StoreError, so the caller saw a dropped connection. It is
        # now the same named StoreError: a 503 with no path, like a file that fails its check.
        import io
        from unittest import mock
        if os.geteuid() == 0:
            self.skipTest("root reads a mode-0000 file, so this cannot provoke EACCES")
        state = Path(self.cfg.state, "items-state")
        state.mkdir()
        (state / IT.ITEMS).write_text(json.dumps({"items": {}}))
        os.chmod(state / IT.ITEMS, 0)
        self.console.adapter = IT.SnapshotAdapter(state)
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            code_owner, owner = self.req("GET", "/api/view", tok=token())
            code_agent, agent = SV.agent_request(self.cfg.socket, "GET", "/view")
        for route, code, body in (("/api/view", code_owner, owner), ("/view", code_agent, agent)):
            with self.subTest(route=route):
                self.assertEqual((code, body), (503, {"error": SV.VIEW_UNREADABLE}))
        self.assertEqual(err.getvalue().count("cannot be read: Permission denied"), 2, err.getvalue())

    def test_board_is_404_when_the_adapter_offers_none(self):
        # The kit is project-neutral: a project without board() keeps its static page.
        code, body = self.req("GET", "/api/board", tok=token())
        self.assertEqual(code, 404)
        self.assertIn("board()", body["error"])

    def test_a_malformed_board_is_refused_not_served(self):
        # The adapter is project code; the page applies what it gets to the DOM,
        # so a non-string value must never reach it.
        for bad in ({"shape": "s", "values": {"pct": 5}}, {"shape": 1, "values": {}},
                    {"values": {}}, {"shape": "s", "values": ["x"]}, ["s"], None):
            with self.subTest(bad=bad):
                self.with_board(lambda bad=bad: bad)
                code, body = self.req("GET", "/api/board", tok=token())
                self.assertEqual(code, 503)
                self.assertNotIn("values", body)

    def test_a_board_that_raises_is_503_and_the_server_lives(self):
        def boom():
            raise ValueError("register caught mid-write")
        self.with_board(boom)
        code, body = self.req("GET", "/api/board", tok=token())
        self.assertEqual(code, 503)
        self.assertNotIn("mid-write", json.dumps(body))  # the reason goes to the log, not the page
        self.assertEqual(self.req("GET", "/api/view", tok=token())[0], 200)

    def test_loopback_request_without_a_token_is_refused(self):
        # AC: refused directly on loopback, not only through the public hostname.
        for path in ("/", "/api/view"):
            code, body = self.req("GET", path)
            self.assertEqual(code, 403, path)
        code, _ = self.req("POST", "/api/answer", self.answer())
        self.assertEqual(code, 403)
        self.assertEqual(len(self.console.store.records()), 1)  # only the seed

    def test_bad_signature_is_refused(self):
        # Counter-check: a server that checks only that the header is present passes every other
        # refusal test that sends no header, and must fail this one.
        code, body = self.req("GET", "/api/view", tok=token(key=OTHER))
        self.assertEqual(code, 403)
        self.assertIn("InvalidSignatureError", body["error"])

    def test_wrong_audience_issuer_or_expiry_is_refused(self):
        cases = {
            "aud": token(aud=["b" * 64]),
            "iss": token(iss="https://other.cloudflareaccess.com"),
            "exp": token(exp=int(time.time()) - 3600, iat=int(time.time()) - 7200),
            "no exp": token(exp=None),
            "garbage": "not.a.jwt",
        }
        for name, tok in cases.items():
            code, _ = self.req("GET", "/api/view", tok=tok)
            self.assertEqual(code, 403, name)

    def test_an_unsigned_or_symmetric_token_is_refused(self):
        # Catches: algorithms taken from the token's header instead of pinned to RS256.
        claims = {"aud": [AUD], "iss": f"https://{TEAM}", "iat": int(time.time()), "exp": int(time.time()) + 600}
        unsigned = jwt.api_jws.base64url_encode(json.dumps({"alg": "none", "typ": "JWT"}).encode()).decode() + "." + \
            jwt.api_jws.base64url_encode(json.dumps(claims).encode()).decode() + "."
        hs = jwt.encode(claims, "shared-secret-that-is-long-enough-for-hs256", algorithm="HS256")
        for tok in (unsigned, hs):
            code, _ = self.req("GET", "/api/view", tok=tok)
            self.assertEqual(code, 403)

    def test_a_valid_token_is_admitted(self):
        code, body = self.req("GET", "/api/view", tok=token())
        self.assertEqual(code, 200)
        self.assertEqual(body["view"]["inbox"], ["LANE.1/Q1"])
        self.assertEqual(set(body["items"]), set(ITEMS))
        code, page = self.req("GET", "/", tok=token())
        self.assertEqual(code, 200)
        self.assertIn('id="overture-config"', page)

    def test_the_page_config_carries_the_running_kit_version(self):
        # Owner, 2026-10-01: "version number should be in footer". Catches: a footer version typed into
        # console.js (it would drift from the release), or one the page has no way to learn.
        import re
        from overture import __version__
        code, page = self.req("GET", "/", tok=token())
        self.assertEqual(code, 200)
        m = re.search(r'<script type="application/json" id="overture-config">(.*?)</script>', page, re.S)
        self.assertIsNotNone(m, "no config block in the served page")
        self.assertEqual(json.loads(m.group(1)).get("version"), __version__)
        health = SV.agent_request(self.cfg.socket, "GET", "/health")[1]
        self.assertEqual(health.get("version"), __version__)   # the same value /health reports
        js = (KIT / "overture" / "console.js").read_text(encoding="utf-8")
        self.assertNotIn(__version__, js)   # never hardcoded in the page's script

    def test_every_other_method_meets_the_gate(self):
        # Catches: only GET and POST gated, so PUT/OPTIONS/... reach a default handler unchecked
        # (Sourcery on #165).
        for method in ("HEAD", "PUT", "DELETE", "PATCH", "OPTIONS"):
            self.assertEqual(self.req(method, "/api/view")[0], 403, method)
            self.assertEqual(self.req(method, "/api/view", tok=token())[0], 405, method)

    def test_the_server_binds_loopback_only(self):
        self.assertEqual(self.owner.server_address[0], "127.0.0.1")

    # -- owner writes and the doorbell (D8) -----------------------------------

    def test_owner_answer_is_stamped_owner_and_rings_once(self):
        code, body = self.req("POST", "/api/answer", self.answer(), tok=token())
        self.assertEqual(code, 200, body)
        self.assertEqual(body["record"]["by"], "owner")
        # A retried submit (same nonce) stores nothing new and rings nothing new.
        code, again = self.req("POST", "/api/answer", self.answer(), tok=token())
        self.assertEqual((code, again["record"]["id"]), (200, body["record"]["id"]))
        bell = self.doorbell()
        self.assertEqual(len(bell), 1)
        self.assertEqual((bell[0]["qid"], bell[0]["seq"]), ("LANE.1/Q1", body["record"]["seq"]))

    def test_a_fork_request_rings_with_its_intent(self):
        # §6.3: the queue shows a fork without reading the store. Catches: a doorbell line
        # that names the item only, so a waiting fork looks like any other message.
        body = {"item": "LANE.1", "text": "deliberate on this", "intent": "fork", "mode": "tighten",
                "focus": "code", "nonce": "ownerfork001"}
        code, got = self.req("POST", "/api/message", body, tok=token())
        self.assertEqual(code, 200, got)
        self.assertEqual(got["record"]["by"], "owner")
        bell = self.doorbell()
        self.assertEqual((bell[-1]["item"], bell[-1]["intent"]), ("LANE.1", "fork"))
        code, _ = self.req("POST", "/api/message", {"item": "LANE.1", "text": "plain", "nonce": "ownerplain01"},
                           tok=token())
        self.assertNotIn("intent", self.doorbell()[-1])

    def test_a_follow_up_on_one_answer_rings_as_a_fork_and_is_checked_at_the_door(self):
        # 0.4.0: the "Follow up" button on a locked answer. Catches: a server that trusts
        # the page (an unknown question, or a roar on an unlocked one, accepted), and a
        # doorbell line the agent's watch does not wake on, so the follow-up would sit unrun.
        # Seats on an OPEN question are a deliberation before answering (ruling build_reply).
        from overture import doorbell as D
        body = {"item": "LANE.1", "text": "follow up", "intent": "fork", "mode": "tighten",
                "about_qid": "LANE.1/Q1", "roles": ["devops", "other:Legal"], "nonce": "ownerabout01"}
        code, got = self.req("POST", "/api/message", dict(body, roles=["roar"], nonce="ownerabout00"),
                             tok=token())
        self.assertEqual(code, 400, got)
        self.assertIn("no locked answer", got["error"])
        code, a = self.req("POST", "/api/answer", self.answer(), tok=token())
        self.assertEqual(code, 200, a)
        code, _ = self.req("POST", "/api/lock", {"qid": "LANE.1/Q1", "answer": a["record"]["id"],
                                                 "nonce": "ownerlock001"}, tok=token())
        self.assertEqual(code, 200)
        code, got = self.req("POST", "/api/message", dict(body, nonce="ownerabout02"), tok=token())
        self.assertEqual(code, 200, got)
        line = self.doorbell()[-1]
        self.assertEqual((line["intent"], line["about_qid"]), ("fork", "LANE.1/Q1"))
        self.assertEqual(D.pending(self.cfg.inbox, 0)[-1]["seq"], got["record"]["seq"])
        view = self.console.payload()["view"]
        self.assertEqual(view["forks"][got["record"]["id"]]["message"]["about_qid"], "LANE.1/Q1")
        code, got = self.req("POST", "/api/message", dict(body, about_qid="LANE.1/Q77", nonce="ownerabout03"),
                             tok=token())
        self.assertEqual(code, 400, got)
        self.assertIn("names no question", got["error"])

    def test_the_ready_signal_rings_once_per_press_and_only_the_owner_sends_it(self):
        # §7.3 and §7.5 F2. Catches: a ready signal the agent door accepts (so an agent could
        # start its own processing run), and a retried press that rings twice.
        body = {"item": "LANE.1", "text": "Answers are in: process them.", "intent": "process",
                "nonce": "ownerready01"}
        code, got = self.req("POST", "/api/message", body, tok=token())
        self.assertEqual(code, 200, got)
        code, again = self.req("POST", "/api/message", body, tok=token())
        self.assertEqual((code, again["record"]["id"]), (200, got["record"]["id"]))
        bell = [b for b in self.doorbell() if b.get("intent") == "process"]
        self.assertEqual(len(bell), 1)
        self.assertEqual(bell[0]["item"], "LANE.1")
        code, refused = SV.agent_request(self.cfg.socket, "POST", "/message",
                                         {"item": "LANE.1", "text": "go", "intent": "process",
                                          "nonce": "agentready01"})
        self.assertEqual(code, 400, refused)
        self.assertIn("owner", refused["error"])

    def agent_cli(self, *args):
        import contextlib
        import io
        import agent as AG
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = AG.main(["--state", str(self.cfg.state), *args])
        return rc, out.getvalue(), err.getvalue()

    def test_ask_posts_a_batch_and_stops_at_the_first_refusal(self):
        # Owner, 2026-09-29: questions go to the console, several at once (console-ask skill).
        # Catches: a batch that posts only the first file; one that carries on past a refusal,
        # so a later question lands while an earlier one is silently missing; and one that
        # does not name what was left unsent.
        from test_kit import question
        files = []
        for q in ("LANE.1/Q7", "LANE.1/Q8", "LANE.1/Q7", "LANE.1/Q9"):  # the third is a re-ask
            body = {k: v for k, v in question(q).items() if k not in ("type", "by", "nonce", "schemaVersion")}
            f = self.cfg.state / f"q{len(files)}.json"
            f.write_text(json.dumps(body))
            files.append(f)
        rc, out, err = self.agent_cli("ask", *map(str, files[:2]))
        self.assertEqual(rc, 0, out + err)
        self.assertIn("posted 2 questions", err)
        rc, _, err = self.agent_cli("ask", *map(str, files[2:]))
        self.assertEqual(rc, 1)
        self.assertIn(f"posted 0 of 2; not sent: {files[2]} {files[3]}", err)
        _, body = self.req("GET", "/api/view", tok=token())
        asked = [q for q in body["view"]["questions"] if q.startswith("LANE.1/Q")]
        self.assertIn("LANE.1/Q8", asked)
        self.assertNotIn("LANE.1/Q9", asked)  # never sent past the refusal
        bad = self.cfg.state / "bad.json"
        bad.write_text("[1, 2]")
        rc, _, err = self.agent_cli("ask", str(files[3]), str(bad))
        self.assertEqual(rc, 1)
        self.assertIn("not a JSON object", err)
        self.assertIn("posted 1 of 2", err)

    def test_the_agent_cli_end_to_end(self):
        # §7.5 F3 and the PR #170 review LOW (`answers` had no test of its own). The whole
        # loop: the owner presses the ready button, `watch` wakes, the agent reads the sheet
        # and the round's bundle, and `synced --through` stops the same signal waking it again.
        fork_body = {"item": "LANE.1", "text": "deliberate", "intent": "fork", "mode": "explore",
                     "nonce": "ownerfork777"}
        code, f = self.req("POST", "/api/message", fork_body, tok=token())
        self.assertEqual(code, 200, f)
        rc, out, _ = self.agent_cli("watch", "--timeout", "0")
        self.assertEqual(rc, 0)
        [line] = [json.loads(x) for x in out.splitlines()]
        self.assertEqual(line["intent"], "fork")

        rc, out, _ = self.agent_cli("fork-context", f["record"]["id"])
        self.assertEqual(rc, 0)
        self.assertIn("LANE.1/Q1", out)
        rc, _, err = self.agent_cli("fork-context", "0" * 24)
        self.assertEqual(rc, 1)
        self.assertIn("no fork", err)

        self.assertEqual(self.req("POST", "/api/answer", self.answer(), tok=token())[0], 200)
        rc, out, _ = self.agent_cli("answers", "--item", "LANE")
        self.assertEqual(rc, 0)
        self.assertIn("## LANE.1/Q1: answered, not locked", out)
        self.assertIn("1 answer in all", out)
        self.assertEqual(self.agent_cli("answers", "--item", "NOPE")[0], 1)

        rc, _, _ = self.agent_cli("synced", "--through", str(line["seq"]))
        self.assertEqual(rc, 0)
        rc, _, err = self.agent_cli("watch", "--timeout", "0")
        self.assertEqual(rc, 3)  # the answer rang the doorbell but does not wake; the fork is processed
        code, _ = self.req("POST", "/api/message", {"item": "LANE.1", "text": "Answers are in.",
                                                    "intent": "process", "nonce": "ownerready77"}, tok=token())
        rc, out, _ = self.agent_cli("watch", "--timeout", "0")
        self.assertEqual((rc, json.loads(out)["intent"]), (0, "process"))
        rc, out, _ = self.agent_cli("inbox")  # after the cursor: the answer and the process signal
        self.assertEqual([json.loads(x)["type"] for x in out.splitlines()], ["answer", "message"])

    def test_the_page_cannot_choose_its_author(self):
        for field in ("by", "type", "schemaVersion"):
            code, body = self.req("POST", "/api/answer", self.answer(**{field: "agent"}), tok=token())
            self.assertEqual(code, 400, field)
        self.assertEqual(self.doorbell(), [])

    def test_store_refusals_come_back_as_400(self):
        code, body = self.req("POST", "/api/answer", self.answer(picks=["zzz"]), tok=token())
        self.assertEqual(code, 400)
        self.assertIn("not options", body["error"])
        code, _ = self.req("POST", "/api/message", {"item": "NOT-AN-ITEM", "text": "x", "nonce": "n0000001"},
                           tok=token())
        self.assertEqual(code, 400)

    def test_cross_site_and_non_json_writes_are_refused(self):
        code, _ = self.req("POST", "/api/answer", self.answer(), tok=token(),
                           headers={"Origin": "https://evil.example"})
        self.assertEqual(code, 403)
        code, _ = self.req("POST", "/api/answer", json.dumps(self.answer()).encode(), tok=token(),
                           headers={"Content-Type": "text/plain"})
        self.assertEqual(code, 415)
        code, _ = self.req("POST", "/api/answer", self.answer(), tok=token(),
                           headers={"Origin": f"https://{HOSTNAME}"})
        self.assertEqual(code, 200)

    def test_a_write_with_no_origin_is_refused(self):
        # Catches: "no Origin" read as trusted (#165 security review, MEDIUM).
        code, _ = self.req("POST", "/api/answer", self.answer(), tok=token(), origin=False)
        self.assertEqual(code, 403)
        self.assertEqual(self.doorbell(), [])

    def test_an_oversized_body_is_refused(self):
        big = self.answer(own_text="x" * (SV.MAX_BODY + 1))
        code, _ = self.req("POST", "/api/answer", big, tok=token())
        self.assertEqual(code, 413)

    def raw_post(self, content_length: str) -> int:
        """Send only the headers, keep the socket open, and read the reply.

        No body is sent. A server that believed the header and tried to read the
        body (for -1, "to the end of the stream") would wait for bytes that never
        come, and the 10 s timeout fails the test. Sending a large body instead
        raced the server's close against unread bytes: the kernel answered with a
        reset, and the test failed about 1 run in 4 (PR #168 review, ENOTCONN).
        """
        import socket as so
        s = so.create_connection(("127.0.0.1", self.port), timeout=10)
        csrf = self.console._csrf
        head = (f"POST /api/answer HTTP/1.1\r\nHost: x\r\nOrigin: https://{HOSTNAME}\r\n"
                f"Cf-Access-Jwt-Assertion: {token()}\r\nContent-Type: application/json\r\n"
                f"X-Overture-CSRF: {csrf}\r\n"
                f"Content-Length: {content_length}\r\nConnection: close\r\n\r\n").encode()
        s.sendall(head)
        data = b""
        while chunk := s.recv(4096):
            data += chunk
        s.close()
        return int(data.split(b" ", 2)[1])

    def test_a_negative_or_malformed_content_length_is_refused(self):
        # Catches: int("-1") accepted, so read(-1) reads to end of stream past the cap
        # (#165 security review, HIGH). Answered without reading any body, or it times out.
        for cl in ("-1", "+5", "1e3", " -0", "²", "9" * 40):
            self.assertEqual(self.raw_post(cl), 400, cl)
        self.assertEqual(self.doorbell(), [])

    def test_an_existing_loose_state_dir_is_tightened(self):
        # Catches: mkdir(mode=0o700, exist_ok=True) leaving a pre-existing 0755 dir as it was
        # (#165 security review, MEDIUM: store.jsonl became readable by every local account).
        loose = Path(self.tmp.name) / "loose"
        loose.mkdir(mode=0o755)
        os.chmod(loose, 0o755)
        SV.Console(SV.Config(**{**self.cfg.__dict__, "state": loose}), FakeAdapter())
        self.assertEqual(stat.S_IMODE(os.stat(loose).st_mode), 0o700)

    # -- the agent door -------------------------------------------------------

    def test_agent_door_is_user_only_and_stamps_agent(self):
        mode = stat.S_IMODE(os.lstat(self.cfg.socket).st_mode)
        self.assertEqual(mode, 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.cfg.state).st_mode), 0o700)
        code, body = SV.agent_request(self.cfg.socket, "POST", "/message",
                                      {"item": "LANE", "text": "noted", "nonce": "agentnonce01"})
        self.assertEqual(code, 200, body)
        self.assertEqual(body["record"]["by"], "agent")
        self.assertEqual(self.doorbell(), [])  # the doorbell is for the owner's writes only
        code, body = SV.agent_request(self.cfg.socket, "POST", "/message",
                                      {"item": "LANE", "text": "x", "by": "owner", "nonce": "agentnonce02"})
        self.assertEqual(code, 400)

    def test_cursor_round_trip_reaches_the_page(self):
        code, _ = SV.agent_request(self.cfg.socket, "POST", "/cursor",
                                   {"last_synced_at": "2026-09-28T10:00:00Z", "last_error": None})
        self.assertEqual(code, 200)
        _, body = self.req("GET", "/api/view", tok=token())
        self.assertEqual(body["cursor"]["last_synced_at"], "2026-09-28T10:00:00Z")
        code, _ = SV.agent_request(self.cfg.socket, "POST", "/cursor", {"last_error": "two\nlines"})
        self.assertEqual(code, 400)
        code, _ = SV.agent_request(self.cfg.socket, "POST", "/cursor", {"stray": 1})
        self.assertEqual(code, 400)

    def test_a_working_mark_reaches_the_page_until_the_agent_syncs(self):
        # Catches: "agent active" that never clears, so a finished session still looks at work.
        code, _ = SV.agent_request(self.cfg.socket, "POST", "/working", {"items": ["LANE", "LANE.1"]})
        self.assertEqual(code, 200)
        _, body = self.req("GET", "/api/view", tok=token())
        self.assertEqual(sorted(body["cursor"]["working"]), ["LANE", "LANE.1"])
        SV.agent_request(self.cfg.socket, "POST", "/cursor", {"last_synced_at": "2026-09-29T10:00:00Z"})
        _, body = self.req("GET", "/api/view", tok=token())
        self.assertEqual(body["cursor"]["working"], {})

    def test_a_stale_working_mark_is_not_shown(self):
        # Catches: a session that died mid-work leaving the owner a standing false "agent active".
        old = time.strftime(SV.TS_FORMAT, time.gmtime(time.time() - SV.WORKING_TTL - 5))
        fresh = time.strftime(SV.TS_FORMAT, time.gmtime())
        (self.cfg.state / "working.json").write_text(json.dumps({"LANE": old, "LANE.1": fresh, "bad id!": fresh}))
        _, body = self.req("GET", "/api/view", tok=token())
        self.assertEqual(list(body["cursor"]["working"]), ["LANE.1"])

    def test_working_takes_only_a_short_list_of_item_ids(self):
        for bad in ({}, {"items": []}, {"items": "LANE"}, {"items": ["LANE"], "extra": 1},
                    {"items": ["../x"]}, {"items": ["ok"] * (SV.MAX_WORKING + 1)}, {"items": [7]}):
            with self.subTest(body=bad):
                code, _ = SV.agent_request(self.cfg.socket, "POST", "/working", bad)
                self.assertEqual(code, 400)
        self.assertFalse((self.cfg.state / "working.json").exists())

    def test_the_owner_door_cannot_set_a_working_mark(self):
        # Catches: the owner's side (or anything reaching the tunnel) claiming an agent is at work.
        for path in ("/api/working", "/working"):
            with self.subTest(path=path):
                code, _ = self.req("POST", path, {"items": ["LANE"]}, tok=token())
                self.assertIn(code, (403, 404))
        self.assertFalse((self.cfg.state / "working.json").exists())

    def test_a_socket_path_too_long_is_refused_by_name(self):
        # Catches: a --state deep enough that bind() fails with a bare OSError and the owner door never opens.
        deep = SV.Config(**{**self.cfg.__dict__, "state": self.cfg.state / ("d" * 120)})
        with self.assertRaises(SystemExit) as cm:
            SV.agent_server(SV.Console(deep, FakeAdapter()))
        self.assertIn("pass a shorter --state", str(cm.exception))

    def test_seed_is_idempotent_across_restarts(self):
        again = SV.Console(self.cfg, FakeAdapter())
        self.assertEqual(again.seed(), [])
        self.assertEqual(len(again.store.records()), 1)


    # -- 0.5.0: re-lock re-anchors; /check; reanchor ---------------------------------

    SPEC = "# Spec\n\nIntro.\n\nThe cited claim, line one.\nThe cited claim, line two.\n\nTail.\n"

    def git(self, *args):
        import subprocess
        subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "-c", "commit.gpgsign=false",
                        *args], cwd=self.cfg.root, check=True, capture_output=True)

    def locked_on_spec(self, source="spec.md:5-6"):
        """LANE.1/Q2 asked against spec.md's whole-file hash, answered and locked through the owner door."""
        import hashlib
        spec = self.cfg.root / "spec.md"
        spec.write_text(self.SPEC)
        q = {"qid": "LANE.1/Q2", "item": "LANE.1", "text": "Still?", "kind": "single",
             "options": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}], "star": None,
             "valid_if": [{"kind": "file_sha256", "path": "spec.md",
                           "sha256": hashlib.sha256(self.SPEC.encode()).hexdigest()}],
             "source": source, "nonce": "askspec00001"}
        # Stored as an older kit's ask stored it: `ask` refuses this shape since CONSOLE-kit/Q30 (R1, R2 by name).
        with self.console._lock:
            self.console.store.append({**q, "type": "question", "schemaVersion": SV.S.SCHEMA_VERSION, "by": "agent"})
        code, a = self.req("POST", "/api/answer", self.answer(qid="LANE.1/Q2", nonce="answerspec01"), tok=token())
        self.assertEqual(code, 200, a)
        code, lk = self.req("POST", "/api/lock", {"qid": "LANE.1/Q2", "answer": a["record"]["id"],
                                                  "nonce": "lockspec0001"}, tok=token())
        self.assertEqual(code, 200, lk)
        return spec, a["record"], lk["record"]

    def state_of(self, qid):
        return self.req("GET", "/api/view", tok=token())[1]["view"]["questions"][qid]

    def test_a_first_lock_carries_no_anchors(self):
        # Catches: every lock growing a field, which would make every store unreadable by a 0.4.0 kit.
        _, _, lk = self.locked_on_spec()
        self.assertNotIn("anchors", lk)
        self.assertEqual(self.state_of("LANE.1/Q2")["state"], "locked")

    def test_the_page_cannot_send_anchors_on_a_lock(self):
        spec, a, _ = self.locked_on_spec()
        code, b = self.req("POST", "/api/answer", self.answer(qid="LANE.1/Q2", supersedes=a["id"],
                                                              reason="again", nonce="answerspec02"), tok=token())
        self.assertEqual(code, 200, b)
        before = self.cfg.store.read_bytes()
        forged = [{"kind": "file_sha256", "path": "spec.md", "sha256": "0" * 64}]
        code, out = self.req("POST", "/api/lock", {"qid": "LANE.1/Q2", "answer": b["record"]["id"],
                                                   "anchors": forged, "nonce": "lockspec0002"}, tok=token())
        self.assertEqual(code, 400)
        self.assertIn("anchors is the server's to set", out["error"])
        self.assertEqual(self.cfg.store.read_bytes(), before)

    def test_relocking_a_stale_answer_reanchors_it(self):
        spec, a, _ = self.locked_on_spec()
        spec.write_text(self.SPEC + "An unrelated change.\n")
        self.assertEqual(self.state_of("LANE.1/Q2")["state"], "stale")
        code, out = self.req("POST", "/api/relock", {"qid": "LANE.1/Q2", "nonce": "relockspec01"}, tok=token())
        self.assertEqual(code, 200, out)
        ans, lk = out["records"]
        self.assertEqual((ans["picks"], ans["supersedes"]), (a["picks"], a["id"]))
        self.assertIn("still holds", ans["reason"])
        self.assertEqual(lk["anchors"][0]["sha256"],
                         __import__("hashlib").sha256(spec.read_bytes()).hexdigest())
        q = self.state_of("LANE.1/Q2")
        self.assertEqual((q["state"], q["anchored_by"]), ("locked", "lock"))
        # A retry with the same nonce writes nothing more.
        n = len(self.console.store.records())
        code, again = self.req("POST", "/api/relock", {"qid": "LANE.1/Q2", "nonce": "relockspec01"}, tok=token())
        self.assertEqual((code, [r["id"] for r in again["records"]]), (200, [ans["id"], lk["id"]]))
        self.assertEqual(len(self.console.store.records()), n)
        # Both new records rang the doorbell, so the agent folds the new lock.
        self.assertEqual([x["type"] for x in self.doorbell()][-2:], ["answer", "lock"])

    def test_relock_needs_a_locked_answer(self):
        code, out = self.req("POST", "/api/relock", {"qid": "LANE.1/Q1", "nonce": "relockspec02"}, tok=token())
        self.assertEqual(code, 400)
        self.assertIn("no locked answer", out["error"])
        code, _ = self.req("POST", "/api/relock", {"qid": "LANE.1/Q1", "nonce": "relockspec03",
                                                   "anchors": []}, tok=token())
        self.assertEqual(code, 400)

    def test_check_is_behind_the_gate_and_says_why(self):
        spec, _, _ = self.locked_on_spec()
        spec.unlink()
        self.assertEqual(self.req("GET", "/api/check")[0], 403)
        self.assertEqual(self.req("GET", "/api/check", tok=token(key=OTHER))[0], 403)
        code, out = self.req("GET", "/api/check", tok=token())
        self.assertEqual(code, 200)
        [c] = out["stale"]["LANE.1/Q2"]["conditions"]
        self.assertEqual((c["reason"], c["holds"]), ("file_missing", False))
        code, via_agent = SV.agent_request(self.cfg.socket, "GET", "/check")
        self.assertEqual((code, via_agent), (200, out))

    def test_reanchor_in_the_server_reads_no_git_and_writes_nothing(self):
        # CONSOLE-kit/Q23: the server starts no git, so it cannot find the version a lock was taken
        # on. Catches: a reanchor that silently re-anchors nothing (the reason must name the cause),
        # and one that still reads history in the server. The agent-side half of the same tree is
        # NoServerGitTests.test_agent_side_history_still_finds_what_the_server_cannot.
        seam_closed(self)
        spec, _, lk = self.locked_on_spec()
        self.git("init", "-q")
        self.git("add", "spec.md")
        self.git("commit", "-qm", "v1")
        spec.write_text("A new first line.\n" + self.SPEC)
        self.git("commit", "-qam", "unrelated")
        before = self.cfg.store.read_bytes()
        for args in (("reanchor", "--dry-run"), ("reanchor",)):
            with self.subTest(args=args):
                rc, out, err = self.agent_cli(*args)
                self.assertEqual(rc, 0, err)
                self.assertIn("LANE.1/Q2: left stale, spec.md: git history is unavailable (no git in the server)",
                              out)
                self.assertIn("git history: unavailable (no git in the server)", err)
                self.assertEqual(self.cfg.store.read_bytes(), before)
                self.assertEqual(self.state_of("LANE.1/Q2")["state"], "stale")
        code, out = SV.agent_request(self.cfg.socket, "POST", "/reanchor", {"dry_run": False})
        self.assertEqual((code, out["history"]), (200, "unavailable (no git in the server)"))
        [p] = out["plan"]
        self.assertEqual((p["lock"], p["changes"], p["fresh"]), (lk["id"], [], False))

    def agent_side_plan(self):
        """`plan_reanchor` after the steward pushed git history (Q23 part 2): the server plans it ITSELF."""
        steward_push(self.console, self.cfg.root)
        return SV.A.plan_reanchor

    def test_reanchor_takes_no_anchors_from_the_caller(self):
        code, out = SV.agent_request(self.cfg.socket, "POST", "/reanchor",
                                     {"dry_run": False, "anchors": [{"kind": "excerpt"}]})
        self.assertEqual(code, 400)
        code, _ = SV.agent_request(self.cfg.socket, "POST", "/reanchor", {"dry_run": "no"})
        self.assertEqual(code, 400)
        code, _ = SV.agent_request(self.cfg.socket, "POST", "/anchor",
                                   {"qid": "LANE.1/Q1", "lock": "0" * 24, "anchors": [], "basis": "x"})
        self.assertEqual(code, 404)  # no route writes an anchor record from a caller's body


    def test_reanchor_skips_a_lock_the_owner_changed_while_it_ran(self):
        # Catches: a plan made outside the write lock being applied to a lock that is no longer current.
        from unittest import mock
        spec, _, _ = self.locked_on_spec()
        self.git("init", "-q")
        self.git("add", "spec.md")
        self.git("commit", "-qm", "v1")
        spec.write_text("A new first line.\n" + self.SPEC)
        self.git("commit", "-qam", "unrelated")
        # The write path's guards, fed the plan an agent-side caller would make: the server
        # itself reads no git (Q23), so without this its plan has nothing to write.
        seam_closed(self)
        real = self.agent_side_plan()

        def plan_then_owner_relocks(*a, **kw):
            plan = real(*a, **kw)
            self.console.relock({"qid": "LANE.1/Q2", "nonce": "racerelock01"})
            return plan
        with mock.patch.object(SV.A, "plan_reanchor", plan_then_owner_relocks):
            code, out = SV.agent_request(self.cfg.socket, "POST", "/reanchor", {"dry_run": False})
        self.assertEqual(code, 200, out)
        [p] = out["plan"]
        self.assertIn("changed while this ran", p["skipped"])
        self.assertNotIn("record", p)
        self.assertEqual([r for r in self.console.store.records() if r["type"] == "anchor"], [])


    def test_reanchor_skips_when_the_file_changes_between_plan_and_write(self):
        # Review MEDIUM: the plan reads the tree outside the write lock. Catches: an anchor written
        # for cited text that was edited away after the plan read it.
        from unittest import mock
        spec, _, _ = self.locked_on_spec()
        self.git("init", "-q")
        self.git("add", "spec.md")
        self.git("commit", "-qm", "v1")
        spec.write_text("A new first line.\n" + self.SPEC)
        self.git("commit", "-qam", "unrelated")
        # The write path's guards, fed the plan an agent-side caller would make: the server
        # itself reads no git (Q23), so without this its plan has nothing to write.
        seam_closed(self)
        real = self.agent_side_plan()

        def plan_then_edit(*a, **kw):
            plan = real(*a, **kw)
            spec.write_text(self.SPEC.replace("line two", "line 2"))
            return plan
        with mock.patch.object(SV.A, "plan_reanchor", plan_then_edit):
            code, out = SV.agent_request(self.cfg.socket, "POST", "/reanchor", {"dry_run": False})
        self.assertEqual(code, 200, out)
        [p] = out["plan"]
        self.assertIn("changed while this ran", p["skipped"])
        self.assertEqual([r for r in self.console.store.records() if r["type"] == "anchor"], [])
        self.assertEqual(self.state_of("LANE.1/Q2")["state"], "stale")


class HealthTests(unittest.TestCase):
    """0.6.0 /health: on the agent socket, and on an opt-in loopback port that no proxy can reach."""

    FIELDS = {"ok", "version", "store_seq", "register", "agent", "agent_listening"}

    setUp, tearDown, req = ServerTests.setUp, ServerTests.tearDown, ServerTests.req

    def start_health(self):
        srv = SV.health_server(self.console, 0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv.server_address[1]

    def health(self, port, headers=None, method="GET", path="/health"):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request(method, path, headers=headers or {})
        r = conn.getresponse()
        raw = r.read()
        conn.close()
        return r.status, (json.loads(raw) if raw else None)

    def test_the_agent_socket_answers_health_with_no_owner_content(self):
        # Catches: a health body that grows a field carrying owner text (an item title, a
        # question, a path) — the field set is pinned — and a store_seq that is not the store's.
        from overture import __version__
        code, out = SV.agent_request(self.cfg.socket, "GET", "/health")
        self.assertEqual(code, 200, out)
        self.assertEqual(set(out), self.FIELDS)
        self.assertEqual(out["version"], __version__)
        self.assertEqual(out["store_seq"], len(self.console.store.records()))
        self.assertEqual((out["ok"], out["register"], out["agent"], out["agent_listening"]),
                         (True, "ok", "never", False))
        blob = json.dumps(out)
        for owner_text in ("Which?", "a lane", "a phase", str(self.cfg.root)):
            self.assertNotIn(owner_text, blob)

    def test_health_follows_the_watch(self):
        from overture import doorbell as D
        D.write_watch(self.cfg.state, True)
        out = SV.agent_request(self.cfg.socket, "GET", "/health")[1]
        self.assertEqual((out["agent"], out["agent_listening"]), ("listening", True))
        # The console page reads the same judgement through the cursor.
        self.assertEqual(self.console.payload()["cursor"]["listening"]["state"], "listening")
        D.write_watch(self.cfg.state, False)
        out = SV.agent_request(self.cfg.socket, "GET", "/health")[1]
        self.assertEqual((out["agent"], out["agent_listening"]), ("idle", False))

    def test_a_broken_register_is_503_and_names_nothing(self):
        # Catches: an ok:true while every page would fail, and the adapter's error text
        # (which can hold paths or item text) leaking into the body.
        class Broken(FakeAdapter):
            def items(self):
                raise RuntimeError("secret-path /home/owner/register.toml")
        self.console.adapter = Broken()
        import contextlib
        import io
        with contextlib.redirect_stderr(io.StringIO()):
            code, out = SV.agent_request(self.cfg.socket, "GET", "/health")
        self.assertEqual(code, 503)
        self.assertEqual((out["ok"], out["register"]), (False, "error"))
        self.assertNotIn("secret-path", json.dumps(out))

    def test_the_owner_door_still_gates_health(self):
        # Catches: /health answered before the Access gate on the port the tunnel reaches,
        # from loopback too (cloudflared connects from loopback).
        self.assertEqual(self.req("GET", "/health")[0], 403)
        self.assertEqual(self.req("GET", "/health", headers={"Host": "127.0.0.1"})[0], 403)
        self.assertEqual(self.req("GET", "/health", tok=token())[0], 404)

    def test_the_health_port_answers_a_local_caller(self):
        port = self.start_health()
        for host in (f"127.0.0.1:{port}", f"localhost:{port}", "localhost"):
            with self.subTest(host=host):
                code, out = self.health(port, {"Host": host})
                self.assertEqual(code, 200, out)
                self.assertEqual(set(out), self.FIELDS)

    def test_the_health_port_refuses_anything_a_proxy_or_the_edge_forwarded(self):
        # The refusal that matters: a loopback peer is not proof of a local caller, since
        # cloudflared connects from loopback. Every header the edge or a proxy adds is refused,
        # one at a time, so a check that looks at only one of them fails here.
        port = self.start_health()
        for h in SV.HealthHandler.PROXY_HEADERS:
            with self.subTest(header=h):
                code, out = self.health(port, {"Host": f"127.0.0.1:{port}", h: "203.0.113.7"})
                self.assertEqual(code, 403, out)
                self.assertIn("proxy", out["error"])
                self.assertNotIn("version", out)

    def test_the_health_port_refuses_a_foreign_host_and_a_remote_peer(self):
        # A page that re-binds its hostname to 127.0.0.1 sends its own Host: refused.
        port = self.start_health()
        # `[::1]` too: the listener is IPv4-only, so no honest client of it names IPv6 loopback.
        for host in ("evil.example", f"evil.example:{port}", "127.0.0.1.evil.example", "", "[::1", f"[::1]:{port}"):
            with self.subTest(host=host):
                code, out = self.health(port, {"Host": host})
                self.assertEqual(code, 403, out)
        # The listener is bound to 127.0.0.1, so no remote peer can connect at all; the peer
        # check behind it is exercised directly.
        import email.message
        hdrs = email.message.Message()
        hdrs["Host"] = f"127.0.0.1:{port}"
        fake = type("Fake", (), {"client_address": ("192.0.2.10", 5555), "headers": hdrs,
                                 "PROXY_HEADERS": SV.HealthHandler.PROXY_HEADERS,
                                 "LOCAL_HOSTS": SV.HealthHandler.LOCAL_HOSTS})()
        self.assertIn("loopback", SV.HealthHandler._refusal(fake))
        fake.client_address = ("127.0.0.1", 5555)
        self.assertIsNone(SV.HealthHandler._refusal(fake))

    def test_the_health_port_serves_health_only(self):
        # Catches: a health door that reaches the view, the store, or any write.
        port = self.start_health()
        local = {"Host": f"127.0.0.1:{port}"}
        for path in ("/", "/api/view", "/view", "/check", "/health/../api/view"):
            with self.subTest(path=path):
                self.assertEqual(self.health(port, local, path=path)[0], 404)
        for method in ("POST", "PUT", "DELETE"):
            with self.subTest(method=method):
                self.assertEqual(self.health(port, local, method=method)[0], 405)

    NON_GET = ("HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS")

    def test_every_method_meets_the_refusals_first(self):
        # Review of PR #8 (MEDIUM): the checks ran for GET only, so a proxied or foreign-Host
        # request with another method got 405 instead of 403. Now every method is refused
        # the same way, and only a clean local non-GET reaches 405.
        port = self.start_health()
        local = f"127.0.0.1:{port}"
        for method in self.NON_GET:
            for label, headers, want in (("cloudflare", {"Host": local, "Cf-Connecting-Ip": "203.0.113.7"}, 403),
                                         ("proxy", {"Host": local, "X-Forwarded-For": "203.0.113.7"}, 403),
                                         ("foreign host", {"Host": "evil.example"}, 403),
                                         ("clean local", {"Host": local}, 405)):
                with self.subTest(method=method, case=label):
                    self.assertEqual(self.health(port, headers, method=method)[0], want)

    def test_head_sends_no_body(self):
        # A raw socket, so a body sent after a HEAD's headers is seen rather than ignored by a client.
        import socket
        port = self.start_health()
        for headers in (f"Host: 127.0.0.1:{port}\r\n", f"Host: 127.0.0.1:{port}\r\nCf-Ray: x\r\n"):
            with self.subTest(headers=headers):
                with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
                    s.sendall(f"HEAD /health HTTP/1.1\r\n{headers}Connection: close\r\n\r\n".encode())
                    raw = b""
                    while chunk := s.recv(4096):
                        raw += chunk
                head, _, rest = raw.partition(b"\r\n\r\n")
                self.assertTrue(head.startswith(b"HTTP/1.0 405") or head.startswith(b"HTTP/1.0 403"), head)
                self.assertEqual(rest, b"")

    def test_the_health_port_is_never_the_owner_port(self):
        from unittest import mock
        cfg = SV.Config(**{**self.cfg.__dict__, "port": 4999})
        with self.assertRaises(SystemExit):
            SV.health_server(SV.Console(cfg, FakeAdapter()), 4999)
        with self.assertRaises(SystemExit), mock.patch("sys.stderr"):
            SV.main(["--root", ".", "--page", "p", "--state", "s", "--adapter", "a", "--team-domain", "t",
                     "--aud", "x", "--hostname", "h", "--port", "5000", "--health-port", "5000"])

    def test_the_agent_cli_reports_health_by_exit_code(self):
        rc, out, _ = ServerTests.agent_cli(self, "health")
        self.assertEqual(rc, 0)
        self.assertTrue(json.loads(out)["ok"])


# -- 0.7.0: a live console ---------------------------------------------------------------


class _Live:
    """Shared setup for the 0.7.0 routes: the real server, the real gate, a real store."""

    setUp, tearDown, req, doorbell = ServerTests.setUp, ServerTests.tearDown, ServerTests.req, ServerTests.doorbell
    agent_cli, answer = ServerTests.agent_cli, ServerTests.answer

    @staticmethod
    def seed_q(**over):
        """The seed question as an agent would post it (no `by`: the server stamps it)."""
        return {**{k: v for k, v in SEED[0].items() if k != "by"}, **over}

    def agent_post(self, path, body):
        return SV.agent_request(self.cfg.socket, "POST", path, body)

    def owner_msg(self, **body):
        body.setdefault("nonce", "own" + os.urandom(6).hex())
        code, out = self.req("POST", "/api/message", body, tok=token())
        self.assertEqual(code, 200, out)
        return out["record"]

    def fork(self, mode="tighten"):
        return self.owner_msg(item="LANE.1", text="deliberate", intent="fork", mode=mode)

    def round_q(self, fork_id, n, **over):
        body = {"qid": f"LANE.1/Q{n}", "item": "LANE.1", "text": f"Finding {n}?", "kind": "single",
                "options": [{"id": "fix", "label": "Fix now"}, {"id": "record", "label": "Record in findings.md"},
                            {"id": "leave", "label": "Leave it"}],
                "star": "fix", "star_by": "panel", "forked_from": fork_id,
                "source": "architect/40-specs/owner-console.md:1", "valid_if": [], "nonce": f"roundq{n}{fork_id[:6]}"}
        body.update(over)
        code, out = self.agent_post("/question", body)
        self.assertEqual(code, 200, out)
        return out["record"]

    def get(self, path, tok=True):
        return self.req("GET", path, tok=token() if tok else None)


class DeliberateOpenQuestionTests(_Live, unittest.TestCase):
    """Owner ruling build_reply: "Deliberate before answering" on an OPEN question."""

    BODY = {"item": "LANE.1", "text": "weigh it first", "intent": "fork", "mode": "explore",
            "about_qid": "LANE.1/Q1", "roles": ["analyst", "security"]}

    def state(self):
        v = self.console.payload()["view"]["questions"]["LANE.1/Q1"]
        return v["state"], [(a["id"], a["locked"]) for a in v["answers"]]

    def test_the_owner_door_accepts_seats_on_an_unanswered_or_unlocked_question(self):
        # Catches: the pre-change server, which refused about_qid until the answer was locked;
        # and a fork that moves the question (answers it, locks it, or changes its state).
        from overture import doorbell as D
        before = self.state()
        self.assertEqual(before[0], "awaiting_you")
        f = self.owner_msg(**self.BODY)
        self.assertEqual((f["by"], f["about_qid"], f["roles"]), ("owner", "LANE.1/Q1", ["analyst", "security"]))
        self.assertEqual(self.state(), before)
        line = self.doorbell()[-1]
        self.assertEqual((line["intent"], line["about_qid"]), ("fork", "LANE.1/Q1"))
        self.assertEqual(D.pending(self.cfg.inbox, 0)[-1]["seq"], f["seq"])
        code, a = self.req("POST", "/api/answer", self.answer(), tok=token())
        self.assertEqual(code, 200, a)
        before = self.state()
        self.assertEqual(before[0], "unlocked")
        self.owner_msg(**dict(self.BODY, roles=["ux"]))
        self.assertEqual(self.state(), before)

    def test_every_other_check_still_holds_on_an_open_question(self):
        # Counter-check: the relaxation is the lock rule only.
        code, got = self.agent_post("/question", self.seed_q(qid="LANE/Q1", item="LANE", nonce="parentq00001"))
        self.assertEqual(code, 200, got)
        for over, want in (({"about_qid": "LANE.1/Q77"}, "names no question"),
                           ({"about_qid": "LANE/Q1"}, "outside this fork's scope"),
                           ({"roles": ["chaos"]}, "role"),
                           ({"roles": ["ux", "devops", "security", "analyst"]}, "1 to 3"),
                           ({"roles": ["roar"]}, "no locked answer"),
                           ({"roles": None, "step": "refine"}, "no locked answer")):
            body = {k: v for k, v in dict(self.BODY, **over).items() if v is not None}
            body["nonce"] = "own" + os.urandom(6).hex()
            code, got = self.req("POST", "/api/message", body, tok=token())
            self.assertEqual(code, 400, (over, got))
            self.assertIn(want, got["error"], over)
        self.assertFalse(any(r.get("about_qid") for r in self.console.store.records()))

    def test_the_agent_door_can_neither_start_one_nor_answer_or_lock(self):
        # Catches: an agent that deliberates on its own account, or turns the round's
        # recommendation into the owner's answer or lock.
        f = self.owner_msg(**self.BODY)
        before = self.state()
        code, got = self.agent_post("/message", dict(self.BODY, nonce="agentdelib01"))
        self.assertEqual(code, 400, got)
        self.assertIn("only the owner writes it", got["error"])
        code, got = self.agent_post("/message", {"item": "LANE.1", "text": "★ a", "reply_to": f["id"],
                                                 "about_qid": "LANE.1/Q1", "nonce": "agentdelib02"})
        self.assertEqual(code, 400, got)
        self.assertIn("belong(s) to a fork", got["error"])
        for path, body in (("/answer", {"qid": "LANE.1/Q1", "picks": ["a"], "own_text": "", "nonce": "agentans001"}),
                           ("/lock", {"qid": "LANE.1/Q1", "answer": "a" * 24, "nonce": "agentlock01"})):
            code, got = self.agent_post(path, body)
            self.assertEqual(code, 404, (path, got))
        self.assertEqual(self.state(), before)
        # The round's one result: a reply on the question's item, to the fork.
        code, got = self.agent_post("/message", {"item": "LANE.1", "reply_to": f["id"], "nonce": "agentdelib03",
                                                 "text": "LANE.1/Q1: ★ a (Option A), because ..."})
        self.assertEqual(code, 200, got)
        self.assertEqual(self.state(), before)


class LiveWaitTests(_Live, unittest.TestCase):
    """GET /api/wait: the long poll the page's live loop runs on."""

    def seq_ver(self):
        code, out = self.get("/api/wait?since=0&timeout=0")
        self.assertEqual(code, 200, out)
        return out["seq"], out["ver"]

    def test_wait_is_behind_the_gate(self):
        # Catches: the new route answered before _gate(), so anyone reaching the port could
        # learn the store's size and the agent's state, and hold server threads open.
        t0 = time.monotonic()
        self.assertEqual(self.req("GET", "/api/wait?since=0")[0], 403)
        self.assertEqual(self.req("GET", "/api/wait?since=0", tok=token(key=OTHER))[0], 403)
        self.assertLess(time.monotonic() - t0, 5)  # refused at once, never parked on the condition

    def test_a_page_behind_the_store_is_answered_at_once(self):
        # Catches: a wait that always parks for the full timeout, so a page that missed a
        # write (opened mid-change, or after a sleep) waits 25 s to catch up.
        t0 = time.monotonic()
        code, out = self.get("/api/wait?since=0&timeout=20")
        self.assertEqual(code, 200, out)
        self.assertLess(time.monotonic() - t0, 3)
        self.assertEqual((out["seq"], out["changed"]), (1, True))
        self.assertRegex(out["ver"], r"^[0-9a-f]{8}\.[0-9]+$")
        self.assertIn("listening", out["cursor"])

    def test_nothing_changed_times_out_with_changed_false(self):
        # Catches: a wait that returns at once when nothing changed (the page would then spin,
        # hammering the server), and one that ignores its timeout.
        seq, ver = self.seq_ver()
        t0 = time.monotonic()
        code, out = self.get(f"/api/wait?since={seq}&ver={ver}&timeout=0.4")
        took = time.monotonic() - t0
        self.assertEqual(code, 200, out)
        self.assertFalse(out["changed"])
        self.assertGreaterEqual(took, 0.35)
        self.assertLess(took, 5)

    def _wait_in_thread(self, seq, ver, timeout=10):
        got = {}

        def run():
            t0 = time.monotonic()
            got["resp"] = self.get(f"/api/wait?since={seq}&ver={ver}&timeout={timeout}")
            got["took"] = time.monotonic() - t0
        th = threading.Thread(target=run)
        th.start()
        time.sleep(0.3)
        return th, got

    def test_an_agent_question_wakes_a_waiting_page(self):
        # Catches: a wait that only polls (sleeping out its timeout) and one woken only by OWNER
        # writes: a new question comes through the agent's door, and must appear without reload.
        seq, ver = self.seq_ver()
        th, got = self._wait_in_thread(seq, ver)
        code, out = self.agent_post("/question", self.seed_q(qid="LANE.1/Q2", nonce="wakeq00002"))
        self.assertEqual(code, 200, out)
        th.join(10)
        code, body = got["resp"]
        self.assertEqual(code, 200, body)
        self.assertLess(got["took"], 5)
        self.assertEqual((body["seq"], body["changed"]), (seq + 1, True))

    def test_the_agent_marking_work_wakes_a_page_with_no_store_change(self):
        # Catches: a wait keyed on the store seq alone: "agent active" is not a store record,
        # so the chip would lag by a whole poll.
        seq, ver = self.seq_ver()
        th, got = self._wait_in_thread(seq, ver)
        self.assertEqual(self.agent_post("/working", {"items": ["LANE.1"]})[0], 200)
        th.join(10)
        code, body = got["resp"]
        self.assertEqual(code, 200, body)
        self.assertLess(got["took"], 5)
        self.assertFalse(body["changed"])           # the store did not move...
        self.assertNotEqual(body["ver"], ver)       # ...but the version did
        self.assertIn("LANE.1", body["cursor"]["working"])

    def test_a_retried_write_wakes_nobody(self):
        # Catches: a bump on every append call, so a flaky network's retry re-renders every tab.
        self.assertEqual(self.req("POST", "/api/answer", self.answer(), tok=token())[0], 200)
        seq, ver = self.seq_ver()
        th, got = self._wait_in_thread(seq, ver, timeout=1)
        self.assertEqual(self.req("POST", "/api/answer", self.answer(), tok=token())[0], 200)  # same nonce
        th.join(10)
        self.assertFalse(got["resp"][1]["changed"])
        self.assertEqual(got["resp"][1]["ver"], ver)

    def test_bad_wait_parameters_are_refused_by_name(self):
        # Catches: an unbounded timeout (a thread parked for an hour) and loose parsing.
        for q in ("", "?since=-1", "?since=x", "?since=0&timeout=26", "?since=0&timeout=1e3",
                  "?since=0&other=1", "?since=0&since=1", "?since=0&ver=../etc"):
            with self.subTest(q=q):
                code, body = self.get("/api/wait" + q)
                self.assertEqual(code, 400, body)

    def test_too_many_open_polls_are_told_to_back_off(self):
        # Catches: an unbounded number of parked threads: the page backs off on 429.
        self.console._waiters = SV.MAX_WAITERS
        seq, ver = self.seq_ver()  # behind-the-store answers never park, so they are not counted
        code, body = self.get(f"/api/wait?since={seq}&ver={ver}&timeout=1")
        self.assertEqual(code, 429, body)


class FeedTests(_Live, unittest.TestCase):
    """GET /api/feed: every store record as an event, newest first, filterable."""

    def build(self):
        a = self.req("POST", "/api/answer", self.answer(), tok=token())[1]["record"]
        self.req("POST", "/api/lock", {"qid": "LANE.1/Q1", "answer": a["id"], "nonce": "locknonce01"}, tok=token())
        f = self.fork()
        self.round_q(f["id"], 2)
        self.agent_post("/message", {"item": "LANE.1", "text": "a reply", "nonce": "agentreply1"})
        self.owner_msg(item="LANE", text="a note on the lane")
        self.owner_msg(item="@chat", text="hello?", intent="chat")
        self.owner_msg(item="LANE.1", text="Answers are in", intent="process")
        return f

    def test_feed_is_behind_the_gate(self):
        self.assertEqual(self.req("GET", "/api/feed")[0], 403)

    def test_every_kind_appears_newest_first(self):
        # Catches: a feed built from the view (which drops locks, anchors and chat) instead of
        # the store, and one in oldest-first order.
        self.build()
        code, out = self.get("/api/feed")
        self.assertEqual(code, 200, out)
        kinds = [e["kind"] for e in out["events"]]
        self.assertEqual(kinds, ["process", "chat", "note", "reply", "question", "fork", "lock", "answer", "question"])
        seqs = [e["seq"] for e in out["events"]]
        self.assertEqual(seqs, sorted(seqs, reverse=True))
        lock = next(e for e in out["events"] if e["kind"] == "lock")
        self.assertEqual((lock["item"], lock["qid"], lock["relock"]), ("LANE.1", "LANE.1/Q1", False))
        self.assertIsNone(out["next_before"])

    def test_filters_by_kind_and_by_item_subtree(self):
        # Catches: an item filter that matches the exact id only (LANE's feed would miss its
        # phase's events), and a kind filter applied after the page is cut (short pages).
        self.build()
        _, out = self.get("/api/feed?kind=lock,answer")
        self.assertEqual([e["kind"] for e in out["events"]], ["lock", "answer"])
        _, out = self.get("/api/feed?item=LANE")
        self.assertIn("note", [e["kind"] for e in out["events"]])
        self.assertIn("lock", [e["kind"] for e in out["events"]])  # LANE.1 is under LANE
        _, out = self.get("/api/feed?item=LANE.1")
        self.assertNotIn("note", [e["kind"] for e in out["events"]])  # LANE's own note is not LANE.1's
        _, out = self.get("/api/feed?item=@chat")
        self.assertEqual([e["kind"] for e in out["events"]], ["chat"])

    def test_pages_older_events_with_before(self):
        # Catches: pagination that repeats or skips an event at the page boundary.
        self.build()
        _, first = self.get("/api/feed?limit=4")
        self.assertEqual(len(first["events"]), 4)
        _, rest = self.get(f"/api/feed?limit=50&before={first['next_before']}")
        _, whole = self.get("/api/feed")
        self.assertEqual([e["seq"] for e in first["events"] + rest["events"]], [e["seq"] for e in whole["events"]])

    def test_bad_feed_parameters_are_refused(self):
        for q in ("?kind=merge", "?limit=0", "?limit=201", "?item=../x", "?before=0", "?x=1"):
            with self.subTest(q=q):
                self.assertEqual(self.get("/api/feed" + q)[0], 400)


class EvidenceTests(_Live, unittest.TestCase):
    """Structured evidence on a question: cited lines read by the server, and checked again for the form."""

    SPEC = "docs/spec.md"

    def write_spec(self, text):
        p = self.cfg.root / self.SPEC
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def ask(self, evidence, n=2):
        return self.agent_post("/question", self.seed_q(qid=f"LANE.1/Q{n}", nonce=f"evidq{n:04d}x",
                                                        evidence=evidence))

    def test_the_server_reads_the_cited_lines_when_asked(self):
        # Catches: storing whatever `text` the writer sends (an agent could claim lines say
        # something they do not), and a question stored with no record of what it cited.
        self.write_spec("line one\nThe cap is 64 KiB.\nline three\n")
        code, out = self.ask([{"cite": f"{self.SPEC}:2", "command": "wc -c bundle", "result": "65536"}])
        self.assertEqual(code, 200, out)
        self.assertEqual(out["record"]["evidence"], [{"cite": f"{self.SPEC}:2", "command": "wc -c bundle",
                                                      "result": "65536", "text": "The cap is 64 KiB."}])
        code, out = self.ask([{"cite": f"{self.SPEC}:2", "text": "forged"}], n=3)
        self.assertEqual(code, 400)
        self.assertIn("server's to read", out["error"])

    def test_evidence_paths_stay_inside_the_project(self):
        # Catches: an evidence cite that reads outside the tree (the form shows the lines, so
        # this would be a file-read primitive for anyone who can post a question).
        self.write_spec("a\nb\n")
        outside = Path(self.tmp.name).parent / "secret.txt"
        for cite in ("../secret.txt:1", "/etc/passwd:1", f"{self.SPEC}/../../x:1", "docs//spec.md:1",
                     f"{self.SPEC}:0", f"{self.SPEC}:2-1", f"{self.SPEC}:1-201", f"{self.SPEC}", "a b.md:1"):
            with self.subTest(cite=cite):
                code, out = self.ask([{"cite": cite}])
                self.assertEqual(code, 400, out)
        self.assertFalse(outside.exists())

    def test_symlinks_out_of_the_project_are_refused(self):
        # Catches: a relative path that resolves outside the root through a symlink.
        target = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(target))
        (target / "secret.txt").write_text("the owner's secret line\n")
        (self.cfg.root / "link").symlink_to(target)
        code, out = self.ask([{"cite": "link/secret.txt:1"}])
        self.assertEqual(code, 400, out)
        self.assertIn("outside the project", out["error"])

    def test_caps_and_shapes(self):
        # Catches: unbounded rows, a multi-line command (rendered inline), a huge result.
        self.write_spec("x" * 20 + "\n")
        row = {"cite": f"{self.SPEC}:1"}
        cases = {"rows": [row] * 9, "command": [{**row, "command": "a\nb"}],
                 "result": [{**row, "result": "r" * 2001}], "blank": [{"cite": f"{self.SPEC}:2"}],
                 "missing": [{"cite": "docs/nope.md:1"}], "past end": [{"cite": f"{self.SPEC}:5"}],
                 "extra": [{**row, "url": "x"}], "empty": []}
        for name, ev in cases.items():
            with self.subTest(name=name):
                code, out = self.ask(ev)
                self.assertEqual(code, 400, (name, out))
        self.assertEqual(len(self.console.store.records()), 1)  # nothing got in

    def test_the_form_learns_whether_cited_lines_changed(self):
        # Catches: a form that shows the lines as asked and calls them current, and one that
        # calls a moved paragraph "changed" (the 0.5.0 excerpt rule: moving is not changing).
        self.write_spec("intro\nThe cap is 64 KiB.\nThe seats run in parallel.\nend\n")
        ev = [{"cite": f"{self.SPEC}:2"}, {"cite": f"{self.SPEC}:3"}, {"cite": f"{self.SPEC}:4"}]
        self.assertEqual(self.ask(ev)[0], 200)
        code, out = self.get("/api/evidence?qid=LANE.1/Q2")
        self.assertEqual([r["state"] for r in out["evidence"]], ["unchanged"] * 3)
        self.write_spec("new first line\nintro\nThe cap is 64 KiB.\nThe seats run one at a time.\nend\n")
        code, out = self.get("/api/evidence?qid=LANE.1/Q2")
        self.assertEqual(code, 200, out)
        got = {r["cite"]: r for r in out["evidence"]}
        self.assertEqual(got[f"{self.SPEC}:2"]["state"], "moved")
        self.assertEqual(got[f"{self.SPEC}:2"]["line"], 3)
        self.assertEqual(got[f"{self.SPEC}:3"]["state"], "changed")
        self.assertIn("one at a time", got[f"{self.SPEC}:3"]["diff"])
        self.assertEqual(got[f"{self.SPEC}:4"]["state"], "moved")
        (self.cfg.root / self.SPEC).unlink()
        _, out = self.get("/api/evidence?qid=LANE.1/Q2")
        self.assertEqual({r["state"] for r in out["evidence"]}, {"missing"})

    def test_a_question_without_evidence_has_none(self):
        # Old questions keep working: the form shows their text.
        code, out = self.get("/api/evidence?qid=LANE.1/Q1")
        self.assertEqual((code, out["evidence"]), (200, []))
        self.assertEqual(self.get("/api/evidence?qid=LANE.1/Q9")[0], 404)
        self.assertEqual(self.get("/api/evidence?qid=../x")[0], 400)
        self.assertEqual(self.req("GET", "/api/evidence?qid=LANE.1/Q1")[0], 403)


class ChatTests(_Live, unittest.TestCase):
    """The inbox's chat: an owner message on @chat that wakes a watching session."""

    def chat(self, text="is the build green?", nonce=None):
        body = {"item": "@chat", "text": text, "intent": "chat", "nonce": nonce or "chat" + os.urandom(6).hex()}
        return self.req("POST", "/api/message", body, tok=token())

    def test_a_chat_message_rings_the_doorbell_and_wakes_a_watch(self):
        # Catches: a chat stored but not rung (no session would ever wake), and a ring the
        # watch does not count as a wake line.
        from overture import doorbell as D
        code, out = self.chat()
        self.assertEqual(code, 200, out)
        bell = self.doorbell()
        self.assertEqual((bell[-1]["item"], bell[-1]["intent"]), ("@chat", "chat"))
        woke = D.watch(self.cfg.inbox, 0, poll=0.05, timeout=2)
        self.assertEqual([w["intent"] for w in woke], ["chat"])
        rc, printed, _ = self.agent_cli("watch", "--since", "0", "--timeout", "2", "--poll", "0.05")
        self.assertEqual(rc, 0)
        self.assertIn('"chat"', printed)

    def test_the_agent_replies_in_the_same_thread(self):
        # Catches: a chat reply refused because @chat is not a register item, and a chat that
        # stays "waiting on an agent" after the agent answered.
        code, out = self.chat()
        _, view = self.get("/api/view")
        self.assertTrue(view["view"]["chat"]["awaiting_agent"])
        rc, printed, err = self.agent_cli("reply", "@chat", "Yes, green at abc123.", "--reply-to", out["record"]["id"])
        self.assertEqual(rc, 0, err + printed)
        _, view = self.get("/api/view")
        self.assertFalse(view["view"]["chat"]["awaiting_agent"])
        self.assertEqual([m["by"] for m in view["view"]["threads"]["@chat"]], ["owner", "agent"])
        self.assertNotIn("@chat", view["view"]["awaiting_agent"])  # never counted as a register item

    def test_chat_is_only_the_owners_and_only_on_the_chat_thread(self):
        # Catches: an agent that could ring its own wake-ups, and "chat" smuggled onto an item.
        self.assertEqual(self.req("POST", "/api/message", {"item": "@chat", "text": "x", "nonce": "plainchat1"},
                                  tok=token())[0], 400)
        self.assertEqual(self.req("POST", "/api/message", {"item": "LANE.1", "text": "x", "intent": "chat",
                                                           "nonce": "itemchat01"}, tok=token())[0], 400)
        self.assertEqual(self.agent_post("/message", {"item": "@chat", "text": "x", "intent": "chat",
                                                 "nonce": "agentchat1"})[0], 400)
        self.assertEqual(self.agent_post("/question", self.seed_q(qid="@chat/Q1", item="@chat",
                                                                          nonce="chatquest1"))[0], 400)

    def test_chat_is_size_capped_and_rate_limited(self):
        # Catches: an unbounded chat (each message wakes an agent run), and a limiter that also
        # refuses a retry of a message already stored (the owner would think it was lost).
        self.assertEqual(self.chat("x" * 4001)[0], 400)
        for n in range(SV.CHAT_PER_MINUTE):
            self.assertEqual(self.chat(f"q{n}", nonce=f"chatrate{n:02d}")[0], 200)
        code, out = self.chat("one more")
        self.assertEqual(code, 429, out)
        self.assertIn("Nothing you typed was lost", out["error"])
        self.assertEqual(self.chat("q0", nonce="chatrate00")[0], 200)  # a retry of one already stored
        lines = [b for b in self.doorbell() if b.get("intent") == "chat"]
        self.assertEqual(len(lines), SV.CHAT_PER_MINUTE)


class LockAllTests(_Live, unittest.TestCase):
    """POST /api/lock-all: one press answers and locks a round's drafts, then sends one process request."""

    def setUp(self):
        _Live.setUp(self)
        self.f = self.fork()
        self.qs = [self.round_q(self.f["id"], n) for n in (2, 3, 4)]

    def lock_all(self, entries, nonce="lockallnonce1", origin=True):
        return self.req("POST", "/api/lock-all", {"fork": self.f["id"], "entries": entries, "nonce": nonce},
                        tok=token(), origin=origin)

    def entry(self, n, pick="fix", text=""):
        return {"qid": f"LANE.1/Q{n}", "picks": [pick], "own_text": text}

    def processes(self):
        return [b for b in self.doorbell() if b.get("intent") == "process"]

    def state(self, qid):
        return self.get("/api/view")[1]["view"]["questions"][qid]["state"]

    def test_lock_all_is_gated_and_needs_the_origin(self):
        # Catches: a batch write reachable without Access, or from another site's form.
        self.assertEqual(self.req("POST", "/api/lock-all", {}, tok=None)[0], 403)
        self.assertEqual(self.lock_all([self.entry(2)], origin=False)[0], 403)
        self.assertEqual(self.state("LANE.1/Q2"), "awaiting_you")

    def test_it_locks_each_drafted_answer_and_rings_process_once(self):
        # Catches: a batch that locks but sends no process request (the agent never learns),
        # one that sends a process request per question, and one that touches a left question.
        code, out = self.lock_all([self.entry(2, text="Fix it now, it is small."), self.entry(3, "record")])
        self.assertEqual(code, 200, out)
        self.assertEqual([r["status"] for r in out["results"]], ["locked", "locked"])
        self.assertEqual((self.state("LANE.1/Q2"), self.state("LANE.1/Q3"), self.state("LANE.1/Q4")),
                         ("locked", "locked", "awaiting_you"))
        self.assertEqual(len(self.processes()), 1)
        self.assertEqual(out["process"]["item"], "LANE.1")
        self.assertIn("LANE.1/Q2, LANE.1/Q3", out["process"]["text"])
        own = self.console.store.head("LANE.1/Q2")["own_text"]
        self.assertEqual(own, "Fix it now, it is small.")  # the comment is the answer's own words

    def test_an_unlocked_answer_the_draft_keeps_is_locked_not_answered_again(self):
        # Catches: a second, identical answer written on top of the owner's (noise in the record).
        self.req("POST", "/api/answer", {"qid": "LANE.1/Q2", "picks": ["leave"], "own_text": "", "nonce": "earlyans01"},
                 tok=token())
        code, out = self.lock_all([self.entry(2, "leave")])
        self.assertEqual(code, 200, out)
        self.assertEqual(len(self.console.store.answers("LANE.1/Q2")), 1)
        self.assertEqual(self.state("LANE.1/Q2"), "locked")

    def test_one_refusal_refuses_the_whole_batch_and_names_it(self):
        # Catches: a batch that locks the good ones and drops the bad one silently ("nothing
        # locked halfway"), and a refusal that does not say which question.
        seq = self.console.store.seq()
        bell = len(self.doorbell())
        code, out = self.lock_all([self.entry(2), self.entry(3, "no_such_option"), self.entry(4)])
        self.assertEqual(code, 409, out)
        self.assertIn("nothing was locked", out["error"])
        by = {r["qid"]: r for r in out["results"]}
        self.assertEqual(by["LANE.1/Q3"]["status"], "refused")
        self.assertIn("no_such_option", by["LANE.1/Q3"]["error"])
        self.assertEqual((by["LANE.1/Q2"]["status"], by["LANE.1/Q4"]["status"]), ("ready", "ready"))
        self.assertEqual((self.console.store.seq(), len(self.doorbell())), (seq, bell))

    def test_a_question_from_another_round_is_refused(self):
        # Catches: a form that could lock any question by qid (the seed is not in this round).
        code, out = self.lock_all([self.entry(2), {"qid": "LANE.1/Q1", "picks": ["a"], "own_text": ""}])
        self.assertEqual(code, 409, out)
        self.assertEqual(self.state("LANE.1/Q2"), "awaiting_you")

    def test_locked_since_drafting_is_refused_unless_it_is_the_same_answer(self):
        # Catches: a batch that silently supersedes a lock (D3 needs the owner's reason), and one
        # that refuses a question already locked exactly as drafted (so a retry could never finish).
        a = self.req("POST", "/api/answer", {**self.entry(2, "leave"), "nonce": "otherans01"}, tok=token())[1]["record"]
        self.req("POST", "/api/lock", {"qid": "LANE.1/Q2", "answer": a["id"], "nonce": "otherlock1"}, tok=token())
        code, out = self.lock_all([self.entry(2, "fix"), self.entry(3)])
        self.assertEqual(code, 409, out)
        self.assertIn("supersede", out["results"][0]["error"])
        code, out = self.lock_all([self.entry(2, "leave"), self.entry(3)])
        self.assertEqual(code, 200, out)
        self.assertEqual([r["status"] for r in out["results"]], ["already_locked", "locked"])

    def test_a_failure_part_way_is_reported_and_resumes(self):
        # Catches: a partial write reported as success or as a blanket failure (the owner could
        # not tell what landed), and a retry that double-locks, double-rings or gives up.
        real = self.console.store._write
        calls = []

        def flaky(rec):
            calls.append(rec["type"])
            if len(calls) == 3:  # Q2's answer and lock land; Q3's answer does not
                raise OSError(28, "No space left on device")
            real(rec)
        self.console.store._write = flaky
        body = [self.entry(2), self.entry(3), self.entry(4)]
        code, out = self.lock_all(body)
        self.assertEqual(code, 500, out)
        self.assertEqual([r["status"] for r in out["results"]], ["locked", "not_written", "not_written"])
        self.assertIn("No space left", out["results"][1]["error"])
        self.assertEqual(self.processes(), [])  # no process request for a batch that did not finish
        self.console.store._write = real
        code, out = self.lock_all(body)
        self.assertEqual(code, 200, out)
        self.assertEqual([r["status"] for r in out["results"]], ["already_locked", "locked", "locked"])
        self.assertEqual(len(self.processes()), 1)
        self.assertEqual(len(self.console.store.locks("LANE.1/Q2")), 1)

    def test_pressing_again_after_success_changes_nothing(self):
        # Catches: a double press that writes a second process request and wakes the agent twice.
        body = [self.entry(2), self.entry(3)]
        self.assertEqual(self.lock_all(body)[0], 200)
        seq = self.console.store.seq()
        code, out = self.lock_all(body)
        self.assertEqual(code, 200, out)
        self.assertEqual([r["status"] for r in out["results"]], ["already_locked", "already_locked"])
        self.assertEqual((self.console.store.seq(), len(self.processes())), (seq, 1))

    def test_bad_batches_are_refused_before_the_store_is_read(self):
        for body in ({}, {"fork": self.f["id"], "entries": [], "nonce": "lockallnonce1"},
                     {"fork": "x", "entries": [self.entry(2)], "nonce": "lockallnonce1"},
                     {"fork": self.f["id"], "entries": [self.entry(2), self.entry(2)], "nonce": "lockallnonce1"},
                     {"fork": self.f["id"], "entries": [{**self.entry(2), "extra": 1}], "nonce": "lockallnonce1"},
                     {"fork": self.f["id"], "entries": [self.entry(2)], "nonce": "n" * 49}):
            with self.subTest(body=body):
                self.assertEqual(self.req("POST", "/api/lock-all", body, tok=token())[0], 400)
        seed = next(r for r in self.console.store.records() if r["type"] == "question")
        code, _ = self.req("POST", "/api/lock-all", {"fork": seed["id"], "entries": [self.entry(2)],
                                                     "nonce": "lockallnonce1"}, tok=token())
        self.assertEqual(code, 400)  # the id of a question, not of a fork message


class EvidenceReadLimitTests(_Live, unittest.TestCase):
    """0.7.0 review LOWs: secrets files are refused by name when asked, and big files are never read."""

    ask = EvidenceTests.ask

    def test_secrets_files_are_refused_when_asked_and_near_misses_are_not(self):
        # Catches: a deny-list that is not applied at ask time (the form would show a key file's
        # lines to anyone reading the question), and one so broad an ordinary doc is refused.
        for rel in (".env", "secrets/.git/config", "certs/a.pem", "home/.ssh/id_ed25519", "docs/env.md"):
            p = self.cfg.root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("TOKEN=abc123 line\n")
        n = 10
        for rel, words in ((".env", "a .env file"), ("secrets/.git/config", ".git"), ("certs/a.pem", ".pem key"),
                           ("home/.ssh/id_ed25519", ".ssh")):
            with self.subTest(rel=rel):
                n += 1
                code, out = self.ask([{"cite": f"{rel}:1"}], n=n)
                self.assertEqual(code, 400, out)
                self.assertIn(words, out["error"])
                self.assertIn("never cited as evidence", out["error"])
                self.assertNotIn("abc123", json.dumps(out))
        code, out = self.ask([{"cite": "docs/env.md:1"}], n=n + 1)
        self.assertEqual(code, 200, out)
        code, out = self.agent_post("/question", self.seed_q(
            qid="LANE.1/Q40", nonce="evidq0040x",
            valid_if=[{"kind": "excerpt", "path": ".env", "text": "TOKEN=abc123"}]))
        self.assertEqual(code, 400, out)
        self.assertIn(".env", out["error"])

    def test_an_oversized_file_is_refused_unread(self):
        # Catches: reading the whole of a huge cited file and only truncating what is shown.
        from overture import anchors as A
        big = self.cfg.root / "logs" / "big.log"
        big.parent.mkdir(parents=True, exist_ok=True)
        with open(big, "wb") as fh:
            fh.write(b"first line\n")
            fh.truncate(A.MAX_READ + 1)
        code, out = self.ask([{"cite": "logs/big.log:1"}], n=50)
        self.assertEqual(code, 400, out)
        self.assertIn("is over 2 MiB, so it is not read", out["error"])


# -- 0.8.0: roar, refine/drill, tags, transcripts and visuals through the real server ------

# 1.29.0: visual sanitizer strips <script>; the old MOCK's script would fail the round-trip
# assertion. The prototype use-case is "a static mock an agent drew for the operator to
# look at" — scripts were never going to run inside the sandbox anyway (no allow-scripts).
MOCK = ("<!doctype html><html><head><style>h1{color:#123}</style></head><body><h1>Grid mock</h1>"
        '<p data-feature="mock">Draft layout, no interactivity.</p></body></html>')


class Phase4Tests(_Live, unittest.TestCase):
    CONFIG = {"specs_dir": "specs/", "visuals_dir": "visuals/", "next_step": {"refine": "refine", "drill": "drill"}}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.root = d
        (d / "page.html").write_text(PAGE)
        (d / "specs").mkdir()
        (d / "specs/owner-console.md").write_text("# Owner console\n\nThe console is a page.\n")
        os.utime(d / "specs/owner-console.md", (1_600_000_000, 1_600_000_000))
        (d / ".overture.json").write_text(json.dumps(self.CONFIG))
        self.cfg = SV.Config(root=d, page=d / "page.html", state=d / "state", adapter=d / "unused.py",
                             team_domain=TEAM, aud=AUD, hostname=HOSTNAME, port=0, project="test")
        self.console = SV.Console(self.cfg, FakeAdapter())
        self.console.seed()
        verify = SV.access_verifier(TEAM, AUD, key_for=lambda _t: KEY.public_key())
        self.owner = SV.owner_server(self.console, verify, 0)
        self.port = self.owner.server_address[1]
        threading.Thread(target=self.owner.serve_forever, daemon=True).start()
        self.agent = SV.agent_server(self.console)
        threading.Thread(target=self.agent.serve_forever, daemon=True).start()

    def raw(self, path, tok=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", path, headers={"Cf-Access-Jwt-Assertion": token()} if tok else {})
        r = conn.getresponse()
        body = r.read()
        headers = {k.lower(): v for k, v in r.getheaders()}
        conn.close()
        return r.status, headers, body

    def lock_seed(self):
        code, a = self.req("POST", "/api/answer", self.answer(), tok=token())
        self.assertEqual(code, 200, a)
        code, lk = self.req("POST", "/api/lock", {"qid": "LANE.1/Q1", "answer": a["record"]["id"],
                                                  "nonce": "locknonce01"}, tok=token())
        self.assertEqual(code, 200, lk)

    def visual_request(self):
        return self.owner_msg(item="LANE.1", intent="visual", text="The grid at phone width")

    def post_visual(self, req_id, content=MOCK, fmt="html", **over):
        body = {"request": req_id, "format": fmt, "title": "Grid at 375 px", "content": content,
                "text": "One column per zone; the session block collapses.", "nonce": "vis" + os.urandom(6).hex()}
        body.update(over)
        return self.agent_post("/visual", body)

    # -- roar and the other kinds of fork -------------------------------------------------

    def test_a_second_roar_is_refused_by_the_server_naming_the_first(self):
        # Catches: a once-per-question rule kept only on the page (a second tab, or a hand-made
        # POST, would run six more agents).
        self.lock_seed()
        first = self.owner_msg(item="LANE.1", intent="fork", mode="tighten", about_qid="LANE.1/Q1", roles=["roar"],
                               text="roar it")
        code, out = self.req("POST", "/api/message", {"item": "LANE.1", "intent": "fork", "mode": "tighten",
                                                      "about_qid": "LANE.1/Q1", "roles": ["roar"], "text": "again",
                                                      "nonce": "roaragain01"}, tok=token())
        self.assertEqual(code, 400)
        self.assertIn(first["id"], out["error"])
        self.assertIn("at most once per lock", out["error"])
        # The doorbell rang once, for the first roar only.
        self.assertEqual([b.get("intent") for b in self.doorbell() if b.get("intent") == "fork"], ["fork"])

    def test_a_transcript_is_stored_whole_or_refused_by_name(self):
        # Catches: an agent door that still caps bodies at 64 KiB (a real transcript could never
        # arrive) and a server that trims one over the cap instead of refusing it.
        self.lock_seed()
        r = self.owner_msg(item="LANE.1", intent="fork", mode="tighten", about_qid="LANE.1/Q1", roles=["roar"],
                           text="roar it")
        big = "é" * (SV.S.MAX_TRANSCRIPT // 2)       # 48 KiB of UTF-8, ~290 KiB as escaped JSON
        self.assertEqual(len(big.encode()), SV.S.MAX_TRANSCRIPT)
        code, out = self.agent_post("/transcript", {"fork": r["id"], "text": big + "é", "nonce": "transcript01"})
        self.assertEqual(code, 400)
        self.assertIn("not truncated", out["error"])
        code, out = self.agent_post("/transcript", {"fork": r["id"], "text": big, "nonce": "transcript02"})
        self.assertEqual(code, 200, out)
        view = self.get("/api/view")[1]["view"]
        self.assertEqual(view["transcripts"][r["id"]]["text"], big)
        self.assertEqual(view["forks"][r["id"]]["transcript"], out["record"]["id"])

    def test_refine_and_drill_are_owner_forks_after_a_lock(self):
        # Catches: a refine requested before the answer is locked ("neither writes before a lock").
        code, out = self.req("POST", "/api/message", {"item": "LANE.1", "intent": "fork", "mode": "tighten",
                                                      "step": "refine", "about_qid": "LANE.1/Q1",
                                                      "text": "refine", "nonce": "refine00001"}, tok=token())
        self.assertEqual(code, 400)
        self.assertIn("no locked answer", out["error"])
        self.lock_seed()
        rec = self.owner_msg(item="LANE.1", intent="fork", mode="tighten", step="refine", about_qid="LANE.1/Q1",
                             text="refine")
        self.assertEqual(rec["step"], "refine")
        self.assertEqual(self.doorbell()[-1]["intent"], "fork")

    # -- the tags ride on /api/view ----------------------------------------------------------

    def test_the_view_carries_tags_and_the_project_dirs(self):
        # Catches: tags computed only in a test (never served), or config that never reaches the page.
        self.lock_seed()          # the seed cites architect/40-specs/..., outside this specs_dir: no refine
        view = self.get("/api/view")[1]["view"]
        self.assertEqual(view["config"], {"specs_dir": "specs", "visuals_dir": "visuals", "sections": {}})
        self.assertIn("questions", view["tags"])
        # The seed picked "a" against the ★ "b": deliberate, with its reason.
        [t] = view["tags"]["questions"]["LANE.1/Q1"]
        self.assertEqual(t["step"], "deliberate")
        self.assertIn("went against the ★", t["reason"])

    def test_a_bad_project_config_stops_the_server_by_name(self):
        (self.root / ".overture.json").write_text(json.dumps({"visuals_dir": "../out"}))
        from overture import projectcfg as PC
        with self.assertRaisesRegex(PC.ConfigError, "visuals_dir"):
            SV.Console(self.cfg, FakeAdapter())

    # -- visuals -----------------------------------------------------------------------------

    def test_a_visual_is_stored_in_state_and_served_sandboxed(self):
        # Catches: a mock served as the console's own HTML (its script would run as the owner),
        # a policy that lets it load anything, a route that skips the gate, and (0.8.1, (d)) a
        # visual written into the project instead of the console's state, or an INDEX.md
        # regenerated in the project.
        req = self.visual_request()
        self.assertEqual(self.doorbell()[-1]["intent"], "visual")
        code, out = self.post_visual(req["id"])
        self.assertEqual(code, 200, out)
        rec = out["record"]
        self.assertTrue(rec["path"].startswith("visuals/LANE.1/") and rec["path"].endswith(".html"))
        self.assertIn("visual-export", out["land"])
        self.assertEqual((self.cfg.state / rec["path"]).read_text(), MOCK)
        doc = (self.cfg.state / rec["doc_path"]).read_text()
        self.assertIn("The grid at phone width", doc)
        self.assertIn(rec["path"].rsplit("/", 1)[1], doc)
        self.assertFalse((self.root / "visuals").exists())          # visuals_dir is a destination only
        code, h, body = self.raw(f"/api/visual?id={rec['id']}")
        self.assertEqual((code, body.decode()), (200, MOCK))
        self.assertTrue(h["content-type"].startswith("text/html"))
        csp = h["content-security-policy"]
        for part in ("sandbox;", "default-src 'none'", "frame-ancestors 'self'", "form-action 'none'"):
            self.assertIn(part, csp)
        self.assertNotIn("allow-scripts", csp)
        self.assertNotIn("allow-same-origin", csp)
        self.assertEqual(h["x-content-type-options"], "nosniff")
        self.assertEqual(self.raw(f"/api/visual?id={rec['id']}", tok=False)[0], 403)
        view = self.get("/api/view")[1]["view"]
        self.assertEqual(view["visuals"]["LANE.1"][0]["id"], rec["id"])
        self.assertNotIn("LANE.1", view["awaiting_agent"])     # the visual answered the request

    def test_a_mermaid_visual_is_served_as_plain_text(self):
        req = self.visual_request()
        code, out = self.post_visual(req["id"], content="graph TD\n  A-->B\n", fmt="mermaid")
        self.assertEqual(code, 200, out)
        self.assertTrue(out["record"]["path"].endswith(".mmd"))
        code, h, body = self.raw(f"/api/visual?id={out['record']['id']}")
        self.assertEqual((code, body), (200, b"graph TD\n  A-->B\n"))
        self.assertTrue(h["content-type"].startswith("text/plain"))
        self.assertIn("sandbox", h["content-security-policy"])

    def test_a_refused_visual_writes_no_file(self):
        # Catches: files written before the store's rules run (a refused visual would still land in
        # the project), and an oversized visual cut to fit.
        note = self.owner_msg(item="LANE.1", text="just a note")
        code, out = self.post_visual(note["id"])
        self.assertEqual(code, 400)
        self.assertIn("not an owner visual request", out["error"])
        req = self.visual_request()
        code, out = self.post_visual(req["id"], title="two\nlines")
        self.assertEqual(code, 400)
        self.assertIn("one line", out["error"])
        code, out = self.post_visual(req["id"], content="x" * (SV.S.MAX_VISUAL + 1))
        self.assertEqual(code, 413)
        self.assertIn("refused, not cut", out["error"])
        self.assertFalse((self.root / "visuals").exists())
        self.assertFalse((self.cfg.state / "visuals").exists())

    def test_a_changed_or_escaped_file_is_not_served(self):
        # Catches (d): serving whatever is at the path now (an edit, or a symlink swapped in that
        # points outside the state, would reach the owner's page as the agent's visual), and a
        # server that falls back to reading the project checkout.
        req = self.visual_request()
        rec = self.post_visual(req["id"])[1]["record"]
        p = self.cfg.state / rec["path"]
        in_checkout = self.root / rec["path"]                  # where 0.8.0 would have put it
        in_checkout.parent.mkdir(parents=True)
        in_checkout.write_text(MOCK)
        p.rename(p.with_suffix(".away"))
        code, out = self.get(f"/api/visual?id={rec['id']}")
        self.assertEqual(code, 409)
        self.assertIn("not in the console's state", out["error"])
        p.with_suffix(".away").rename(p)
        p.write_text(MOCK.replace("Grid", "Evil"))
        code, out = self.get(f"/api/visual?id={rec['id']}")
        self.assertEqual(code, 409)
        self.assertIn("changed since", out["error"])
        outside = Path(self.tmp.name + "-outside.html")
        outside.write_text(MOCK)
        self.addCleanup(outside.unlink)
        p.unlink()
        p.symlink_to(outside)
        code, out = self.get(f"/api/visual?id={rec['id']}")
        self.assertEqual(code, 409)
        self.assertIn("symlink", out["error"])
        self.assertEqual(self.get("/api/visual?id=" + "0" * 24)[0], 404)
        self.assertEqual(self.get("/api/visual?id=../../etc/passwd")[0], 400)

    def test_without_visuals_dir_a_visual_is_stored_and_shown_but_not_exported(self):
        # Catches: a visuals_dir still required to store (0.8.0), and an export with no destination.
        (self.root / ".overture.json").write_text(json.dumps({}))
        self.console.project = __import__("overture.projectcfg", fromlist=["load"]).load(self.root)
        req = self.visual_request()
        code, out = self.post_visual(req["id"])
        self.assertEqual(code, 200, out)
        self.assertIn("no visuals_dir", out["land"])
        self.assertEqual(self.raw(f"/api/visual?id={out['record']['id']}")[2].decode(), MOCK)
        code, out = self.agent_post("/visual-export", {"ids": []})
        self.assertEqual(code, 400)
        self.assertIn("no visuals_dir", out["error"])

    def test_the_agent_cli_posts_a_visual_and_a_transcript(self):
        # Catches: subcommands that exist in --help but send the wrong shape.
        import subprocess
        req = self.visual_request()
        work = self.root / "work"
        work.mkdir()
        (work / "v.mmd").write_text("graph LR\n  A-->B\n")
        (work / "v.md").write_text("A to B.")
        agent_py = str(Path(SV.__file__).resolve().parent.parent / "agent.py")
        r = subprocess.run([sys.executable, agent_py, "--state", str(self.cfg.state), "visual", req["id"],
                            "--format", "mermaid", "--file", str(work / "v.mmd"), "--doc", str(work / "v.md"),
                            "--title", "A to B"], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(json.loads(r.stdout)["record"]["format"], "mermaid")  # compact off a terminal (K1, E3)
        self.lock_seed()
        f = self.owner_msg(item="LANE.1", intent="fork", mode="tighten", about_qid="LANE.1/Q1", roles=["roar"],
                           text="roar")
        (work / "t.md").write_text("# Round 1\n...\n")
        r = subprocess.run([sys.executable, agent_py, "--state", str(self.cfg.state), "transcript", f["id"],
                            str(work / "t.md")], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


# -- 0.8.1: the server never writes into the project's working tree ------------------------

GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.com", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
AGENT_PY = str(Path(SV.__file__).resolve().parent.parent / "agent.py")


def git(cwd, *args, check=True):
    import subprocess
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=GIT_ENV, timeout=60)
    if check and r.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {r.stdout}{r.stderr}")
    return r


def snapshot(top: Path) -> dict:
    """Every file and folder of a working tree, tracked or not, outside .git: path -> (kind, mode, bytes)."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(top):
        dirnames[:] = sorted(d for d in dirnames if not (Path(dirpath) == top and d == ".git"))
        for name in dirnames + sorted(filenames):
            p = Path(dirpath) / name
            st = p.lstat()
            rel = p.relative_to(top).as_posix()
            if stat.S_ISLNK(st.st_mode):
                out[rel] = ("link", os.readlink(p))
            elif stat.S_ISDIR(st.st_mode):
                out[rel] = ("dir", stat.S_IMODE(st.st_mode))
            else:
                out[rel] = ("file", stat.S_IMODE(st.st_mode), p.read_bytes())
    return out


class VisualLandingTests(_Live, unittest.TestCase):
    """A service checkout that follows main, a console running from it, and an agent's own clone.

    This is the defect's own shape: 0.8.0 wrote each visual and INDEX.md into
    the service checkout, the agent landed the same paths by PR, and the
    checkout's `git merge --ff-only` was then refused over untracked files.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name).resolve()
        self.base = base
        git(base, "init", "-q", "--bare", "-b", "main", "origin.git")
        seedwt = base / "seed"
        git(base, "clone", "-q", str(base / "origin.git"), "seed")
        git(seedwt, "checkout", "-q", "-b", "main")
        (seedwt / "page.html").write_text(PAGE)
        (seedwt / ".overture.json").write_text(json.dumps({"visuals_dir": "architect/visuals/"}))
        (seedwt / "architect").mkdir()
        (seedwt / "architect/README.md").write_text("# architect\n")
        git(seedwt, "add", "-A")
        git(seedwt, "commit", "-qm", "seed")
        git(seedwt, "push", "-q", "origin", "main")
        self.svc = base / "svc"                       # the live service checkout the console runs from
        git(base, "clone", "-q", "-b", "main", str(base / "origin.git"), "svc")
        self.work = base / "agent"                    # the agent's own clone, on a branch
        git(base, "clone", "-q", "-b", "main", str(base / "origin.git"), "agent")
        git(self.work, "checkout", "-q", "-b", "visuals")
        self.cfg = SV.Config(root=self.svc, page=self.svc / "page.html", state=base / "st", adapter=base / "x.py",
                             team_domain=TEAM, aud=AUD, hostname=HOSTNAME, port=0, project="test")
        self.console = SV.Console(self.cfg, FakeAdapter())
        self.console.seed()
        verify = SV.access_verifier(TEAM, AUD, key_for=lambda _t: KEY.public_key())
        self.owner = SV.owner_server(self.console, verify, 0)
        self.port = self.owner.server_address[1]
        threading.Thread(target=self.owner.serve_forever, daemon=True).start()
        self.agent = SV.agent_server(self.console)
        threading.Thread(target=self.agent.serve_forever, daemon=True).start()

    def cli(self, *args):
        import subprocess
        return subprocess.run([sys.executable, AGENT_PY, "--state", str(self.cfg.state), *args],
                              capture_output=True, text=True, timeout=60, env=GIT_ENV)

    def draw(self, fmt="mermaid", content="graph TD\n  A-->B\n", title="A to B"):
        """The whole round trip as an agent runs it: the owner's request, then `agent.py visual`."""
        req = self.owner_msg(item="LANE.1", intent="visual", text="Draw the flow")
        work = self.base / "scratch"
        work.mkdir(exist_ok=True)
        f = work / ("v" + os.urandom(3).hex() + (".mmd" if fmt == "mermaid" else ".html"))
        f.write_text(content)
        (work / "doc.md").write_text("A flows to B.")
        r = self.cli("visual", req["id"], "--format", fmt, "--file", str(f), "--doc", str(work / "doc.md"),
                     "--title", title)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return json.loads(r.stdout)["record"]

    def test_a_the_service_checkout_is_byte_for_byte_unchanged(self):
        # (a) Catches: any write into the project's working tree: the visual, its doc, INDEX.md, a
        # folder, or a tidy-up that deletes something (v0.8.0 fails: it wrote all three there).
        before = snapshot(self.svc)
        self.assertIn(".overture.json", before)
        rec = self.draw()
        self.draw(fmt="html", content=MOCK, title="Mock")
        self.get("/api/view")                              # a page view too (tags, evidence reads)
        self.assertEqual(self.raw_visual(rec["id"]), b"graph TD\n  A-->B\n")
        self.assertEqual(snapshot(self.svc), before)
        self.assertEqual(git(self.svc, "status", "--porcelain", "--untracked-files=all").stdout, "")

    def raw_visual(self, rid):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", f"/api/visual?id={rid}", headers={"Cf-Access-Jwt-Assertion": token()})
        r = conn.getresponse()
        body = r.read()
        conn.close()
        self.assertEqual(r.status, 200, body)
        return body

    def test_b_export_writes_into_the_agents_worktree_and_refuses_the_rest(self):
        # (b) Catches: an export that lands in the server's checkout, one that overwrites a file it
        # did not write, one that is not idempotent, and one that writes outside a git work tree.
        rec = self.draw()
        name = rec["path"].rsplit("/", 1)[1]
        svc_before = snapshot(self.svc)
        r = self.cli("visual-export", "--project", str(self.svc))
        self.assertEqual(r.returncode, 1)
        self.assertIn("the directory the console server runs from", r.stderr)
        r = self.cli("visual-export", "--project", str(self.svc / "architect"))   # a folder inside it
        self.assertEqual(r.returncode, 1)
        self.assertIn("inside the work tree", r.stderr)
        self.assertEqual(snapshot(self.svc), svc_before)
        plain = self.base / "not-git"
        plain.mkdir()
        r = self.cli("visual-export", "--project", str(plain))
        self.assertEqual(r.returncode, 1)
        self.assertIn("not a git work tree", r.stderr)
        self.assertEqual(list(plain.iterdir()), [])

        r = self.cli("visual-export", "--project", str(self.work))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = json.loads(r.stdout)
        dest = self.work / "architect/visuals"
        self.assertEqual((dest / "LANE.1" / name).read_text(), "graph TD\n  A-->B\n")
        self.assertIn("Draw the flow", (dest / "LANE.1" / (name[:-4] + ".md")).read_text())
        index = (dest / "INDEX.md").read_text()
        self.assertIn(f"[visual](LANE.1/{name})", index)
        self.assertIn("A to B", index)
        self.assertEqual(len(out["written"]), 3)
        again = self.cli("visual-export", "--project", str(self.work))              # idempotent
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertEqual(json.loads(again.stdout)["written"], [])
        self.assertEqual(len(json.loads(again.stdout)["unchanged"]), 3)
        second = self.draw(title="Second")
        r = self.cli("visual-export", "--project", str(self.work), "--visual", second["id"])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Second", (dest / "INDEX.md").read_text())
        self.assertIn("A to B", (dest / "INDEX.md").read_text())       # the index covers what is in DIR
        (dest / "LANE.1" / name).write_text("graph TD\n  mine\n")
        r = self.cli("visual-export", "--project", str(self.work))
        self.assertEqual(r.returncode, 1)
        self.assertIn(f"architect/visuals/LANE.1/{name} is already there with other content", r.stderr)
        self.assertEqual((dest / "LANE.1" / name).read_text(), "graph TD\n  mine\n")
        self.assertEqual(snapshot(self.svc), svc_before)

    def test_c_the_service_checkout_fast_forwards_over_the_landed_visuals(self):
        # (c) The defect reproduced: the console stores visuals while it runs, an agent lands the same
        # paths by PR, and the checkout follows main with `git merge --ff-only` as a deploy does.
        # v0.8.0 fails here: the untracked files it wrote block the merge ("would be overwritten").
        # Round 1 lands the paths by hand, exactly as the 0.8.0 skill told an agent to (the visual,
        # its doc and INDEX.md under visuals_dir), so this needs no 0.8.1 command to reproduce.
        recs = [self.draw(), self.draw(fmt="html", content=MOCK, title="Mock")]
        dest = self.work / "architect/visuals"
        for rec in recs:
            name = rec["path"].rsplit("/", 1)[1]
            (dest / rec["item"]).mkdir(parents=True, exist_ok=True)
            (dest / rec["item"] / name).write_bytes(self.raw_visual(rec["id"]))
            (dest / rec["item"] / (name.rsplit(".", 1)[0] + ".md")).write_text("# doc\n")
        (dest / "INDEX.md").write_text("# Visuals\n")
        git(self.work, "add", "-A")
        git(self.work, "commit", "-qm", "visuals")
        git(self.work, "push", "-q", "origin", "visuals:main")        # the PR, merged
        git(self.svc, "fetch", "-q", "origin")
        r = git(self.svc, "merge", "--ff-only", "origin/main", check=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((self.svc / "architect/visuals/INDEX.md").is_file())
        # Round 2 through `agent.py visual-export`, after another visual: the export overwrites
        # nothing it did not generate, so it is told apart from the hand-landed files...
        self.draw(title="Third")
        r = self.cli("visual-export", "--project", str(self.work))
        self.assertEqual(r.returncode, 1)
        self.assertIn("is already there with other content", r.stderr)      # the hand-written docs
        for rec in recs:
            git(self.work, "rm", "-q", rec["doc_path"].replace("visuals/", "architect/visuals/", 1))
        git(self.work, "commit", "-qm", "hand-landed docs out")
        r = self.cli("visual-export", "--project", str(self.work))
        self.assertEqual(r.returncode, 1)
        self.assertIn("INDEX.md was not written by the kit", r.stderr)       # ...and the hand-written index
        git(self.work, "rm", "-q", "architect/visuals/INDEX.md")
        git(self.work, "commit", "-qm", "hand-landed index out")
        r = self.cli("visual-export", "--project", str(self.work))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        git(self.work, "add", "-A")
        git(self.work, "commit", "-qm", "more")
        git(self.work, "push", "-q", "origin", "visuals:main")
        git(self.svc, "fetch", "-q", "origin")
        r = git(self.svc, "merge", "--ff-only", "origin/main", check=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(git(self.svc, "status", "--porcelain", "--untracked-files=all").stdout, "")

    def test_d_a_partial_export_exits_3_and_a_refused_one_exits_1(self):
        # 0.8.2 (4a). Catches: an export that wrote files but exits 1, the code for "nothing was
        # written", so a caller cannot tell a partial landing from a refusal (v0.8.1 exits 1 here).
        good, bad = self.draw(title="Good"), self.draw(title="Bad")
        (self.cfg.state / bad["path"]).write_text("graph TD\n  tampered\n")    # its hash no longer matches
        r = self.cli("visual-export", "--project", str(self.work))
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn(f"not exported: visual {bad['id']}", r.stderr)
        out = json.loads(r.stdout)
        name = good["path"].rsplit("/", 1)[1]
        self.assertIn(f"architect/visuals/LANE.1/{name}", out["written"])
        self.assertEqual([x["id"] for x in out["refused"]], [bad["id"]])
        # Every chosen visual refused: nothing is written at all, not even INDEX.md, and it exits 1.
        other = self.base / "agent2"
        git(self.base, "clone", "-q", "-b", "main", str(self.base / "origin.git"), "agent2")
        before = snapshot(other)
        r = self.cli("visual-export", "--project", str(other), "--visual", bad["id"])
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("nothing was written", r.stderr)
        self.assertEqual(snapshot(other), before)
        # The codes are documented where a caller looks.
        self.assertIn("exits 3", self.cli("visual-export", "--help").stdout)


# -- 0.8.2: agent names on records, and each agent's own working marks ----------------------


class AgentNameTests(_Live, unittest.TestCase):
    """Several agent sessions share one console: each may say who it is (`agent.py --as NAME`)."""

    def cli(self, *args, env=None):
        """agent.py in-process with a controlled environment (OVERTURE_AGENT unset unless given)."""
        from unittest import mock
        clean = {k: v for k, v in os.environ.items() if k != "OVERTURE_AGENT"}
        with mock.patch.dict(os.environ, {**clean, **(env or {})}, clear=True):
            return self.agent_cli(*args)

    def view(self):
        code, body = self.get("/api/view")
        self.assertEqual(code, 200, body)
        return body

    def stored_lines(self):
        return [json.loads(x) for x in self.cfg.store.read_text().splitlines()]

    def test_a_named_agents_records_carry_its_name_in_the_view_and_the_feed(self):
        # Catches: a name that never leaves agent.py, one stored but not shown, and one shown on
        # the reply but not on the question or the visual (v0.8.1 has no --as at all).
        from test_kit import question
        qfile = self.cfg.root / "q.json"
        qfile.write_text(json.dumps({k: v for k, v in question(qid="LANE.1/Q2", item="LANE.1").items()
                                     if k not in ("type", "schemaVersion", "by", "nonce")}))
        rc, out, err = self.cli("--as", "agent-6", "ask", str(qfile))
        self.assertEqual(rc, 0, out + err)
        rc, out, err = self.cli("--as", "agent-6", "reply", "LANE.1", "On it.")
        self.assertEqual(rc, 0, out + err)
        reply = json.loads(out)["record"]
        rc, out, err = self.cli("reply", "LANE.1", "From a session with a name in its env.",
                                env={"OVERTURE_AGENT": "agent-7"})
        self.assertEqual(rc, 0, out + err)
        from_env = json.loads(out)["record"]
        rc, out, err = self.cli("--as", "agent-8", "reply", "LANE", "--as beats the env.",
                                env={"OVERTURE_AGENT": "agent-7"})
        self.assertEqual(rc, 0, out + err)
        beats = json.loads(out)["record"]
        req = self.owner_msg(item="LANE.1", intent="visual", text="Draw it")
        (self.cfg.root / "v.mmd").write_text("graph TD\n  A-->B\n")
        (self.cfg.root / "v.md").write_text("A to B.")
        rc, out, err = self.cli("--as", "agent-6", "visual", req["id"], "--format", "mermaid", "--file",
                                str(self.cfg.root / "v.mmd"), "--doc", str(self.cfg.root / "v.md"), "--title", "A to B")
        self.assertEqual(rc, 0, out + err)
        view = self.view()["view"]
        self.assertEqual(view["questions"]["LANE.1/Q2"]["question"]["agent"], "agent-6")
        by_id = {m["id"]: m for m in view["threads"]["LANE.1"] + view["threads"]["LANE"]}
        self.assertEqual(by_id[reply["id"]]["agent"], "agent-6")
        self.assertEqual(by_id[from_env["id"]]["agent"], "agent-7")
        self.assertEqual(by_id[beats["id"]]["agent"], "agent-8")
        self.assertEqual(view["visuals"]["LANE.1"][0]["agent"], "agent-6")
        self.assertEqual(by_id[reply["id"]]["by"], "agent")          # the writer is still the agent door
        code, feed = self.get("/api/feed?limit=20")
        self.assertEqual(code, 200, feed)
        named = {e["id"]: e.get("agent") for e in feed["events"]}
        self.assertEqual(named[reply["id"]], "agent-6")
        self.assertIsNone(named[req["id"]])                           # the owner's request names no agent

    def test_an_invalid_name_is_refused_by_name_and_writes_nothing(self):
        # Catches: a name taken as given (markup, spaces, a newline on the owner's page or in the
        # rulings record), a name that impersonates the owner, and a refusal that still writes.
        before = self.console.store.seq()
        for bad in ("Agent 6", "agent_6", "a" * 33, "agent-6\n", "-agent", "agent--6", "owner", "agent", ""):
            with self.subTest(name=bad):
                rc, out, err = self.cli("--as=" + bad, "reply", "LANE.1", "hello")   # = so "-agent" is a value
                self.assertEqual(rc, 2, out + err)
                self.assertIn(repr(bad), err)
                if bad:                                    # an empty variable is an unset one
                    rc, out, err = self.cli("reply", "LANE.1", "hello", env={"OVERTURE_AGENT": bad})
                    self.assertEqual(rc, 2, out + err)
                    self.assertIn("OVERTURE_AGENT", err)
        # The server refuses it too, whoever calls the door.
        code, out = SV.agent_request(self.cfg.socket, "POST", "/message",
                                     {"item": "LANE.1", "text": "hi", "nonce": "badname001"}, agent="Bad Name")
        self.assertEqual(code, 400)
        self.assertIn("'Bad Name'", out["error"])
        self.assertEqual(self.console.store.seq(), before)
        self.assertFalse((self.cfg.state / "names.jsonl").exists())

    def test_a_sync_by_one_agent_leaves_another_agents_working_marks(self):
        # Catches: `synced` deleting working.json whole, so agent-5 finishing its request wipes the
        # "agent active" marks agent-6 still holds (v0.8.1 does exactly that).
        self.assertEqual(self.cli("--as", "agent-5", "working", "LANE")[0], 0)
        self.assertEqual(self.cli("--as", "agent-6", "working", "LANE.1")[0], 0)
        self.assertEqual(self.cli("working", "LANE")[0], 0)                   # an unnamed session
        cur = self.view()["cursor"]
        self.assertEqual(sorted(cur["working"]), ["LANE", "LANE.1"])
        self.assertEqual({k: sorted(v) for k, v in cur["working_by"].items()},
                         {"agent": ["LANE"], "agent-5": ["LANE"], "agent-6": ["LANE.1"]})
        self.assertEqual(self.cli("--as", "agent-5", "synced")[0], 0)
        cur = self.view()["cursor"]
        self.assertEqual({k: sorted(v) for k, v in cur["working_by"].items()},
                         {"agent": ["LANE"], "agent-6": ["LANE.1"]})
        self.assertEqual(sorted(cur["working"]), ["LANE", "LANE.1"])
        self.assertEqual(self.cli("synced")[0], 0)                            # the unnamed bucket only
        cur = self.view()["cursor"]
        self.assertEqual({k: sorted(v) for k, v in cur["working_by"].items()}, {"agent-6": ["LANE.1"]})
        self.assertEqual(self.cli("--as", "agent-6", "synced")[0], 0)
        self.assertEqual(self.view()["cursor"]["working"], {})

    def test_a_0_8_1_working_file_is_read_as_the_unnamed_bucket(self):
        # Catches: an upgrade that drops the marks a 0.8.1 server wrote (a flat item -> time map).
        now = time.strftime(SV.TS_FORMAT, time.gmtime())
        self.cfg.working.write_text(json.dumps({"LANE.1": now}))
        cur = self.view()["cursor"]
        self.assertEqual(cur["working"], {"LANE.1": now})
        self.assertEqual(cur["working_by"], {"agent": {"LANE.1": now}})
        self.assertEqual(self.cli("--as", "agent-6", "synced")[0], 0)         # not agent-6's mark
        self.assertEqual(self.view()["cursor"]["working"], {"LANE.1": now})

    def test_an_unnamed_record_is_stored_and_rendered_exactly_as_before(self):
        # Catches: a name written into store.jsonl (a 0.8.1 kit refuses the WHOLE store on an
        # unknown field, so a rollback would not start), and an unnamed record that gains a key.
        from overture import fold as F
        from overture import schema as S
        from overture.store import Store
        rc, out, _ = self.cli("reply", "LANE.1", "No name.")
        plain = json.loads(out)["record"]
        rc, out, _ = self.cli("--as", "agent-6", "reply", "LANE.1", "Named.")
        named = json.loads(out)["record"]
        for rec in self.stored_lines():
            self.assertNotIn("agent", rec)
            self.assertEqual(S.validate(rec), [], rec)          # what 0.8.1's Store._load runs, line by line
        self.assertEqual(Store(self.cfg.store).seq(), len(self.stored_lines()))
        self.assertEqual((self.cfg.state / "names.jsonl").read_text(),
                         json.dumps({"agent": "agent-6", "id": named["id"]}, separators=(",", ":")) + "\n")
        view = self.view()["view"]
        by_id = {m["id"]: m for m in view["threads"]["LANE.1"]}
        self.assertEqual(by_id[plain["id"]], self.console.store.get(plain["id"]))   # not a key more
        self.assertEqual(by_id[named["id"]], {**self.console.store.get(named["id"]), "agent": "agent-6"})
        self.assertEqual(view["questions"]["LANE.1/Q1"]["question"], self.console.store.get(
            view["questions"]["LANE.1/Q1"]["question"]["id"]))
        events = {e["id"]: e for e in self.get("/api/feed?limit=20")[1]["events"]}
        self.assertNotIn("agent", events[plain["id"]])

        # fold export: a question an agent asked by name says so; the seed (no name) is byte-identical.
        from test_kit import question
        q = {k: v for k, v in question(qid="LANE.1/Q2", item="LANE.1").items()
             if k not in ("type", "schemaVersion", "by", "nonce")}
        code, _ = SV.agent_request(self.cfg.socket, "POST", "/question", {**q, "nonce": "namedq0001"},
                                   agent="agent-6")
        self.assertEqual(code, 200)
        for qid in ("LANE.1/Q1", "LANE.1/Q2"):
            code, a = self.req("POST", "/api/answer", self.answer(qid=qid, nonce="ans" + qid[-1] * 9), tok=token())
            self.assertEqual(code, 200, a)
            code, lk = self.req("POST", "/api/lock", {"qid": qid, "answer": a["record"]["id"],
                                                      "nonce": "lck" + qid[-1] * 9}, tok=token())
            self.assertEqual(code, 200, lk)
        with_names = F.export(Store(self.cfg.store), names=F.names_beside(self.cfg.store))
        without = F.export(Store(self.cfg.store))
        self.assertEqual(with_names["LANE.1__Q1.json"], without["LANE.1__Q1.json"])
        self.assertNotIn("asked_by_agent", with_names["LANE.1__Q1.json"])
        self.assertEqual(with_names["LANE.1__Q2.json"]["asked_by_agent"], "agent-6")
        self.assertEqual(with_names["LANE.1__Q2.json"]["asked_by"], "agent")
        self.assertEqual(F.check_entry("LANE.1__Q2.json", with_names["LANE.1__Q2.json"], ITEMS), [])
        bad = {**with_names["LANE.1__Q2.json"], "asked_by_agent": "Agent <b>6</b>"}
        self.assertTrue(any("asked_by_agent" in e for e in F.check_entry("LANE.1__Q2.json", bad, ITEMS)))
        # The CLI export reads the names file beside the store it is given.
        import contextlib
        import io
        outdir = self.cfg.root / "locked"
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            rc = F.main(["--root", str(self.cfg.root), "export", "--store", str(self.cfg.store), "--out", "locked"])
        self.assertEqual(rc, 0, printed.getvalue())
        self.assertEqual(json.loads((outdir / "LANE.1__Q2.json").read_text())["asked_by_agent"], "agent-6")


    def test_one_agent_keeps_at_most_32_marks_its_newest(self):
        # 0.8.2 review follow-up (server.py `set_working`). Catches: a cap that is never applied, one
        # that keeps the OLDEST marks (the items this agent left long ago), and one that cuts
        # another agent's marks to make room.
        self.assertEqual(self.cli("--as", "agent-5", "working", "LANE")[0], 0)
        items = [f"ITEM.{n:02d}" for n in range(40)]
        for i in items:
            rc, out, err = self.cli("--as", "agent-6", "working", i)
            self.assertEqual(rc, 0, out + err)
        by = self.view()["cursor"]["working_by"]
        self.assertEqual(len(by["agent-6"]), SV.MAX_WORKING)
        self.assertEqual(SV.MAX_WORKING, 32)
        self.assertEqual(sorted(by["agent-6"]), items[-32:])
        self.assertEqual(list(by["agent-5"]), ["LANE"])


# -- 0.8.3: the steward -------------------------------------------------------------------


class StewardDoorTests(_Live, unittest.TestCase):
    """Under a steward, every other session still asks, replies and marks work through the door."""

    def setUp(self):
        _Live.setUp(self)
        from unittest import mock
        from overture import registry as R
        cfg = Path(self.tmp.name) / "cfg"
        self._env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(cfg)})
        self._env.start()
        project = Path(self.tmp.name) / "project"
        project.mkdir()
        R.register(project, self.cfg.state, KIT)
        R.set_steward(self.cfg.state, "agent-5")

    def tearDown(self):
        self._env.stop()
        _Live.tearDown(self)

    def test_ask_reply_and_working_stay_open_to_every_session(self):
        from test_kit import question
        qfile = self.cfg.root / "q.json"
        qfile.write_text(json.dumps({k: v for k, v in question(qid="LANE.1/Q2", item="LANE.1").items()
                                     if k not in ("type", "schemaVersion", "by", "nonce")}))
        for who in (["--as", "agent-6"], []):
            with self.subTest(who=who):
                rc, out, err = ServerTests.agent_cli(self, *who, "reply", "LANE.1", "Posted, not asked live.")
                self.assertEqual(rc, 0, out + err)
                rc, out, err = ServerTests.agent_cli(self, *who, "working", "LANE.1")
                self.assertEqual(rc, 0, out + err)
        rc, out, err = ServerTests.agent_cli(self, "--as", "agent-6", "ask", str(qfile))
        self.assertEqual(rc, 0, out + err)
        rc, out, err = ServerTests.agent_cli(self, "--as", "agent-6", "synced")
        self.assertEqual(rc, 1, out + err)
        self.assertIn("steward, agent-5", err)
        rc, out, err = ServerTests.agent_cli(self, "--as", "agent-5", "synced")
        self.assertEqual(rc, 0, out + err)


# -- 0.8.6: the footer's usage and account probe ------------------------------------------------

GOOD_USAGE = {
    "updated_at": "2026-09-30T18:00:00.000Z",
    "five_hour": {"used_percentage": 42, "resets_at": "2026-09-30T21:00:00.000Z"},
    "seven_day": {"used_percentage": 18.5, "resets_at": None},
    "extra_field_never_forwarded": "planted-usage-marker",
}
NATIVE_USAGE = {  # the shape Claude Code hands a status line command (abridged)
    "session_id": "planted-session-marker",
    "transcript_path": "/home/planted-path-marker/t.jsonl",
    "cost": {"total_cost_usd": 1.25},
    "rate_limits": {
        "five_hour": {"used_percentage": 42, "resets_at": 1790802000},
        "seven_day": {"used_percentage": 18, "resets_at": 1791158400},
    },
}
ACCOUNT_DOC = {
    "oauthAccount": {"emailAddress": "owner@example.com", "accountUuid": "planted-uuid-marker"},
    "primaryApiKey": "planted-secret-marker",
    "projects": {"/x": {"history": ["planted-history-marker"]}},
}


class UsageTests(_Live, unittest.TestCase):
    """GET /api/usage: the footer's probe. Off unless configured, gated, and it forwards only the
    named fields of two files that belong to someone else."""

    def configure(self, usage=None, account=None):
        d = Path(self.tmp.name)
        u = a = None
        if usage is not None:
            u = d / "usage.json"
            u.write_bytes(usage if isinstance(usage, bytes) else json.dumps(usage).encode())
        if account is not None:
            a = d / "settings.json"
            a.write_bytes(account if isinstance(account, bytes) else json.dumps(account).encode())
        self.console.cfg = SV.Config(**{**self.cfg.__dict__, "usage_file": u, "account_file": a})
        return u, a

    def usage(self):
        code, out = self.get("/api/usage")
        self.assertEqual(code, 200, out)
        return out

    def test_the_probe_is_behind_the_gate(self):
        self.configure(GOOD_USAGE, ACCOUNT_DOC)
        self.assertEqual(self.req("GET", "/api/usage")[0], 403)

    def test_off_unless_configured(self):
        # Catches: a kit that reads a default path nobody asked it to, on an install with no footer.
        self.assertEqual(self.usage(), {"enabled": False, "usage": None, "usage_problem": None, "account": None})

    def test_a_good_snapshot_forwards_only_the_named_fields(self):
        # Catches: passing the file through (the extra field would reach the page), or a number
        # forwarded as the string it arrived as.
        self.configure(GOOD_USAGE)
        out = self.usage()
        self.assertTrue(out["enabled"])
        self.assertEqual(out["usage"], {
            "updated_at": "2026-09-30T18:00:00Z",
            "five_hour": {"used_percentage": 42.0, "resets_at": "2026-09-30T21:00:00Z"},
            "seven_day": {"used_percentage": 18.5, "resets_at": None}})
        self.assertIsNone(out["usage_problem"])
        self.assertNotIn("planted-usage-marker", json.dumps(out))

    def test_a_bad_snapshot_is_named_never_echoed(self):
        # Catches: a crash (500) on a file the status line wrote badly, a problem that quotes the
        # file back, and a percentage or timestamp let through unchecked.
        cases = [
            (b"{not json", "is not JSON"),
            (b"[1, 2]", "is not a JSON object"),
            (b" " * (SV.USAGE_MAX_BYTES + 1), "larger than 16 KB"),
            ({**GOOD_USAGE, "five_hour": {"used_percentage": 150, "resets_at": None}}, "percentage is not 0-100"),
            ({**GOOD_USAGE, "five_hour": {"used_percentage": True, "resets_at": None}}, "percentage is not 0-100"),
            ({**GOOD_USAGE, "updated_at": "2026-09-30T18:00:00"}, "has no time zone"),
            ({**GOOD_USAGE, "updated_at": "<script>planted</script>"}, "is not a timestamp"),
            ({k: v for k, v in GOOD_USAGE.items() if k != "seven_day"}, "has no seven_day window"),
            # Review of #16: each of these escaped as a 500 with a traceback before it was caught.
            ({**GOOD_USAGE, "updated_at": "0001-01-01T00:00:00+05:00"}, "is out of range"),
            ({**GOOD_USAGE, "five_hour": {"used_percentage": 1, "resets_at": "9999-12-31T23:59:59-05:00"}},
             "is out of range"),
            (b"[" * 10000, "is not JSON"),
            (b'{"updated_at": ' + b"9" * 5000 + b"}", "is not JSON"),
            (b"\xff\xfe not utf-8", "is not JSON"),
        ]
        for body, problem in cases:
            with self.subTest(problem=problem):
                self.configure(body)
                out = self.usage()
                self.assertIsNone(out["usage"])
                self.assertIn(problem, out["usage_problem"])
                self.assertNotIn("planted", json.dumps(out))

    def test_claude_codes_own_status_line_input_is_read_as_is(self):
        # Claude Code hands its status line command a JSON object with the windows under
        # rate_limits, reset times as epoch seconds and no updated_at; a status line that saves it
        # is the usage file. Catches: a kit that reads only claude-hud's shape (the footer stays
        # empty on a host whose status line is not claude-hud), an updated_at invented as "now"
        # instead of the file's own write time (a stale file would never read as stale), and the
        # session's other fields (paths, session id) reaching the page.
        u, _ = self.configure(NATIVE_USAGE)
        os.utime(u, (1790791200, 1790791200))
        out = self.usage()
        self.assertIsNone(out["usage_problem"])
        self.assertEqual(out["usage"], {
            "updated_at": "2026-09-30T18:00:00Z",
            "five_hour": {"used_percentage": 42.0, "resets_at": "2026-09-30T21:00:00Z"},
            "seven_day": {"used_percentage": 18.0, "resets_at": "2026-10-05T00:00:00Z"}})
        self.assertNotIn("planted", json.dumps(out))

    def test_a_bad_epoch_reset_is_named(self):
        # Catches: a boolean or float taken as seconds, and a negative or far-future number
        # turned into a date (or a 500) instead of a named problem.
        for resets, problem in [(True, "five_hour reset time is not a timestamp"),
                                (1.5, "five_hour reset time is not a timestamp"),
                                (-1, "five_hour reset time is out of range"),
                                (10 ** 12, "five_hour reset time is out of range"),
                                (10 ** 400, "five_hour reset time is out of range")]:
            with self.subTest(resets=resets):
                limits = {**NATIVE_USAGE["rate_limits"],
                          "five_hour": {"used_percentage": 1, "resets_at": resets}}
                self.configure({**NATIVE_USAGE, "rate_limits": limits})
                out = self.usage()
                self.assertIsNone(out["usage"])
                self.assertIn(problem, out["usage_problem"])

    def test_a_write_time_in_the_future_is_named_not_shown_as_fresh(self):
        # Review of #17: a clock skew or a touch -d would otherwise read as fresh for years.
        # Catches: forwarding any mtime unchecked; and one minute of slack is allowed, so a
        # host whose clock sits a few seconds behind the writer's still shows the numbers.
        u, _ = self.configure(NATIVE_USAGE)
        ahead = time.time() + 3600
        os.utime(u, (ahead, ahead))
        out = self.usage()
        self.assertIsNone(out["usage"])
        self.assertIn("write time is in the future", out["usage_problem"])
        nearly = time.time() + 5
        os.utime(u, (nearly, nearly))
        self.assertIsNone(self.usage()["usage_problem"])

    def test_claude_code_input_before_its_first_limits_says_so(self):
        # Claude Code sends no rate_limits until the session's first API answer. Catches: the
        # misleading "updated_at is not a timestamp" the review of #17 measured for that case.
        self.configure({k: v for k, v in NATIVE_USAGE.items() if k != "rate_limits"})
        self.assertEqual(self.usage()["usage_problem"], "the usage file has no usage limits yet")

    def test_a_huge_percentage_is_named(self):
        # Review of #17: 10**400 made math.isfinite raise, which reached only the generic message.
        limits = {**NATIVE_USAGE["rate_limits"], "five_hour": {"used_percentage": 10 ** 400, "resets_at": None}}
        self.configure({**NATIVE_USAGE, "rate_limits": limits})
        self.assertIn("five_hour percentage is not 0-100", self.usage()["usage_problem"])

    def test_top_level_windows_win_over_rate_limits(self):
        # Catches: a file carrying both shapes read from the one the writer did not mean.
        self.configure({**GOOD_USAGE, "rate_limits": {"five_hour": {"used_percentage": 99, "resets_at": None},
                                                      "seven_day": {"used_percentage": 99, "resets_at": None}}})
        self.assertEqual(self.usage()["usage"]["five_hour"]["used_percentage"], 42.0)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "needs a FIFO")
    def test_a_fifo_where_a_file_should_be_is_refused_without_blocking(self):
        # Catches: open() on a FIFO with no writer, which holds a handler thread forever.
        u, _ = self.configure(GOOD_USAGE)
        u.unlink()
        os.mkfifo(u)
        self.assertEqual(self.usage()["usage_problem"], "the usage file is not a regular file")

    def test_a_missing_snapshot_says_so(self):
        # The status line writes the file only while a session runs: before its first write the
        # footer must say "not yet", not fail.
        u, _ = self.configure(GOOD_USAGE)
        u.unlink()
        out = self.usage()
        self.assertEqual(out["usage_problem"], "the usage file does not exist yet")

    def test_the_account_file_gives_up_its_email_and_nothing_else(self):
        # Catches: forwarding oauthAccount whole, or any other key of a file that also holds
        # credentials and project history.
        self.configure(account=ACCOUNT_DOC)
        out = self.usage()
        self.assertEqual(out["account"], "owner@example.com")
        text = json.dumps(out)
        for marker in ("planted-uuid-marker", "planted-secret-marker", "planted-history-marker"):
            self.assertNotIn(marker, text)

    def test_an_unusable_account_file_shows_no_account(self):
        for doc in (b"{broken", [], {"oauthAccount": "x"}, {"oauthAccount": {"emailAddress": "no-at-sign"}},
                    {"oauthAccount": {"emailAddress": "a b@example.com"}},
                    {"oauthAccount": {"emailAddress": "x@y\n<script>"}},
                    b"[" * 100000, b'{"n": ' + b"9" * 5000 + b"}"):
            with self.subTest(doc=str(doc)[:40]):
                self.configure(account=doc)
                self.assertIsNone(self.usage()["account"])
        _, a = self.configure(account=ACCOUNT_DOC)
        a.unlink()
        self.assertIsNone(self.usage()["account"])

    def test_the_cli_refuses_a_relative_path(self):
        for flag in ("--usage-file", "--account-file"):
            with self.subTest(flag=flag), self.assertRaises(SystemExit):
                with contextlib.redirect_stderr(io.StringIO()):
                    SV.main(["--root", ".", "--page", "p", "--state", "s", "--adapter", "a", "--team-domain", "t",
                             "--aud", "x", "--hostname", "h", flag, "relative.json"])


class SlimReadCliTests(_Live, unittest.TestCase):
    """K1 through `agent.py` against the real server: the 64 KiB cap (AC1.3), compact JSON (E3),
    `todo` (E1), `view --item` (E2) and `--since` (E5)."""

    def big_thread(self, n=4):
        for k in range(n):  # four agent replies of 19 000 characters: a view of ~76 KB
            code, out = self.agent_post("/message", {"item": "LANE.1", "text": f"{k} " + "x" * 19_000,
                                                     "nonce": f"bigreply{k:04d}"})
            self.assertEqual(code, 200, out)
        return out["record"]["seq"]

    def test_a_read_over_64_kib_is_refused_whole_and_names_the_narrower_command(self):
        # AC1.3. Catches: a cap that truncates and exits 0, or prints the first 64 KiB of JSON.
        last = self.big_thread()
        for args in (("view",), ("view", "--item", "LANE"), ("view", "--since", "0")):
            rc, out, err = self.agent_cli(*args)
            self.assertEqual(rc, 4, (args, err))
            self.assertEqual(out, "", args)          # no partial JSON, not even an opening brace
            self.assertIn("todo", err)
            self.assertIn("view --item ID", err)
            self.assertIn("--full", err)
        rc, out, err = self.agent_cli("view", "--full")
        self.assertEqual(rc, 0, err)
        self.assertGreater(len(out.encode()), 64 * 1024)
        self.assertEqual(json.loads(out)["view"]["seq"], last)
        # The narrower reads answer: todo, and only what came after the big replies.
        rc, out, err = self.agent_cli("todo")
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)["inbox"], ["LANE.1/Q1"])
        rc, out, err = self.agent_cli("view", "--since", str(last))
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)["view"]["threads"], {})

    def test_json_is_compact_off_a_terminal_and_indented_on_one(self):
        # E3. Catches: compact output a person at a terminal has to read, or indented output piped to an agent.
        rc, out, _ = self.agent_cli("view")
        self.assertEqual(rc, 0)
        self.assertEqual(out.count("\n"), 1)
        self.assertNotIn(": ", out.split('"text"')[0])

        class Tty(io.StringIO):
            def isatty(self):
                return True

        import agent as AG
        tty = Tty()
        with contextlib.redirect_stdout(tty):
            self.assertEqual(AG.main(["--state", str(self.cfg.state), "todo"]), 0)
        self.assertIn('\n  "forks": []', tty.getvalue())

    def test_a_server_one_kit_older(self):
        # The fix for the defect measured on a live 0.8.8 server: `todo` raised KeyError on its view,
        # which has no waiting_visuals. The server here builds its view exactly so. Catches: a crash,
        # and a todo that drops the waiting visual; and, for a view missing what todo cannot derive,
        # a traceback or a silently empty answer instead of one line naming the version.
        from unittest import mock
        req = self.owner_msg(item="LANE.1", intent="visual", text="draw it")
        real = SV.V.build

        def old_build(*a, **kw):  # a 0.8.8 view: none of the three fields K1 added
            v = {k: x for k, x in real(*a, **kw).items() if k != "waiting_visuals"}
            v["questions"] = {q: {k: x for k, x in d.items() if k != "last_seq"} for q, d in v["questions"].items()}
            v["transcripts"] = {f: {k: x for k, x in t.items() if k != "seq"} for f, t in v["transcripts"].items()}
            return v

        with mock.patch.object(SV.V, "build", old_build):
            rc, out, err = self.agent_cli("todo")
            self.assertEqual(rc, 0, err)
            self.assertEqual(json.loads(out)["visuals"], [{"id": req["id"], "item": "LANE.1"}])
            rc, out, err = self.agent_cli("view", "--item", "LANE.1")
            self.assertEqual(rc, 0, err)
            self.assertEqual(json.loads(out)["view"]["waiting_visuals"], [{"id": req["id"], "item": "LANE.1"}])
            rc, out, err = self.agent_cli("answers", "--json")  # answers without --since needs nothing new
            self.assertEqual(rc, 0, err)
            # --since cannot see a 0.8.8 view's locks, so it refuses rather than drop them (review LOW c).
            for args in (("answers", "--since", "0"), ("answers", "--json", "--since", "0"), ("view", "--since", "0")):
                rc, out, err = self.agent_cli(*args)
                self.assertEqual((rc, out), (1, ""), args)
                said = [ln for ln in err.splitlines() if not ln.startswith("overture ")]
                self.assertEqual(len(said), 1, err)
                self.assertNotIn("Traceback", err)
                self.assertIn("questions.*.last_seq", said[0])
                self.assertIn("restart the console server", said[0])
        self.fork()

        def older_still(*a, **kw):
            v = real(*a, **kw)
            return {**v, "forks": {f: {k: x for k, x in d.items() if k != "done"} for f, d in v["forks"].items()}}

        with mock.patch.object(SV.V, "build", older_still):
            for args in (("todo",), ("view", "--item", "LANE.1"), ("view", "--since", "0")):
                rc, out, err = self.agent_cli(*args)
                self.assertEqual((rc, out), (1, ""), args)
                self.assertNotIn("Traceback", err)
                said = [ln for ln in err.splitlines() if not ln.startswith("overture ")]  # the server's own log
                self.assertEqual(len(said), 1, err)
                self.assertTrue(said[0].startswith("refused: "), err)
                self.assertIn(f"runs kit {SV.__version__}", err)
                self.assertIn("forks.*.done", err)
                self.assertIn("restart the console server", err)

    def test_view_item_and_answers_since_through_the_cli(self):
        # E2 and E5 at the door. Catches: --item that ignores an unknown item, --since on answers ignored.
        self.owner_msg(item="LANE", text="a note on the parent")
        rc, out, err = self.agent_cli("view", "--item", "LANE.1")
        self.assertEqual(rc, 0, err)
        got = json.loads(out)
        self.assertEqual((set(got["items"]), set(got["view"]["threads"])), ({"LANE.1"}, set()))
        self.assertEqual(json.loads(self.agent_cli("todo")[1])["awaiting_agent"], ["LANE"])
        rc, _, err = self.agent_cli("view", "--item", "NOPE")
        self.assertEqual(rc, 1)
        self.assertIn("no item 'NOPE'", err)
        seq = json.loads(self.agent_cli("todo")[1])["seq"]
        rc, out, _ = self.agent_cli("answers", "--json", "--since", str(seq))
        self.assertEqual((rc, json.loads(out)["rows"]), (0, []))
        rc, out, _ = self.agent_cli("answers", "--json", "--since", "0")
        self.assertEqual([r["qid"] for r in json.loads(out)["rows"]], ["LANE.1/Q1"])


# -- CONSOLE-kit/Q23: the console server spawns no git ------------------------------------

NOGIT = "unavailable (no git in the server)"
SPAWN_EVENTS = ("subprocess.Popen", "os.posix_spawn", "os.exec", "os.spawn", "os.system", "os.fork",
                "os.forkpty", "pty.spawn")


def git_project(root: Path) -> str:
    """A project in a git work tree: specs/spec.md committed, then committed again with an unrelated first line.

    The spec's file time is set long before any lock, so a refine tag is due
    whichever time is read. Returns the first version's sha256: the version a
    question is locked against, which only git history still holds.
    """
    import hashlib
    (root / "specs").mkdir()
    spec = root / "specs/spec.md"
    spec.write_text(ServerTests.SPEC)
    (root / ".overture.json").write_text(json.dumps({"specs_dir": "specs/"}))
    git(root, "init", "-q")
    git(root, "add", "specs/spec.md", ".overture.json")
    git(root, "commit", "-qm", "v1")
    spec.write_text("A new first line.\n" + ServerTests.SPEC)
    git(root, "commit", "-qam", "unrelated")
    os.utime(spec, (1_600_000_000, 1_600_000_000))
    return hashlib.sha256(ServerTests.SPEC.encode()).hexdigest()


def legacy_ask(store_path: Path, *bodies: dict) -> None:
    """Write questions straight into a store that no server holds yet, as an older kit's `ask` stored them.

    `spec_question` anchors a whole-file hash on the file its `source` cites
    lines of: the shape six live answers went stale with, and the one `ask`
    refuses since CONSOLE-kit/Q30 (R1). Those answers still exist and still
    need reanchoring, so tests of that path start from a stored question.
    """
    from overture.store import Store
    st = Store(store_path)
    for b in bodies:
        st.append({**b, "type": "question", "schemaVersion": SV.S.SCHEMA_VERSION, "by": "agent"})


def spec_question(v1: str) -> dict:
    return {"qid": "LANE.1/Q2", "item": "LANE.1", "text": "Still?", "kind": "single",
            "options": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}], "star": None,
            "valid_if": [{"kind": "file_sha256", "path": "specs/spec.md", "sha256": v1}],
            "source": "specs/spec.md:5-6", "nonce": "nogitquest01"}


# Modules that may start a process, and why each is allowed (the no-spawn walk skips them):
SPAWN_ALLOWED = {
    "overture/gitseam.py": "the one door for git; it starts nothing once the server closes it",
    "agent.py": "run by an agent inside its own jail; the server process never imports it",
    "onboard.py": "operator setup/verify tool (1.24.0 verify runs curl/systemctl); never imported by the server",
    "tools/": "operator tools (verify_vendor.py runs git on a kit clone); never imported by the server",
}
SPAWN_MODULES = ("subprocess", "pty", "multiprocessing")
SPAWN_OS = ("system", "popen", "exec", "spawn", "posix_spawn", "fork")


def spawn_sites(source: str) -> list[str]:
    """Every way `source` could start a process: imports of subprocess, pty or multiprocessing in any form,
    os.system/popen/exec*/spawn*/posix_spawn*/fork* (called, or imported from os), and a dynamic import of
    one of those modules by name."""
    import ast
    tree = ast.parse(source)
    os_names = {"os"}
    found: list[str] = []

    def banned_module(name: str) -> bool:
        return name.split(".")[0] in SPAWN_MODULES

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if banned_module(a.name):
                    found.append(f"import {a.name}")
                if a.name == "os":
                    os_names.add(a.asname or "os")
        elif isinstance(node, ast.ImportFrom) and node.module:
            if banned_module(node.module):
                found += [f"from {node.module} import {a.name}" for a in node.names]
            elif node.module == "os":
                found += [f"from os import {a.name}" for a in node.names
                          if a.name == "*" or a.name.startswith(SPAWN_OS)]
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id in os_names
                and f.attr.startswith(SPAWN_OS)):
            found.append(f"os.{f.attr}()")
        name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
        if (name in ("__import__", "import_module") and node.args and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str) and banned_module(node.args[0].value)):
            found.append(f"{name}({node.args[0].value!r})")
    return found


def spawn_scan(top: Path) -> dict[str, list[str]]:
    """`spawn_sites` for every .py under `top`, recursively, keyed by its path relative to `top`."""
    return {p.relative_to(top).as_posix(): spawn_sites(p.read_text(encoding="utf-8"))
            for p in sorted(top.rglob("*.py")) if "__pycache__" not in p.parts}


# The real server (`server.serve`, as `server.py` runs it) in a child process that audits itself.
# The hook goes in before the kit is imported, and records every way Python can start a process.
AUDIT_CHILD = r'''
import json, sys
SPAWN = tuple(json.loads(sys.argv[2]))
WATCH = json.loads(sys.argv[1]).get("watch")   # Q24: a path no event may name (the project's adapter)
spawned, touched = [], []
def hook(event, args):
    if event.startswith(SPAWN):
        spawned.append([event, repr(args)[:300]])
    if WATCH and WATCH in repr(args):   # import, open, compile, exec, stat ... of the adapter: any at all
        touched.append([event, repr(args)[:300]])
sys.addaudithook(hook)

import http.client, os, socket, threading, time
from pathlib import Path
p = json.loads(sys.argv[1])
sys.path.insert(0, p["kit"])
from overture import server as SV

root = Path(p["root"])
s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
cfg = SV.Config(root=root, page=Path(p.get("page") or root / "page.html"), state=root / "state",
                adapter=root / "adapter.py", team_domain="team.example.cloudflareaccess.com", aud="a" * 64,
                hostname=p["host"], port=port, project="audit")
if p.get("question"):   # stored as an older kit's ask stored it: `ask` refuses this shape since Q30 (R1)
    from overture.store import Store
    Store(cfg.store).append({**p["question"], "type": "question", "schemaVersion": 1, "by": "agent"})
threading.Thread(target=SV.serve, args=(cfg, lambda _tok: {"email": "owner@example.com"}), daemon=True).start()
for _ in range(200):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
        if cfg.socket.exists():
            break
    except OSError:
        pass
    time.sleep(0.05)

_csrf = {"v": None}
def _grab_csrf():
    import re as _re
    if _csrf["v"] is not None:
        return _csrf["v"]
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    c.request("GET", "/", headers={"Cf-Access-Jwt-Assertion": "x"})
    r = c.getresponse(); raw = r.read().decode("utf-8", "replace"); c.close()
    m = _re.search(r'"csrf":\s*"([^"]+)"', raw)
    if m:
        _csrf["v"] = m.group(1)
    return _csrf["v"]

def owner(method, path, body=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    h = {"Cf-Access-Jwt-Assertion": "x", "Origin": "https://" + p["host"]}
    data = None if body is None else json.dumps(body).encode()
    if data is not None:
        h["Content-Type"] = "application/json"
    if method == "POST":
        tok = _grab_csrf()
        if tok:
            h["X-Overture-CSRF"] = tok
    c.request(method, path, body=data, headers=h)
    r = c.getresponse(); raw = r.read(); c.close()
    try:
        return r.status, json.loads(raw)
    except ValueError:
        return r.status, raw.decode("utf-8", "replace")   # the page itself

def agent(method, path, body=None):
    return SV.agent_request(cfg.socket, method, path, body)

codes, out = {}, {}
def call(name, fn, *a):
    code, body = fn(*a)
    codes[name] = code
    return body

out["view_before_push"] = call("owner GET /api/view (before push)", owner, "GET", "/api/view")
# Q24: the server never runs the adapter; the steward ran it and pushes its output, as data.
call("agent POST /items", agent, "POST", "/items", p.get("items") or
     {"items": {"LANE": {"title": "a lane", "parent": None, "status": "open"},
                "LANE.1": {"title": "a phase", "parent": "LANE", "status": "open"}},
      "seed_questions": [], "board": None})
out["prs_before"] = call("owner GET /api/prs (before push)", owner, "GET", "/api/prs")
call("agent POST /prs", agent, "POST", "/prs", {"repo": "octo/repo", "window_days": 30, "prs": [
     {"number": 7, "title": "<img src=x onerror=alert(1)> LANE.1/Q2", "state": "open", "draft": False,
      "head": "lane-1", "base": "main", "author": "octo", "created_at": "2026-09-30T10:00:00Z",
      "updated_at": "2026-09-30T11:00:00Z", "merged_at": None, "closed_at": None,
      "url": "https://github.com/octo/repo/pull/7", "merge_commit": None, "checks": "pending"}]})
out["prs"] = call("owner GET /api/prs", owner, "GET", "/api/prs")
a = call("owner POST /api/answer", owner, "POST", "/api/answer",
         {"qid": "LANE.1/Q2", "picks": ["a"], "own_text": "", "nonce": "nogitanswer1"})
call("owner POST /api/lock", owner, "POST", "/api/lock",
     {"qid": "LANE.1/Q2", "answer": a["record"]["id"], "nonce": "nogitlock001"})
if "push" in p:   # Q23 part 2: what the steward computed with git in ITS process, pushed as data
    out["wants"] = call("agent GET /history-wants", agent, "GET", "/history-wants")
    for b in p["push"]["blobs"]:
        call("agent POST /history-blob", agent, "POST", "/history-blob", b)
    out["specs"] = call("agent POST /history-specs", agent, "POST", "/history-specs", {"specs": p["push"]["specs"]})
for path in ("/", "/index.html", "/api/board", "/api/usage", "/api/feed", "/api/wait?since=0&timeout=0",
             "/api/evidence?qid=LANE.1/Q2", "/api/visual?id=" + "0" * 24):
    call("owner GET " + path, owner, "GET", path)
out["page_before_snapshot"] = owner("GET", "/")[1]
if p.get("snapshot"):   # Q28: the page, as `agent.py page-snapshot` sends it; Q29: staged, then the owner publishes
    call("agent POST /page-snapshot", agent, "POST", "/page-snapshot", p["snapshot"])
    out["page_staged"] = owner("GET", "/")[1]
    out["preview"] = call("owner GET /api/page-staged", owner, "GET", "/api/page-staged")
    call("owner POST /api/page-publish", owner, "POST", "/api/page-publish", {"commit": p["snapshot"]["commit"]})
out["page"] = owner("GET", "/")[1]
out["view"] = call("owner GET /api/view", owner, "GET", "/api/view")
out["check"] = call("owner GET /api/check", owner, "GET", "/api/check")
for path in ("/view", "/check", "/health"):
    call("agent GET " + path, agent, "GET", path)
out["reanchor_dry"] = call("agent POST /reanchor dry", agent, "POST", "/reanchor", {"dry_run": True})
out["reanchor"] = call("agent POST /reanchor", agent, "POST", "/reanchor", {"dry_run": False})
call("agent POST /visual-export", agent, "POST", "/visual-export", {"ids": []})
call("owner POST /api/message", owner, "POST", "/api/message",
     {"item": "LANE.1", "text": "deliberate", "intent": "fork", "mode": "tighten", "nonce": "nogitfork001"})
call("owner POST /api/lock-all", owner, "POST", "/api/lock-all", {})
call("owner POST /api/relock", owner, "POST", "/api/relock", {"qid": "LANE.1/Q2", "nonce": "nogitrelock1"})
sys.stdout.write(json.dumps({"spawned": spawned, "touched": touched, "codes": codes, "out": out}) + "\n")
sys.stdout.flush()
os._exit(0)
'''


class NoServerGitTests(_Live, unittest.TestCase):
    """CONSOLE-kit/Q23: the server spawns no git; each feature git fed says why it is missing.

    Owner ruling, "Stop server git now, restore it agent-side next": git obeys
    the repo's own .git/config, which an agent can write, and some keys make
    git run a program, outside every agent's jail. Each test runs on a project
    that IS a git work tree with the history the old code would have read, so a
    feature that still reached git would find something.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.root = d
        seam_closed(self)
        self.v1 = git_project(d)
        (d / "page.html").write_text(PAGE)
        self.cfg = SV.Config(root=d, page=d / "page.html", state=d / "state", adapter=d / "unused.py",
                             team_domain=TEAM, aud=AUD, hostname=HOSTNAME, port=0, project="test")
        legacy_ask(self.cfg.store, spec_question(self.v1))   # R1 refuses this shape on a new ask
        self.console = SV.Console(self.cfg, FakeAdapter())
        self.console.seed()
        verify = SV.access_verifier(TEAM, AUD, key_for=lambda _t: KEY.public_key())
        self.owner = SV.owner_server(self.console, verify, 0)
        self.port = self.owner.server_address[1]
        threading.Thread(target=self.owner.serve_forever, daemon=True).start()
        self.agent = SV.agent_server(self.console)
        threading.Thread(target=self.agent.serve_forever, daemon=True).start()
        code, a = self.req("POST", "/api/answer", self.answer(qid="LANE.1/Q2", nonce="nogitanswer1"), tok=token())
        self.assertEqual(code, 200, a)
        code, lk = self.req("POST", "/api/lock", {"qid": "LANE.1/Q2", "answer": a["record"]["id"],
                                                  "nonce": "nogitlock001"}, tok=token())
        self.assertEqual(code, 200, lk)
        self.lock = lk["record"]

    # (a) ---------------------------------------------------------------------------------

    def test_the_served_server_starts_no_process_on_any_route_that_reached_git(self):
        # The real `server.serve` in a child with sys.addaudithook watching subprocess.Popen,
        # os.posix_spawn, os.exec*, os.spawn*, os.system and os.fork*. Every route that used to
        # reach git is exercised (the view's tags, /check from both doors, /reanchor dry and real),
        # and the rest of the routes besides. MUTATIONS, run by hand on this commit, each red:
        # delete `G.close()` from `serve` (12 spawns: `git rev-parse`/`status`/`log` from the
        # view's tags, `git log` + `git cat-file --batch` from /check and /reanchor); and drop
        # the closed-seam return in `gitseam.run` (the tags' `git rev-parse` and `git status`).
        # Re-pointing only `History._git` at subprocess stays green, by design: with the seam
        # closed, the check and reanchor paths return their label before any history lookup.
        import subprocess
        (self.root / "adapter.py").write_text(
            "def items():\n    return {'LANE': {'title': 'a lane', 'parent': None, 'status': 'open'},\n"
            "            'LANE.1': {'title': 'a phase', 'parent': 'LANE', 'status': 'open'}}\n"
            "def seed_questions():\n    return []\n"
            "def record(entries, dry_run):\n    return []\n")
        root = Path(tempfile.mkdtemp(prefix="ck-audit-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        v1 = git_project(root)
        (root / "page.html").write_text(PAGE)
        (root / "adapter.py").write_text((self.root / "adapter.py").read_text())
        params = {"kit": str(Path(SV.__file__).resolve().parent.parent), "root": str(root), "host": HOSTNAME,
                  "question": spec_question(v1)}
        r = subprocess.run([sys.executable, "-c", AUDIT_CHILD, json.dumps(params), json.dumps(SPAWN_EVENTS)],
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        got = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertEqual(got["spawned"], [], "the server started a process")
        # The routes really ran, and reached the code git used to feed (else zero spawns proves nothing).
        for name, code in got["codes"].items():
            self.assertLess(code, 500, name)
        out = got["out"]
        # The PR push ran under the same hook (the steward's gh ran elsewhere): kept, served, nothing spawned.
        self.assertEqual(got["codes"]["agent POST /prs"], 200)
        self.assertEqual((out["prs_before"]["pushed"], out["prs_before"]["note"]), (False, PR.NOT_PUSHED))
        self.assertEqual((out["prs"]["pushed"], [p["number"] for p in out["prs"]["prs"]]), (True, [7]))
        self.assertEqual(out["check"]["history"], NOGIT)
        [c] = out["check"]["stale"]["LANE.1/Q2"]["conditions"]
        self.assertEqual((c["reason"], c["history"]), ("file_changed", NOGIT))
        self.assertEqual(out["view"]["view"]["tags"]["git"], NOGIT)
        self.assertEqual([t["step"] for t in out["view"]["view"]["tags"]["questions"]["LANE.1/Q2"]][:1], ["refine"])
        for k in ("reanchor_dry", "reanchor"):
            self.assertEqual(out[k]["history"], NOGIT)
            self.assertIn(NOGIT, out[k]["plan"][0]["unresolved"][0]["why"])

    def test_a_closed_seam_refuses_without_starting_anything(self):
        # Catches: a fail-open seam (one that tries git and reports the failure), a typed result
        # read as "not a git work tree", and a git call in the kit that goes around the seam.
        import subprocess
        from unittest import mock
        from overture import gitseam as G
        self.assertFalse(G.is_open())
        with mock.patch.object(subprocess, "run", side_effect=AssertionError("git was started")):
            self.assertIs(G.run(["status"], self.root, timeout=5), G.NO_GIT)
            self.assertIsNone(SV.A.History(self.root).versions("specs/spec.md"))
            self.assertIsNone(SV.T._Git(self.root).head())
        self.assertEqual((G.NO_GIT.reason, G.UNAVAILABLE), (NOGIT, NOGIT))
        with seam_open():
            self.assertRegex(G.run(["rev-parse", "HEAD"], self.root, timeout=30), rb"^[0-9a-f]{40}\n$")

    def test_no_kit_module_but_the_seam_can_start_a_process(self):
        # An AST walk, not a regex (security review of a4a363b): `from subprocess import run`,
        # `import subprocess as sp`, `os.system`/`os.popen` and a file in a subfolder all got past
        # the regex. Every .py under plugin/kit, recursively, is read; gitseam.py is the one door.
        kit = Path(SV.__file__).resolve().parent.parent
        found = spawn_scan(kit)
        self.assertGreater(len(found), 15)   # the walk really read the kit, subfolders included
        self.assertIn("overture/server.py", found)
        self.assertIn("tools/verify_vendor.py", found)
        for rel, sites in sorted(found.items()):
            if any(rel == k or (k.endswith("/") and rel.startswith(k)) for k in SPAWN_ALLOWED):
                continue   # each with its reason in SPAWN_ALLOWED
            with self.subTest(file=rel):
                self.assertEqual(sites, [], f"{rel} can start a process outside gitseam")
        # The allowlist is not dead weight: verify_vendor.py really does spawn git, agent side.
        self.assertIn("import subprocess", found["tools/verify_vendor.py"])

    def test_the_spawn_walk_catches_each_evasion(self):
        # Decoys: each form the regex missed, and each must be flagged.
        decoys = {
            "import subprocess as sp\nsp.run(['git'])\n": "import subprocess",
            "from subprocess import run\nrun(['git'])\n": "from subprocess import run",
            "from subprocess import *\n": "from subprocess import *",
            "import os\nos.system('git log')\n": "os.system()",
            "import os\nos.popen('git log')\n": "os.popen()",
            "import os as o\no.execvp('git', ['git'])\n": "os.execvp()",
            "import os\nos.posix_spawn('/usr/bin/git', [], {})\n": "os.posix_spawn()",
            "import os\nos.spawnlp(0, 'git', 'git')\n": "os.spawnlp()",
            "import os\nos.fork()\n": "os.fork()",
            "from os import system\nsystem('git')\n": "from os import system",
            "import pty\n": "import pty",
            "import multiprocessing.pool\n": "import multiprocessing.pool",
            "def f():\n    import subprocess\n": "import subprocess",
            "__import__('subprocess')\n": "__import__('subprocess')",
            "import importlib\nimportlib.import_module('subprocess')\n": "import_module('subprocess')",
        }
        for src, want in decoys.items():
            with self.subTest(src=src):
                self.assertIn(want, spawn_sites(src))
        self.assertEqual(spawn_sites("import os\nos.walk('.')\nos.environ.get('X')\n"), [])   # no false alarm
        with tempfile.TemporaryDirectory() as td:
            nested = Path(td) / "a" / "b" / "deep.py"
            nested.parent.mkdir(parents=True)
            nested.write_text("import subprocess as quiet\n")
            (Path(td) / "top.py").write_text("x = 1\n")
            self.assertEqual(spawn_scan(Path(td)), {"a/b/deep.py": ["import subprocess"], "top.py": []})

    # (b) ---------------------------------------------------------------------------------

    def test_check_history_names_the_cause_on_both_doors(self):
        code, out = self.req("GET", "/api/check", tok=token())
        self.assertEqual(code, 200, out)
        self.assertEqual(out["history"], NOGIT)
        [c] = out["stale"]["LANE.1/Q2"]["conditions"]
        self.assertEqual((c["reason"], c["holds"], c["history"]), ("file_changed", False, NOGIT))
        self.assertIn(f"Git history is {NOGIT}", c["words"])
        self.assertNotIn("locked_version", c)
        self.assertEqual(SV.agent_request(self.cfg.socket, "GET", "/check"), (200, out))
        rc, printed, err = self.agent_cli("check")
        self.assertEqual(rc, 0, err)
        self.assertIn(f"Git history is {NOGIT}", printed)
        self.assertIn(f"git history: {NOGIT}", err)

    def test_reanchor_history_names_the_cause(self):
        before = self.cfg.store.read_bytes()
        for dry in (True, False):
            code, out = SV.agent_request(self.cfg.socket, "POST", "/reanchor", {"dry_run": dry})
            self.assertEqual((code, out["history"]), (200, NOGIT))
            [p] = out["plan"]
            self.assertEqual(p["changes"], [])
            self.assertEqual(p["unresolved"][0]["why"], f"git history is {NOGIT}, so the version of specs/spec.md "
                                                        f"it was locked against cannot be looked up")
        self.assertEqual(self.cfg.store.read_bytes(), before)

    def test_a_refine_tag_says_it_shows_the_file_time_and_why(self):
        code, out = self.req("GET", "/api/view", tok=token())
        self.assertEqual(code, 200, out)
        tags = out["view"]["tags"]
        self.assertEqual(tags["git"], NOGIT)
        self.assertEqual(tags["basis"], ["mtime"])
        [refine] = [t for t in tags["questions"]["LANE.1/Q2"] if t["step"] == "refine"]
        self.assertIn(f"(file time 2020-09-13T12:26:40Z; the last-commit time is {NOGIT}, lock ", refine["reason"])

    # (c) ---------------------------------------------------------------------------------

    def test_agent_side_history_still_finds_what_the_server_cannot(self):
        # The same store and tree, read the way an agent-side caller reads them (outside the
        # server process): git history is there, and every feature the server labels has its data.
        from overture import tags as T
        self.enterContext(seam_open())
        items = self.console.items()
        status = {k: v.get("status") for k, v in items.items()}
        [c] = SV.A.check(self.console.store, self.root, status)["LANE.1/Q2"]["conditions"]
        self.assertEqual(c["cited_text"], "unchanged")
        self.assertRegex(c["locked_version"], r"^[0-9a-f]{12}$")
        self.assertNotIn("history", c)
        [p] = SV.A.plan_reanchor(self.console.store, self.root, status)
        self.assertEqual([ch["to"] for ch in p["changes"]],
                         [{"kind": "excerpt", "path": "specs/spec.md",
                           "text": "The cited claim, line one.\nThe cited claim, line two."}])
        view = self.console.payload()["view"]
        tags = T.compute(self.console.store, view, items, self.root, "specs")
        self.assertNotIn("git", tags)
        self.assertEqual(tags["basis"], ["git"])
        [refine] = [t for t in tags["questions"]["LANE.1/Q2"] if t["step"] == "refine"]
        self.assertIn("(last commit ", refine["reason"])
        self.assertNotIn(NOGIT, refine["reason"])


# -- K3: the one server, run as its own process (an audit hook cannot be removed, and a kill -9 needs one) ---

ONE_SERVER = r'''
import json, os, sys
kit, server_file, sock, pem, audit_log, roots = sys.argv[1:7]
roots = [r.rstrip("/") + "/" for r in roots.split(",") if r]
log = open(audit_log, "a", buffering=1)
armed = [False]
SPAWN = {"subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn", "os.fork", "os.forkpty"}
import sysconfig
TRUSTED = tuple(os.path.realpath(sysconfig.get_paths()[k]) + "/" for k in ("stdlib", "platstdlib", "purelib",
                                                                           "platlib")) + (os.path.realpath(kit) + "/",)

def under(p):
    try:
        p = os.path.realpath(os.fsdecode(p))
    except Exception:
        return False
    return any((p + "/").startswith(r) for r in roots)

def hook(event, args):
    why = None
    if event in SPAWN:
        why = f"{event} {args[:2]!r}"
    elif event == "import" and len(args) > 1 and args[1] and under(args[1]):
        why = f"import {args[0]} from {args[1]}"
    # A NET, NOT THE GUARD: a confined read (rootfs: one component, opened by dir_fd) reaches this hook with a bare
    # name resolved against nothing it can see, so `under()` is false for it. The adapter running is caught by the
    # import, compile and spawn rules, and by the marker file it writes; this rule only adds a plain open by path.
    elif event == "open" and isinstance(args[0], (str, bytes)) and os.fsdecode(args[0]).endswith(".py") \
            and under(args[0]):
        why = f"open {os.fsdecode(args[0])}"
    elif event == "compile" and len(args) > 1 and isinstance(args[1], (str, bytes)) and under(args[1]):
        why = f"compile {args[1]}"
    elif event in ("compile", "exec") and armed[0]:
        # While serving, code may come only from the interpreter's own library (a lazy stdlib import, e.g.
        # _strptime on first use) or the kit. A string ("<string>") or any other file is flagged: that is
        # how an importer that reads the adapter's source and execs it would show.
        code = args[0]
        name = getattr(code, "co_filename", None) or (args[1] if len(args) > 1 else None)
        name = os.fsdecode(name) if isinstance(name, (str, bytes)) else "<unknown>"
        if not any(name.startswith(p) for p in TRUSTED):
            why = f"{event} while serving: {name} {getattr(code, 'co_name', '')}"
    if why:
        log.write(why + "\n")

sys.addaudithook(hook)
sys.path.insert(0, kit)
from overture import multiserver as MS
from overture import server as SV
from cryptography.hazmat.primitives import serialization
pub = serialization.load_pem_public_key(open(pem, "rb").read())
r = MS.start(server_file, sock, verify_for=lambda aud: SV.access_verifier("team.example.cloudflareaccess.com", aud,
             key_for=lambda t: pub), port_for=lambda h: 0)
armed[0] = True
print(json.dumps({"ports": r.ports(), "refused": r.ms.refused(), "stores": len(r.ms.stores())}), flush=True)
sys.stdin.read()
r.shutdown()
'''


class _OneServer:
    """A fixture of two projects, alpha and beta, on one server run as its own process."""

    def setUp(self):
        import test_kit as TK
        from overture import serverfile as SF
        from overture import registry as R
        from cryptography.hazmat.primitives import serialization
        self.TK, self.SF, self.R = TK, SF, R
        self.tmp = tempfile.TemporaryDirectory()
        self.t = Path(os.path.realpath(self.tmp.name))
        self.cfg = self.t / "cfg"
        self.reg = self.cfg / "overture" / "projects.json"
        self.sfile = self.reg.parent / "server.json"
        self.sock = self.sfile.parent / "server.sock"          # where agent.py looks for the one server
        self.audit = self.t / "audit.log"
        self.pem = self.t / "pub.pem"
        self.pem.write_bytes(KEY.public_key().public_bytes(serialization.Encoding.PEM,
                                                             serialization.PublicFormat.SubjectPublicKeyInfo))
        self.p = {}
        for i, name in enumerate(("alpha", "beta")):
            root, state = self.t / f"root-{name}", self.t / f"state-{name}"
            root.mkdir()
            state.mkdir()
            (root / "index.html").write_text(f"<!doctype html><html><body>{name} board</body></html>\n")
            marker = self.t / f"ADAPTER-RAN-{name}"
            # The project's adapter: if anything in the server imports or runs it, the marker appears.
            (root / "console_adapter.py").write_text(
                f"open({str(marker)!r}, 'w').write('ran')\n"
                "def items():\n    return {'FROM_ADAPTER': {'title': 'the adapter says', 'parent': None, "
                "'status': 'open'}}\n"
                "def seed_questions():\n    return []\n"
                "def record(entries, dry_run):\n    return []\n")
            R.register(root, state, HERE / "plugin" / "kit", path=self.reg)
            SF.add(name, state, f"{name}.example.com", AUD, 4901 + i, TEAM, registry=self.reg, path=self.sfile)
            self.p[name] = {"root": root, "state": state, "marker": marker}
        # Each project's token as `server add` minted it (K4): `agent()` sends the path's project's own token.
        self.tokens = {n: SF.read_token(n, self.sfile) for n in self.p}
        self.proc = None
        self.logs: list[str] = []

    def tearDown(self):
        self.stop()
        self.tmp.cleanup()

    def spawn(self, script=ONE_SERVER, prefix=()):
        roots = ",".join(str(v["root"]) for v in self.p.values())
        # stderr to a file, not a pipe: an undrained pipe fills at 64 KiB of access lines and stalls the server.
        self.errlog = open(self.t / f"server-{len(self.logs)}.err", "w+")
        self.proc = subprocess.Popen([*prefix, sys.executable, "-c", script, str(HERE / "plugin" / "kit"),
                                      str(self.sfile), str(self.sock), str(self.pem), str(self.audit), roots],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.errlog,
                                     text=True, env={**os.environ, "XDG_CONFIG_HOME": str(self.cfg)})
        line = self.proc.stdout.readline()
        if not line:
            self.proc.wait(timeout=60)
            self.errlog.seek(0)
            raise AssertionError(f"the one server did not start: {self.errlog.read()}")
        self.info = json.loads(line)
        return self.info

    def stop(self, kill=False):
        if self.proc is None:
            return
        if kill:
            self.proc.kill()
            self.proc.communicate(timeout=60)
        else:
            self.proc.communicate(input="", timeout=60)
        self.errlog.seek(0)
        self.logs.append(self.errlog.read())   # every log line the server wrote, for the sentinel checks
        self.errlog.close()
        self.proc = None

    def agent(self, method, path, body=None):
        """The agent door with the token of the project the path names (K4); `/health` and the rest go bare."""
        m = MS.PROJECT_PATH.match(path)
        return SV.agent_request(self.sock, method, path, body, token=self.tokens.get(m.group(1)) if m else None)

    def _csrf_for(self, name: str) -> str | None:
        """1.27.0: lazy-fetch and cache the per-project CSRF token from the served HTML config block."""
        if not hasattr(self, "_csrf_cache"):
            self._csrf_cache = {}
        if name in self._csrf_cache:
            return self._csrf_cache[name]
        import re as _re
        conn = http.client.HTTPConnection("127.0.0.1", self.info["ports"][name], timeout=30)
        conn.request("GET", "/", headers={"Cf-Access-Jwt-Assertion": token()})
        r = conn.getresponse(); raw = r.read().decode("utf-8", "replace"); conn.close()
        m = _re.search(r'"csrf":\s*"([^"]+)"', raw)
        self._csrf_cache[name] = m.group(1) if m else None
        return self._csrf_cache[name]

    def owner(self, name, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.info["ports"][name], timeout=30)
        h = {"Cf-Access-Jwt-Assertion": token()}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h.update({"Content-Type": "application/json", "Origin": f"https://{name}.example.com"})
            tok = self._csrf_for(name)
            if tok:
                h["X-Overture-CSRF"] = tok
        conn.request(method, path, body=data, headers=h)
        r = conn.getresponse()
        raw = r.read()
        conn.close()
        try:
            return r.status, json.loads(raw)
        except ValueError:
            return r.status, raw.decode()

    def push(self, name, items=None):
        items = items if items is not None else {"PUSHED": {"title": "pushed", "parent": None, "status": "open"}}
        return self.agent("POST", f"/p/{name}/items", {"items": items, "seed_questions": [], "board": None})

    def question(self, item, n, name="alpha"):
        return self.agent("POST", f"/p/{name}/question", {
            "qid": f"{item}/Q{n}", "item": item, "text": "Which?", "kind": "single",
            "options": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}], "star": "b", "valid_if": [],
            "source": "index.html:1", "nonce": f"k3q{item.lower()}{name}{n:04d}"})

    def violations(self):
        return self.audit.read_text().splitlines() if self.audit.exists() else []


class OneServerTests(_OneServer, unittest.TestCase):
    """K3 step 3: the one server's doors, items-push, and its rules (AC3.3, AC3.5, AC3.6)."""

    def test_each_projects_page_config_carries_the_running_kit_version(self):
        # The footer's version (owner, 2026-10-01) on the ONE server. Catches: a ProjectConsole.page() of its
        # own that builds the config without `version`; today it holds only because page() is inherited.
        import re
        from overture import __version__
        self.spawn()
        for name in ("alpha", "beta"):
            code, html = self.owner(name, "GET", "/")
            self.assertEqual(code, 200, name)
            m = re.search(r'<script type="application/json" id="overture-config">(.*?)</script>', html, re.S)
            self.assertIsNotNone(m, f"{name}: no config block in the served page")
            self.assertEqual(json.loads(m.group(1)).get("version"), __version__, name)

    def test_ac35_no_project_code_and_items_only_from_the_push(self):
        # Catches: importing the adapter (or reading its source and exec-ing a string), spawning git or anything
        # else, and validating writes against the adapter rather than the pushed snapshot.
        info = self.spawn()
        self.assertEqual(info["refused"], {})
        for name in ("alpha", "beta"):
            self.assertEqual(self.push(name)[0], 200)
            code, out = self.question("FROM_ADAPTER", 1, name)
            self.assertEqual(code, 400, out)                       # the adapter's item is not the pushed one
            self.assertEqual(self.question("PUSHED", 1, name)[0], 200)
            for path in ("/view", "/check", "/health"):
                self.assertIn(self.agent("GET", f"/p/{name}{path}")[0], (200, 503), path)
            for path, body in (("/message", {"item": "PUSHED", "text": "hi", "nonce": f"k3msg{name}01"}),
                               ("/cursor", {"last_synced_at": "2026-10-01T00:00:00Z", "last_error": None}),
                               ("/working", {"items": ["PUSHED"]}), ("/reanchor", {"dry_run": True})):
                self.assertEqual(self.agent("POST", f"/p/{name}{path}", body)[0], 200, path)
            for path in ("/", "/api/view", "/api/check", "/api/board", "/api/usage", "/api/feed",
                         "/api/wait?since=0&timeout=0", "/api/evidence?qid=PUSHED/Q1"):
                self.assertIn(self.owner(name, "GET", path)[0], (200, 404), path)
            self.assertEqual(self.owner(name, "POST", "/api/message", {"item": "PUSHED", "text": "owner",
                                                                       "nonce": f"k3own{name}01"})[0], 200)
            view = self.agent("GET", f"/p/{name}/view")[1]
            self.assertEqual(view["view"]["tags"]["git"], NOGIT)   # labelled (F63), the key the page renders
        self.assertEqual(self.agent("GET", "/p/alpha/check")[1]["history"], NOGIT)
        # The page reads the OWNER door: the same fields the single-project page renders (test_browser.py, the
        # Q23 tests) must arrive there, so the label reaches the screen by the one rendering path.
        self.assertEqual(self.owner("alpha", "GET", "/api/check")[1]["history"], NOGIT)
        self.assertEqual(self.owner("alpha", "GET", "/api/view")[1]["view"]["tags"]["git"], NOGIT)
        health = self.agent("GET", "/health")
        self.assertEqual(health[0], 200)
        self.assertNotIn("alpha", json.dumps(health[1]))        # it names no project
        self.stop()
        self.assertEqual(self.violations(), [])
        for v in self.p.values():
            self.assertFalse(v["marker"].exists())
        # The negative control: the same hook DOES see an adapter run and a git spawn in its process.
        probe = self.t / "probe.py"
        probe.write_text("import runpy, subprocess, sys\nrunpy.run_path(sys.argv[8])\n"
                         "subprocess.run(['git', '--version'], capture_output=True)\n")
        ctl = ONE_SERVER.split("sys.path.insert(0, kit)")[0] + "exec(open(sys.argv[7]).read())\n"
        ctl_log = self.t / "audit-control.log"
        r = subprocess.run([sys.executable, "-c", ctl, "k", "s", "x", "p", str(ctl_log), str(self.p["alpha"]["root"]),
                            str(probe), str(self.p["alpha"]["root"] / "console_adapter.py")],
                           capture_output=True, text=True, timeout=60)
        seen = ctl_log.read_text()
        self.assertIn("console_adapter.py", seen, r.stderr)
        self.assertIn("subprocess.Popen", seen)

    AGENT_GETS = ("/view", "/check", "/health", "/history-wants", "/no-such-route")
    AGENT_POSTS = ("/items", "/prs", "/issues", "/cursor", "/working", "/reanchor", "/visual", "/visual-export",
                   "/question", "/message", "/transcript", "/history-blob", "/history-specs", "/page-snapshot",
                   "/anchor-proposal", "/refactor-advice", "/playbook", "/item-move", "/no-such-route")

    def test_the_seam_is_the_only_admission_point(self):
        # K4 replaces `authorize` alone, so this is behaviour, not source text: with `authorize` refusing, EVERY
        # route of every project (served, refused, unknown) answers the one 403, and with it admitting, they don't.
        # Catches: any route (or a fault check) reached before the seam, and an unknown project told apart.
        self.assertEqual(set(self.AGENT_POSTS) >= set(SV.AGENT_ROUTES), True)   # the list keeps up with the table
        (self.p["beta"]["state"] / "store.jsonl").write_text('{"not": "a record"}\n')   # beta is a refused project
        self.spawn(ONE_SERVER.replace("r = MS.start(", "MS.authorize = lambda ms, project, headers: False\nr = MS.start("))
        for name in ("alpha", "beta", "nobody"):
            for path in self.AGENT_GETS:
                self.assertEqual(self.agent("GET", f"/p/{name}{path}"), (403, {"error": "forbidden"}), (name, path))
            for path in self.AGENT_POSTS:
                self.assertEqual(self.agent("POST", f"/p/{name}{path}", {}), (403, {"error": "forbidden"}),
                                 (name, path))
        self.assertEqual(self.agent("GET", "/health")[0], 200)   # the server's own liveness names no project
        self.stop()
        self.spawn()                                              # the control: admitted, no route is that 403
        for path in self.AGENT_GETS:
            self.assertNotEqual(self.agent("GET", f"/p/alpha{path}")[0], 403, path)
        for path in self.AGENT_POSTS:
            self.assertNotEqual(self.agent("POST", f"/p/alpha{path}", {})[0], 403, path)
        self.assertEqual(self.agent("GET", "/p/beta/view")[0], 503)
        self.assertEqual(self.agent("GET", "/p/nobody/view"), (403, {"error": "forbidden"}))

    def test_ac36_one_bad_store_is_503_there_and_a_kill_loses_nothing_acknowledged(self):
        # Catches: a server that refuses to start for one project's fault, and a restart test that writes nothing.
        (self.p["beta"]["state"] / "store.jsonl").write_text('{"not": "a record"}\n')
        info = self.spawn()
        self.assertEqual(list(info["refused"]), ["beta"])
        code, out = self.agent("GET", "/p/beta/view")             # the steward's own socket: named
        self.assertEqual(code, 503)
        self.assertIn("beta's console cannot open", out["error"])
        code, out = self.owner("beta", "GET", "/api/view")        # the browser: one body that names nothing
        self.assertEqual((code, out), (503, MS.FAULT_BODY))
        self.assertEqual(self.owner("alpha", "GET", "/api/view")[0], 200)
        self.assertEqual(self.push("alpha")[0], 200)
        seqs = []
        for n in (1, 2):
            code, out = self.question("PUSHED", n)
            self.assertEqual(code, 200, out)
            seqs.append(out["record"]["seq"])
        store = self.p["alpha"]["state"] / "store.jsonl"
        held = store.read_bytes()
        self.stop(kill=True)                                  # SIGKILL: no shutdown code runs
        self.assertEqual(store.read_bytes(), held)            # both acknowledged writes are on disk
        self.spawn()
        code, out = self.question("PUSHED", 3)
        self.assertEqual(code, 200, out)
        self.assertEqual(out["record"]["seq"], seqs[-1] + 1)   # seq continues
        self.assertTrue(store.read_bytes().startswith(held))  # append-only across the kill

    def test_ac33_a_fixture_copy_keeps_its_bytes_and_every_released_kit_starts_on_it_after(self):
        # Catches: hashing the live dir, and never starting the old kit afterwards (a new file it refuses
        # would go unseen until a rollback was needed).
        import shutil
        made = self.t / "made-by-old"
        kits = {tag: self.TK.old_kit(tag, self.t / f"kit-{tag}", ("plugin/kit",))
                for tag in self.TK.released_single_store_tags()}
        self.TK.start_old_server(kits[sorted(kits)[0]], self.t / "work-make", made, stay=False)
        fixture = self.p["alpha"]["state"]
        shutil.rmtree(fixture)
        shutil.copytree(made, fixture)                        # a COPY: every command runs against it
        def snapshot():   # EVERY path in the state dir, its kind and (for a file) its hash: not a chosen few
            out = {}
            for p in sorted(fixture.rglob("*")):
                rel = p.relative_to(fixture).as_posix()
                if p.is_symlink():
                    out[rel] = ("link", os.readlink(p))
                elif p.is_file():
                    out[rel] = ("file", hashlib.sha256(p.read_bytes()).hexdigest())
                else:
                    out[rel] = ("dir" if p.is_dir() else "other", None)
            return out
        before = snapshot()
        self.assertEqual(before["store.jsonl"][0], "file")
        (self.p["alpha"]["root"] / "console_adapter.py").write_text(
            "def items():\n    return {'LANE': {'title': 'a lane', 'parent': None, 'status': 'open'}}\n"
            "def seed_questions():\n    return []\n"
            "def record(entries, dry_run):\n    return []\n")
        env = {**os.environ, "XDG_CONFIG_HOME": str(self.cfg), "PYTHONDONTWRITEBYTECODE": "1"}
        env.pop("OVERTURE_AGENT", None)
        agent_py = str(HERE / "plugin" / "kit" / "agent.py")
        r = subprocess.run([sys.executable, agent_py, "--state", str(fixture), "server", "add", "alpha", "--hostname",
                            "alpha.example.com", "--aud", AUD, "--port", "4901", "--team-domain", TEAM],
                           capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.spawn()
        r = subprocess.run([sys.executable, agent_py, "--state", str(fixture), "items-push", "--adapter",
                            "console_adapter.py", "--project", str(self.p["alpha"]["root"])],
                           capture_output=True, text=True, env=env, timeout=60, cwd=self.p["alpha"]["root"])
        self.assertEqual(r.returncode, 0, r.stderr)
        for path in ("/view", "/check", "/health"):
            self.assertEqual(self.agent("GET", f"/p/alpha{path}")[0], 200, path)
        self.assertEqual(self.owner("alpha", "GET", "/api/view")[0], 200)
        self.stop()
        after = snapshot()
        self.assertEqual({k: v for k, v in after.items() if k in before}, before)   # nothing it had was touched
        # 1.20.1: the kit now also writes refs.json alongside items.json (dotted-number refs
        # assigned on items-push); the test's whitelist grew by one.
        self.assertEqual(sorted(set(after) - set(before)), ["items.json", "refs.json", "server.lock"])
        for tag, kit in kits.items():                          # the rollback: every released kit starts on it
            self.TK.start_old_server(kit, self.t / f"work-{tag}", fixture, stay=False)


class FaultIsolationTests(_OneServer, unittest.TestCase):
    """K3 review fixes: a project's bad entry, port or stored items is ITS fault (AC3.6), and a refused project's
    owner door keeps the Access gate and names nothing past it."""

    REAL_PORTS = "port_for=lambda h: 0"

    def rewrite(self, fn):
        doc = json.loads(self.sfile.read_text())
        fn(doc["projects"])
        self.sfile.write_text(json.dumps(doc))

    def owner_raw(self, name, path, headers):
        conn = http.client.HTTPConnection("127.0.0.1", self.info["ports"][name], timeout=30)
        conn.request("GET", path, headers=headers)
        r = conn.getresponse()
        raw = r.read()
        conn.close()
        return r.status, json.loads(raw)

    def alpha_serves(self):
        self.assertEqual(self.agent("GET", "/p/alpha/view")[0], 200)
        self.assertEqual(self.owner("alpha", "GET", "/api/view")[0], 200)

    def test_an_entry_with_no_port_refuses_that_project_and_the_server_starts(self):
        # Catches: start() reading entry["port"] for an entry entry_problems already refused (KeyError).
        self.rewrite(lambda p: p["beta"].pop("port"))
        info = self.spawn(ONE_SERVER.replace(self.REAL_PORTS, "port_for=lambda h: h.entry['port'] and 0"))
        self.assertEqual(list(info["refused"]), ["beta"])
        self.assertIn("port", info["refused"]["beta"])
        self.assertNotIn("beta", info["ports"])                  # no door is built from a broken entry
        self.assertEqual(self.agent("GET", "/p/beta/view")[0], 503)
        self.alpha_serves()

    def test_a_port_in_use_refuses_that_project_and_the_server_starts(self):
        # Catches: a bind's OSError escaping start() and taking every project down with it.
        import socket as so
        held = so.socket(so.AF_INET, so.SOCK_STREAM)
        try:
            held.bind(("127.0.0.1", 0))
            held.listen(1)
            free = so.socket(so.AF_INET, so.SOCK_STREAM)
            free.bind(("127.0.0.1", 0))
            alpha_port = free.getsockname()[1]
            free.close()
            busy = held.getsockname()[1]

            def ports(p):
                p["alpha"]["port"], p["beta"]["port"] = alpha_port, busy
            self.rewrite(ports)
            info = self.spawn(ONE_SERVER.replace(self.REAL_PORTS, "port_for=lambda h: h.entry['port']"))
            self.assertEqual(list(info["refused"]), ["beta"])
            self.assertIn(f"cannot listen on port {busy}", info["refused"]["beta"])
            self.assertEqual(info["ports"], {"alpha": alpha_port})
            self.assertEqual(self.agent("GET", "/p/beta/view")[0], 503)
            self.alpha_serves()
            self.stop()
        finally:
            held.close()                                          # the listener is released whatever happened

    def test_a_bad_stored_items_file_refuses_that_project_only(self):
        # Catches: open_project catching too few exception types ({"items": [1]} raised TypeError out of
        # MultiServer()), and a snapshot read without the push's own check. KEPT on purpose (lane 5 fix 3):
        # the one server refuses just that project, the others serve; only the SINGLE server, which has no
        # other project to keep serving, tolerates the file instead (SingleServerStartTests).
        for doc in ({"items": [1]}, {"items": {"X": {"title": 5, "parent": None}}},
                    {"items": {"X": {"title": "x", "parent": ["q"]}}}, "not an object", {"items": {}, "seed_questions": "q"},
                    {"items": {}, "seed_questions": [{"text": "no qid"}]}):
            (self.p["beta"]["state"] / "items.json").write_text(json.dumps(doc))
            info = self.spawn()
            self.assertEqual(list(info["refused"]), ["beta"], doc)
            self.assertIn("items.json", info["refused"]["beta"])
            self.alpha_serves()
            self.stop()

    def test_a_bad_push_is_refused_naming_the_field_and_nothing_is_kept(self):
        # Catches: a push accepted with a number title or a list parent, after which every payload() 500s.
        self.spawn()
        self.assertEqual(self.push("alpha")[0], 200)
        kept = (self.p["alpha"]["state"] / "items.json").read_bytes()
        for items, field in (({"X": {"title": 5, "parent": None}}, "items.X.title must be str, not int"),
                             ({"X": {"title": "x", "parent": ["q"]}}, "items.X.parent must be str or null, not list"),
                             ({"X": {"title": "x", "status": 3}}, "items.X.status must be str or null, not int"),
                             ({"X": {"parent": None}}, "items.X.title is missing"),
                             ({"X": {"title": "x", "owner": "me"}}, "items.X has unknown fields ['owner']")):
            code, out = self.push("alpha", items)
            self.assertEqual(code, 400, out)
            self.assertIn(field, out["error"])
            self.assertEqual((self.p["alpha"]["state"] / "items.json").read_bytes(), kept)
        self.assertEqual(self.agent("GET", "/p/alpha/view")[0], 200)

    def test_a_refused_projects_owner_door_keeps_the_gate_and_names_nothing(self):
        # Catches: the Access verify skipped on a refused project's door (its 503 then reaches anyone at the tunnel),
        # and a 503 body that names a path. Beta gets its own audience, so alpha's token is another project's.
        other_aud = "b" * 64
        self.rewrite(lambda p: p["beta"].update(aud=other_aud))
        (self.p["beta"]["state"] / "store.jsonl").write_text('{"not": "a record"}\n')
        info = self.spawn()
        self.assertEqual(list(info["refused"]), ["beta"])
        refusals = {}
        for case, headers in (("no token", {}),
                              ("wrong aud", {"Cf-Access-Jwt-Assertion": token(aud=["c" * 64])}),
                              ("another project's token", {"Cf-Access-Jwt-Assertion": token()}),
                              ("wrong key", {"Cf-Access-Jwt-Assertion": token(key=OTHER, aud=[other_aud])})):
            code, out = self.owner_raw("beta", "/api/view", headers)
            self.assertEqual(code, 403, case)
            self.assertNotIn("store", json.dumps(out), case)
            self.assertNotIn(str(self.t), json.dumps(out), case)
            refusals[case] = (code, out)
        # Alpha's own door refuses the same headers the same way: one gate, whatever the project's state.
        for case, headers in (("no token", {}), ("wrong aud", {"Cf-Access-Jwt-Assertion": token(aud=["c" * 64])})):
            self.assertEqual(self.owner_raw("alpha", "/api/view", headers), refusals[case], case)
        code, out = self.owner_raw("beta", "/api/view", {"Cf-Access-Jwt-Assertion": token(aud=[other_aud])})
        self.assertEqual((code, out), (503, MS.FAULT_BODY))      # past the gate: still names nothing
        self.stop()
        self.assertIn("beta's console cannot open", "\n".join(self.logs))   # the operator's reason is in the log


REFUSAL = "is not a plain file under the project root"


def naive_read(root: Path, rel: str) -> bytes:
    """NEGATIVE CONTROL: open the path as given."""
    return (Path(root) / rel).read_bytes()


def prefix_read(root: Path, rel: str) -> bytes:
    """NEGATIVE CONTROL: a check on the path string (`startswith root`) without resolving symlinks."""
    p = os.path.abspath(os.path.join(root, rel))
    if not p.startswith(str(root).rstrip("/") + "/"):
        raise PermissionError(rel)
    return Path(p).read_bytes()


def page_opens(trace: str, names: tuple[str, ...]) -> tuple[int, list[str]]:
    """(safe count, unsafe lines) among the open syscalls in a strace log that name one of `names`.

    Safe: openat/openat2 with a real dirfd (never AT_FDCWD), a path of ONE component (no "/"), and O_NOFOLLOW
    (openat2 shows RESOLVE_NO_SYMLINKS instead). Anything else naming the file or its folder is unsafe.
    """
    import re
    safe, unsafe = 0, []
    call = re.compile(r'^\d+\s+(open|openat|openat2)\((?:(AT_FDCWD|\d+),\s*)?"([^"]*)",\s*(.*)$')
    for line in trace.splitlines():
        m = call.match(line)
        if not m:
            continue
        sysc, dirfd, path, rest = m.groups()
        if not any(path == n or path.endswith("/" + n) or ("/" + n + "/") in path for n in names):
            continue
        ok = (sysc in ("openat", "openat2") and dirfd not in (None, "AT_FDCWD") and "/" not in path
              and ("O_NOFOLLOW" in rest or "RESOLVE_NO_SYMLINKS" in rest))
        if ok:
            safe += 1
        else:
            unsafe.append(line)
    return safe, unsafe


NO_STRACE = "OVERTURE_NO_STRACE"


def need_strace(test: unittest.TestCase) -> None:
    """Every syscall test starts here. Missing strace FAILS it, so the gap cannot hide as a quiet skip.

    `OVERTURE_NO_STRACE=1` is the explicit opt-out: the test is then skipped, BY NAME, saying it did not run.
    """
    import shutil
    if shutil.which("strace") is not None:
        return
    if os.environ.get(NO_STRACE) == "1":
        test.skipTest(f"strace is not installed and {NO_STRACE}=1: this syscall test did not run")
    test.fail(f"strace is not installed, so this syscall test cannot run: install strace, "
              f"or set {NO_STRACE}=1 to skip it by name")


class StraceGateTests(unittest.TestCase):
    """The syscall tests fail without strace unless explicitly opted out (lane 4 review gap c)."""

    def test_missing_strace_fails_and_the_opt_out_skips_by_name(self):
        # Catches: the gate skipping where strace is missing, as the K3 and lane 4 syscall tests did, so a
        # machine without strace reports OK and the dir-fd and AC3.8 assertions never ran.
        # Every outcome is turned into a value FIRST: a SkipTest escaping assertRaises would skip THIS test,
        # and a gate that skips would then pass its own test the same quiet way it hides the others.
        from unittest import mock
        probe = unittest.TestCase()

        def outcome() -> tuple[str, str]:
            try:
                need_strace(probe)
            except unittest.SkipTest as e:
                return "skip", str(e)
            except probe.failureException as e:
                return "fail", str(e)
            return "run", ""

        env = {k: v for k, v in os.environ.items() if k != NO_STRACE}
        with mock.patch("shutil.which", lambda name: None), mock.patch.dict(os.environ, env, clear=True):
            kind, why = outcome()
            self.assertEqual(kind, "fail", why)
            self.assertIn(f"set {NO_STRACE}=1", why)
            with mock.patch.dict(os.environ, {NO_STRACE: "1"}):
                kind, why = outcome()
            self.assertEqual(kind, "skip", why)
            self.assertIn("did not run", why)
            with mock.patch.dict(os.environ, {NO_STRACE: "yes"}):   # only the exact opt-out skips
                self.assertEqual(outcome()[0], "fail")
        with mock.patch("shutil.which", lambda name: "/usr/bin/strace"):
            self.assertEqual(outcome(), ("run", ""))                 # present: the test runs


WRITE_FAULT_CHILD = r'''
import errno, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from overture import stewardgit as SG
os.urandom = lambda n: b"\0" * n          # the temporary's name, fixed so the trace can be read for it
def full(*a, **k):
    raise OSError(errno.ENOSPC, "No space left on device")
os.rename = full                           # the write's rename fails, as on a full disk
f = SG._Folders.open(Path(sys.argv[2]), create=True)
try:
    f.write(f.blobs, "a" * 64, b"some bytes")
    print("WROTE")
except OSError as e:
    print("REFUSED", e.errno)
print(sorted(os.listdir(f.blobs)))
'''


class StewardGitWriteFaultTests(unittest.TestCase):
    """`_Folders.write`'s error path (lane 4 review gap b): the temporary is removed, through the held folder."""

    def test_a_failed_write_removes_its_temporary_relative_to_the_folder(self):
        # Catches: a temporary left behind when the rename fails, and one removed BY PATH (a link swapped in
        # for the folder would redirect that unlink). Run under strace with the rename made to fail: the
        # temporary is created and unlinked as one component relative to a held dirfd, and nothing is left.
        import errno
        import subprocess
        need_strace(self)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name)
        (d / "state").mkdir()
        (d / "child.py").write_text(WRITE_FAULT_CHILD)
        trace = d / "write.strace"
        r = subprocess.run(["strace", "-f", "-qq", "-e", "trace=%file", "-o", str(trace), sys.executable,
                            str(d / "child.py"), str(HERE / "plugin" / "kit"), str(d / "state")],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.split("\n")[:2], [f"REFUSED {errno.ENOSPC}", "[]"])   # failed, and nothing left
        name = ".tmp." + "00" * 8
        text = trace.read_text()
        safe, unsafe = dirfd_ops(text, (name,))
        self.assertEqual(unsafe, [])
        calls = [ln.split("(", 1)[0].split()[-1] for ln in text.splitlines() if f'"{name}"' in ln]
        self.assertEqual(calls, ["openat", "unlinkat"], text[-2000:])   # created, then removed, both dir-relative
        self.assertEqual(safe, 2)

    def test_the_temporary_is_fsynced_before_it_is_renamed_over_the_file(self):
        # Catches (lane 5 review, LOW): a rename with the bytes still in the page cache, so a crash just after it
        # can leave the NAME pointing at an empty or partial file. Records the order of fsync and rename on the
        # temporary's descriptor and name, for both callers' write (steward-git blobs and items.json).
        from unittest import mock
        from overture import atfile as AF
        real_fsync, real_rename, order = os.fsync, os.rename, []
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        folder = os.open(tmp.name, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, folder)

        def fsync(fd):
            order.append(("fsync", os.readlink(f"/proc/self/fd/{fd}").rsplit("/", 1)[-1]))
            return real_fsync(fd)

        def rename(src, dst, **kw):
            order.append(("rename", src))
            return real_rename(src, dst, **kw)
        with mock.patch("os.fsync", fsync), mock.patch("os.rename", rename):
            AF.write_at(folder, "f.json", b"{}")
        self.assertEqual([op for op, _ in order], ["fsync", "rename"], order)
        self.assertEqual(order[0][1], order[1][1])   # the same temporary: synced, then named
        self.assertEqual(Path(tmp.name, "f.json").read_bytes(), b"{}")


AT_CALLS = {"openat": "open", "openat2": "open", "newfstatat": "stat", "statx": "stat", "fstatat64": "stat",
            "mkdirat": "", "unlinkat": "", "renameat": "", "renameat2": "", "readlinkat": ""}


def dirfd_ops(trace: str, names: tuple[str, ...]) -> tuple[int, list[str]]:
    """(safe count, unsafe lines) among ALL file syscalls in a `strace -e trace=%file` log naming one of `names`.

    Safe: an *at call, every path it names given as ONE component (no "/") after a real dirfd (never AT_FDCWD);
    an open also carries O_NOFOLLOW (openat2: RESOLVE_NO_SYMLINKS), a stat AT_SYMLINK_NOFOLLOW. Any other call
    naming one of `names` (open, mkdir, rename, unlink, stat, lstat ...) is unsafe: it looks the path up again.
    """
    import re
    safe, unsafe = 0, []
    call = re.compile(r'^\d+\s+(\w+)\((.*)$')
    pair = re.compile(r'(?:^|,\s*)(AT_FDCWD|-?\d+),\s*"([^"]*)"')
    for line in trace.splitlines():
        m = call.match(line)
        if not m:
            continue
        sysc, args = m.groups()
        paths = re.findall(r'"([^"]*)"', args)
        if not any(p == n or p.endswith("/" + n) or ("/" + n + "/") in p for p in paths for n in names):
            continue
        pairs = pair.findall(args)
        ok = (sysc in AT_CALLS and len(pairs) == len(paths)
              and all(fd != "AT_FDCWD" and "/" not in p for fd, p in pairs))
        if ok and AT_CALLS[sysc] == "open":
            ok = "O_NOFOLLOW" in args or "RESOLVE_NO_SYMLINKS" in args
        if ok and AT_CALLS[sysc] == "stat":
            ok = "AT_SYMLINK_NOFOLLOW" in args
        if ok:
            safe += 1
        else:
            unsafe.append(line)
    return safe, unsafe


def check_then_open_read(root: Path, rel: str) -> bytes:
    """NEGATIVE CONTROL for the race: resolve, compare, then open the resolved path (two steps)."""
    p = os.path.realpath(os.path.join(root, rel))
    if not p.startswith(str(root).rstrip("/") + "/"):
        raise PermissionError(rel)
    with open(p, "rb") as fh:
        return fh.read()


class RootConfinementTests(_OneServer, unittest.TestCase):
    """K3 step 4: root reads are confined (AC3.7) and cannot race (AC3.8)."""

    def setUp(self):
        super().setUp()
        A = self.p["alpha"]
        self.SF.add("alpha", A["state"], "alpha.example.com", AUD, 4901, TEAM, page="pg/page.html",
                    registry=self.reg, path=self.sfile)
        (A["root"] / "pg").mkdir()
        (A["root"] / "pg" / "page.html").write_text("<html><body>ORDINARY-PAGE</body></html>\n")
        (A["root"] / "ok.txt").write_text("ORDINARY-OK a plain file inside the root\n")

    def plant(self):
        """Targets with a sentinel each, and the links in alpha's root that point at them."""
        A, B = self.p["alpha"], self.p["beta"]
        tokens = self.cfg / "overture" / "tokens"
        tokens.mkdir(parents=True, exist_ok=True)
        (tokens / "beta").write_text("SENTINEL-TOKEN-FILE-beta\n")
        (B["root"] / "secret.txt").write_text("SENTINEL-B-ROOT-FILE\n")
        (B["root"] / "pgb").mkdir()
        (B["root"] / "pgb" / "page.html").write_text("SENTINEL-B-ROOT-PAGE\n")
        (A["root"] / "inside.txt").write_text("SENTINEL-INSIDE-A-ROOT\n")
        doc = json.loads(self.sfile.read_text())
        doc["note"] = "SENTINEL-SERVER-JSON"
        self.sfile.write_text(json.dumps(doc))
        targets = {"b_store": (B["state"] / "store.jsonl", "SENTINEL-B-STORE"),
                   "a_store": (A["state"] / "store.jsonl", "SENTINEL-A-STORE"),
                   "b_token": (tokens / "beta", "SENTINEL-TOKEN-FILE-beta"),
                   "server_json": (self.sfile, "SENTINEL-SERVER-JSON"),
                   "b_root": (B["root"] / "secret.txt", "SENTINEL-B-ROOT-FILE")}
        host = Path("/etc/hostname")
        if host.is_file() and len(host.read_text().strip()) >= 10:   # the excerpt is [1:-1] and needs 8+
            targets["etc_hostname"] = (host, host.read_text().strip())
        self.cases = []   # (case, path in alpha's root, sentinel)
        for i, (case, (target, sentinel)) in enumerate(targets.items()):
            (A["root"] / f"t{i}").symlink_to(target)
            self.cases.append((case, f"t{i}", sentinel))
        (A["root"] / "mid").symlink_to(B["root"])              # a symlinked directory in the middle
        self.cases.append(("middle_dir", "mid/secret.txt", "SENTINEL-B-ROOT-FILE"))
        (A["root"] / "inlink").symlink_to(A["root"] / "inside.txt")   # a symlink pointing inside the root
        self.cases.append(("inside_link", "inlink", "SENTINEL-INSIDE-A-ROOT"))
        (A["root"] / "loopa").symlink_to(A["root"] / "loopb")
        (A["root"] / "loopb").symlink_to(A["root"] / "loopa")
        self.cases.append(("loop", "loopa", None))
        self.cases.append(("dotdot", "../root-beta/secret.txt", "SENTINEL-B-ROOT-FILE"))
        self.sentinels = {s for _, _, s in self.cases if s} | {"SENTINEL-B-ROOT-PAGE"}

    def point_page(self, case: str, rel: str) -> None:
        """Make alpha's host page `pg/page.html` lead where the case's link leads."""
        A = self.p["alpha"]
        pg = A["root"] / "pg"
        if pg.is_symlink():
            pg.unlink()
        elif pg.exists():
            for f in pg.iterdir():
                f.unlink()
            pg.rmdir()
        if case == "middle_dir":
            pg.symlink_to(self.p["beta"]["root"] / "pgb")       # the middle component is the link
            return
        pg.mkdir()
        if case == "loop":
            (pg / "page.html").symlink_to(pg / "page2.html")
            (pg / "page2.html").symlink_to(pg / "page.html")
        elif case == "dotdot":
            (pg / "page.html").symlink_to("../../root-beta/secret.txt")   # a relative link out
        else:
            (pg / "page.html").symlink_to(os.readlink(A["root"] / rel))

    def test_ac37_every_planted_link_is_refused_alike_and_no_sentinel_leaves(self):
        # Catches: a `startswith root` string check (every link passes it), and a check of only the final
        # component (the symlinked middle directory passes it). Both ship below as named negative controls.
        self.spawn()
        for name, sentinel in (("alpha", "SENTINEL-A-STORE"), ("beta", "SENTINEL-B-STORE")):
            self.assertEqual(self.push(name)[0], 200)
            code, out = self.agent("POST", f"/p/{name}/message", {"item": "PUSHED", "text": sentinel,
                                                                  "nonce": f"k3sent{name}01"})
            self.assertEqual(code, 200, out)
        self.plant()
        seen: list[str] = []
        for n, (case, rel, sentinel) in enumerate(self.cases, 1):
            # As the host page: since Q28 it is never read from the root at all, so whatever it points at, the door
            # serves the kit-only page (no snapshot was taken) and never the link's target.
            self.point_page(case, rel)
            code, out = self.owner("alpha", "GET", "/")
            seen.append(json.dumps(out))
            self.assertEqual(code, 200, case)
            self.assertIn(html_escape(PS.NO_SNAPSHOT), out, case)
            # As an anchor's file: an evidence row is read when the question is asked.
            code, out = self.question_with(n, evidence=[{"cite": f"{rel}:1"}])
            seen.append(json.dumps(out))
            self.assertEqual(code, 400, case)
            if case != "dotdot":                              # '..' is refused by the cite grammar, before any read
                self.assertIn(f"{rel} {REFUSAL}", out["error"], case)
            # As a valid_if path: the excerpt is the sentinel minus its ends, so a followed link would HOLD, and the
            # whole sentinel can appear in a reply only if the file's bytes did (the reply echoes the excerpt).
            code, out = self.question_with(100 + n, valid_if=[{"kind": "excerpt", "path": rel,
                                                                "text": (sentinel or "-SENTINEL-LOOP-NEVER-")[1:-1]}])
            seen.append(json.dumps(out))
            if code == 200:
                self.lock(f"PUSHED/Q{100 + n}")
                code, chk = self.agent("GET", "/p/alpha/check")
                seen.append(json.dumps(chk))
                self.assertIn(f"{rel} {REFUSAL}", json.dumps(chk), case)
            else:
                self.assertEqual(case, "dotdot", out)          # only the grammar's own refusal comes earlier
        # After the loop and all the rest, everything else still serves, and an ordinary file is still read.
        pg = self.p["alpha"]["root"] / "pg"
        if pg.is_symlink():
            pg.unlink()
        else:
            for f in pg.iterdir():
                f.unlink()
            pg.rmdir()
        pg.mkdir()
        (pg / "page.html").write_text("<html><body>SENTINEL-ORDINARY-PAGE</body></html>\n")   # a plain page, too
        self.sentinels.add("SENTINEL-ORDINARY-PAGE")
        code, html = self.owner("alpha", "GET", "/")
        seen.append(html)
        self.assertEqual(code, 200)
        self.assertIn(html_escape(PS.NO_SNAPSHOT), html)
        self.assertEqual(self.question_with(999, evidence=[{"cite": "ok.txt:1"}])[0], 200)
        self.assertEqual(self.agent("GET", "/p/beta/view")[0], 200)
        self.assertEqual(self.owner("alpha", "GET", "/api/view")[0], 200)
        self.stop()
        blob = "\n".join(seen + self.logs)
        for s in self.sentinels:
            self.assertNotIn(s, blob)
        self.assertNotIn("alpha.example.com", "\n".join(self.logs))   # nothing of server.json in a log line

    def test_ac37_negative_controls_leak_where_the_real_reader_refuses(self):
        # The counter-check: the same planted tree read three ways, in this process.
        from overture import rootfs as RF
        self.spawn()
        for name, sentinel in (("alpha", "SENTINEL-A-STORE"), ("beta", "SENTINEL-B-STORE")):
            self.push(name)
            self.agent("POST", f"/p/{name}/message", {"item": "PUSHED", "text": sentinel, "nonce": f"k3ctl{name}01"})
        self.stop()
        self.plant()
        root = self.p["alpha"]["root"]
        RF.hold(root)

        def leaks(reader):
            out = []
            for case, rel, sentinel in self.cases:
                try:
                    data = reader(root, rel)
                except (OSError, ValueError):
                    continue
                if sentinel and sentinel.encode() in data:
                    out.append(case)
            return out
        naive = leaks(naive_read)
        self.assertEqual(naive[:1], [self.cases[0][0]])          # fails on its FIRST planted link
        symlinks = [c for c, _, s in self.cases if s and c != "dotdot"]
        self.assertEqual(sorted(leaks(prefix_read)), sorted(symlinks))   # every symlink case passes a string check
        self.assertEqual(leaks(lambda r, p: RF.read(r, p, 1 << 20)), [])
        self.assertEqual(RF.read(root, "ok.txt", 1 << 20), b"ORDINARY-OK a plain file inside the root\n")

    def question_with(self, n, **fields):
        body = {"qid": f"PUSHED/Q{n}", "item": "PUSHED", "text": "Which?", "kind": "single",
                "options": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}], "star": "b", "valid_if": [],
                "source": "ok.txt:1", "nonce": f"k3conf{n:05d}", **fields}
        return self.agent("POST", "/p/alpha/question", body)

    def lock(self, qid):
        code, a = self.owner("alpha", "POST", "/api/answer", {"qid": qid, "picks": ["a"], "own_text": "",
                                                              "nonce": f"ans{qid.split('Q')[1]:0>9}"})
        self.assertEqual(code, 200, a)
        code, lk = self.owner("alpha", "POST", "/api/lock", {"qid": qid, "answer": a["record"]["id"],
                                                             "nonce": f"lck{qid.split('Q')[1]:0>9}"})
        self.assertEqual(code, 200, lk)

    def test_ac38_a_swapped_parent_never_serves_the_other_side(self):
        # Catches: realpath-then-open (passes AC3.7, loses this race) and O_NOFOLLOW on the last component only.
        from overture import rootfs as RF
        A, B = self.p["alpha"], self.p["beta"]
        (B["state"] / "page.html").write_text("SENTINEL-RACE-B-STATE\n")
        self.SF.add("alpha", A["state"], "alpha.example.com", AUD, 4901, TEAM, page="race/page.html",
                    registry=self.reg, path=self.sfile)
        race, real = A["root"] / "race", A["root"] / "race.real"
        race.mkdir()
        (race / "page.html").write_text("<html><body>ORDINARY-RACE</body></html>\n")
        stop = threading.Event()

        def swap():
            while not stop.is_set():
                try:
                    os.rename(race, real)
                    os.symlink(B["state"], race)
                    os.unlink(race)
                    os.rename(real, race)
                except OSError:
                    pass

        def run(reader, n, deadline):
            leaked = served = refused = 0
            t0 = time.monotonic()
            for _ in range(n):
                if time.monotonic() - t0 > deadline:
                    break
                try:
                    data = reader()
                except (OSError, ValueError):
                    refused += 1
                    continue
                if b"SENTINEL-RACE-B-STATE" in data:
                    leaked += 1
                else:
                    served += 1
            return leaked, served, refused
        RF.hold(A["root"])
        th = threading.Thread(target=swap, daemon=True)
        th.start()
        try:
            # The named negative control must lose the race, or the swap is too slow to prove anything.
            leaked, _, _ = run(lambda: check_then_open_read(A["root"], "race/page.html"), 200_000, 20.0)
            if leaked == 0:
                self.fail("inconclusive: the check-then-open control never served B's sentinel, so the swap loop "
                          "is too slow to prove the real reader safe")
            self.assertEqual(run(lambda: RF.read(A["root"], "race/page.html", 1 << 20), 200_000, 20.0)[0], 0)
            # And through the server's own door: since Q28 the host page is never read from the root, so the door
            # serves neither side of the race, only the kit-only page.
            stop.set()
            th.join()
            if real.exists():
                race.unlink(missing_ok=True) if race.is_symlink() else None
                os.rename(real, race)
            self.spawn()
            stop.clear()
            th = threading.Thread(target=swap, daemon=True)
            th.start()
            for _ in range(500):
                code, body = self.owner("alpha", "GET", "/")
                self.assertEqual(code, 200, body)
                self.assertNotIn("SENTINEL-RACE-B-STATE", body)
                self.assertNotIn("ORDINARY-RACE", body)
                self.assertIn(html_escape(PS.NO_SNAPSHOT), body)
        finally:
            stop.set()
            th.join()

    def test_ac38_every_open_of_a_root_file_is_dir_relative_and_follows_no_link(self):
        # The syscall half of AC3.8: the server, run under strace, opens a cited file's folder and the file itself
        # only RELATIVE to a held descriptor (openat with a real dirfd, a single component, never AT_FDCWD or a
        # path with a "/") and with O_NOFOLLOW. Catches: a reader that checks safely and then opens by path,
        # which the race above catches only when it happens to lose. NEGATIVE CONTROL: the same parser must
        # flag a plain open() of the same file, or a parser that sees nothing would pass anything.
        # Since Q28 the host page is not read from the root at all, so the probe is an evidence cite (read when
        # the question is asked), and the host page's own name must appear in no open at all.
        import subprocess
        need_strace(self)
        A = self.p["alpha"]
        self.SF.add("alpha", A["state"], "alpha.example.com", AUD, 4901, TEAM, page="host/index.html",
                    registry=self.reg, path=self.sfile)
        (A["root"] / "race").mkdir()
        (A["root"] / "race" / "page.html").write_text("ORDINARY-RACE a cited file\n")
        (A["root"] / "host").mkdir()
        (A["root"] / "host" / "index.html").write_text("<html><body>HOST-PAGE</body></html>\n")
        trace = self.t / "server.strace"
        self.spawn(prefix=("strace", "-f", "-qq", "-e", "trace=open,openat,openat2", "-o", str(trace)))
        self.assertEqual(self.push("alpha")[0], 200)
        for n in range(20):
            code, body = self.question_with(500 + n, evidence=[{"cite": "race/page.html:1"}])
            self.assertEqual(code, 200, body)
            code, body = self.owner("alpha", "GET", "/")
            self.assertEqual(code, 200, body)
            self.assertNotIn("HOST-PAGE", body)
        self.stop()
        text = trace.read_text()
        safe, unsafe = page_opens(text, ("race", "page.html"))
        self.assertEqual(unsafe, [])
        self.assertGreaterEqual(safe, 40)                     # the folder and the file, each of the 20 times
        self.assertEqual(page_opens(text, ("host", "index.html")), (0, []))   # Q28: the host page, never opened
        ctl = self.t / "control.strace"
        subprocess.run(["strace", "-f", "-qq", "-e", "trace=open,openat,openat2", "-o", str(ctl), sys.executable,
                        "-c", f"open({str(A['root'] / 'race' / 'page.html')!r}).read()"], check=True, timeout=60)
        self.assertEqual(len(page_opens(ctl.read_text(), ("race", "page.html"))[1]), 1)   # the control is caught


class StewardGitTests(_Live, unittest.TestCase):
    """CONSOLE-kit/Q23 part 2, "restore it agent-side": git runs in the steward, the server gets DATA.

    Same project as NoServerGitTests (a git work tree whose spec was committed,
    locked against, then committed again), the seam closed as `serve` closes it.
    Each feature is shown both ways: the steward's data when its key matches,
    the existing label when it does not.
    """

    setUp = NoServerGitTests.setUp

    def push(self):
        return steward_push(self.console, self.root)

    def blob_dir(self):
        return self.cfg.state / "steward-git" / "blobs"

    def condition(self):
        code, out = self.req("GET", "/api/check", tok=token())
        self.assertEqual(code, 200, out)
        [c] = out["stale"]["LANE.1/Q2"]["conditions"]
        return out, c

    def refine(self):
        tags = self.req("GET", "/api/view", tok=token())[1]["view"]["tags"]
        [r] = [t for t in tags["questions"]["LANE.1/Q2"] if t["step"] == "refine"]
        return tags, r["reason"]

    # -- what the server asks for -------------------------------------------------------------

    def test_the_server_asks_only_for_what_its_own_store_and_tree_name(self):
        code, want = SV.agent_request(self.cfg.socket, "GET", "/history-wants")
        self.assertEqual(code, 200, want)
        self.assertEqual(want["blobs"], [{"path": "specs/spec.md", "sha256": self.v1}])
        st = (self.root / "specs/spec.md").stat()
        self.assertEqual(want["specs"], [{"path": "specs/spec.md", "mtime_ns": st.st_mtime_ns, "size": st.st_size}])

    def test_a_malformed_condition_is_skipped_not_a_500(self):
        # Catches (lane 4 review, LOW): wants() indexing c["sha256"] and calling .get on every condition with
        # none of named_shas' isinstance guards, so one malformed condition turned /history-wants into a 500.
        from unittest import mock
        from overture import anchors as A
        real = A.conditions_for
        junk = ["not a dict", None, {"kind": "file_sha256"}, {"kind": "file_sha256", "sha256": 5, "path": "x"},
                {"kind": "file_sha256", "sha256": "a" * 64, "path": ["specs/spec.md"]}]
        with mock.patch.object(A, "conditions_for", lambda store, q: (junk + list(real(store, q)[0]),
                                                                       real(store, q)[1])):
            code, want = SV.agent_request(self.cfg.socket, "GET", "/history-wants")
        self.assertEqual(code, 200, want)
        self.assertEqual(want["blobs"], [{"path": "specs/spec.md", "sha256": self.v1}])

    # -- 1. check history ----------------------------------------------------------------------

    def test_check_history_returns_from_the_steward_and_the_label_without_it(self):
        # Before: the label. After a push: the server's OWN verdict on the cited lines, from a verified blob.
        out, c = self.condition()
        self.assertEqual((out["history"], c["history"]), (NOGIT, NOGIT))
        self.push()
        out, c = self.condition()
        commit = subprocess_git(self.root, "rev-list", "--max-parents=0", "HEAD")
        self.assertTrue(out["history"].startswith("from the steward, pushed "), out["history"])
        self.assertEqual((c["reason"], c["history"], c["cited_text"], c["locked_version"]),
                         ("file_changed", "from the steward", "unchanged", commit[:12]))
        self.assertIn("came from the steward, checked against the lock's hash", c["words"])
        self.assertNotIn(NOGIT, c["words"])

    def test_a_blob_that_does_not_hash_to_its_key_is_refused_and_the_label_stays(self):
        # Catches: storing what the steward says without re-hashing it.
        good = self.push()["blobs"][0]
        __import__("shutil").rmtree(self.blob_dir())
        lie = {**good, "content_b64": __import__("base64").b64encode(b"# Spec\nsomething else\n").decode()}
        code, out = SV.agent_request(self.cfg.socket, "POST", "/history-blob", lie)
        self.assertEqual(code, 400, out)
        self.assertIn(f"does not hash to {self.v1}", out["error"])
        self.assertFalse((self.blob_dir() / self.v1).exists())
        self.assertEqual(self.condition()[1]["history"], NOGIT)

    def test_a_blob_changed_on_disk_after_the_push_proves_nothing(self):
        # Catches: trusting the stored file on read (the hash is checked again every time it is used).
        self.push()
        (self.blob_dir() / self.v1).write_text("# Spec\n\nThe cited claim, line one.\nThe cited claim, line two.\n")
        self.assertEqual(self.condition()[1]["history"], NOGIT)

    def test_a_blob_no_current_lock_names_is_neither_stored_nor_kept(self):
        # Catches: a store that fills with whatever is pushed, and a prune that never runs.
        import base64, hashlib
        data = b"a version no lock was taken against\n"
        sha = hashlib.sha256(data).hexdigest()
        code, out = SV.agent_request(self.cfg.socket, "POST", "/history-blob",
                                     {"sha256": sha, "commit": "a" * 40, "content_b64": base64.b64encode(data).decode()})
        self.assertEqual(code, 400, out)
        self.assertIn(f"no current lock names {sha}", out["error"])
        self.assertFalse((self.blob_dir() / sha).exists())
        self.push()
        planted = self.blob_dir() / sha             # one that got there some other way
        planted.write_bytes(data)
        self.console.push_history_specs({"specs": []})
        self.assertFalse(planted.exists())
        self.assertTrue((self.blob_dir() / self.v1).exists())

    def test_a_client_sent_path_never_names_a_file(self):
        # Catches: a blob stored under a name the client chose (the closed schema refuses the key outright).
        good = self.push()["blobs"][0]
        __import__("shutil").rmtree(self.blob_dir())
        code, out = SV.agent_request(self.cfg.socket, "POST", "/history-blob",
                                     {**good, "path": "../../escaped"})
        # First what reached the disk, then the status: a server that took the path writes `escaped` somewhere.
        self.assertEqual(sorted(str(p) for p in self.cfg.state.parent.rglob("escaped*")), [])
        self.assertEqual(code, 400, out)
        self.assertEqual(self.condition()[1]["history"], NOGIT)

    def test_the_stored_versions_have_a_total_cap(self):
        # Catches: a per-blob cap with no total, so many locks fill the state disk.
        from unittest import mock
        from overture import stewardgit as SG
        good = steward_push(self.console, self.root)["blobs"][0]
        (self.blob_dir() / ("f" * 64)).write_bytes(b"x" * 100)   # what the folder already holds, for a live lock
        real = SG.named_shas
        also = mock.patch.object(SG, "named_shas", lambda store: {**real(store), "f" * 64: "specs/other.md"})
        size = (self.blob_dir() / self.v1).stat().st_size
        with also, mock.patch.object(SG, "MAX_STORED", size + 99):
            code, out = SV.agent_request(self.cfg.socket, "POST", "/history-blob", good)
        self.assertEqual(code, 400, out)
        self.assertIn(f"would pass {size + 99} bytes", out["error"])
        with also, mock.patch.object(SG, "MAX_STORED", size + 100):   # the control: exactly at the cap is allowed
            self.assertEqual(SV.agent_request(self.cfg.socket, "POST", "/history-blob", good)[0], 200)

    def test_blobs_no_lock_names_never_count_against_the_cap(self):
        # Catches (lane 4 review, MEDIUM): a cap that counts dead blobs, so versions whose locks have moved on
        # fill it and every later push of a NAMED blob is refused until a specs push happens to prune them.
        from unittest import mock
        from overture import stewardgit as SG
        good = steward_push(self.console, self.root)["blobs"][0]
        size = (self.blob_dir() / self.v1).stat().st_size
        (self.blob_dir() / self.v1).unlink()
        dead = [self.blob_dir() / (c * 64) for c in "abcd"]
        for p in dead:
            p.write_bytes(b"x" * size)                            # four dead versions, together 4x the cap
        with mock.patch.object(SG, "MAX_STORED", size):
            code, out = SV.agent_request(self.cfg.socket, "POST", "/history-blob", good)
        self.assertEqual(code, 200, out)
        self.assertEqual(sorted(p.name for p in self.blob_dir().iterdir()), [self.v1])
        self.assertEqual(self.condition()[1]["history"], "from the steward")

    def test_a_symlink_planted_where_a_blob_goes_is_not_followed(self):
        # Catches: opening a blob by name with the link followed. The target holds the TRUE v1 bytes, so
        # only O_NOFOLLOW (not the hash) stands between it and the panel.
        self.push()
        outside = self.root.parent / f"{self.root.name}-v1-outside"
        outside.write_bytes((self.blob_dir() / self.v1).read_bytes())
        self.addCleanup(lambda: outside.unlink(missing_ok=True))
        (self.blob_dir() / self.v1).unlink()
        (self.blob_dir() / self.v1).symlink_to(outside)
        self.assertEqual(self.condition()[1]["history"], NOGIT)

    def test_a_fifo_planted_where_a_blob_goes_is_refused_at_once(self):
        # Catches (lane 5 review, MEDIUM): the blob read waiting forever on a FIFO in the blob's place (no
        # O_NONBLOCK), stalling the check; and a second check queued behind the first.
        self.push()
        (self.blob_dir() / self.v1).unlink()
        planted_fifo(self, self.blob_dir() / self.v1)
        for attempt in ("first", "second"):
            got = within(self, lambda: self.condition()[1]["history"], seconds=10)
            self.assertEqual(got.get("value"), NOGIT, attempt)

    def test_a_symlink_planted_where_the_steward_folder_goes_is_refused_and_never_read(self):
        # Catches: opening STATE/steward-git (or blobs/) with the link followed. The link's target holds the TRUE
        # blob and index, so only O_NOFOLLOW on the folder stands between them and the panel, and a push must not
        # write through it either.
        import shutil
        good = self.push()["blobs"][0]
        outside = self.root.parent / f"{self.root.name}-sg-outside"
        self.addCleanup(lambda: shutil.rmtree(outside, ignore_errors=True))
        shutil.move(str(self.cfg.state / "steward-git"), str(outside))
        (self.cfg.state / "steward-git").symlink_to(outside)
        before = sorted(p.name for p in (outside / "blobs").iterdir())
        self.assertEqual(self.condition()[1]["history"], NOGIT)
        code, out = SV.agent_request(self.cfg.socket, "POST", "/history-blob", good)
        self.assertEqual(code, 400, out)
        self.assertIn("steward-git in the state folder is not a plain folder", out["error"])
        self.assertEqual(sorted(p.name for p in (outside / "blobs").iterdir()), before)

    def test_an_index_nested_past_the_decoders_depth_reads_as_none(self):
        # Catches (lane 4 review, LOW): a planted index.json nested deeper than json.loads recurses, whose
        # RecursionError escaped `except ValueError` and turned every check and view into a 500.
        self.push()
        (self.cfg.state / "steward-git" / "index.json").write_text("[" * 100_000 + "]" * 100_000)
        out, c = self.condition()                                 # 200, not a 500
        self.assertEqual((c["history"], c["locked_version"]), ("from the steward", None))   # the blob, no commit
        self.assertEqual(self.req("GET", "/api/view", tok=token())[0], 200)

    def test_a_commit_id_in_any_other_shape_is_dropped(self):
        good = self.push()["blobs"][0]
        code, out = SV.agent_request(self.cfg.socket, "POST", "/history-blob", {**good, "commit": "HEAD; rm -rf"})
        self.assertEqual((code, out["commit"]), (200, None), out)
        _, c = self.condition()
        self.assertIsNone(c["locked_version"])
        self.assertIn("from the steward", c["history"])
        self.assertNotIn("rm -rf", json.dumps(c))

    # -- 2. reanchor ------------------------------------------------------------------------

    def test_reanchor_is_planned_by_the_server_from_the_blob_and_written(self):
        # The agent sends only {"dry_run": false}; the replacement excerpt is computed here, from a verified blob,
        # and re-checked against the tree before the write (still_supported). Without a push: the label.
        code, out = SV.agent_request(self.cfg.socket, "POST", "/reanchor", {"dry_run": False})
        self.assertEqual((code, out["history"]), (200, NOGIT))
        self.assertIn(NOGIT, out["plan"][0]["unresolved"][0]["why"])
        self.push()
        code, out = SV.agent_request(self.cfg.socket, "POST", "/reanchor", {"dry_run": False})
        self.assertEqual(code, 200, out)
        [p] = out["plan"]
        self.assertIn("record", p)
        self.assertEqual(p["anchors"], [{"kind": "excerpt", "path": "specs/spec.md",
                                         "text": "The cited claim, line one.\nThe cited claim, line two."}])
        self.assertIn("(from the steward)", p["changes"][0]["why"])
        self.assertEqual(self.req("GET", "/api/view", tok=token())[1]["view"]["questions"]["LANE.1/Q2"]["state"],
                         "locked")

    # -- 3. refine tags ---------------------------------------------------------------------

    def test_refine_shows_the_last_commit_while_its_key_matches_and_the_label_after(self):
        tags, why = self.refine()
        self.assertEqual(tags["git"], NOGIT)
        self.assertIn(f"file time 2020-09-13T12:26:40Z; the last-commit time is {NOGIT}", why)
        self.push()
        head = subprocess_git(self.root, "rev-parse", "HEAD")
        ct = int(subprocess_git(self.root, "log", "-1", "--format=%ct", "--", "specs/spec.md"))
        tags, why = self.refine()
        self.assertEqual(tags["git"], "from the steward")
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ct))
        self.assertIn(f"last commit {stamp} (from the steward, HEAD {head[:12]})", why)
        # The key moves (the file is touched, same bytes): the pushed time is never shown for it again.
        os.utime(self.root / "specs/spec.md", (1_600_000_100, 1_600_000_100))
        tags, why = self.refine()
        self.assertEqual(tags["git"], NOGIT)
        self.assertIn(f"the last-commit time is {NOGIT}", why)
        self.assertNotIn("from the steward", why)

    def test_a_spec_time_the_server_did_not_ask_for_is_ignored(self):
        # Catches: storing a record for a path the client chose, or for a stat the server does not see.
        st = (self.root / "specs/spec.md").stat()
        rec = {"path": "specs/spec.md", "mtime_ns": st.st_mtime_ns, "size": st.st_size, "head": "b" * 40,
               "edited": 1}
        out = self.console.push_history_specs({"specs": [{**rec, "path": "specs/other.md"},
                                                         {**rec, "size": st.st_size + 1}]})
        self.assertEqual((out["specs"], out["ignored"]), (0, ["specs/other.md", "specs/spec.md"]))
        self.assertIn(f"the last-commit time is {NOGIT}", self.refine()[1])
        code, out = SV.agent_request(self.cfg.socket, "POST", "/history-specs", {"specs": [{**rec, "path": "../x"}]})
        self.assertEqual(code, 400, out)

    # -- the steward's own command, and the server starting nothing --------------------------

    def test_history_push_from_the_cli_in_its_own_process(self):
        # The real `agent.py history-push`, a separate process (git allowed there), against this server.
        from overture import registry as R
        reg = self.root.parent / f"{self.root.name}-cfg" / "overture" / "projects.json"
        R.register(self.root, self.cfg.state, Path(SV.__file__).resolve().parent.parent, path=reg)
        self.addCleanup(lambda: __import__("shutil").rmtree(reg.parent.parent, ignore_errors=True))
        env = {**os.environ, "XDG_CONFIG_HOME": str(reg.parent.parent)}
        env.pop("OVERTURE_AGENT", None)
        r = subprocess.run([sys.executable, str(Path(SV.__file__).resolve().parent.parent / "agent.py"),
                            "--state", str(self.cfg.state), "history-push", "--project", str(self.root)],
                           capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        got = json.loads(r.stdout)
        self.assertEqual((got["blobs"], got["specs"]["specs"], got["refused"]),
                         ({"asked": 1, "sent": 1, "not_in_history": 0}, 1, []))
        self.assertEqual(self.condition()[1]["history"], "from the steward")

    def test_the_served_server_starts_no_process_with_steward_data_in_hand(self):
        # The real `serve`, an audit hook on every spawn event, every route that once reached git, AFTER a
        # push. Catches a server that, given the steward's data, goes to git anyway (or instead).
        root = Path(tempfile.mkdtemp(prefix="ck-audit-push-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        v1 = git_project(root)
        (root / "page.html").write_text(PAGE)
        (root / "adapter.py").write_text(
            "def items():\n    return {'LANE': {'title': 'a lane', 'parent': None, 'status': 'open'},\n"
            "            'LANE.1': {'title': 'a phase', 'parent': 'LANE', 'status': 'open'}}\n"
            "def seed_questions():\n    return []\n"
            "def record(entries, dry_run):\n    return []\n")
        st = (root / "specs/spec.md").stat()
        from overture import stewardgit as SG
        with seam_open():
            blobs, specs = SG.collect(root, {"blobs": [{"path": "specs/spec.md", "sha256": v1}],
                                             "specs": [{"path": "specs/spec.md", "mtime_ns": st.st_mtime_ns,
                                                        "size": st.st_size}]})
        params = {"kit": str(Path(SV.__file__).resolve().parent.parent), "root": str(root), "host": HOSTNAME,
                  "question": spec_question(v1), "push": {"blobs": blobs, "specs": specs}}
        r = subprocess.run([sys.executable, "-c", AUDIT_CHILD, json.dumps(params), json.dumps(SPAWN_EVENTS)],
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        got = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertEqual(got["spawned"], [], "the server started a process")
        for name, code in got["codes"].items():
            self.assertLess(code, 500, name)
        self.assertEqual(got["codes"]["agent POST /history-blob"], 200)
        out = got["out"]
        self.assertEqual(out["specs"]["specs"], 1)
        [c] = out["check"]["stale"]["LANE.1/Q2"]["conditions"]
        self.assertEqual((c["history"], c["cited_text"]), ("from the steward", "unchanged"))
        self.assertEqual(out["view"]["view"]["tags"]["git"], "from the steward")
        self.assertIn("record", out["reanchor"]["plan"][0])


def subprocess_git(root: Path, *args) -> str:
    import subprocess
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True).stdout.strip()


class OneServerStewardGitTests(_OneServer, unittest.TestCase):
    """The same data reaches the one server (K3) through /p/<name>/, with the audit hook watching it."""

    def test_history_push_reaches_the_one_server_and_it_starts_nothing(self):
        import subprocess
        alpha = self.p["alpha"]
        v1 = git_project(alpha["root"])
        legacy_ask(alpha["state"] / "store.jsonl", spec_question(v1))   # R1 refuses this shape on a new ask
        self.spawn()
        items = {"LANE": {"title": "a lane", "parent": None, "status": "open"},
                 "LANE.1": {"title": "a phase", "parent": "LANE", "status": "open"}}
        self.assertEqual(self.push("alpha", items)[0], 200)
        code, a = self.owner("alpha", "POST", "/api/answer", {"qid": "LANE.1/Q2", "picks": ["a"], "own_text": "",
                                                             "nonce": "k3gitanswer1"})
        self.assertEqual(code, 200, a)
        code, lk = self.owner("alpha", "POST", "/api/lock", {"qid": "LANE.1/Q2", "answer": a["record"]["id"],
                                                           "nonce": "k3gitlock001"})
        self.assertEqual(code, 200, lk)
        _, before = self.agent("GET", "/p/alpha/check")
        self.assertEqual(before["history"], NOGIT)
        env = {**os.environ, "XDG_CONFIG_HOME": str(self.cfg)}
        env.pop("OVERTURE_AGENT", None)
        r = subprocess.run([sys.executable, str(HERE / "plugin" / "kit" / "agent.py"), "--state", str(alpha["state"]),
                            "history-push", "--project", str(alpha["root"])],
                           capture_output=True, text=True, env=env, timeout=120, cwd=alpha["root"])   # K4: cwd picks
        self.assertEqual(r.returncode, 0, r.stderr)
        _, after = self.agent("GET", "/p/alpha/check")
        [c] = after["stale"]["LANE.1/Q2"]["conditions"]
        self.assertEqual((c["history"], c["cited_text"]), ("from the steward", "unchanged"))
        self.assertEqual(self.owner("alpha", "GET", "/api/view")[1]["view"]["tags"]["git"], "from the steward")
        self.assertEqual(self.agent("GET", "/p/beta/check")[1]["history"], NOGIT)   # beta's data is its own
        self.stop()
        self.assertEqual(self.violations(), [])                 # no spawn, import or compile from a root

    def history_push(self, alpha):
        import subprocess
        env = {**os.environ, "XDG_CONFIG_HOME": str(self.cfg)}
        env.pop("OVERTURE_AGENT", None)
        r = subprocess.run([sys.executable, str(HERE / "plugin" / "kit" / "agent.py"), "--state", str(alpha["state"]),
                            "history-push", "--project", str(alpha["root"])],
                           capture_output=True, text=True, env=env, timeout=120, cwd=alpha["root"])   # K4: cwd picks
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_every_steward_git_file_operation_is_dir_relative_and_follows_no_link(self):
        # The syscall half of the K4 review's dir-fd finding: under strace, every file syscall of the server that
        # names steward-git, blobs/, index.json or a blob (create, write, rename, list, stat, prune, read) goes
        # through a held folder descriptor, one component at a time, with no link followed. Catches: any one of
        # them done by path, which a symlink swapped in between two calls would redirect. NEGATIVE CONTROL: the
        # same parser must flag path-based calls on the same names, or a parser that sees nothing passes anything.
        import subprocess
        need_strace(self)
        alpha = self.p["alpha"]
        v1 = git_project(alpha["root"])
        trace = self.t / "steward.strace"
        import base64, hashlib
        now = (alpha["root"] / "specs/spec.md").read_bytes()
        v2 = hashlib.sha256(now).hexdigest()                      # a second lock, on the current version
        pairs = (("LANE.1/Q2", v1), ("LANE.1/Q3", v2))
        legacy_ask(alpha["state"] / "store.jsonl",                # R1 refuses this shape on a new ask
                   *({**spec_question(sha), "qid": qid, "nonce": f"k4dirfdque{n}"} for n, (qid, sha) in enumerate(pairs)))
        self.spawn(prefix=("strace", "-f", "-qq", "-e", "trace=%file", "-o", str(trace)))
        items = {"LANE": {"title": "a lane", "parent": None, "status": "open"},
                 "LANE.1": {"title": "a phase", "parent": "LANE", "status": "open"}}
        self.assertEqual(self.push("alpha", items)[0], 200)
        for n, (qid, sha) in enumerate(pairs):
            code, a = self.owner("alpha", "POST", "/api/answer", {"qid": qid, "picks": ["a"], "own_text": "",
                                                                 "nonce": f"k4dirfdans{n}"})
            self.assertEqual(code, 200, a)
            self.assertEqual(self.owner("alpha", "POST", "/api/lock", {"qid": qid, "answer": a["record"]["id"],
                                                                       "nonce": f"k4dirfdloc{n}"})[0], 200)
        self.history_push(alpha)                                  # creates the folders, writes a blob and the index
        code, out = self.agent("POST", "/p/alpha/history-blob",   # a second named blob: the cap stats the first
                               {"sha256": v2, "commit": None, "content_b64": base64.b64encode(now).decode()})
        self.assertEqual(code, 200, out)
        dead = "e" * 64
        (alpha["state"] / "steward-git" / "blobs" / dead).write_bytes(b"no lock names this\n")
        self.history_push(alpha)                                  # stats, lists and prunes, then writes again
        self.assertFalse((alpha["state"] / "steward-git" / "blobs" / dead).exists())
        [c] = self.agent("GET", "/p/alpha/check")[1]["stale"]["LANE.1/Q2"]["conditions"]
        self.assertEqual(c["history"], "from the steward")       # the blob and the index were read
        self.assertEqual(self.owner("alpha", "GET", "/api/view")[1]["view"]["tags"]["git"], "from the steward")
        self.stop()
        names = ("steward-git", "blobs", "index.json", v1, v2, dead)
        safe, unsafe = dirfd_ops(trace.read_text(), names)
        self.assertEqual(unsafe, [])
        self.assertGreaterEqual(safe, 12)                         # every kind above happened, each at least once
        ctl = self.t / "control.strace"
        sg = alpha["state"] / "steward-git"
        subprocess.run(["strace", "-f", "-qq", "-e", "trace=%file", "-o", str(ctl), sys.executable, "-c",
                        f"import os; os.lstat({str(sg)!r}); open({str(sg / 'index.json')!r}).read(); "
                        f"os.rename({str(sg / 'index.json')!r}, {str(sg / 'index.json')!r}); "
                        f"os.listdir({str(sg / 'blobs')!r})"], check=True, timeout=60)
        self.assertEqual(len(dirfd_ops(ctl.read_text(), names)[1]), 4)   # each path-based call is caught


# -- K4: project tokens and the agent door (spec §3.5, §3.8, §8.2) ---------------------------------------------

# NEGATIVE CONTROL gates for AC4.1, spliced into ONE_SERVER before MS.start. Each must make the isolation check fail.
ALWAYS_SERVES = "MS.authorize = lambda ms, project, headers: True\n"
SERVES_BY_TOKENS_PROJECT = r"""
import hashlib as _hl
def _by_token(self):
    m = MS.PROJECT_PATH.match(self.path)
    auth = self.headers.get("Authorization") or ""
    got = _hl.sha256(auth[len("Bearer "):].encode()).hexdigest() if auth.startswith("Bearer ") else None
    for name, h in self.multi.projects.items():          # the TOKEN's project, whatever the path names
        if m and got and self.multi.token_hash(name) == got and h.console is not None:
            self.console, self.path = h.console, m.group(2)
            return h
    self._send(403, MS.FORBIDDEN)
    return None
MS.MultiAgentHandler._project = _by_token
"""
TOKEN_SHAPE = r"ck1_[A-Za-z0-9_-]{43}"


class _RawAgent:
    """Bytes off the agent socket, headers and all, so a test asserts on what the socket carried."""

    def raw(self, method, path, body=None, headers=()):
        """One request written in one send (so no refusal can race the body), the response read whole."""
        import socket as so
        data = b"" if body is None else json.dumps(body).encode()
        lines = [f"{method} {path} HTTP/1.1", "Host: localhost", *(f"{k}: {v}" for k, v in headers)]
        if body is not None:
            lines += ["Content-Type: application/json", f"Content-Length: {len(data)}"]
        sock = so.socket(so.AF_UNIX, so.SOCK_STREAM)
        sock.settimeout(30)
        sock.connect(str(self.sock))
        try:
            sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode() + data)
            r = http.client.HTTPResponse(sock)
            r.begin()
            out = (r.status, sorted((k, v) for k, v in r.getheaders() if k.lower() != "date"), r.read())
        finally:
            sock.close()
        return out

    @staticmethod
    def bearer(tok):
        return (("Authorization", f"Bearer {tok}"),)


def state_files(d: Path) -> dict:
    """Every file in a state dir, by sha256: the store, names, inbox, cursor, working, items and the rest."""
    return {p.relative_to(d).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(d.rglob("*")) if p.is_file() and not p.is_symlink()}


class ProjectTokenTests(_RawAgent, _OneServer, unittest.TestCase):
    """K4: a token per project at the one server's agent door; the project chosen by where agent.py runs."""

    def setUp(self):
        super().setUp()
        self.outputs: list[str] = []      # every byte agent.py printed, for AC4.2

    def agent_py(self, cwd, *args, state=None, check=None):
        env = {**os.environ, "XDG_CONFIG_HOME": str(self.cfg), "PYTHONDONTWRITEBYTECODE": "1"}
        env.pop("OVERTURE_AGENT", None)
        env.pop("OVERTURE_SESSION", None)
        r = subprocess.run([sys.executable, str(KIT / "agent.py"), "--state", str(state), *args],
                           capture_output=True, text=True, env=env, timeout=120, cwd=cwd)
        self.outputs += [r.stdout, r.stderr]
        if check is not None:
            self.assertEqual(r.returncode, check, (args, r.stdout, r.stderr))
        return r

    def seed_sentinels(self, tag=""):
        for name, sentinel in (("alpha", "SENTINEL-K4-ALPHA"), ("beta", "SENTINEL-K4-BETA")):
            self.assertEqual(self.push(name)[0], 200)
            code, out = self.agent("POST", f"/p/{name}/message", {"item": "PUSHED", "text": sentinel,
                                                                  "nonce": f"k4sent{name}{tag}01"})
            self.assertEqual(code, 200, out)

    # AC4.1 -------------------------------------------------------------------------------------------------

    def isolation(self, tag):
        """The AC4.1 check against whatever gate the running server has. Raises AssertionError on a leak."""
        self.seed_sentinels(tag)
        old_b = self.tokens["beta"]
        self.SF.rotate("beta", self.sfile, self.reg)                       # a rotated B token
        self.tokens["beta"] = self.SF.read_token("beta", self.sfile)
        made_up = "ck1_" + "A" * 43
        bad = {"A's token": self.bearer(self.tokens["alpha"]), "no token": (), "rotated B token": self.bearer(old_b),
               "made-up token": self.bearer(made_up), "malformed": (("Authorization", "Bearer not-a-token"),),
               "B's token sent twice": self.bearer(self.tokens["beta"]) * 2,
               "B's token as Basic": (("Authorization", f"Basic {self.tokens['beta']}"),)}
        routes = ([("GET", p) for p in OneServerTests.AGENT_GETS]
                  + [("POST", p) for p in OneServerTests.AGENT_POSTS])
        b_state = self.p["beta"]["state"]
        before = state_files(b_state)
        seen = []                                                         # (label, (status, headers, body))
        # The first request of all is A's token on B's first route: the always-serving control fails right here.
        for case, h in bad.items():
            for method, path in routes:
                seen.append((f"{case} {method} /p/beta{path}", self.raw(method, f"/p/beta{path}", {}
                                                                         if method == "POST" else None, h)))
        for case, h in {**bad, "B's token": self.bearer(self.tokens["beta"])}.items():   # a project not hosted
            seen.append((f"{case} GET /p/gamma/view", self.raw("GET", "/p/gamma/view", None, h)))
        for method, path in routes:                                       # B's own token on A's path
            seen.append((f"B's token {method} /p/alpha{path}",
                         self.raw(method, f"/p/alpha{path}", {} if method == "POST" else None,
                                  self.bearer(self.tokens["beta"]))))
        for label, (_st, _h, body) in seen:                               # no B record text, in any refusal
            if b"SENTINEL-K4-BETA" in body:
                raise AssertionError(f"sentinel leaked on {label}")
        first = seen[0][1]
        self.assertEqual(first[0], 403)
        self.assertEqual(json.loads(first[2]), MS.FORBIDDEN)
        for label, resp in seen:
            self.assertEqual(resp, first, f"not the one refusal: {label}")  # status, headers and body bytes
        self.assertEqual(state_files(b_state), before)                    # B's files untouched by any refusal
        for method, path in routes:                                       # B's token serves every B route
            code = self.raw(method, f"/p/beta{path}", {} if method == "POST" else None,
                            self.bearer(self.tokens["beta"]))[0]
            self.assertNotEqual(code, 403, path)
        self.assertEqual(self.raw("GET", "/p/beta/view", None, self.bearer(self.tokens["beta"]))[0], 200)

    def test_ac41_token_isolation_and_its_two_negative_controls(self):
        # Catches: a gate that admits any well-formed token, and one that serves the TOKEN's project rather than
        # the path's. Both ship here as stub gates, and the check must fail against each.
        self.spawn()
        self.isolation("r")
        self.stop()
        self.spawn(ONE_SERVER.replace("r = MS.start(", ALWAYS_SERVES + "r = MS.start("))
        with self.assertRaises(AssertionError) as cm:
            self.isolation("s")
        self.assertIn("sentinel leaked on A's token GET /p/beta/view", str(cm.exception))   # its first A request
        self.stop()
        self.spawn(ONE_SERVER.replace("r = MS.start(", SERVES_BY_TOKENS_PROJECT + "r = MS.start("))
        with self.assertRaises(AssertionError) as cm:
            self.isolation("t")
        self.assertIn("sentinel leaked on B's token", str(cm.exception))

    def test_ac41_the_token_compare_is_hmac_compare_digest_on_the_paths_project(self):
        # Pins the constant-time compare (a timing test is not practical): `==` in its place goes red here, and
        # so does an `authorize` that admits anyone again. An unknown project still costs one compare.
        import email.message
        import hmac
        from unittest import mock
        from overture import serverfile as SF
        tok, other = "ck1_" + "a" * 43, "ck1_" + "b" * 43

        class FakeMS:
            def token_hash(self, project):
                return {"alpha": SF.token_hash(tok), "beta": SF.token_hash(other)}.get(project)

        def headers(*values):
            m = email.message.Message()
            for v in values:
                m["Authorization"] = v
            return m
        cases = [("alpha", (f"Bearer {tok}",), True), ("beta", (f"Bearer {tok}",), False),
                 ("gamma", (f"Bearer {tok}",), False), ("alpha", (), False), ("alpha", ("Bearer nope",), False),
                 ("alpha", (f"Bearer {tok}", f"Bearer {tok}"), False), ("alpha", (f"Basic {tok}",), False),
                 ("alpha", (f"Bearer {tok} ",), False)]
        for project, values, want in cases:
            with mock.patch.object(MS.hmac, "compare_digest", wraps=hmac.compare_digest) as cd:
                self.assertIs(MS.authorize(FakeMS(), project, headers(*values)), want, (project, values))
            self.assertEqual(cd.call_count, 1, (project, values))
            got, stored = cd.call_args.args
            if want:
                self.assertEqual(got, stored)
                self.assertEqual(got, SF.token_hash(tok).encode())

    # AC4.2 -------------------------------------------------------------------------------------------------

    def test_ac42_a_token_is_never_in_a_repository_or_echoed(self):
        # Catches: a check of server.json alone while an error message prints the token. Every byte the processes
        # wrote is searched: both state dirs, server.json, the server's stderr and every agent.py output.
        import shutil
        A, B = self.p["alpha"], self.p["beta"]
        tdir = self.cfg / "overture" / "tokens"
        self.assertEqual(stat.S_IMODE(tdir.stat().st_mode), 0o700)
        for name in ("alpha", "beta"):
            self.assertEqual(stat.S_IMODE((tdir / name).lstat().st_mode), 0o600)
        self.assertNotIn(self.tokens["alpha"], self.sfile.read_text())
        self.assertEqual(json.loads(self.sfile.read_text())["projects"]["alpha"]["token_sha256"],
                         self.SF.token_hash(self.tokens["alpha"]))
        minted = set(self.tokens.values())
        # A tokens directory that resolves inside a registered root is refused by `server add` and `rotate`.
        cfg2 = self.t / "cfg2"
        (cfg2 / "overture").mkdir(parents=True)
        shutil.copy(self.reg, cfg2 / "overture" / "projects.json")
        (A["root"] / "tok").mkdir()
        (cfg2 / "overture" / "tokens").symlink_to(A["root"] / "tok")
        env = {**os.environ, "XDG_CONFIG_HOME": str(cfg2)}
        r = subprocess.run([sys.executable, str(KIT / "agent.py"), "--state", str(A["state"]), "server", "add",
                            "alpha", "--hostname", "alpha.example.com", "--aud", AUD, "--port", "4901",
                            "--team-domain", TEAM], capture_output=True, text=True, env=env, timeout=60)
        self.outputs += [r.stdout, r.stderr]
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("inside the registered project root", r.stderr)
        self.assertFalse((cfg2 / "overture" / "server.json").exists())
        shutil.copy(self.sfile, cfg2 / "overture" / "server.json")
        held = (cfg2 / "overture" / "server.json").read_bytes()
        r = subprocess.run([sys.executable, str(KIT / "agent.py"), "--state", str(A["state"]), "server", "token",
                            "rotate", "alpha"], capture_output=True, text=True, env=env, timeout=60)
        self.outputs += [r.stdout, r.stderr]
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("inside the registered project root", r.stderr)
        self.assertEqual(list((A["root"] / "tok").iterdir()), [])
        self.assertEqual((cfg2 / "overture" / "server.json").read_bytes(), held)
        # And a config dir that is itself a plain directory inside a registered root (no link to see through).
        cfg3 = B["root"] / "cfg-in-repo"
        (cfg3 / "overture").mkdir(parents=True)
        shutil.copy(self.reg, cfg3 / "overture" / "projects.json")
        r = subprocess.run([sys.executable, str(KIT / "agent.py"), "--state", str(B["state"]), "server", "add",
                            "beta", "--hostname", "beta.example.com", "--aud", AUD, "--port", "4902",
                            "--team-domain", TEAM], capture_output=True, text=True,
                           env={**os.environ, "XDG_CONFIG_HOME": str(cfg3)}, timeout=60)
        self.outputs += [r.stdout, r.stderr]
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn(f"inside the registered project root {B['root']}", r.stderr)
        self.assertFalse((cfg3 / "overture" / "tokens").exists())
        self.assertFalse((cfg3 / "overture" / "server.json").exists())
        # A full run: every agent route through agent.py, refusals and errors included, and a rotation.
        self.spawn()
        self.agent_py(A["root"], "items-push", "--adapter", "console_adapter.py", state=A["state"], check=0)
        for args in (("view",), ("todo",), ("health",), ("check",), ("answers",), ("answers", "--json"),
                     ("inbox", "--all"), ("reply", "PUSHED", "hello"), ("working", "PUSHED"), ("synced",),
                     ("reanchor", "--dry-run"), ("reply", "NO-SUCH-ITEM", "refused"), ("fork-context", "nope")):
            self.agent_py(A["root"], *args, state=A["state"])
        self.agent_py(A["root"], "view", state=B["state"], check=1)        # the wrong console: refused
        self.agent_py(self.t, "view", state=A["state"], check=1)           # outside every root: refused
        made_up = "ck1_" + "Z" * 43
        minted.add(made_up)
        good = (tdir / "alpha").read_bytes()
        (tdir / "alpha").write_text(made_up + "\n")                        # a token the server does not hold
        self.agent_py(A["root"], "view", state=A["state"], check=1)
        self.agent_py(A["root"], "reply", "PUSHED", "x", state=A["state"], check=1)
        (tdir / "alpha").write_text("not a token at all\n")               # a broken file: named, never shown
        r = self.agent_py(A["root"], "view", state=A["state"], check=1)
        self.assertIn(str(tdir / "alpha"), r.stderr)
        (tdir / "alpha").write_bytes(good)
        self.agent_py(A["root"], "server", "token", "rotate", "alpha", state=A["state"], check=0)
        minted.add(self.SF.read_token("alpha", self.sfile))
        self.agent_py(A["root"], "view", state=A["state"], check=0)
        self.stop()
        written = list(self.outputs) + list(self.logs) + [self.sfile.read_text()]
        for st in (A["state"], B["state"]):
            written += [p.read_bytes().decode("utf-8", "replace") for p in st.rglob("*")
                        if p.is_file() and not p.is_socket()]
        blob = "\n".join(written)
        for tok in minted:
            self.assertNotIn(tok, blob)
        self.assertIsNone(__import__("re").search(TOKEN_SHAPE, blob))     # no token of any kind, anywhere

    # AC4.3 -------------------------------------------------------------------------------------------------

    def test_ac43_rotation_refuses_the_old_token_at_once_with_no_restart(self):
        # Catches: hashes cached at start-up (the old token works until a restart). One server process throughout.
        B = self.p["beta"]
        self.spawn()
        pid = self.proc.pid
        self.agent_py(B["root"], "view", state=B["state"], check=0)
        old = self.tokens["beta"]
        self.assertEqual(self.raw("GET", "/p/beta/view", None, self.bearer(old))[0], 200)
        common = self.raw("GET", "/p/beta/view", None, self.bearer("ck1_" + "Q" * 43))
        r = self.agent_py(self.t, "server", "token", "rotate", "beta", state=B["state"], check=0)
        self.assertIn("tokens/beta", r.stdout)
        new = self.SF.read_token("beta", self.sfile)
        self.assertNotEqual(new, old)
        self.assertEqual(self.raw("GET", "/p/beta/view", None, self.bearer(old)), common)   # the common 403
        self.assertEqual(self.raw("GET", "/p/beta/view", None, self.bearer(new))[0], 200)
        self.agent_py(B["root"], "view", state=B["state"], check=0)       # its next call, no restart anywhere
        self.assertEqual((self.proc.pid, self.proc.poll()), (pid, None))

    # AC4.4 -------------------------------------------------------------------------------------------------

    def a_reads(self, fork):
        """A's reads: the bytes off the socket for /view and /check, and agent.py's six read commands."""
        A = self.p["alpha"]
        h = self.bearer(self.tokens["alpha"])
        out = {"socket /view": self.raw("GET", "/p/alpha/view", None, h)[2],
               "socket /check": self.raw("GET", "/p/alpha/check", None, h)[2]}
        for args in (("view", "--full"), ("answers", "--full"), ("answers", "--json", "--full"),
                     ("inbox", "--all"), ("check",), ("fork-context", fork)):
            r = self.agent_py(A["root"], *args, state=A["state"], check=0)
            out[" ".join(args)] = r.stdout.encode()
        return out

    def test_ac44_each_read_is_the_bytes_a_one_project_server_gives(self):
        # Catches: a server that sends both projects and lets agent.py filter (the socket carried B's records):
        # the bytes compared are read off the socket, as well as agent.py's output.
        import shutil
        self.spawn()
        self.seed_sentinels()
        self.assertEqual(self.question("PUSHED", 1)[0], 200)
        self.assertEqual(self.question("PUSHED", 1, "beta")[0], 200)
        code, a = self.owner("alpha", "POST", "/api/answer", {"qid": "PUSHED/Q1", "picks": ["a"], "own_text": "",
                                                              "nonce": "k4answer0001"})
        self.assertEqual(code, 200, a)
        code, lk = self.owner("alpha", "POST", "/api/lock", {"qid": "PUSHED/Q1", "answer": a["record"]["id"],
                                                             "nonce": "k4lock000001"})
        self.assertEqual(code, 200, lk)
        code, fk = self.owner("alpha", "POST", "/api/message", {"item": "PUSHED", "text": "deliberate",
                                                                "intent": "fork", "mode": "tighten",
                                                                "nonce": "k4fork000001"})
        self.assertEqual(code, 200, fk)
        fork = fk["record"]["id"]
        two = self.a_reads(fork)
        for k, v in two.items():
            self.assertNotIn(b"SENTINEL-K4-BETA", v, k)
        self.assertIn(b"SENTINEL-K4-ALPHA", two["socket /view"])          # the reads are not empty
        for n in range(100):                                              # B grows; A's reads do not move
            code, out = self.agent("POST", "/p/beta/message", {"item": "PUSHED", "text": f"SENTINEL-K4-BETA {n}",
                                                                "nonce": f"k4grow{n:06d}"})
            self.assertEqual(code, 200, out)
        self.assertEqual(self.a_reads(fork), two)
        # A server that loaded the stores from disk orders a record's keys as stored, one that appended them in
        # memory as sent: compare like with like, both freshly started on the same files.
        self.stop()
        self.spawn()
        two = self.a_reads(fork)
        self.assertNotIn(b"SENTINEL-K4-BETA", b"".join(two.values()))
        self.stop()
        # The one-project server, built on A's state alone: its own config dir holding A and nothing of B.
        one = self.t / "cfg-one" / "overture"
        (one / "tokens").mkdir(parents=True, mode=0o700)
        os.chmod(one, 0o700)
        shutil.copy(self.reg, one / "projects.json")
        doc = json.loads(self.sfile.read_text())
        doc["projects"] = {"alpha": doc["projects"]["alpha"]}
        (one / "server.json").write_text(json.dumps(doc))
        shutil.copy(self.cfg / "overture" / "tokens" / "alpha", one / "tokens" / "alpha")
        os.chmod(one / "tokens" / "alpha", 0o600)
        self.cfg, self.sfile, self.sock = one.parent, one / "server.json", one / "server.sock"
        info = self.spawn()
        self.assertEqual(list(info["ports"]), ["alpha"])
        self.assertEqual(self.a_reads(fork), two)

    # AC4.5 -------------------------------------------------------------------------------------------------

    def test_ac45_the_working_directory_picks_the_project_and_a_mismatch_sends_nothing(self):
        # Catches: honouring --state to choose the token (so `--state B` quietly becomes B's agent). No server
        # runs: a listener on the one server's socket records every request that reaches it.
        import socket as so
        A, B = self.p["alpha"], self.p["beta"]
        seen: list[bytes] = []
        lsn = so.socket(so.AF_UNIX, so.SOCK_STREAM)
        lsn.bind(str(self.sock))
        lsn.listen(16)
        lsn.settimeout(0.2)
        stop = threading.Event()

        def serve():
            while not stop.is_set():
                try:
                    c, _ = lsn.accept()
                except OSError:
                    continue
                c.settimeout(5)
                data = b""
                while b"\r\n\r\n" not in data:
                    chunk = c.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                seen.append(data)
                c.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\n"
                          b"Connection: close\r\n\r\n{}")
                c.close()
        th = threading.Thread(target=serve, daemon=True)
        th.start()
        try:
            for args in (("view",), ("health",), ("check",), ("reply", "PUSHED", "hi"), ("working", "PUSHED"),
                         ("synced",), ("inbox", "--all"), ("watch", "--timeout", "0.2", "--poll", "0.1"),
                         ("items-push", "--adapter", "console_adapter.py")):
                r = self.agent_py(A["root"], *args, state=B["state"], check=1)   # in A's root, told B
                self.assertIn(f"--state {B['state']}", r.stderr, args)
                self.assertIn(str(A["state"]), r.stderr, args)
                self.assertIn(str(A["root"]), r.stderr, args)
                r = self.agent_py(self.t, *args, state=A["state"], check=1)      # outside every root
                self.assertIn("inside no registered project root", r.stderr, args)
            self.assertEqual(seen, [])                                    # not one request reached the socket
            self.assertFalse((B["state"] / "watch.json").exists())         # and B's doorbell was never watched
            # The control: from each project's own root, its own token on its own path.
            for name in ("alpha", "beta"):
                self.agent_py(self.p[name]["root"], "health", state=self.p[name]["state"], check=0)
                head = seen[-1].decode()
                self.assertTrue(head.startswith(f"GET /p/{name}/health "), head)
                self.assertIn(f"Authorization: Bearer {self.tokens[name]}\r\n", head)
        finally:
            stop.set()
            th.join()
            lsn.close()

    # AC4.6 -------------------------------------------------------------------------------------------------

    def test_ac46_a_doorbell_on_b_wakes_only_bs_watch_and_a_cursor_moves_only_its_own(self):
        # Catches: both stewards watching one shared inbox, each ignoring the other's lines once woken. Wake exits
        # are counted per watcher.
        A, B = self.p["alpha"], self.p["beta"]
        self.spawn()
        self.seed_sentinels()
        env = {**os.environ, "XDG_CONFIG_HOME": str(self.cfg)}
        env.pop("OVERTURE_AGENT", None)
        watchers = {}
        for name, timeout in (("alpha", "8"), ("beta", "60")):
            v = self.p[name]
            watchers[name] = subprocess.Popen([sys.executable, str(KIT / "agent.py"), "--state", str(v["state"]),
                                               "watch", "--timeout", timeout, "--poll", "0.1"], cwd=v["root"],
                                              env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + 30
        while not all((v["state"] / "watch.json").exists() for v in (A, B)):   # both are polling
            self.assertLess(time.monotonic(), deadline, "the watchers never started")
            time.sleep(0.05)
        code, out = self.owner("beta", "POST", "/api/message", {"item": "@chat", "text": "only for beta",
                                                               "intent": "chat", "nonce": "k4chatbeta01"})
        self.assertEqual(code, 200, out)
        results = {n: p.communicate(timeout=90) + (p.returncode,) for n, p in watchers.items()}
        wakes = {n: int(r[2] == 0) for n, r in results.items()}
        self.assertEqual(wakes, {"alpha": 0, "beta": 1}, results)
        self.assertEqual(results["alpha"][2], 3)                          # A timed out: nothing for it
        self.assertIn('"intent": "chat"', results["beta"][0])
        self.assertFalse((A["state"] / "inbox.jsonl").exists() and (A["state"] / "inbox.jsonl").read_text())
        # POST /p/alpha/cursor moves A's cursor and working bucket, and nothing of B's.
        self.agent_py(B["root"], "working", "PUSHED", state=B["state"], check=0)
        self.agent_py(A["root"], "working", "PUSHED", state=A["state"], check=0)
        b_before = {f: (B["state"] / f).read_bytes() if (B["state"] / f).exists() else None
                    for f in ("cursor.json", "working.json", "inbox.jsonl")}
        a_before = (A["state"] / "working.json").read_bytes()
        self.agent_py(A["root"], "synced", state=A["state"], check=0)
        self.assertIsNotNone(json.loads((A["state"] / "cursor.json").read_text())["last_synced_at"])
        self.assertNotEqual((A["state"] / "working.json").read_bytes(), a_before)
        self.assertEqual({f: (B["state"] / f).read_bytes() if (B["state"] / f).exists() else None
                          for f in b_before}, b_before)



class TokenReviewTests(_RawAgent, _OneServer, unittest.TestCase):
    """K4 review round 1: the token file's mode, the drain's deadline, the writers' lock, stale temp files, and a
    server.json too deeply nested to parse."""

    @contextlib.contextmanager
    def fake_door(self):
        """A listener on the one server's socket that records every request reaching it, and answers 200 {}."""
        import socket as so
        seen: list[bytes] = []
        lsn = so.socket(so.AF_UNIX, so.SOCK_STREAM)
        lsn.bind(str(self.sock))
        lsn.listen(16)
        lsn.settimeout(0.2)
        stop = threading.Event()

        def serve():
            while not stop.is_set():
                try:
                    c, _ = lsn.accept()
                except OSError:
                    continue
                c.settimeout(5)
                data = b""
                while b"\r\n\r\n" not in data:
                    chunk = c.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                seen.append(data)
                c.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\n"
                          b"Connection: close\r\n\r\n{}")
                c.close()
        th = threading.Thread(target=serve, daemon=True)
        th.start()
        try:
            yield seen
        finally:
            stop.set()
            th.join()
            lsn.close()

    def agent_py(self, cwd, *args, state):
        env = {**os.environ, "XDG_CONFIG_HOME": str(self.cfg), "PYTHONDONTWRITEBYTECODE": "1"}
        env.pop("OVERTURE_AGENT", None)
        env.pop("OVERTURE_SESSION", None)
        return subprocess.run([sys.executable, str(KIT / "agent.py"), "--state", str(state), *args],
                              capture_output=True, text=True, env=env, timeout=120, cwd=cwd)

    def test_a_token_file_others_can_read_is_refused_and_nothing_is_sent(self):
        # Catches: dropping the mode (or owner) check on the token file. A token another user could read is
        # treated as no token: read_token and agent.py both refuse, naming the FILE, and no request is made.
        from unittest import mock
        A = self.p["alpha"]
        f = self.cfg / "overture" / "tokens" / "alpha"
        with self.fake_door() as seen:
            r = self.agent_py(A["root"], "health", state=A["state"])
            self.assertEqual(r.returncode, 0, r.stderr)                   # the control: 0600 is sent
            self.assertEqual(len(seen), 1)
            os.chmod(f, 0o644)
            with self.assertRaises(self.SF.ServerFileError) as cm:
                self.SF.read_token("alpha", self.sfile)
            self.assertIn(str(f), str(cm.exception))
            self.assertNotIn(self.tokens["alpha"], str(cm.exception))
            for args in (("health",), ("view",), ("reply", "PUSHED", "hi")):
                r = self.agent_py(A["root"], *args, state=A["state"])
                self.assertEqual(r.returncode, 1, (args, r.stderr))
                self.assertIn(str(f), r.stderr)
                self.assertIn("mode 0600", r.stderr)
                self.assertNotIn(self.tokens["alpha"], r.stdout + r.stderr)
            self.assertEqual(len(seen), 1)                                # nothing more reached the socket
        # Owned by another uid: chown needs root, so the owner half is checked by making this process another
        # user as far as read_token can tell.
        os.chmod(f, 0o600)
        with mock.patch.object(self.SF.os, "getuid", return_value=os.getuid() + 1):
            with self.assertRaises(self.SF.ServerFileError) as cm:
                self.SF.read_token("alpha", self.sfile)
        self.assertIn("you own", str(cm.exception))
        self.assertEqual(self.SF.read_token("alpha", self.sfile), self.tokens["alpha"])

    def test_the_one_servers_post_routes_are_the_base_handlers(self):
        # Catches: a route the base handler serves missing from POST_ROUTES (the one server would 404 it), or a
        # name there the base does not serve.
        import ast
        import inspect
        import textwrap
        tree = ast.parse(textwrap.dedent(inspect.getsource(SV.AgentHandler.do_POST)))
        served = {n.comparators[0].value for n in ast.walk(tree)
                  if isinstance(n, ast.Compare) and isinstance(n.left, ast.Attribute) and n.left.attr == "path"
                  and isinstance(n.comparators[0], ast.Constant)}
        self.assertEqual(MS.POST_ROUTES, served | set(SV.AGENT_ROUTES))
        self.assertEqual(set(OneServerTests.AGENT_POSTS) - {"/no-such-route"}, MS.POST_ROUTES)

    def test_a_refused_request_is_answered_at_once_and_its_body_drained_within_the_base_deadline(self):
        # Catches: a refusal that waits on the body before answering, and a drain bounded only by the per-read
        # timeout (a client trickling a byte at a time resets it). The base `_Handler.finish` answers FIRST and
        # drains after, within server.DRAIN_SECONDS in all; this holds for the one server's /p/ 403, 503 and 404.
        import socket as so
        (self.p["beta"]["state"] / "store.jsonl").write_text('{"not": "a record"}\n')   # beta: a 503
        self.spawn()
        cases = (("403", "/p/alpha/question", ()), ("403 unknown", "/p/gamma/question", ()),
                 ("503", "/p/beta/question", self.bearer(self.tokens["beta"])),
                 ("404", "/p/alpha/no-such-route", self.bearer(self.tokens["alpha"])))
        for label, path, headers in cases:
            for mode in ("silent", "trickle"):
                head = (f"POST {path} HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\n"
                        + "".join(f"{k}: {v}\r\n" for k, v in headers) + "Content-Length: 1000000\r\n\r\n").encode()
                sock = so.socket(so.AF_UNIX, so.SOCK_STREAM)
                sock.settimeout(SV.DRAIN_SECONDS + 5)
                sock.connect(str(self.sock))
                stop = threading.Event()

                def trickle():
                    while not stop.wait(0.1):
                        try:
                            sock.sendall(b" ")
                        except OSError:
                            return
                th = threading.Thread(target=trickle, daemon=True)
                t0 = time.monotonic()
                sock.sendall(head)
                if mode == "trickle":
                    th.start()
                try:
                    first = sock.recv(4096)
                except so.timeout:
                    first = b""
                answered = time.monotonic() - t0
                rest = b""
                try:
                    while True:                                           # then the close, after the drain
                        chunk = sock.recv(4096)
                        if not chunk:
                            break
                        rest += chunk
                except (so.timeout, OSError):
                    pass
                closed = time.monotonic() - t0
                stop.set()
                if th.is_alive():
                    th.join()
                sock.close()
                code = label.split()[0]
                self.assertTrue(first.startswith(f"HTTP/1.0 {code}".encode()), (label, mode, first[:60]))
                self.assertLess(answered, 1.0, (label, mode))           # well under the deadline: answered first
                self.assertLess(closed, SV.DRAIN_SECONDS + 1.5, (label, mode))
        self.assertEqual(self.agent("GET", "/p/alpha/view")[0], 200)     # and the server still serves

    def test_a_two_write_client_reads_every_refusal_never_a_broken_pipe(self):
        # Catches: a /p/ 403, 503 or 404 closed with the body unread, so a client that sends its body in a second
        # write (http.client, i.e. agent.py) gets EPIPE in place of the answer. Made deterministic: the body is
        # written only AFTER the answer has been read, which succeeds only while the server is still draining.
        import socket as so
        (self.p["beta"]["state"] / "store.jsonl").write_text('{"not": "a record"}\n')
        self.spawn()
        body = b'{"text": "late"}'
        for code, path, headers in (("403", "/p/alpha/question", ()), ("403", "/p/gamma/question", ()),
                                    ("503", "/p/beta/question", self.bearer(self.tokens["beta"])),
                                    ("404", "/p/alpha/no-such-route", self.bearer(self.tokens["alpha"]))):
            sock = so.socket(so.AF_UNIX, so.SOCK_STREAM)
            sock.settimeout(10)
            sock.connect(str(self.sock))
            sock.sendall((f"POST {path} HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\n"
                          + "".join(f"{k}: {v}\r\n" for k, v in headers)
                          + f"Content-Length: {len(body)}\r\n\r\n").encode())
            first = sock.recv(4096)
            self.assertTrue(first.startswith(f"HTTP/1.0 {code}".encode()), (path, first[:60]))
            time.sleep(0.2)                                               # the second write, well after the answer
            try:
                sock.sendall(body)
            except OSError as e:
                self.fail(f"{path}: the body's second write failed ({e!r}): the server closed without draining")
            finally:
                sock.close()
        for _ in range(20):                                               # and the real two-write client
            self.assertEqual(SV.agent_request(self.sock, "POST", "/p/alpha/question", {}), (403, MS.FORBIDDEN))

    def test_the_sweep_never_runs_in_a_tokens_dir_linked_into_a_root(self):
        # Catches: sweeping tokens/ before checking it, which deletes .token.*.tmp files in a repository that a
        # symlinked tokens/ points into.
        import shutil
        A, SF = self.p["alpha"], self.SF
        cfg2 = self.t / "cfg-link" / "overture"
        cfg2.mkdir(parents=True)
        shutil.copy(self.reg, cfg2 / "projects.json")
        shutil.copy(self.sfile, cfg2 / "server.json")
        (A["root"] / "tok").mkdir()
        planted = A["root"] / "tok" / ".token.repo-file.tmp"
        (cfg2 / "tokens").symlink_to(A["root"] / "tok")
        for run in (lambda: SF.rotate("alpha", cfg2 / "server.json", self.reg),
                    lambda: SF.add("alpha", A["state"], "alpha.example.com", AUD, 4901, TEAM, registry=self.reg,
                                   path=cfg2 / "server.json")):
            planted.write_text("a file of the repository's\n")
            with self.assertRaises(SF.ServerFileError) as cm:
                run()
            self.assertIn("inside the registered project root", str(cm.exception))
            self.assertEqual(planted.read_text(), "a file of the repository's\n")
        # A link to a directory outside every root is refused too, and left unswept.
        outside = self.t / "outside-tok"
        outside.mkdir()
        (outside / ".token.x.tmp").write_text("kept\n")
        (cfg2 / "tokens").unlink()
        (cfg2 / "tokens").symlink_to(outside)
        with self.assertRaises(SF.ServerFileError) as cm:
            SF.rotate("alpha", cfg2 / "server.json", self.reg)
        self.assertIn("must be a directory you own", str(cm.exception))
        self.assertTrue((outside / ".token.x.tmp").exists())

    def test_two_interleaved_writers_cannot_resurrect_a_revoked_hash(self):
        # Catches: load-modify-write with no lock. An add that loaded server.json before a rotation would write
        # the revoked hash back; the add is held between its load and its write while the rotation runs.
        from unittest import mock
        A, SF = self.p["alpha"], self.SF
        old_hash = json.loads(self.sfile.read_text())["projects"]["alpha"]["token_sha256"]
        loaded, go = threading.Event(), threading.Event()
        real_load = SF.load

        def slow_load(path=None):
            doc = real_load(path)
            if threading.current_thread().name == "adder" and not loaded.is_set():
                loaded.set()
                go.wait(10)
            return doc
        errs = []

        def run(fn):
            try:
                fn()
            except Exception as e:   # noqa: BLE001 - reported below
                errs.append(e)
        with mock.patch.object(SF, "load", slow_load):
            adder = threading.Thread(name="adder", target=run, args=(lambda: SF.add(
                "alpha", A["state"], "alpha.example.com", AUD, 4901, TEAM, registry=self.reg, path=self.sfile),))
            rotator = threading.Thread(name="rotator", target=run,
                                       args=(lambda: SF.rotate("alpha", self.sfile, self.reg),))
            adder.start()
            self.assertTrue(loaded.wait(10))
            rotator.start()
            time.sleep(0.5)                                               # the rotation runs now, if it can
            go.set()
            adder.join(30)
            rotator.join(30)
        self.assertEqual(errs, [])
        stored = json.loads(self.sfile.read_text())["projects"]["alpha"]["token_sha256"]
        self.assertNotEqual(stored, old_hash)
        self.assertEqual(stored, SF.token_hash(SF.read_token("alpha", self.sfile)))

    def test_add_and_rotate_sweep_stale_plaintext_temp_files(self):
        # Catches: a crash between mkstemp and rename leaving a plaintext token in tokens/.token.*.tmp for ever.
        A, SF = self.p["alpha"], self.SF
        tdir = self.cfg / "overture" / "tokens"
        stale = "ck1_" + "S" * 43
        for run in (lambda: SF.rotate("alpha", self.sfile, self.reg),
                    lambda: SF.add("alpha", A["state"], "alpha.example.com", AUD, 4901, TEAM, registry=self.reg,
                                   path=self.sfile)):
            (tdir / ".token.dead1234.tmp").write_text(stale + "\n")
            (tdir / "keep-me.txt").write_text("not a temp file\n")
            run()
            self.assertEqual(sorted(p.name for p in tdir.iterdir()), ["alpha", "beta", "keep-me.txt"])
            self.assertNotIn(stale, "".join(p.read_text() for p in tdir.iterdir()))

    def test_a_server_json_nested_too_deeply_refuses_every_token_and_says_so_once(self):
        # Catches: RecursionError escaping the loader (it is not a ValueError), which would 500 every request or
        # crash agent.py instead of refusing alike.
        SF = self.SF
        ms = MS.MultiServer(self.sfile)
        try:
            self.assertIsNotNone(ms.token_hash("alpha"))
            self.sfile.write_text("[" * 200_000)
            with self.assertRaises(SF.ServerFileError):
                SF.load(self.sfile)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                got = [ms.token_hash(n) for n in ("alpha", "beta", "alpha")]
            self.assertEqual(got, [None, None, None])
            lines = err.getvalue().splitlines()
            self.assertEqual(len(lines), 1, lines)
            self.assertIn("every agent token is refused", lines[0])
            self.assertNotRegex(lines[0], TOKEN_SHAPE)
            for tok in self.tokens.values():
                self.assertNotIn(tok, lines[0])
        finally:
            ms.close()
        r = self.agent_py(self.p["alpha"]["root"], "view", state=self.p["alpha"]["state"])
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("nested too deeply", r.stderr)
        self.assertNotIn("Traceback", r.stderr)


# -- Q24 ("items_push_now"): the single server never runs the adapter; items arrive by push only ------------

LANE_ITEMS = {"items": {"LANE": {"title": "a lane", "parent": None, "status": "open"},
                        "LANE.1": {"title": "a phase", "parent": "LANE", "status": "open"}},
              "seed_questions": [], "board": None}
BAD_PUSHES = [   # each refused by the one check, `items.snapshot_problem`, in the same words on both servers
    [],
    {"items": {}, "extra": 1},
    {"seed_questions": []},
    {"items": {"LANE": {"title": "t", "owner": "x"}}},
    {"items": {"LANE": {"parent": None}}},
    {"items": {"LANE": {"title": 5}}},
    {"items": {"bad id!": {"title": "t"}}},
    {"items": {}, "seed_questions": [{"text": "no qid"}]},
    {"items": {}, "board": {"shape": "s", "values": {"k": 1}}},
]
MARKER_ADAPTER = ("open({marker!r}, 'w').write('ran')\n"
                  "def items():\n    return {{'FROM_ADAPTER': {{'title': 'the adapter says', 'parent': None, "
                  "'status': 'open'}}}}\n"
                  "def seed_questions():\n    return []\n"
                  "def record(entries, dry_run):\n    return []\n")


def raw_agent(sock: Path, method: str, path: str, data: bytes | None = None,
              token: str | None = None) -> tuple[int, bytes]:
    """One agent-door request, answered as (status, raw body bytes): refusals are compared byte for byte.

    `token`: the project's bearer token, which the one server's door requires (K4).
    """
    import socket as so

    class Conn(http.client.HTTPConnection):
        def connect(self):
            self.sock = so.socket(so.AF_UNIX, so.SOCK_STREAM)
            self.sock.connect(str(sock))

    c = Conn("localhost", timeout=10)
    headers = {} if data is None else {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    c.request(method, path, body=data, headers=headers)
    r = c.getresponse()
    raw = r.read()
    c.close()
    return r.status, raw


def within(test: unittest.TestCase, fn, seconds: float = 5.0) -> dict:
    """Run `fn` on a thread and fail if it is still running after `seconds`: a hang becomes a failure, not a stall.

    The result is {"value": ...} or {"error": exception}.
    """
    out: dict = {}

    def run():
        try:
            out["value"] = fn()
        except BaseException as e:   # noqa: BLE001: whatever it raised is the answer
            out["error"] = e

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(seconds)
    test.assertFalse(t.is_alive(), f"blocked for more than {seconds} s")
    return out


def planted_fifo(test: unittest.TestCase, path: Path) -> None:
    """A FIFO where a file is read. Cleanup opens it for writing once, which releases any reader stuck in open()."""
    os.mkfifo(path)

    def release():
        try:
            fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)   # ENXIO when no reader waits: nothing to release
        except OSError:
            return
        os.close(fd)
    test.addCleanup(release)


class _SingleServer:
    """One project's single server (`serve`'s Console and agent door), in this process, adapter configured."""

    def single(self, base: Path) -> SV.Config:
        root = base / "root"
        root.mkdir(parents=True)
        (root / "page.html").write_text(PAGE)
        self.marker = base / "ADAPTER-RAN"
        (root / "adapter.py").write_text(MARKER_ADAPTER.format(marker=str(self.marker)))
        self.scfg = SV.Config(root=root, page=root / "page.html", state=base / "state", adapter=root / "adapter.py",
                              team_domain=TEAM, aud=AUD, hostname=HOSTNAME, port=0, project="single")
        self.start_single()
        return self.scfg

    def start_single(self):
        self.sconsole = SV.Console(self.scfg, IT.SnapshotAdapter(self.scfg.state, tolerant=True))   # as `serve` does
        self.sconsole.seed()
        self.ssrv = SV.agent_server(self.sconsole)
        threading.Thread(target=self.ssrv.serve_forever, daemon=True).start()
        self.addCleanup(self.stop_single)

    def stop_single(self):
        if getattr(self, "ssrv", None) is not None:
            self.ssrv.shutdown()
            self.ssrv.server_close()
            self.ssrv = None

    def sview(self) -> dict:
        code, out = SV.agent_request(self.scfg.socket, "GET", "/view")
        self.assertEqual(code, 200, out)
        return out


class SingleServerStartTests(unittest.TestCase):
    """Lane 5 review (MEDIUM, live safety): a stored items.json the single server cannot read never stops it.

    `serve` used to turn that StoreError into SystemExit, so one bad file planted in STATE kept the console
    down. Now it starts, logs one stderr line, answers /health not-ok, shows the board's UNREADABLE note, and
    the next items-push replaces the file and recovers live. The ONE server is unchanged and keeps refusing
    only that project, the others fine: FaultIsolationTests.test_a_bad_stored_items_file_refuses_that_project_only.
    """

    def serve(self, plant):
        """The real `serve`, in a thread, over a STATE where `plant(items_path, base)` put something unreadable."""
        from unittest import mock
        seam_closed(self)   # `serve` closes the git seam for the whole process: restored after the test
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = base = Path(os.path.realpath(tmp.name))
        root, state = base / "root", base / "state"
        root.mkdir()
        state.mkdir(mode=0o700)
        (root / "page.html").write_text(PAGE)
        plant(state / IT.ITEMS, base)
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        self.health_port = s.getsockname()[1]
        s.close()
        cfg = SV.Config(root=root, page=root / "page.html", state=state, adapter=root / "adapter.py",
                        team_domain=TEAM, aud=AUD, hostname=HOSTNAME, port=0, project="single",
                        health_port=self.health_port)
        owners, real = [], SV.owner_server

        def capture(*a, **k):
            owners.append(real(*a, **k))
            return owners[-1]
        self.err = io.StringIO()
        for p in (mock.patch.object(SV, "owner_server", capture), mock.patch("sys.stderr", self.err)):
            p.start()
            self.addCleanup(p.stop)
        t = threading.Thread(target=SV.serve, args=(cfg, lambda _tok: {"email": "owner@example.com"}), daemon=True)
        t.start()
        deadline = time.monotonic() + 15
        while not (owners and cfg.socket.exists()) and t.is_alive() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(owners and cfg.socket.exists(), f"the server did not start: {self.err.getvalue()[-600:]}")

        def stop():
            owners[0].shutdown()
            t.join(10)
            owners[0].server_close()
        self.addCleanup(stop)
        return cfg

    def check_starts_unreadable_then_recovers(self, plant, problem):
        cfg = self.serve(plant)
        # Degraded, not down: 200 on both health doors (a restart cannot fix the file; a 503 invites a restart
        # loop), with the state in the body: register "error" and a note saying what to do, never the path.
        for door, (code, health) in (("agent socket", SV.agent_request(cfg.socket, "GET", "/health")),
                                     ("health port", HealthTests.health(self, self.health_port))):
            with self.subTest(door=door):
                self.assertEqual((code, health["ok"], health["register"]), (200, True, "error"))
                self.assertEqual(health["register_note"], IT.UNREADABLE)
                self.assertIn("items-push", health["register_note"])
                self.assertNotIn(str(self.base), json.dumps(health))
        code, view = SV.agent_request(cfg.socket, "GET", "/view")
        self.assertEqual((code, view["items"], view["view"]["items_note"]), (200, {}, IT.UNREADABLE))
        self.assertNotIn(str(self.base), json.dumps(view))
        logged = [ln for ln in self.err.getvalue().splitlines() if ln.startswith("console items: ")]
        self.assertEqual(len(logged), 1, logged)   # one line, not one per request
        self.assertIn(problem, logged[0])
        self.assertEqual(SV.agent_request(cfg.socket, "POST", "/items", LANE_ITEMS)[0], 200)
        code, health = SV.agent_request(cfg.socket, "GET", "/health")
        self.assertEqual((code, health["ok"], health["register"]), (200, True, "ok"))
        self.assertNotIn("register_note", health)   # recovered: the healthy body's pinned field set again
        code, view = SV.agent_request(cfg.socket, "GET", "/view")
        self.assertEqual((code, view["items"], view["view"]["items_note"]), (200, LANE_ITEMS["items"], None))
        st = os.lstat(cfg.state / IT.ITEMS)
        self.assertTrue(stat.S_ISREG(st.st_mode))
        return cfg

    def test_a_file_that_is_not_json_does_not_stop_the_start(self):
        self.check_starts_unreadable_then_recovers(lambda p, _b: p.write_bytes(b"{not json"), "is not JSON")

    def test_a_file_that_fails_the_check_does_not_stop_the_start(self):
        self.check_starts_unreadable_then_recovers(lambda p, _b: p.write_text(json.dumps({"items": [1]})),
                                                   "items is an object")

    def test_a_symlink_does_not_stop_the_start_and_its_target_is_untouched(self):
        def plant(p, base):
            (base / "outside.json").write_text(json.dumps({"items": {"OUTSIDE": {"title": "not pushed"}}}))
            p.symlink_to(base / "outside.json")
        self.check_starts_unreadable_then_recovers(plant, "not a plain file")
        self.assertIn("OUTSIDE", (self.base / "outside.json").read_text())

    def test_a_fifo_does_not_stop_the_start(self):
        self.check_starts_unreadable_then_recovers(lambda p, _b: planted_fifo(self, p), "not a plain file")

    def test_a_directory_does_not_stop_the_start_and_is_set_aside_not_removed(self):
        def plant(p, _base):
            p.mkdir()
            (p / "kept").write_text("whatever was here")
        cfg = self.check_starts_unreadable_then_recovers(plant, "not a plain file")
        aside = [n for n in os.listdir(cfg.state) if n.startswith(IT.SET_ASIDE)]
        self.assertEqual(len(aside), 1, os.listdir(cfg.state))
        self.assertEqual((cfg.state / aside[0] / "kept").read_text(), "whatever was here")

    def test_a_file_that_cannot_be_opened_does_not_stop_the_start(self):
        # Lane 5 re-review (MEDIUM): tolerance caught only StoreError, so a mode-0000 items.json raised
        # PermissionError out of serve() (the restart loop again), out of /view (no answer) and into /health (503).
        # Any OSError reading the file is now the same unreadable store.
        if os.geteuid() == 0:
            self.skipTest("root reads a mode-0000 file, so this cannot provoke EACCES")

        def plant(p, _base):
            p.write_text(json.dumps(LANE_ITEMS))
            os.chmod(p, 0)
        self.check_starts_unreadable_then_recovers(plant, "cannot be read: Permission denied")


SET_ASIDE_CHILD = r'''
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from overture import items as IT
IT.store_snapshot(Path(sys.argv[2]), json.loads(sys.argv[3]))
'''


class SingleServerItemsTests(_SingleServer, unittest.TestCase):
    """Q24: the single server never imports or runs the project's adapter; items reach it by push only."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(os.path.realpath(tmp.name))
        self.single(self.base)

    def test_the_served_server_never_imports_or_runs_the_adapter(self):
        # AC 1. The real `serve` in a child with an audit hook, an adapter CONFIGURED whose import leaves a marker,
        # and every route exercised. Catches: `serve` loading the adapter again (at start or on any route), which
        # runs project code an agent can write in the server's process, outside every jail. The audit records
        # every event naming the adapter's path (import, open, compile, exec, stat), not only the marker.
        root = Path(tempfile.mkdtemp(prefix="ck-audit-items-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        v1 = git_project(root)
        (root / "page.html").write_text(PAGE)
        marker = root.parent / f"{root.name}-ADAPTER-RAN"
        self.addCleanup(lambda: marker.unlink(missing_ok=True))
        (root / "adapter.py").write_text(MARKER_ADAPTER.format(marker=str(marker)))
        params = {"kit": str(HERE / "plugin" / "kit"), "root": str(root), "host": HOSTNAME,
                  "question": spec_question(v1), "watch": str(root / "adapter.py")}
        r = subprocess.run([sys.executable, "-c", AUDIT_CHILD, json.dumps(params), json.dumps(SPAWN_EVENTS)],
                           capture_output=True, text=True, timeout=120)
        self.assertFalse(marker.exists(), "the server imported the adapter")   # first: it holds even if the child died
        self.assertEqual(r.returncode, 0, r.stderr)
        got = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertEqual(got["touched"], [], "the server touched the adapter's file")
        self.assertEqual(got["spawned"], [])
        for name, code in got["codes"].items():   # the routes really ran
            self.assertLess(code, 500, name)
        self.assertEqual(got["codes"]["agent POST /items"], 200)
        before, after = got["out"]["view_before_push"], got["out"]["view"]
        self.assertEqual((before["items"], before["view"]["items_note"]), ({}, IT.NOT_PUSHED))
        self.assertEqual((sorted(after["items"]), after["view"]["items_note"]), (["LANE", "LANE.1"], None))

    def test_the_served_server_makes_no_syscall_naming_the_adapter(self):
        # AC 1, the syscall half (lane 5 review, LOW: `os.stat(cfg.adapter)` in serve SURVIVED the audit test,
        # because a stat raises no audit event). The same child under strace: no file syscall at all, a stat, an
        # access or a readlink included, names the configured adapter's path.
        need_strace(self)
        root = Path(tempfile.mkdtemp(prefix="ck-strace-items-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        v1 = git_project(root)
        (root / "page.html").write_text(PAGE)
        adapter = root / "adapter.py"
        adapter.write_text(MARKER_ADAPTER.format(marker=str(root.parent / f"{root.name}-ADAPTER-RAN")))
        params = {"kit": str(HERE / "plugin" / "kit"), "root": str(root), "host": HOSTNAME,
                  "question": spec_question(v1)}   # no "watch": the adapter's path is not in the child's argv
        trace = root.parent / f"{root.name}.strace"
        self.addCleanup(lambda: trace.unlink(missing_ok=True))
        r = subprocess.run(["strace", "-f", "-qq", "-e", "trace=%file", "-o", str(trace), sys.executable, "-c",
                            AUDIT_CHILD, json.dumps(params), json.dumps(SPAWN_EVENTS)],
                           capture_output=True, text=True, timeout=180)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        got = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertEqual(got["codes"]["agent POST /items"], 200)   # the routes really ran under the trace
        text = trace.read_text()
        self.assertIn(str(root / "state" / "store.jsonl"), text)   # the trace sees the server's own file calls
        self.assertEqual([ln for ln in text.splitlines() if str(adapter) in ln], [])

    def test_before_the_first_push_the_board_says_why_it_is_empty(self):
        # AC 3. Catches: a silent blank (no note), fake items, and a note that outlives the first push. A push
        # of NO items is a real answer: the board is empty and the note is gone, never shown as "not pushed".
        out = self.sview()
        self.assertEqual((out["items"], out["view"]["items_note"]), ({}, IT.NOT_PUSHED))
        self.assertIn("agent.py items-push", IT.NOT_PUSHED)
        self.assertIsNone(self.sconsole.board())   # no board pushed: the door's 404, never a 503 "malformed"
        self.assertEqual(SV.agent_request(self.scfg.socket, "POST", "/items", LANE_ITEMS)[0], 200)
        out = self.sview()
        self.assertEqual((out["items"], out["view"]["items_note"]), (LANE_ITEMS["items"], None))
        self.assertEqual(SV.agent_request(self.scfg.socket, "POST", "/items", {"items": {}})[0], 200)
        out = self.sview()
        self.assertEqual((out["items"], out["view"]["items_note"]), ({}, None))
        self.assertFalse(self.marker.exists())

    def test_items_reach_the_single_server_by_push_only_through_the_one_check(self):
        # AC 2. The K3 route on the single server's own agent door, refusing as K3 does; unknown keys refused.
        code, out = SV.agent_request(self.scfg.socket, "POST", "/items", LANE_ITEMS)
        self.assertEqual((code, out), (200, {"items": 2, "seeds_added": []}))
        for bad in BAD_PUSHES:
            code, out = SV.agent_request(self.scfg.socket, "POST", "/items", bad)
            self.assertEqual((code, out), (400, {"error": IT.snapshot_problem(bad)}), bad)
        self.assertEqual(self.sview()["items"], LANE_ITEMS["items"])   # every refusal left the push standing
        seed = {**LANE_ITEMS, "seed_questions": [{"qid": "LANE.1/Q1", "item": "LANE.1", "text": "Seeded?",
                                                   "kind": "single",
                                                   "options": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
                                                   "star": None, "valid_if": [], "source": "specs/x.md:1",
                                                   "by": "agent", "nonce": "itemsseed001"}]}
        self.assertEqual(SV.agent_request(self.scfg.socket, "POST", "/items", seed), (200, {"items": 2,
                                                                                        "seeds_added": ["LANE.1/Q1"]}))
        self.assertFalse(self.marker.exists())

    def test_a_push_survives_a_restart(self):
        # AC 4. Catches: items held only in memory, so a restart shows the "not pushed" board until the next push.
        self.assertEqual(SV.agent_request(self.scfg.socket, "POST", "/items", LANE_ITEMS)[0], 200)
        self.stop_single()
        self.start_single()
        out = self.sview()
        self.assertEqual((out["items"], out["view"]["items_note"]), (LANE_ITEMS["items"], None))
        mode = os.stat(self.scfg.state / IT.ITEMS).st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_a_symlink_planted_where_items_json_goes_is_never_followed(self):
        # AC 4. Catches: items.json opened or written through a planted link. The link's target holds items the
        # steward never pushed: they never reach the view, and a push replaces the link, not its target.
        outside = self.base / "outside.json"
        outside.write_text(json.dumps({"items": {"OUTSIDE": {"title": "not pushed"}}}))
        (self.scfg.state / IT.ITEMS).symlink_to(outside)
        with self.assertRaises(SV.StoreError):
            IT.SnapshotAdapter(self.scfg.state).items()
        code, raw = raw_agent(self.scfg.socket, "GET", "/view")   # the single server tolerates it: no items shown
        self.assertEqual(code, 200)
        self.assertNotIn(b"OUTSIDE", raw)
        self.assertEqual(json.loads(raw)["view"]["items_note"], IT.UNREADABLE)
        self.assertEqual(SV.agent_request(self.scfg.socket, "POST", "/items", LANE_ITEMS)[0], 200)
        self.assertIn("OUTSIDE", outside.read_text())                    # the target is untouched
        self.assertFalse((self.scfg.state / IT.ITEMS).is_symlink())      # the link itself was replaced
        self.assertEqual(self.sview()["items"], LANE_ITEMS["items"])

    def test_a_fifo_at_items_json_is_refused_at_once_and_blocks_nothing_after_it(self):
        # Catches (lane 5 review, MEDIUM): opening a FIFO planted as items.json without O_NONBLOCK, which waits
        # for a writer forever, holding the adapter's lock, so every later view, pushed() and push stalls too.
        planted_fifo(self, self.scfg.state / IT.ITEMS)
        adapter = IT.SnapshotAdapter(self.scfg.state)
        for attempt in ("first", "second"):   # the second would queue behind a first one stuck holding the lock
            got = within(self, adapter.items)
            self.assertIsInstance(got.get("error"), SV.StoreError, attempt)
            self.assertIn("not a plain file", str(got["error"]))

    def test_every_items_file_operation_is_dir_relative_and_follows_no_link(self):
        # AC 4, the syscall half: under strace, a push, a re-read and a view touch STATE/items.json only as one
        # component relative to a held STATE descriptor, with no link followed. Catches: any one done by path.
        need_strace(self)
        child = self.base / "items_child.py"
        child.write_text(ITEMS_CHILD)
        root = self.base / "root2"
        root.mkdir()
        (root / "page.html").write_text(PAGE)
        trace = self.base / "items.strace"
        r = subprocess.run(["strace", "-f", "-qq", "-e", "trace=%file", "-o", str(trace), sys.executable, str(child),
                            str(HERE / "plugin" / "kit"), str(root), json.dumps(LANE_ITEMS)],
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), ["LANE", "LANE.1"])
        safe, unsafe = dirfd_ops(trace.read_text(), (IT.ITEMS,))
        self.assertEqual(unsafe, [])
        self.assertGreaterEqual(safe, 3)          # a stat, a read and a rename at least

    def test_a_directory_is_set_aside_relative_to_the_held_state_descriptor(self):
        # Lane 5 re-review (LOW, a surviving mutant): the set-aside rename done by path, which a link swapped in
        # for STATE would redirect. Under strace, the store with a directory at items.json: every call naming it,
        # the set-aside rename included, is an *at call on a real dirfd with one component.
        need_strace(self)
        state = self.base / "state2"
        (state / IT.ITEMS).mkdir(parents=True)
        child = self.base / "aside_child.py"
        child.write_text(SET_ASIDE_CHILD)
        trace = self.base / "aside.strace"
        r = subprocess.run(["strace", "-f", "-qq", "-e", "trace=%file", "-o", str(trace), sys.executable, str(child),
                            str(HERE / "plugin" / "kit"), str(state), json.dumps(LANE_ITEMS)],
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        text = trace.read_text()
        safe, unsafe = dirfd_ops(text, (IT.ITEMS,))
        self.assertEqual(unsafe, [])
        aside = [ln for ln in text.splitlines() if "rename" in ln and IT.SET_ASIDE in ln]
        self.assertEqual(len(aside), 1, text[-2000:])   # the set-aside rename happened, and it is among the safe
        self.assertGreaterEqual(safe, 3)                # the stat, the set-aside rename and the write's rename

    def test_a_push_replaces_a_bad_file_or_a_link_and_sets_only_a_directory_aside(self):
        # Lane 5 re-review (LOW, a surviving mutant): the S_ISDIR guard dropped, so ANY file at items.json would be
        # set aside instead of replaced. A plain bad file and a symlink are replaced in place; only a directory,
        # which a rename cannot replace, is set aside.
        state = self.scfg.state
        outside = self.base / "outside.json"
        outside.write_text(json.dumps({"items": {"OUTSIDE": {"title": "not pushed"}}}))
        p = state / IT.ITEMS
        for kind, asides in (("bad file", 0), ("link", 0), ("dir", 1)):
            with self.subTest(kind=kind):
                if p.exists() or p.is_symlink():
                    p.unlink()
                if kind == "bad file":
                    p.write_bytes(b"{not json")
                elif kind == "link":
                    p.symlink_to(outside)
                else:
                    p.mkdir()
                self.assertEqual(SV.agent_request(self.scfg.socket, "POST", "/items", LANE_ITEMS)[0], 200)
                self.assertTrue(stat.S_ISREG(os.lstat(p).st_mode))
                self.assertEqual(len([n for n in os.listdir(state) if n.startswith(IT.SET_ASIDE)]), asides)
                self.assertEqual(self.sview()["items"], LANE_ITEMS["items"])
        self.assertIn("OUTSIDE", outside.read_text())   # the link's target, untouched

    def test_the_cli_pushes_to_the_single_server_and_the_adapter_runs_in_the_steward(self):
        # AC 3. The real `agent.py items-push`, a separate process, against the single server's own socket.
        # The adapter runs THERE (its marker appears, and only after the CLI ran); the server gets its output.
        from overture import registry as R
        reg = self.base / "cfg" / "overture" / "projects.json"
        R.register(self.scfg.root, self.scfg.state, HERE / "plugin" / "kit", path=reg)
        env = {**os.environ, "XDG_CONFIG_HOME": str(reg.parent.parent)}
        env.pop("OVERTURE_AGENT", None)
        self.assertFalse(self.marker.exists())
        r = subprocess.run([sys.executable, str(HERE / "plugin" / "kit" / "agent.py"), "--state",
                            str(self.scfg.state), "items-push", "--project", str(self.scfg.root),
                            "--adapter", "adapter.py"], capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), {"items": 1, "seeds_added": []})
        self.assertTrue(self.marker.exists())     # it ran, in the steward's process
        out = self.sview()
        self.assertEqual((sorted(out["items"]), out["view"]["items_note"]), (["FROM_ADAPTER"], None))


ITEMS_CHILD = r'''
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from overture import items as IT
from overture import server as SV
root = Path(sys.argv[2])
cfg = SV.Config(root=root, page=root / "page.html", state=root / "state", adapter=root / "none.py",
                team_domain="t.example.com", aud="a" * 64, hostname="h.example.com", port=0)
c = SV.Console(cfg, IT.SnapshotAdapter(cfg.state))
c.seed()
c.push_items(json.loads(sys.argv[3]))
again = SV.Console(cfg, IT.SnapshotAdapter(cfg.state))   # a restart: read back from STATE
print(json.dumps(sorted(again.payload()["items"])))
'''


class ItemsParityTests(_OneServer, _SingleServer, unittest.TestCase):
    """Q24: the same push gives the same answer, byte for byte, on the single server and the one server."""

    def test_the_same_push_gives_the_same_answers_and_the_same_view_on_both_servers(self):
        # Catches: two validators drifting apart (a refusal worded, coded or decided differently on one server),
        # and a /view that differs for the same pushed items.
        self.single(self.t / "single")
        self.spawn()
        both = (lambda m, p, d=None: raw_agent(self.scfg.socket, m, p, d),
                lambda m, p, d=None: raw_agent(self.sock, m, "/p/alpha" + p, d, token=self.tokens["alpha"]))
        views = [json.loads(door("GET", "/view")[1]) for door in both]
        self.assertEqual([(v["items"], v["view"]["items_note"]) for v in views], [({}, IT.NOT_PUSHED)] * 2)
        for body in [*BAD_PUSHES, LANE_ITEMS, {"items": {}, "zzz": [1, 2]}]:
            data = json.dumps(body).encode()
            single, one = (door("POST", "/items", data) for door in both)
            self.assertEqual(single, one, body)
        self.assertEqual(single[0], 400)          # the last one, after the good push: refused on both
        views = [json.loads(door("GET", "/view")[1]) for door in both]
        self.assertEqual(views[0]["items"], LANE_ITEMS["items"])
        self.assertEqual([(v["items"], v["view"]["items_note"]) for v in views],
                         [(LANE_ITEMS["items"], None)] * 2)
        self.stop()
        self.assertFalse(self.marker.exists())
        self.assertFalse(self.p["alpha"]["marker"].exists())


# -- CONSOLE-kit/Q28 ("reviewed_snapshot"): the page is the steward's snapshot in STATE, never a project file ----

DASH = ('<!doctype html><html><body><p id="dash">DASHBOARD-V1</p>'
        '<script>document.body.dataset.pageScript = "ran";</script></body></html>\n')
SENTINEL_PAGE = "<html><body>SENTINEL-PAGE-TARGET a page an agent planted</body></html>\n"


def page_body(html: str, *, ref: str = "origin/main", commit: str = "c0ffee" + "0" * 34,
              path: str = "docs/index.html", reviewed: bool = True) -> dict:
    """A /page-snapshot body for `html`, as `agent.py page-snapshot` sends it."""
    import base64
    return {"content": base64.b64encode(html.encode("utf-8")).decode("ascii"), "ref": ref, "commit": commit,
            "path": path, "reviewed": reviewed}


PAGE_CHILD = r'''
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from overture import items as IT
from overture import server as SV
root = Path(sys.argv[2])
cfg = SV.Config(root=root, page=None, state=root / "state", adapter=root / "none.py",
                team_domain="t.example.com", aud="a" * 64, hostname="h.example.com", port=0)
c = SV.Console(cfg, IT.SnapshotAdapter(cfg.state, tolerant=True))
body = json.loads(sys.argv[3])
print(json.dumps(c.push_page_snapshot(body)))
print(json.dumps(c.page()))   # the staged page shown as a proposal: both files read
print(json.dumps(c.publish_page({"commit": body["commit"]})))
again = SV.Console(cfg, IT.SnapshotAdapter(cfg.state, tolerant=True))   # a restart: read back from STATE
print(json.dumps("DASHBOARD-V1" in again.page() and "DASHBOARD-V1" in again.page()))
'''


class PageSnapshotTests(_SingleServer, unittest.TestCase):
    """Q28: no server reads a page from a project; it serves only the snapshot `agent.py page-snapshot` pushed."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(os.path.realpath(tmp.name))
        self.single(self.base)

    def push(self, body):
        return SV.agent_request(self.scfg.socket, "POST", "/page-snapshot", body)

    def publish(self, commit: str = "c0ffee" + "0" * 34) -> dict:
        """The owner's "Use this page" (Q29), as the owner route calls it; the route itself is PageStagePublishTests'."""
        return self.sconsole.publish_page({"commit": commit})

    def page(self) -> str:
        return self.sconsole.page()   # what the owner door's GET / sends, byte for byte

    # -- point 5: no snapshot yet ------------------------------------------------------------------------

    def test_with_no_snapshot_the_page_is_the_console_with_a_labelled_note(self):
        # AC 5. Catches: a blank page (F63), the project's own page served as a fallback (the root holds one, and
        # `--page` names it), and a console that stops working without a page.
        html = self.page()
        self.assertIn(f'<p class="ck-page-note" role="status">{html_escape(PS.NO_SNAPSHOT)}</p>', html)
        self.assertIn("agent.py page-snapshot", PS.NO_SNAPSHOT)
        self.assertIn(P.BEGIN, html)
        self.assertNotIn("<p>board</p>", html)   # the root's page.html (PAGE), which `--page` named before Q28

    # -- point 2: the snapshot in STATE, read through atfile ----------------------------------------------

    def test_a_pushed_snapshot_is_served_with_its_provenance_and_survives_a_restart(self):
        # AC 2. Catches: the snapshot held in memory only; no provenance line, or one that calls an unreviewed ref
        # reviewed; and the page's own markup or script dropped (point 4: both scripts stay in the page).
        code, out = self.push(page_body(DASH))
        self.assertEqual((code, out), (200, {"staged": "c0ffee" + "0" * 34, "ref": "origin/main", "reviewed": True}))
        self.assertEqual(os.stat(self.scfg.state / PS.STAGED).st_mode & 0o777, 0o600)
        self.stop_single()
        self.start_single()   # the staged page survives a restart too, and is published after it
        self.assertEqual(self.publish(), {"published": "c0ffee" + "0" * 34, "ref": "origin/main", "reviewed": True})
        self.stop_single()
        self.start_single()
        html = self.page()
        self.assertIn('<p id="dash">DASHBOARD-V1</p>', html)
        self.assertIn('document.body.dataset.pageScript = "ran";', html)
        self.assertIn(P.BEGIN, html)
        self.assertLess(html.index("pageScript"), html.index(P.BEGIN))   # the page's script, then the console's
        self.assertIn("Dashboard page from origin/main @ c0ffee000000 (staged by the steward, published by you)",
                      html)
        self.assertEqual(os.stat(self.scfg.state / PS.SNAPSHOT).st_mode & 0o777, 0o600)
        self.assertFalse(os.path.lexists(self.scfg.state / PS.STAGED))   # published: no proposal left
        self.assertNotIn('<div class="ck-page-proposed"', html)
        self.assertEqual(self.push(page_body(DASH, ref="feature/x", reviewed=False))[0], 200)
        self.publish()
        html = self.page()
        self.assertIn("Dashboard page from feature/x @ c0ffee000000 (staged from an unreviewed ref, published by you)",
                      html)
        self.assertNotIn("staged by the steward", html)

    def test_the_route_takes_only_the_closed_schema_and_a_refusal_keeps_the_last_snapshot(self):
        # AC 3, the server's half. Catches: an unknown or missing key accepted, a ref or path that could carry
        # markup into the footer, a page the console cannot be injected into, and a refusal that clobbers the
        # last good snapshot.
        import base64
        self.assertEqual(self.push(page_body(DASH))[0], 200)
        self.publish()
        good = page_body(DASH)
        bads = [{**good, "extra": 1}, {k: v for k, v in good.items() if k != "reviewed"}, [good], "page",
                {**good, "ref": "-x"}, {**good, "ref": "a b"}, {**good, "ref": "<script>"}, {**good, "ref": ""},
                {**good, "commit": "abc123"}, {**good, "commit": "C0FFEE" + "0" * 34}, {**good, "path": "../x.html"},
                {**good, "path": "/abs.html"}, {**good, "reviewed": "yes"}, {**good, "content": "not base64!"},
                {**good, "content": base64.b64encode(b"\xff\xfe<body></body>").decode()},
                page_body("<html>no body</html>"), page_body("<body></body><body></body>"),
                page_body(f"<html><body>{P.BEGIN}x{P.END}</body></html>")]
        for bad in bads:
            code, out = self.push(bad)
            self.assertEqual((code, out), (400, {"error": PS.snapshot_problem(bad)}), bad)
            self.assertIsNotNone(PS.snapshot_problem(bad))
        over = page_body("<html><body>" + "x" * PS.MAX_PAGE + "</body></html>")   # just over: named
        self.assertEqual(self.push(over), (400, {"error": f"the page is over {PS.MAX_PAGE} bytes"}))
        import socket as so   # over the route's body cap: refused on the declared length, before any body is read
        with so.socket(so.AF_UNIX, so.SOCK_STREAM) as s:
            s.settimeout(10)
            s.connect(str(self.scfg.socket))
            s.sendall(f"POST /page-snapshot HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\n"
                      f"Content-Length: {PS.MAX_BODY + 1}\r\n\r\n".encode())
            self.assertTrue(s.recv(4096).startswith(b"HTTP/1.0 413"))
        self.assertIn("DASHBOARD-V1", self.page())   # every refusal left the last snapshot standing
        self.assertFalse(os.path.lexists(self.scfg.state / PS.STAGED))   # and staged nothing

    def test_a_link_fifo_or_directory_at_the_snapshot_is_never_followed_and_a_push_recovers(self):
        # AC 2, for both files (Q29). Catches: either opened or written by path (a planted link would serve, or
        # publish, the agent's page), an open that waits on a FIFO, a directory that leaves the page unreadable for
        # good, and a publish that takes a staged file without checking it again.
        outside = self.base / "outside.json"
        planted = json.dumps(page_body(SENTINEL_PAGE))   # a VALID snapshot: only a followed link would serve it
        outside.write_text(planted)
        kinds = ("link", "fifo", "dir", "unchecked") + (("no access",) if os.geteuid() != 0 else ())
        for name, note in ((PS.SNAPSHOT, PS.UNREADABLE), (PS.STAGED, PS.STAGED_UNREADABLE)):
            snap = self.scfg.state / name
            for kind in kinds:
                with self.subTest(name=name, kind=kind):
                    if kind == "link":
                        snap.symlink_to(outside)
                    elif kind == "no access":   # EACCES out of read_at: the note, never an error out of page()
                        snap.write_text(planted)
                        os.chmod(snap, 0)
                    elif kind == "unchecked":   # a plain file written past the route: re-checked when it is read
                        snap.write_text(json.dumps(page_body(SENTINEL_PAGE, ref="<script>")))
                    elif kind == "fifo":
                        planted_fifo(self, snap)
                    else:
                        snap.mkdir()
                        (snap / "kept").write_text("kept")
                    got = within(self, self.page)
                    html = got.get("value", "")
                    self.assertIn(html_escape(note), html)
                    self.assertNotIn("SENTINEL-PAGE-TARGET", html)
                    if name == PS.STAGED:   # the planted proposal is never published, whatever commit is named
                        err = within(self, lambda: self.publish(page_body(SENTINEL_PAGE)["commit"])).get("error")
                        self.assertIsInstance(err, SV.RequestError)
                        self.assertEqual((err.code, str(err)), (409, PS.STAGED_UNREADABLE))
                        self.assertFalse(os.path.lexists(self.scfg.state / PS.SNAPSHOT))
                    self.assertEqual(self.push(page_body(DASH))[0], 200)
                    self.publish()
                    self.assertIn("DASHBOARD-V1", self.page())
                    self.assertTrue(stat.S_ISREG(os.lstat(self.scfg.state / PS.SNAPSHOT).st_mode))
                    self.assertEqual(outside.read_text(), planted)   # the link's target, untouched
                    for n in (PS.SNAPSHOT, PS.STAGED):
                        (self.scfg.state / n).unlink(missing_ok=True)
            aside = [n for n in os.listdir(self.scfg.state) if n.startswith(f".{name}.set-aside.")]
            self.assertEqual([(self.scfg.state / a / "kept").read_text() for a in aside], ["kept"])

    def test_every_snapshot_file_operation_is_dir_relative_and_follows_no_link(self):
        # AC 2, the syscall half: under strace, a push, a restart and two page reads touch page-snapshot.json only
        # as one component relative to a held STATE descriptor, with no link followed. Catches: any by path.
        need_strace(self)
        child = self.base / "page_child.py"
        child.write_text(PAGE_CHILD)
        root = self.base / "root2"
        root.mkdir()
        trace = self.base / "page.strace"
        r = subprocess.run(["strace", "-f", "-qq", "-e", "trace=%file", "-o", str(trace), sys.executable, str(child),
                            str(HERE / "plugin" / "kit"), str(root), json.dumps(page_body(DASH))],
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.splitlines()[-1], "true")
        text = trace.read_text()
        safe, unsafe = dirfd_ops(text, (PS.SNAPSHOT, PS.STAGED))
        self.assertEqual(unsafe, [])
        self.assertGreaterEqual(safe, 6)   # each file's set-aside stat, rename and reads, and the staged unlink
        for name in (PS.SNAPSHOT, PS.STAGED):   # both are exercised, so the count is not one file's alone
            self.assertEqual(dirfd_ops(text, (name,))[1], [])
            self.assertGreaterEqual(dirfd_ops(text, (name,))[0], 2, name)

    # -- point 1: --page is never read -----------------------------------------------------------------------

    def test_the_served_server_never_reads_its_page_flag_in_a_project_or_through_a_link(self):
        # AC 1. The real `serve` in a child under strace, its --page naming a page inside the project: a plain
        # file, and a symlink to a page outside it. Catches: --page opened, stat'ed or resolved at start or on any
        # route (the old page()), and a link followed to an agent's page. The console starts, says so once, serves
        # the kit-only page, and after a push serves the snapshot.
        need_strace(self)
        for kind in ("plain", "link"):
            with self.subTest(kind=kind):
                root = Path(tempfile.mkdtemp(prefix="ck-q28-page-"))
                self.addCleanup(lambda root=root: __import__("shutil").rmtree(root, ignore_errors=True))
                v1 = git_project(root)
                (root / "page.html").write_text(PAGE)
                (root / "adapter.py").write_text("")
                target = root.parent / f"{root.name}-outside.html"
                self.addCleanup(lambda t=target: t.unlink(missing_ok=True))
                target.write_text(SENTINEL_PAGE)
                page = root / "docs-page.html"
                if kind == "link":
                    page.symlink_to(target)
                else:
                    page.write_text(SENTINEL_PAGE)
                params = {"kit": str(HERE / "plugin" / "kit"), "root": str(root), "host": HOSTNAME,
                          "question": spec_question(v1), "page": str(page), "snapshot": page_body(DASH)}
                trace = root.parent / f"{root.name}.strace"
                self.addCleanup(lambda t=trace: t.unlink(missing_ok=True))
                r = subprocess.run(["strace", "-f", "-qq", "-e", "trace=%file", "-o", str(trace), sys.executable,
                                    "-c", AUDIT_CHILD, json.dumps(params), json.dumps(SPAWN_EVENTS)],
                                   capture_output=True, text=True, timeout=180)
                self.assertEqual(r.returncode, 0, r.stderr[-2000:])
                got = json.loads(r.stdout.strip().splitlines()[-1])
                self.assertEqual(got["codes"]["agent POST /page-snapshot"], 200)
                self.assertEqual(got["codes"]["owner GET /api/page-staged"], 200)
                self.assertEqual(got["codes"]["owner POST /api/page-publish"], 200)
                before, staged, after = (got["out"]["page_before_snapshot"], got["out"]["page_staged"],
                                         got["out"]["page"])
                self.assertIn(html_escape(PS.NO_SNAPSHOT), before)
                self.assertIn(html_escape(PS.NO_SNAPSHOT), staged)   # staged alone serves nothing (point 5)
                self.assertNotIn("DASHBOARD-V1", staged)
                self.assertIn("Proposed dashboard: origin/main @ c0ffee000000", staged)
                self.assertIn("DASHBOARD-V1", got["out"]["preview"])
                self.assertIn("DASHBOARD-V1", after)
                for html in (before, staged, after):
                    self.assertNotIn("SENTINEL-PAGE-TARGET", html)
                text = trace.read_text()
                self.assertIn(str(root / "state" / "store.jsonl"), text)   # the trace sees the server's file calls
                self.assertEqual([ln for ln in text.splitlines() if str(page) in ln or str(target) in ln], [])
                self.assertEqual(r.stderr.count("is not read (CONSOLE-kit/Q28)"), 1, r.stderr[-2000:])

    # -- point 3: the steward's command ------------------------------------------------------------------

    def cli_project(self):
        """The single server's root as a git work tree: the page committed and merged (origin/main), registered."""
        from overture import registry as R
        root = self.scfg.root
        (root / "docs").mkdir()
        (root / "docs" / "index.html").write_text(DASH)
        git(root, "init", "-q", "-b", "main")
        git(root, "add", "docs/index.html")
        git(root, "commit", "-qm", "page v1")
        git(root, "update-ref", "refs/remotes/origin/main", "HEAD")   # what a `git fetch` of the merged page gives
        reg = self.base / "cfg" / "overture" / "projects.json"
        R.register(root, self.scfg.state, HERE / "plugin" / "kit", path=reg)
        self.env = {**os.environ, **GIT_ENV, "XDG_CONFIG_HOME": str(reg.parent.parent)}
        self.env.pop("OVERTURE_AGENT", None)
        return root

    def cli(self, *args):
        return subprocess.run([sys.executable, str(HERE / "plugin" / "kit" / "agent.py"), "--state",
                               str(self.scfg.state), "page-snapshot", *args], cwd=self.scfg.root,
                              capture_output=True, text=True, env=self.env, timeout=120)

    def test_the_cli_sends_the_merged_commit_and_a_working_tree_edit_never_reaches_the_page(self):
        # AC 3 and the brief's named test. Catches: the page read from the working tree (an agent's uncommitted or
        # staged edit would reach the owner's browser), and a commit nobody merged accepted as reviewed.
        root = self.cli_project()
        r = self.cli("--path", "docs/index.html")
        self.assertEqual(r.returncode, 0, r.stderr)
        head = git(root, "rev-parse", "HEAD").stdout.strip()
        self.assertEqual(json.loads(r.stdout), {"staged": head, "ref": "origin/main", "reviewed": True})
        self.assertIn(f'staged {head[:12]}: the console shows it as a proposal; it is served once the owner presses '
                      f'"{PS.USE}"', r.stderr)
        self.assertIn(html_escape(PS.NO_SNAPSHOT), self.page())   # Q29: staging alone serves nothing
        self.publish(head)
        self.assertIn(f"Dashboard page from origin/main @ {head[:12]} (staged by the steward, published by you)",
                      self.page())
        (root / "docs" / "index.html").write_text(DASH.replace("DASHBOARD-V1", "EDITED-IN-THE-WORKING-TREE"))
        git(root, "add", "docs/index.html")   # staged, too: still not a commit
        self.assertEqual(self.cli("--path", "docs/index.html").returncode, 0)
        self.publish(head)
        self.assertIn("DASHBOARD-V1", self.page())
        self.assertNotIn("EDITED-IN-THE-WORKING-TREE", self.page())
        git(root, "commit", "-qm", "an unmerged edit")   # on main, not on origin/main
        r = self.cli("--from-ref", "main", "--path", "docs/index.html")
        self.assertEqual(r.returncode, 1)
        self.assertIn("not in origin/main", r.stderr)
        self.assertIn("refused, nothing sent", r.stderr)
        self.assertNotIn("EDITED-IN-THE-WORKING-TREE", self.page())
        r = self.cli("--from-ref", "main", "--path", "docs/index.html", "--unreviewed")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("EDITED-IN-THE-WORKING-TREE", self.page())   # proposed, not served
        self.assertIn("· unreviewed", self.page())
        self.publish(json.loads(r.stdout)["staged"])
        html = self.page()
        self.assertIn("EDITED-IN-THE-WORKING-TREE", html)
        self.assertIn("(staged from an unreviewed ref, published by you)", html)

    def test_the_cli_reads_head_from_the_commit_even_with_a_dirty_tree(self):
        # Code review LOW (M3 survived): the page read from the working tree when the ref is HEAD, the one ref a
        # dirty checkout makes look the same as "what is here". HEAD is a commit like any other: with an
        # uncommitted edit (unstaged, then staged), the snapshot carries HEAD's committed bytes, never the edit.
        root = self.cli_project()
        head = git(root, "rev-parse", "HEAD").stdout.strip()
        dirty = DASH.replace("DASHBOARD-V1", "DIRTY-WORKING-TREE")
        for staged in (False, True):
            with self.subTest(staged=staged):
                (root / "docs" / "index.html").write_text(dirty)
                if staged:
                    git(root, "add", "docs/index.html")
                r = self.cli("--from-ref", "HEAD", "--path", "docs/index.html", "--unreviewed")
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(json.loads(r.stdout), {"staged": head, "ref": "HEAD", "reviewed": False})
                doc, _ = PS.load(self.scfg.state, PS.STAGED)
                self.assertEqual(PS.page_text(doc["content"]), DASH)   # the committed bytes, exactly
                self.assertNotIn("DIRTY-WORKING-TREE", self.page())

    def test_the_cli_refuses_a_link_in_the_commit_an_absent_page_and_no_origin_main(self):
        # AC 3. Catches: a page that is a symlink in the commit (git stores the link's target as its blob), a
        # missing --path, and the ancestor check passing when origin/main was never fetched.
        root = self.cli_project()
        (root / "docs" / "link.html").symlink_to("index.html")
        git(root, "add", "docs/link.html")
        git(root, "commit", "-qm", "a link")
        git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
        for args, why in ((("--path", "docs/link.html"), "is not a plain file"),
                          (("--path", "docs/none.html"), "is not a plain file"),
                          ((), "pass --path"),
                          (("--from-ref=-x", "--path", "docs/index.html"), "a ref of plain characters")):
            with self.subTest(args=args):
                r = self.cli(*args)
                self.assertEqual(r.returncode, 1, r.stderr)
                self.assertIn(why, r.stderr)
        git(root, "update-ref", "-d", "refs/remotes/origin/main")
        r = self.cli("--from-ref", "main", "--path", "docs/index.html")
        self.assertEqual(r.returncode, 1)
        self.assertIn("origin/main is not in", r.stderr)
        self.assertIn(html_escape(PS.NO_SNAPSHOT), self.page())   # nothing was ever sent


PREVIEW_IFRAME = '<iframe sandbox="" src="/api/page-staged" title="Proposed dashboard page" referrerpolicy="no-referrer">'


class PageStagePublishTests(_Live, unittest.TestCase):
    """Q29 ("Agents stage, I publish"): a pushed page is a proposal; only the owner's route serves it."""

    C1 = "c0ffee" + "0" * 34
    C2 = "beef" + "1" * 36

    def stage(self, html=DASH, commit=C1, **kw):
        code, out = self.agent_post("/page-snapshot", page_body(html, commit=commit, **kw))
        self.assertEqual(code, 200, out)
        return out

    def page(self) -> str:
        code, html = self.get("/")
        self.assertEqual(code, 200)
        return html

    def publish(self, body, **kw):
        return self.req("POST", "/api/page-publish", body, tok=kw.pop("tok", token()), **kw)

    def served(self) -> bytes | None:
        snap = self.cfg.state / PS.SNAPSHOT
        return snap.read_bytes() if snap.exists() else None

    def raw_get(self, path, tok=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", path, headers={"Cf-Access-Jwt-Assertion": token()} if tok else {})
        r = conn.getresponse()
        out = (r.status, dict(r.getheaders()), r.read())
        conn.close()
        return out

    def test_staging_alone_changes_nothing_served_and_shows_the_proposal(self):
        # Points 1, 3 and 5. Catches: /page-snapshot serving directly (the mutant the brief names), no proposal
        # shown, a proposal that leaves out the ref, commit, size or kind, and the no-snapshot note dropped.
        out = self.stage()
        self.assertEqual(out, {"staged": self.C1, "ref": "origin/main", "reviewed": True})
        self.assertIsNone(self.served())
        html = self.page()
        self.assertNotIn("DASHBOARD-V1", html)
        self.assertNotIn("pageScript", html)
        self.assertIn(f'<p class="ck-page-note" role="status">{html_escape(PS.NO_SNAPSHOT)}</p>', html)
        size = len(DASH.encode())
        self.assertIn(f'<p class="ck-page-proposed-line">Proposed dashboard: origin/main @ c0ffee000000 · '
                      f'{size} bytes · reviewed (agent&#x27;s claim, not checked by the server)</p>', html)
        self.assertIn("not checked by the server", PS.REVIEWED_CLAIM)   # the label is the agent's, never the server's
        self.assertIn(f'<button type="button" class="ck-page-use" data-commit="{self.C1}">Use this page</button>',
                      html)
        self.assertIn(PREVIEW_IFRAME, html)
        # Owner, 2026-10-02: reviewed in a modal dialog, never opened by the server (nothing opens on load); the
        # waiting button is hidden until console.js can open the dialog, which shows inline until then.
        import re
        self.assertEqual(re.findall(r"<dialog\b[^>]*>", html[:html.index(P.BEGIN)]),   # console.js has its own
                         ['<dialog class="ck-page-dialog" aria-labelledby="ck-page-dialog-title" tabindex="-1">'])
        self.assertIn('<h2 class="ck-page-dialog-title" id="ck-page-dialog-title">New dashboard page</h2>', html)
        self.assertIn(f'<button type="button" class="ck-page-waiting" aria-haspopup="dialog" hidden>{PS.WAITING}'
                      '</button>', html)
        self.assertIn('<button type="button" class="ck-page-not-now">Not now</button>', html)
        self.stage(commit=self.C2, ref="feature/x", reviewed=False)   # a second staging replaces the proposal
        html = self.page()
        self.assertIn(f"Proposed dashboard: feature/x @ {self.C2[:12]} · {size} bytes · unreviewed", html)
        self.assertNotIn(self.C1[:12], html)
        self.assertIsNone(self.served())

    def test_the_preview_frame_is_sandboxed_with_no_allowance_at_all(self):
        # Point 3, the attribute layer. Catches: allow-scripts or allow-same-origin added to the frame (the second
        # would hand the staged script the console's origin), and the frame pointed anywhere but the gated route.
        import re
        self.stage()
        html = self.page()
        strip = html[html.index('<div class="ck-page-proposed"'):html.index(P.BEGIN)]   # console.js has its own
        frames = re.findall(r"<iframe\b[^>]*>", strip)
        self.assertEqual(frames, [PREVIEW_IFRAME])

    def test_the_preview_route_is_gated_and_answers_under_the_sandbox_csp(self):
        # Point 3, the header layer: each layer alone stops the script. Catches: the route ungated, the staged page
        # sent under the console's own CSP (which runs scripts), or as anything but the exact staged bytes.
        self.assertEqual(self.raw_get("/api/page-staged")[0], 404)   # nothing staged
        self.stage()
        self.assertEqual(self.raw_get("/api/page-staged", tok=False)[0], 403)
        code, headers, body = self.raw_get("/api/page-staged")
        self.assertEqual(code, 200)
        self.assertEqual(headers["Content-Security-Policy"], SV.VISUAL_HTML_CSP)
        self.assertTrue(SV.VISUAL_HTML_CSP.startswith("sandbox; default-src 'none';"))
        self.assertNotIn("script-src", SV.VISUAL_HTML_CSP)
        self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
        self.assertEqual(body, DASH.encode())
        self.assertIsNone(self.served())   # a preview publishes nothing

    def test_publish_takes_the_staged_commit_and_refuses_a_mismatch_by_name(self):
        # Point 2. Catches: a publish with nothing staged, a stale page's button publishing a newer staging the
        # owner never saw, a bad body accepted, and a staged file left behind to be published twice.
        self.assertEqual(self.publish({"commit": self.C1}),
                         (404, {"error": "no dashboard page is staged; ask the steward to run agent.py page-snapshot"}))
        self.stage(commit=self.C2)
        code, out = self.publish({"commit": self.C1})
        self.assertEqual(code, 409)
        self.assertEqual(out["error"], f"the staged page is now {self.C2[:12]}, not {self.C1[:12]}: a newer one was "
                                       f"staged since this page loaded; reload, check it, and press \"Use this page\" "
                                       f"again")
        for bad in ({}, {"commit": "abc"}, {"commit": self.C2, "extra": 1}, [self.C2], {"commit": self.C2.upper()}):
            self.assertEqual(self.publish(bad)[0], 400, bad)
        self.assertIsNone(self.served())
        self.assertEqual(self.publish({"commit": self.C2}),
                         (200, {"published": self.C2, "ref": "origin/main", "reviewed": True}))
        html = self.page()
        self.assertIn("DASHBOARD-V1", html)
        self.assertIn(f"Dashboard page from origin/main @ {self.C2[:12]} (staged by the steward, published by you)",
                      html)
        self.assertNotIn('<div class="ck-page-proposed"', html)
        self.assertNotIn(html_escape(PS.NO_SNAPSHOT), html)
        self.assertFalse(os.path.lexists(self.cfg.state / PS.STAGED))
        self.assertEqual(self.publish({"commit": self.C2})[0], 404)   # once

    def test_publish_checks_the_staged_file_again(self):
        # Point 2: the inject and caps checks re-run at publish. Catches: a staged file written past the route
        # (a page the console cannot be injected into, a markup ref) published because the route checked it once.
        for bad in (page_body("<html>no body</html>"), page_body(DASH, ref="<script>"),
                    page_body("<html><body>" + "x" * PS.MAX_PAGE + "</body></html>")):
            (self.cfg.state / PS.STAGED).write_text(json.dumps(bad))
            self.assertEqual(self.publish({"commit": bad["commit"]}), (409, {"error": PS.STAGED_UNREADABLE}))
            self.assertIsNone(self.served())
            self.assertIn(html_escape(PS.STAGED_UNREADABLE), self.page())

    def test_the_publish_route_is_behind_access_and_origin(self):
        # Point 2: the answer/lock gate. Catches: the route without the Access check, or without the Origin check
        # (a page on another site, or a request with no Origin, publishing in the owner's session).
        self.stage()
        for case, kw in (("no token", {"tok": None}), ("bad token", {"tok": "x"}),
                         ("another site", {"headers": {"Origin": "https://evil.example.com"}}),
                         ("no Origin", {"origin": False})):
            with self.subTest(case=case):
                self.assertEqual(self.publish({"commit": self.C1}, **kw)[0], 403)
                self.assertIsNone(self.served())
        self.assertEqual(self.publish({"commit": self.C1})[0], 200)

    def test_no_agent_route_changes_the_served_page(self):
        # Point 4. Every agent-door route, GET and POST, driven with a staging body, a publish body and nothing,
        # while a different page is staged. Catches: an agent verb that publishes, /page-snapshot publishing, and
        # a publish route reachable from the agent socket. The route list is the one server's own, so a new agent
        # route is driven here without editing this test.
        self.stage()
        self.assertEqual(self.publish({"commit": self.C1})[0], 200)
        served = self.served()
        self.stage(DASH.replace("DASHBOARD-V1", "STAGED-V2"), commit=self.C2)
        posts = sorted(MS.POST_ROUTES | {"/page-publish", "/api/page-publish", "/publish", "/use"})
        bodies = (page_body(DASH.replace("DASHBOARD-V1", "STAGED-V2"), commit=self.C2), {"commit": self.C2}, None)
        for path in posts:
            for body in bodies:
                SV.agent_request(self.cfg.socket, "POST", path, body)
                self.assertEqual(self.served(), served, (path, body))
        for path in ("/page-publish", "/api/page-publish"):
            self.assertEqual(SV.agent_request(self.cfg.socket, "POST", path, {"commit": self.C2})[0], 404)
        for path in ("/view", "/check", "/health", "/history-wants", "/page-staged", "/api/page-staged"):
            SV.agent_request(self.cfg.socket, "GET", path)
        self.assertEqual(self.served(), served)
        html = self.page()
        self.assertIn("DASHBOARD-V1", html)
        self.assertNotIn("STAGED-V2", html)


class PageParityTests(_OneServer, _SingleServer, unittest.TestCase):
    """Q28: the same push is answered alike and serves the same page on the single server and the one server."""

    def test_the_same_snapshot_gives_the_same_answers_and_the_same_page_on_both_servers(self):
        # Catches: the one server still reading its root's page (server.json's "page" names one), a /page-snapshot
        # route that is missing there or checks differently, and pages that differ for the same push.
        self.single(self.t / "single")
        self.spawn()
        both = (lambda m, p, d=None: raw_agent(self.scfg.socket, m, p, d),
                lambda m, p, d=None: raw_agent(self.sock, m, "/p/alpha" + p, d, token=self.tokens["alpha"]))
        (self.p["alpha"]["root"] / "index.html").write_text(SENTINEL_PAGE)   # server.json's default page
        for body in (page_body(DASH, ref="-x"), {"content": ""}, page_body(DASH)):
            data = json.dumps(body).encode()
            self.assertEqual(both[0]("POST", "/page-snapshot", data), both[1]("POST", "/page-snapshot", data), body)
        proposed = P.strip(self.sconsole.page())   # Q29: the same proposal, and nothing served, on both
        code, one = self.owner("alpha", "GET", "/")
        self.assertEqual((code, P.strip(one)), (200, proposed))
        self.assertIn("Proposed dashboard: origin/main @ c0ffee000000", proposed)
        self.assertNotIn("DASHBOARD-V1", proposed)
        commit = page_body(DASH)["commit"]
        for wrong in ({"commit": "f" * 40}, {}):   # refused alike
            code, out = self.owner("alpha", "POST", "/api/page-publish", wrong)
            with self.assertRaises(SV.RequestError) as e:
                self.sconsole.publish_page(wrong)
            self.assertEqual((code, out), (e.exception.code, {"error": str(e.exception)}), wrong)
        self.assertEqual(self.owner("alpha", "POST", "/api/page-publish", {"commit": commit}),
                         (200, self.sconsole.publish_page({"commit": commit})))
        single = P.strip(self.sconsole.page())
        code, one = self.owner("alpha", "GET", "/")
        self.assertEqual(code, 200)
        self.assertEqual(P.strip(one), single)
        self.assertIn("DASHBOARD-V1", single)
        self.assertNotIn("SENTINEL-PAGE-TARGET", one)


# -- CONSOLE-kit/Q30-Q32: refactoring a stale answer ---------------------------------------------------------

RULES = ("# Rules\n\nRULE-ALPHA: every release is reviewed by a second agent before it ships.\n"
         "RULE-BETA: the console never runs the project's code.\n\nNotes follow.\n")
REFACTOR_ACTIONS = {
    "withdraw": lambda t: {"action": "withdraw", "qid": t.QID, "lock": t.lock_id, "reason": "obsolete"},
    "untrack": lambda t: {"action": "untrack", "qid": t.QID, "lock": t.lock_id},
    "confirm": lambda t: {"action": "confirm", "qid": t.QID, "proposal": t.proposal_id or "0" * 24},
    "propose": lambda t: {"qid": t.QID, "cites": ["rules.md:4"], "basis": "the rule moved"},
}


class RefactorTests(_Live, unittest.TestCase):
    """CONSOLE-kit/Q30 both, Q31 owner_confirms, Q32 stands_until: the six properties of lane 7."""

    QID = "LANE.1/Q2"

    def setUp(self):
        ServerTests.setUp(self)
        seam_closed(self)
        self.proposal_id = None
        (self.cfg.root / "rules.md").write_text(RULES)
        q = {"qid": self.QID, "item": "LANE.1", "text": "Is RULE-ALPHA still the rule?", "kind": "single",
             "options": [{"id": "a", "label": "Yes"}, {"id": "b", "label": "No"}], "star": "a",
             "valid_if": [{"kind": "excerpt", "path": "rules.md",
                           "text": "RULE-ALPHA: every release is reviewed by a second agent"}],
             "source": "rules.md:3", "nonce": "rxask000001"}
        code, out = self.agent_post("/question", q)
        self.assertEqual(code, 200, out)
        code, a = self.req("POST", "/api/answer", self.answer(qid=self.QID, nonce="rxanswer01"), tok=token())
        self.assertEqual(code, 200, a)
        code, lk = self.req("POST", "/api/lock", {"qid": self.QID, "answer": a["record"]["id"], "nonce": "rxlock0001"},
                            tok=token())
        self.assertEqual(code, 200, lk)
        self.lock_id = lk["record"]["id"]

    def go_stale(self, extra=""):
        """The rule is reworded: the excerpt no longer holds. `extra` adds lines a proposal can cite."""
        (self.cfg.root / "rules.md").write_text(RULES.replace("every release is reviewed by a second agent",
                                                               "a release ships after one review") + extra)

    def act(self, body, **kw):
        if isinstance(body, dict) and "action" in body and "nonce" not in body:
            body = {**body, "nonce": "rxact" + os.urandom(5).hex()}
        return self.req("POST", "/api/refactor", body, tok=kw.pop("tok", token()), **kw)

    def view_q(self, qid=None):
        code, out = self.req("GET", "/api/view", tok=token())
        self.assertEqual(code, 200, out)
        return out["view"], out["view"]["questions"][qid or self.QID]

    def propose(self, cites=("rules.md:4",), basis="RULE-BETA carries the ruling now"):
        return self.agent_post("/anchor-proposal", {"qid": self.QID, "cites": list(cites), "basis": basis,
                                                    "nonce": "rxprop" + os.urandom(4).hex()})

    def store_kinds(self):
        return [json.loads(ln)["type"] for ln in self.cfg.store.read_text().splitlines()]

    # -- property 2: only a stale answer -------------------------------------------------------

    def test_every_act_on_an_answer_whose_anchor_holds_is_refused_by_name(self):
        # Catches: refactoring a ruling that still holds (a withdraw that needs no reason to exist), and a
        # proposal against a fresh answer. Each refusal names why, and nothing is written.
        for name in ("withdraw", "untrack"):
            code, out = self.act(REFACTOR_ACTIONS[name](self))
            self.assertEqual(code, 409, out)
            self.assertIn(f"{self.QID} is not stale", out["error"])
        code, out = self.propose()
        self.assertEqual(code, 409, out)
        self.assertIn("is not stale", out["error"])
        code, out = self.act({"action": "withdraw", "qid": "LANE.1/Q1", "lock": self.lock_id, "reason": "x"})
        self.assertEqual(code, 409, out)
        self.assertIn("has no locked answer", out["error"])
        self.assertFalse((self.cfg.state / "refactor.jsonl").exists())

    # -- Q30: withdraw, keep unchecked ---------------------------------------------------------------

    def test_withdraw_needs_a_reason_settles_the_ruling_and_deletes_nothing(self):
        # Catches: a withdraw without the owner's reason, a withdrawn ruling still in the inbox or counted stale,
        # and the old answer, lock or anchor removed (property 3: nothing is deleted).
        self.go_stale()
        before = self.cfg.store.read_bytes()
        code, out = self.act({"action": "withdraw", "qid": self.QID, "lock": self.lock_id})
        self.assertEqual(code, 400, out)
        self.assertIn("reason", out["error"])
        code, out = self.act(REFACTOR_ACTIONS["withdraw"](self))
        self.assertEqual(code, 200, out)
        view, q = self.view_q()
        self.assertEqual(q["state"], "withdrawn")
        self.assertNotIn(self.QID, view["inbox"])
        self.assertEqual(view["items"]["LANE.1"]["own"]["stale"], 0)
        o = q["refactor"]["outcome"]
        self.assertEqual((o["kind"], o["by"], o["reason"]), ("withdrawn", "owner", "obsolete"))
        self.assertEqual(self.cfg.store.read_bytes(), before)   # the store is untouched; the act is beside it
        self.assertEqual(self.store_kinds().count("lock"), 1)
        self.assertEqual(self.console.check()["stale"], {})
        code, out = self.act(REFACTOR_ACTIONS["untrack"](self))   # settled once
        self.assertEqual(code, 409, out)
        self.assertIn("already withdrawn", out["error"])

    def test_keep_unchecked_takes_an_optional_reason_and_the_ruling_stands(self):
        # Catches: keep needing a reason (agent-5's ruling: optional), a kept ruling read as withdrawn, and one
        # still checked against the files.
        self.go_stale()
        code, out = self.act(REFACTOR_ACTIONS["untrack"](self))
        self.assertEqual(code, 200, out)
        view, q = self.view_q()
        self.assertEqual(q["state"], "locked")
        self.assertEqual(q["failing"], [])
        self.assertEqual(q["refactor"]["outcome"]["kind"], "untracked")
        self.assertNotIn("reason", q["refactor"]["outcome"])
        self.assertNotIn(self.QID, view["inbox"])

    def test_an_act_on_a_page_that_is_out_of_date_is_refused(self):
        # Catches: an act that ignores the lock the page showed: after a supersede and re-lock the old page's
        # Withdraw would settle a ruling the owner never saw.
        self.go_stale()
        code, out = self.act({**REFACTOR_ACTIONS["withdraw"](self), "lock": "f" * 24})
        self.assertEqual(code, 409, out)
        self.assertIn("is not the current lock", out["error"])
        for bad in ({"action": "nuke", "qid": self.QID}, {"action": "withdraw"}, [1],
                    {**REFACTOR_ACTIONS["untrack"](self), "proposal": "x"}):
            self.assertEqual(self.act(bad)[0], 400, bad)

    # -- Q31: propose, then confirm --------------------------------------------------------------------

    def test_a_proposal_changes_nothing_until_the_owner_confirms_it(self):
        # Catches: a proposal that re-anchors by itself (the steward choosing its own passage), and a confirm that
        # does not make the confirmed anchor decide the lock.
        self.go_stale()
        code, out = self.propose()
        self.assertEqual(code, 200, out)
        p = out["record"]
        self.assertEqual(p["anchors"], [{"kind": "excerpt", "path": "rules.md",
                                         "text": "RULE-BETA: the console never runs the project's code."}])
        _, q = self.view_q()
        self.assertEqual(q["state"], "stale")                     # proposed is not confirmed
        self.assertEqual(q["refactor"]["proposal"]["id"], p["id"])
        self.assertEqual(q["lock"], self.lock_id)
        code, out = self.act({"action": "confirm", "qid": self.QID, "proposal": p["id"]})
        self.assertEqual(code, 200, out)
        _, q = self.view_q()
        self.assertEqual((q["state"], q["anchored_by"]), ("locked", "confirmed"))
        self.assertEqual(q["refactor"]["confirmed"]["proposal"], p["id"])
        self.assertNotIn("proposal", q["refactor"])
        code, out = self.act({"action": "confirm", "qid": self.QID, "proposal": p["id"]})
        self.assertEqual(code, 409, out)   # fresh now: not stale, so nothing to confirm

    def test_a_proposal_is_checked_when_proposed_and_again_when_confirmed(self):
        # Property 4. Catches: a cite to text found twice (it would hold while the one meant changed), a cite past
        # the end, a secrets file, and a confirm that trusts what was read at propose time.
        self.go_stale(extra="RULE-BETA: the console never runs the project's code.\n")
        code, out = self.propose()
        self.assertEqual(code, 400, out)
        self.assertIn("2 times", out["error"])
        self.go_stale()
        for cites, why in ((["rules.md:400"], "runs past the end"), ([".env:1"], "secrets"),
                           (["rules.md:2"], "blank")):
            code, out = self.propose(cites=cites)
            self.assertEqual(code, 400, (cites, out))
            self.assertIn(why, out["error"])
        code, out = self.propose()
        self.assertEqual(code, 200, out)
        pid = out["record"]["id"]
        (self.cfg.root / "rules.md").write_text(RULES.replace("every release is reviewed by a second agent", "x")
                                                .replace("never runs", "runs"))   # the proposed text is gone now
        code, out = self.act({"action": "confirm", "qid": self.QID, "proposal": pid})
        self.assertEqual(code, 409, out)
        self.assertIn("no longer holds", out["error"])
        _, q = self.view_q()
        self.assertEqual(q["state"], "stale")
        self.assertNotIn("confirmed", q.get("refactor", {}))

    def test_only_the_latest_proposal_can_be_confirmed(self):
        # Catches: confirming a proposal the owner was not shown because a newer one replaced it.
        self.go_stale(extra="RULE-GAMMA: answers are locked by the owner alone.\n")
        first = self.propose()[1]["record"]["id"]
        second = self.propose(cites=("rules.md:7",))[1]["record"]["id"]
        code, out = self.act({"action": "confirm", "qid": self.QID, "proposal": first})
        self.assertEqual(code, 409, out)
        self.assertIn("no longer the latest", out["error"])
        self.assertEqual(self.act({"action": "confirm", "qid": self.QID, "proposal": second})[0], 200)

    # -- Q32: replace ------------------------------------------------------------------------------

    def replacement(self, n=3, **over):
        q = {"qid": f"LANE.1/Q{n}", "item": "LANE.1", "text": "What is the review rule now?", "kind": "single",
             "options": [{"id": "a", "label": "One review"}, {"id": "b", "label": "Two"}], "star": "a",
             "valid_if": [], "source": "rules.md:3", "replaces": self.QID, "nonce": f"rxrepl{n:04d}"}
        q.update(over)
        return self.agent_post("/question", q)

    def test_a_replacement_leaves_the_old_ruling_in_force_until_it_is_locked(self):
        # Catches: the old ruling suspended while its replacement is open, never marked superseded once the
        # replacement locks, and a replacement of a ruling that still holds.
        code, out = self.replacement()
        self.assertEqual(code, 409, out)
        self.assertIn("is not stale", out["error"])
        self.go_stale()
        code, out = self.replacement()
        self.assertEqual(code, 200, out)
        self.assertEqual(out["record"]["replaces"], self.QID)
        self.assertNotIn("replaces", self.cfg.store.read_text())   # the question record is an ordinary question
        _, old = self.view_q()
        self.assertEqual(old["state"], "stale")                     # in force, stale, linked
        self.assertEqual(old["refactor"]["replaced_by"], "LANE.1/Q3")
        self.assertEqual(self.view_q("LANE.1/Q3")[1]["refactor"]["replaces"], self.QID)
        code, out = self.replacement(n=4)
        self.assertEqual(code, 409, out)
        self.assertIn("already being replaced by LANE.1/Q3", out["error"])
        self.assertIsNone(self.console.store.question("LANE.1/Q4"))   # refused before the question was stored
        code, a = self.req("POST", "/api/answer", self.answer(qid="LANE.1/Q3", nonce="rxanswer03"), tok=token())
        code, lk = self.req("POST", "/api/lock", {"qid": "LANE.1/Q3", "answer": a["record"]["id"],
                                                  "nonce": "rxlock0003"}, tok=token())
        self.assertEqual(code, 200, lk)
        view, old = self.view_q()
        self.assertEqual(old["state"], "superseded")
        o = old["refactor"]["outcome"]
        self.assertEqual((o["kind"], o["replaced_by"], o["record"], o["by"]),
                         ("superseded", "LANE.1/Q3", lk["record"]["id"], "owner"))
        self.assertNotIn(self.QID, view["inbox"])

    def test_an_ask_whose_link_was_not_written_is_linked_by_its_retry(self):
        # Lane 7 review LOW. The question lands first (the link must name a question that exists), so a failed
        # link write leaves it unlinked behind a named 500. Catches: a 500 that does not say how to recover, a
        # retry that returns the question still unlinked, and a link written after the owner answered it unlinked.
        self.go_stale()
        real = self.console._rx_append

        def broken(rec):
            raise SV.RequestError(409, "the disk is full")
        self.console._rx_append = broken
        try:
            code, out = self.replacement()
        finally:
            self.console._rx_append = real
        self.assertEqual(code, 500, out)
        self.assertIn('"nonce": "rxrepl0003"', out["error"])
        self.assertIn("the disk is full", out["error"])
        _, old = self.view_q()
        self.assertNotIn("replaced_by", old.get("refactor", {}))
        code, out = self.replacement()                                   # the same ask, the same nonce
        self.assertEqual(code, 200, out)
        self.assertEqual(out["record"]["replaces"], self.QID)
        self.assertEqual(self.view_q()[1]["refactor"]["replaced_by"], "LANE.1/Q3")
        self.assertEqual(self.replacement()[1]["record"]["replaces"], self.QID)   # and again: one link, not two
        log = (self.cfg.state / "refactor.jsonl").read_text().splitlines()
        self.assertEqual([json.loads(ln)["type"] for ln in log], ["replaces"])

    def test_a_reused_nonce_with_another_replaces_is_refused_by_name(self):
        # Review LOW. `replaces` is kept beside the store, so the question record two asks dedupe on does not
        # carry it. Catches: a retry naming another ruling (or none) taken as the earlier ask and answered with
        # the earlier link, which tells the agent its new link was made when it was not.
        self.go_stale()
        self.assertEqual(self.replacement()[0], 200)
        for other, said in (("LANE.1/Q1", "LANE.1/Q1"), (None, "no ruling")):
            with self.subTest(replaces=other):
                if other:
                    code, out = self.replacement(replaces=other)
                else:   # the same ask with `replaces` left out
                    q = {"qid": "LANE.1/Q3", "item": "LANE.1", "text": "What is the review rule now?",
                         "kind": "single", "options": [{"id": "a", "label": "One review"}, {"id": "b", "label": "Two"}],
                         "star": "a", "valid_if": [], "source": "rules.md:3", "nonce": "rxrepl0003"}
                    code, out = self.agent_post("/question", q)
                self.assertEqual(code, 409, out)
                self.assertIn("rxrepl0003", out["error"])
                self.assertIn(self.QID, out["error"])
                self.assertIn(said, out["error"])
        log = (self.cfg.state / "refactor.jsonl").read_text().splitlines()
        self.assertEqual([(json.loads(ln)["type"], json.loads(ln)["replaces"]) for ln in log],
                         [("replaces", self.QID)])
        self.assertEqual(self.replacement()[0], 200)   # the same ask, the same replaces: still a plain retry

    def test_a_retry_does_not_link_a_question_the_owner_already_answered(self):
        self.go_stale()
        real = self.console._rx_append
        self.console._rx_append = lambda rec: (_ for _ in ()).throw(SV.RequestError(409, "the disk is full"))
        try:
            self.assertEqual(self.replacement()[0], 500)
        finally:
            self.console._rx_append = real
        self.assertEqual(self.req("POST", "/api/answer", self.answer(qid="LANE.1/Q3", nonce="rxanswer03"),
                                  tok=token())[0], 200)
        code, out = self.replacement()
        self.assertEqual(code, 409, out)
        self.assertIn("has an answer already, given without its link", out["error"])
        self.assertFalse((self.cfg.state / "refactor.jsonl").exists())

    # -- property 1: every act is the owner's ------------------------------------------------------------

    def test_no_agent_route_settles_confirms_or_changes_any_answer(self):
        # Every agent-door route, driven with every action's body, plus the owner route's own names. The only
        # refactor record an agent may write is a proposal (or a replacement link), and no answer's state,
        # outcome or confirmed anchor moves. Catches: an agent route that withdraws, keeps or confirms.
        self.go_stale()
        code, out = self.propose()
        self.assertEqual(code, 200, out)
        self.proposal_id = out["record"]["id"]
        _, before = self.view_q()
        posts = sorted(MS.POST_ROUTES | {"/refactor", "/api/refactor", "/withdraw", "/untrack", "/confirm"})
        for path in posts:
            for name, make in REFACTOR_ACTIONS.items():
                body = {**make(self), "nonce": f"rxagent{abs(hash((path, name))) % 10**8:08d}"}
                SV.agent_request(self.cfg.socket, "POST", path, body)
                _, q = self.view_q()
                self.assertEqual(q["state"], before["state"], (path, name))
                for key in ("outcome", "confirmed"):
                    self.assertNotIn(key, q.get("refactor", {}), (path, name))
        log = self.cfg.state / "refactor.jsonl"
        kinds = {json.loads(ln)["type"] for ln in log.read_text().splitlines()}
        self.assertEqual(kinds, {"proposal"})
        for path in ("/refactor", "/api/refactor"):
            self.assertEqual(SV.agent_request(self.cfg.socket, "POST", path,
                                              REFACTOR_ACTIONS["withdraw"](self))[0], 404)

    def test_the_refactor_route_is_behind_access_and_origin(self):
        # Catches: the route without the Access check, or without the Origin check that answer and lock have.
        self.go_stale()
        for case, kw in (("no token", {"tok": None}), ("bad token", {"tok": "x"}),
                         ("another site", {"headers": {"Origin": "https://evil.example.com"}}),
                         ("no Origin", {"origin": False})):
            with self.subTest(case=case):
                self.assertEqual(self.act(REFACTOR_ACTIONS["untrack"](self), **kw)[0], 403)
        self.assertFalse((self.cfg.state / "refactor.jsonl").exists())
        self.assertEqual(self.act(REFACTOR_ACTIONS["untrack"](self))[0], 200)

    def test_the_log_refuses_an_owner_kind_written_as_the_agent(self):
        # The shape check under the routes: a withdraw, keep or confirm by "agent" is refused at append and on
        # load, so a hand-written line cannot make an agent's act look like the owner's.
        from overture import refactor as RXM
        self.go_stale()
        for kind, extra in (("withdraw", {"reason": "x"}), ("untrack", {}), ("confirm", {"proposal": "0" * 24})):
            rec = {"type": kind, "by": "agent", "qid": self.QID, "lock": self.lock_id, "nonce": "rxforged01",
                   **extra}
            with self.assertRaises(RXM.RefactorError) as e:
                self.console.store.refactor.append(rec, self.console.store)
            self.assertIn("only the owner may write one", str(e.exception))

    def test_the_log_itself_refuses_a_second_outcome_and_a_second_replacement(self):
        # The rules under the routes, driven at the log, since the routes check first and would hide a log that
        # forgot them. Catches: a settled lock settled again (two outcomes, which one is the record?), a second
        # open replacement, and a replacement of a ruling that is no longer in force or no longer this lock.
        from overture import refactor as RXM
        log = self.console.store.refactor
        self.go_stale()
        self.assertEqual(self.replacement()[0], 200)                      # LANE.1/Q3 replaces Q2
        code, out = self.agent_post("/question", {"qid": "LANE.1/Q4", "item": "LANE.1", "text": "Another?",
                                                  "kind": "single", "options": [{"id": "a", "label": "A"},
                                                                                {"id": "b", "label": "B"}],
                                                  "star": None, "valid_if": [], "source": "notes.md",
                                                  "nonce": "rxplainq4"})
        self.assertEqual(code, 200, out)
        link = {"type": "replaces", "qid": "LANE.1/Q4", "replaces": self.QID, "lock": self.lock_id, "by": "agent"}

        def refused(rec, words):
            with self.assertRaises(RXM.RefactorError) as e:
                log.append({**rec, "nonce": "rxlog" + os.urandom(4).hex()}, self.console.store)
            self.assertIn(words, str(e.exception))
        refused(link, "is already being replaced by LANE.1/Q3")
        refused({**link, "lock": "f" * 24}, "has no current lock")
        refused({**link, "qid": "LANE.1/Q9"}, "no question LANE.1/Q9")
        self.assertEqual(self.act(REFACTOR_ACTIONS["withdraw"](self))[0], 200)
        refused({"type": "untrack", "qid": self.QID, "lock": self.lock_id, "by": "owner"}, "is already withdrawn")
        refused({"type": "withdraw", "qid": self.QID, "lock": self.lock_id, "by": "owner", "reason": "again"},
                "is already withdrawn")
        refused(link, "is already withdrawn")
        kinds = [json.loads(ln)["type"] for ln in (self.cfg.state / "refactor.jsonl").read_text().splitlines()]
        self.assertEqual(kinds, ["replaces", "withdraw"])

    # -- property 6 and the sidecar's own discipline -------------------------------------------------------

    def test_an_older_kit_reads_the_store_whole_and_every_settled_answer_stale(self):
        # Catches: any lane 7 record (or a `replaces` field) in store.jsonl, which an older kit refuses whole, and
        # a confirmed anchor an older kit would read as fresh. "Older kit" = the store without its sidecar.
        from overture.store import Store
        self.go_stale(extra="RULE-GAMMA: answers are locked by the owner alone.\n")
        pid = self.propose(cites=("rules.md:7",))[1]["record"]["id"]
        self.assertEqual(self.act({"action": "confirm", "qid": self.QID, "proposal": pid})[0], 200)
        self.assertEqual(self.replacement(n=5, replaces="LANE.1/Q1")[0], 409)   # Q1 is not locked: refused
        old_kinds = ("question", "message", "answer", "lock", "anchor", "transcript", "visual")
        for line in self.cfg.store.read_text().splitlines():
            rec = json.loads(line)
            self.assertIn(rec["type"], old_kinds)
            self.assertEqual(SV.S.validate(rec), [], rec)
        (self.cfg.state / "refactor.jsonl").rename(self.cfg.state / "aside.jsonl")
        older = Store(self.cfg.store)
        from overture import view as VW
        holds = SV.A.evaluator(self.cfg.root, {k: v["status"] for k, v in ITEMS.items()})
        self.assertEqual(VW.question_state(older, older.question(self.QID), holds), "stale")

    def test_a_bad_sidecar_disables_refactor_acts_and_never_stops_the_console(self):
        # agent-5's choice, made here: a hand-edited or torn line makes the WHOLE file unreadable, named; every
        # refactor act is refused naming it; every answer reads as if it held nothing (stale stays stale, never
        # fresh); and the console keeps serving.
        self.go_stale()
        self.assertEqual(self.act(REFACTOR_ACTIONS["untrack"](self))[0], 200)
        log = self.cfg.state / "refactor.jsonl"
        good = log.read_text()
        for bad, why in ((good.replace('"untrack"', '"withdraw"'), "id"),    # an edit the id no longer matches
                         (good + '{"type": "withdraw", "qid"', "line 2")):   # a torn last line
            with self.subTest(why=why):
                log.write_text(bad)
                self.console.store.refactor = SV.RX.Log(self.cfg.state, self.console.store.clock)
                problem = self.console.store.refactor.problem
                self.assertIsNotNone(problem)
                self.assertIn("refactor.jsonl", problem)
                code, out = self.act(REFACTOR_ACTIONS["withdraw"](self))
                self.assertEqual(code, 503, out)
                self.assertIn("refactor.jsonl", out["error"])
                code, out = self.propose()
                self.assertEqual(code, 503, out)
                _, q = self.view_q()
                self.assertEqual(q["state"], "stale")          # the untrack it held is not read: stale, never fresh
                self.assertIn("refactor.jsonl", q["refactor"]["problem"])
                self.assertEqual(log.read_text(), bad)          # refused acts never rewrite the file
        self.assertEqual(self.req("GET", "/api/view", tok=token())[0], 200)   # and the console keeps serving

    def test_a_link_or_folder_at_the_sidecar_is_never_followed(self):
        # The sidecar is read and written relative to a held STATE descriptor: a planted link is not followed
        # (its target is neither read nor overwritten), and the file it names reads as unreadable, by name.
        self.go_stale()
        target = self.cfg.root / "planted.jsonl"
        target.write_text("planted\n")
        (self.cfg.state / "refactor.jsonl").symlink_to(target)
        self.console.store.refactor = SV.RX.Log(self.cfg.state, self.console.store.clock)
        self.assertIn("is not a plain file", self.console.store.refactor.problem)
        code, out = self.act(REFACTOR_ACTIONS["untrack"](self))
        self.assertEqual(code, 503, out)
        self.assertIn("refactor.jsonl is not a plain file", out["error"])
        self.assertEqual(target.read_text(), "planted\n")
        _, q = self.view_q()
        self.assertEqual(q["state"], "stale")
        self.assertIn("refactor.jsonl", q["refactor"]["problem"])

    def test_the_sidecar_is_written_whole_with_the_stores_file_mode(self):
        self.go_stale()
        self.assertEqual(self.act(REFACTOR_ACTIONS["untrack"](self))[0], 200)
        self.assertEqual(os.stat(self.cfg.state / "refactor.jsonl").st_mode & 0o777, 0o600)

    # -- property 5: the fold records the outcome -------------------------------------------------------------

    def test_the_export_carries_each_outcome_and_fold_refuses_an_adapter_that_would_drop_it(self):
        # Catches: an outcome missing from the export (the ruling silently vanishes from the record), a fold that
        # passes it to an adapter that ignores it, and an outcome on a lock folded before never folded at all.
        from overture import fold as F
        from overture.store import Store
        out_dir, ledger = self.cfg.root / "locked", self.cfg.root / "folded.txt"

        class Adapter:
            seen: list = []

            def items(self):
                return dict(ITEMS)

            def record(self, entries, dry_run):
                Adapter.seen += entries
                return [e["qid"] for e in entries]

            def seed_questions(self):
                return []
        F.write_export(F.export(Store(self.cfg.store)), out_dir)
        F.fold(out_dir, ledger, Adapter(), dry_run=False)            # the lock, folded before any outcome
        self.go_stale()
        self.assertEqual(self.act(REFACTOR_ACTIONS["withdraw"](self))[0], 200)
        entries = F.export(Store(self.cfg.store))
        e = entries[F.locked_filename(self.QID)]
        self.assertEqual({k: e["outcome"][k] for k in ("kind", "by", "reason")},
                         {"kind": "withdrawn", "by": "owner", "reason": "obsolete"})
        self.assertEqual(F.check_entry(F.locked_filename(self.QID), e, ITEMS), [])
        F.write_export(entries, out_dir)
        with self.assertRaises(F.FoldError) as err:
            F.fold(out_dir, ledger, Adapter(), dry_run=False)
        self.assertIn("does not say it records outcomes", str(err.exception))
        self.assertIn("RECORDS_OUTCOMES = True", str(err.exception))
        Adapter.RECORDS_OUTCOMES = True
        Adapter.seen = []
        F.fold(out_dir, ledger, Adapter(), dry_run=False)
        self.assertEqual([x["qid"] for x in Adapter.seen], [self.QID])
        self.assertIn(f"outcome:{e['outcome']['record']} {self.QID}", ledger.read_text())
        Adapter.seen = []
        F.fold(out_dir, ledger, Adapter(), dry_run=False)            # once only
        self.assertEqual(Adapter.seen, [])
        bad = {**e, "outcome": {**e["outcome"], "by": "agent"}}
        self.assertTrue(any("only the owner" in m for m in F.check_entry(F.locked_filename(self.QID), bad, ITEMS)))

    # -- ask refusals R1 and R2 ----------------------------------------------------------------------------------

    def ask(self, n, valid_if, source="notes.md", **over):
        q = {"qid": f"LANE.1/Q{n}", "item": "LANE.1", "text": "Still?", "kind": "single",
             "options": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}], "star": None,
             "valid_if": valid_if, "source": source, "nonce": f"rxasknew{n:03d}", **over}
        return self.agent_post("/question", q)

    def sha(self, name):
        import hashlib
        return {"kind": "file_sha256", "path": name,
                "sha256": hashlib.sha256((self.cfg.root / name).read_bytes()).hexdigest()}

    def test_r1_refuses_a_whole_file_hash_on_a_file_the_question_cites_lines_of(self):
        # Catches: the shape six live answers went stale with (source names lines; valid_if hashes the whole
        # file), through `source` and through an evidence cite, and an excerpt refused by mistake.
        code, out = self.ask(10, [self.sha("rules.md")], source="rules.md:3-4")
        self.assertEqual(code, 400, out)
        self.assertIn("cites lines 3-4 of that same file", out["error"])
        self.assertIn('"kind": "excerpt", "path": "rules.md"', out["error"])
        code, out = self.ask(11, [self.sha("rules.md")], evidence=[{"cite": "rules.md:3"}])
        self.assertEqual(code, 400, out)
        self.assertIn("same file", out["error"])
        code, out = self.ask(12, [{"kind": "excerpt", "path": "rules.md", "text": "RULE-BETA: the console never"}],
                             source="rules.md:4")
        self.assertEqual(code, 200, out)

    def test_r2_takes_a_whole_file_hash_of_100_lines_and_refuses_101(self):
        # Catches: R2's boundary moved either way (the mutant on the comparison).
        for n, lines in ((20, 100), (21, 101)):
            name = f"f{lines}.txt"
            (self.cfg.root / name).write_text("".join(f"line {i}\n" for i in range(lines)))
            code, out = self.ask(n, [self.sha(name)])
            if lines == 100:
                self.assertEqual(code, 200, out)
            else:
                self.assertEqual(code, 400, out)
                self.assertIn(f"which is 101 lines", out["error"])
        stored = self.console.store.question("LANE.1/Q20")
        code, out = self.agent_post("/question", {k: v for k, v in stored.items()
                                                  if k not in ("id", "seq", "ts", "by", "type", "schemaVersion")})
        self.assertEqual(code, 200, out)   # a retry of an ask that landed is not a new ask

    # -- the read caps ---------------------------------------------------------------------------------------

    def max_shape(self, proposals=True):
        """Six stale answers at the largest a proposal may be: 4 cites of 4000 characters and a 20 000 basis."""
        def line(tag, n):
            head = f"{tag}-{n:03d}: "
            return head + ("the ruling rests on this line, which is as long as an excerpt may be. " * 60)[
                :SV.S.MAX_EXCERPT - len(head)]
        (self.cfg.root / "max.md").write_text("".join(line("OLD", n) + "\n" for n in range(6)))
        for n in range(6):
            q = {"qid": f"LANE.1/Q{40 + n}", "item": "LANE.1", "text": f"Does rule {n} still hold?", "kind": "single",
                 "options": [{"id": "a", "label": "Yes"}, {"id": "b", "label": "No"}], "star": "a",
                 "valid_if": [{"kind": "excerpt", "path": "max.md", "text": line("OLD", n)}],
                 "source": f"max.md:{n + 1}", "nonce": f"rxmax{n:04d}"}
            self.assertEqual(self.agent_post("/question", q)[0], 200)
            code, a = self.req("POST", "/api/answer", self.answer(qid=q["qid"], nonce=f"rxmaxa{n:04d}"), tok=token())
            self.assertEqual(self.req("POST", "/api/lock", {"qid": q["qid"], "answer": a["record"]["id"],
                                                            "nonce": f"rxmaxl{n:04d}"}, tok=token())[0], 200)
        (self.cfg.root / "max.md").write_text("".join(line("NEW", n) + "\n" for n in range(24)))
        if proposals:
            for n in range(6):
                cites = [f"max.md:{4 * n + k + 1}" for k in range(4)]
                code, out = self.agent_post("/anchor-proposal", {"qid": f"LANE.1/Q{40 + n}", "cites": cites,
                                                                 "basis": "b" * SV.S.MAX_TEXT,
                                                                 "nonce": f"rxmaxp{n:04d}"})
                self.assertEqual(code, 200, out)

    def test_six_proposals_at_the_largest_stay_under_every_slim_read_cap(self):
        # Lane 7 review MEDIUM. Measured: unslimmed, this shape is view 297 953 and answers --json 269 891 bytes
        # (55 556 and 27 494 without the proposals), so every whole read was refused. Catches: a whole read that
        # carries a proposal's text again, a slim read that drops the proposal (the agent must still see one
        # waits, and which lines it cites), and `--item` losing the text it is the way to read.
        import subprocess
        self.max_shape()

        def cli(*args):
            r = subprocess.run([sys.executable, AGENT_PY, "--state", str(self.cfg.state), *args],
                               capture_output=True, text=True, timeout=60, env=GIT_ENV)
            return r.returncode, r.stdout, r.stderr
        for args in (("view",), ("todo",), ("answers",), ("answers", "--json")):
            rc, out, err = cli(*args)
            self.assertEqual(rc, 0, (args, err))
            self.assertLess(len(out.encode("utf-8")), 64 * 1024, args)
        view = json.loads(cli("view")[1])["view"]
        for n in range(6):
            p = view["questions"][f"LANE.1/Q{40 + n}"]["refactor"]["proposal"]
            self.assertEqual(p["cites"], [f"max.md:{4 * n + k + 1}" for k in range(4)])
            self.assertEqual(len(p["digest"]), 64)
            self.assertIn("view --item LANE.1", p["text"])
            self.assertFalse({"base", "anchors", "basis"} & set(p))
        rows = json.loads(cli("answers", "--json")[1])["rows"]
        self.assertTrue(all("anchors" not in r["refactor"]["proposal"] for r in rows if r["qid"] >= "LANE.1/Q40"))
        rc, out, _ = cli("view", "--item", "LANE.1", "--full")
        self.assertEqual(rc, 0)
        whole = json.loads(out)["view"]["questions"]["LANE.1/Q40"]["refactor"]["proposal"]
        self.assertEqual(len(whole["anchors"]), 4)
        self.assertEqual(len(whole["basis"]), SV.S.MAX_TEXT)
        self.assertEqual(whole["id"], view["questions"]["LANE.1/Q40"]["refactor"]["proposal"]["id"])
        code, owner = self.req("GET", "/api/view", tok=token())                # the owner's page keeps it whole
        self.assertEqual(owner["view"]["questions"]["LANE.1/Q41"]["refactor"]["proposal"]["anchors"][0]["text"],
                         (self.cfg.root / "max.md").read_text().splitlines()[4])

    def test_every_read_the_slim_pointer_names_prints_what_it_left_out(self):
        # kit-lows review MEDIUM. Catches: a pointer naming a read that does not print the text (the markdown
        # `answers --item` says only that a proposal waits), for a proposal and for a confirmed basis alike.
        import re
        import subprocess

        def cli(*args):
            r = subprocess.run([sys.executable, AGENT_PY, "--state", str(self.cfg.state), *args],
                               capture_output=True, text=True, timeout=60, env=GIT_ENV)
            self.assertEqual(r.returncode, 0, (args, r.stderr))
            return r.stdout

        def pointed_reads(key):
            text = json.loads(cli("view"))["view"]["questions"][self.QID]["refactor"][key]["text"]
            reads = re.findall(r"`([^`]+)`", text)
            self.assertGreaterEqual(len(reads), 1, text)
            return reads
        self.go_stale(extra="RULE-BETA: a release ships after one review.\n")
        code, out = self.propose(basis="BASIS-MARKER-P: RULE-BETA carries the ruling now")
        self.assertEqual(code, 200, out)
        for read in pointed_reads("proposal"):
            self.assertIn("BASIS-MARKER-P", cli(*read.split()), read)
        code, out = self.act({"action": "confirm", "qid": self.QID, "proposal": out["record"]["id"]})
        self.assertEqual(code, 200, out)
        for read in pointed_reads("confirmed"):
            self.assertIn("BASIS-MARKER-P", cli(*read.split()), read)

    def test_six_stale_answers_with_proposals_stay_under_the_slim_read_cap(self):
        # agent-5: `view` / `todo` / `answers` merge refactor state, so they must stay under 64 KiB with the live
        # shape: six stale answers, each with a proposal of a few hundred characters.
        from overture import view as VW
        para = "RULE-{n}: " + "the console keeps every ruling the owner made readable in the record. " * 5
        (self.cfg.root / "big.md").write_text("".join(para.format(n=n) + "\n" for n in range(6)))
        for n in range(6):
            q = {"qid": f"LANE.1/Q{30 + n}", "item": "LANE.1", "text": "Still the rule? " * 20, "kind": "single",
                 "options": [{"id": "a", "label": "Yes"}, {"id": "b", "label": "No"}], "star": "a",
                 "valid_if": [{"kind": "excerpt", "path": "big.md", "text": para.format(n=n)[:200]}],
                 "source": f"big.md:{n + 1}", "nonce": f"rxbig{n:04d}"}
            self.assertEqual(self.agent_post("/question", q)[0], 200)
            code, a = self.req("POST", "/api/answer", self.answer(qid=q["qid"], nonce=f"rxbiga{n:04d}"), tok=token())
            self.req("POST", "/api/lock", {"qid": q["qid"], "answer": a["record"]["id"], "nonce": f"rxbigl{n:04d}"},
                     tok=token())
        (self.cfg.root / "big.md").write_text("".join(para.format(n=n).replace("RULE-", "MOVED-") + "\nANCHOR-" + str(n) +
                                                      ": " + para.format(n=n)[10:300] + "\n" for n in range(6)))
        for n in range(6):
            code, out = self.agent_post("/anchor-proposal", {"qid": f"LANE.1/Q{30 + n}", "cites": [f"big.md:{2 * n + 2}"],
                                                             "basis": "the rule moved " * 10, "nonce": f"rxbigp{n:04d}"})
            self.assertEqual(code, 200, out)
        code, view = SV.agent_request(self.cfg.socket, "GET", "/view")
        self.assertEqual(code, 200)
        size = len(json.dumps(view).encode("utf-8"))
        self.assertLess(size, 64 * 1024, size)
        self.assertEqual(sum(1 for q in view["view"]["questions"].values() if q.get("refactor", {}).get("proposal")), 6)
        todo = json.dumps(VW.todo(view["view"]))
        self.assertLess(len(todo.encode("utf-8")), 64 * 1024)


# -- CONSOLE-kit/Q40, Q41: scan for a resolve ---------------------------------------------------------------

class ScanTests(_Live, unittest.TestCase):
    """Q40 button_now, Q41 side file: the owner asks, the steward answers, the owner still decides."""

    QID, QID3 = "LANE.1/Q2", "LANE.1/Q3"
    setUp = RefactorTests.setUp
    act = RefactorTests.act
    view_q = RefactorTests.view_q

    def second_ruling(self):
        """LANE.1/Q3, locked against RULE-BETA, so both rulings go stale together in `stale_both`."""
        q = {"qid": self.QID3, "item": "LANE.1", "text": "Is RULE-BETA still the rule?", "kind": "single",
             "options": [{"id": "a", "label": "Yes"}, {"id": "b", "label": "No"}], "star": "a",
             "valid_if": [{"kind": "excerpt", "path": "rules.md", "text": "RULE-BETA: the console never runs"}],
             "source": "rules.md:4", "nonce": "rxask000003"}
        self.assertEqual(self.agent_post("/question", q)[0], 200)
        code, a = self.req("POST", "/api/answer", self.answer(qid=self.QID3, nonce="rxanswer03"), tok=token())
        self.assertEqual(code, 200, a)
        code, lk = self.req("POST", "/api/lock", {"qid": self.QID3, "answer": a["record"]["id"], "nonce": "rxlock0003"},
                            tok=token())
        self.assertEqual(code, 200, lk)
        self.lock3 = lk["record"]["id"]

    def stale_both(self):
        (self.cfg.root / "rules.md").write_text(
            RULES.replace("every release is reviewed by a second agent", "a release ships after one review")
                 .replace("the console never runs", "the console may run") + "RULE-GAMMA: answers are owner-locked.\n")

    def scan(self, qids, locks, **kw):
        return self.act({"action": "scan", "qids": qids, "locks": locks, **kw})

    def advise(self, qid, star="withdraw", evidence="RULE-ALPHA was dropped in the rules rewrite; nothing replaced it"):
        return self.agent_post("/refactor-advice", {"qid": qid, "star": star, "evidence": evidence,
                                                    "nonce": "rxadv" + os.urandom(4).hex()})

    def rx_lines(self):
        p = self.cfg.state / "refactor.jsonl"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def todo(self):
        from overture import view as VW
        code, view = SV.agent_request(self.cfg.socket, "GET", "/view")
        self.assertEqual(code, 200, view)
        return VW.todo(view["view"])

    # -- property 2: one owner record, nothing changed --------------------------------------------------

    def test_scan_all_is_one_record_naming_every_stale_ruling_and_changes_nothing(self):
        # Catches: N records for "scan all" (one per ruling), a scan that writes into store.jsonl (an older kit
        # would refuse to start), a scan that touches a ruling (withdraws, re-anchors, locks), and a "scan all"
        # that misses a ruling gone stale after the page loaded.
        self.second_ruling()
        self.stale_both()
        store_before = self.cfg.store.read_bytes()
        code, out = self.scan([self.QID], [self.lock_id], all=True)
        self.assertEqual(code, 409, out)
        self.assertIn(self.QID3, out["error"])
        self.assertEqual(self.rx_lines(), [])
        code, out = self.scan([self.QID, self.QID3], [self.lock_id, self.lock3], all=True)
        self.assertEqual(code, 200, out)
        recs = self.rx_lines()
        self.assertEqual([(r["type"], r["by"], r["qids"], r["locks"]) for r in recs],
                         [("scan", "owner", [self.QID, self.QID3], [self.lock_id, self.lock3])])
        self.assertEqual(self.cfg.store.read_bytes(), store_before)
        view, q2 = self.view_q()
        self.assertEqual((q2["state"], view["questions"][self.QID3]["state"]), ("stale", "stale"))
        self.assertNotIn("outcome", q2.get("refactor", {}))

    def test_a_scan_names_only_stale_rulings_with_the_locks_the_owner_saw(self):
        # Catches: a scan of a ruling that holds (nothing to resolve), and one taken on a page from before a re-lock.
        code, out = self.scan([self.QID], [self.lock_id])
        self.assertEqual(code, 409, out)
        self.assertIn("is not stale", out["error"])
        RefactorTests.go_stale(self)
        code, out = self.scan([self.QID], ["0" * 24])
        self.assertEqual(code, 409, out)
        self.assertIn("is not the current lock", out["error"])
        self.assertEqual(self.rx_lines(), [])

    # -- property 5: a second press while one is open is refused, by name --------------------------------

    def test_a_second_scan_while_one_is_open_is_refused_naming_it_and_a_retry_is_the_first(self):
        # Catches: a second scan queued behind the first, a refusal that does not say which scan is open or what
        # it waits on, and a retried press (same nonce) refused or written twice.
        self.second_ruling()
        self.stale_both()
        body = {"action": "scan", "qids": [self.QID, self.QID3], "locks": [self.lock_id, self.lock3],
                "nonce": "rxscan00001"}
        code, first = self.act(body)
        self.assertEqual(code, 200, first)
        self.assertEqual(self.act(body), (200, first))   # the retry is the record that landed
        code, out = self.scan([self.QID], [self.lock_id])
        self.assertEqual(code, 409, out)
        self.assertIn(f"scan {first['record']['id'][:8]}", out["error"])
        self.assertIn("still open", out["error"])
        self.assertIn(f"{self.QID}, {self.QID3}", out["error"])
        self.assertEqual(len(self.rx_lines()), 1)
        self.assertEqual(len([b for b in self.doorbell() if b.get("intent") == "scan"]), 1)

    # -- property 3: the done-rule -------------------------------------------------------------------------

    def test_a_scan_is_done_once_every_ruling_is_answered_after_it_or_settled(self):
        # Catches: a scan done at once (nothing to wait for), one never done (sits in todo forever), an answer
        # written BEFORE the scan counted as its answer, and a ruling the owner settled still waiting.
        self.second_ruling()
        self.stale_both()
        self.assertEqual(self.advise(self.QID)[0], 200)            # before the scan: does not answer it
        code, sc = self.scan([self.QID, self.QID3], [self.lock_id, self.lock3])
        self.assertEqual(code, 200, sc)
        sid = sc["record"]["id"]
        self.assertEqual(self.todo()["scans"], [{"id": sid, "ts": sc["record"]["ts"],
                                                 "waiting": [self.QID, self.QID3]}])
        code, adv = self.advise(self.QID, star="keep", evidence="RULE-ALPHA's premise holds under its new wording")
        self.assertEqual(code, 200, adv)
        self.assertEqual(self.todo()["scans"][0]["waiting"], [self.QID3])
        view, _ = self.view_q()
        self.assertEqual(view["scans"][sid]["rulings"][self.QID], {"answered": adv["record"]["id"], "by": "advice"})
        self.assertEqual(self.act({"action": "withdraw", "qid": self.QID3, "lock": self.lock3,
                                   "reason": "RULE-BETA is gone"})[0], 200)
        self.assertEqual(self.todo()["scans"], [])
        view, _ = self.view_q()
        self.assertTrue(view["scans"][sid]["done"])
        self.assertEqual(view["scans"][sid]["rulings"][self.QID3], {"settled": True})
        self.assertEqual(self.scan([self.QID], [self.lock_id])[0], 200)   # the next scan may be asked now

    def test_a_proposal_or_a_replacement_answers_a_ruling(self):
        # Catches: a done-rule that knows only the new advice kind, not the existing proposal and replaces.
        self.second_ruling()
        self.stale_both()
        code, sc = self.scan([self.QID, self.QID3], [self.lock_id, self.lock3])
        self.assertEqual(code, 200, sc)
        sid = sc["record"]["id"]
        code, p = self.agent_post("/anchor-proposal", {"qid": self.QID3, "cites": ["rules.md:7"],
                                                       "basis": "RULE-GAMMA carries it", "nonce": "rxprop00003"})
        self.assertEqual(code, 200, p)
        q = {"qid": "LANE.1/Q4", "item": "LANE.1", "text": "What is the review rule now?", "kind": "single",
             "options": [{"id": "a", "label": "One review"}, {"id": "b", "label": "Two"}], "star": "a",
             "valid_if": [], "source": "rules.md:3", "replaces": self.QID, "nonce": "rxrepl00004"}
        self.assertEqual(self.agent_post("/question", q)[0], 200)
        view, _ = self.view_q()
        st = view["scans"][sid]
        self.assertEqual((st["rulings"][self.QID3]["by"], st["rulings"][self.QID]["by"]), ("proposal", "replaces"))
        self.assertTrue(st["done"])
        self.assertEqual(view["questions"][self.QID]["state"], "stale")   # answered, still nothing decided

    # -- the doorbell: watch wakes, the second cursor -------------------------------------------------------

    def test_a_scan_rings_a_line_that_wakes_a_watch_until_the_agent_marks_its_rx(self):
        # Catches: a scan that rings nothing (watch never wakes), a line keyed only by the store seq (the
        # agent's cursor is already there, so it would never wake), and an rx line that wakes forever.
        from overture import doorbell as D
        RefactorTests.go_stale(self)
        D.write_cursor(self.cfg.state, self.console.store.seq())   # the agent is up to date with the store
        code, sc = self.scan([self.QID], [self.lock_id])
        self.assertEqual(code, 200, sc)
        line = [b for b in self.doorbell() if b.get("intent") == "scan"]
        self.assertEqual([(b["type"], b["rx"], b["seq"]) for b in line],
                         [("scan", sc["record"]["seq"], self.console.store.seq())])
        since, rx = D.read_cursor(self.cfg.state), D.read_rx_cursor(self.cfg.state)
        self.assertEqual([b["rx"] for b in D.watch(self.cfg.inbox, since, rx_since=rx, timeout=0)],
                         [sc["record"]["seq"]])
        r = subprocess.run([sys.executable, AGENT_PY, "--state", str(self.cfg.state), "synced", "--rx-through",
                            str(sc["record"]["seq"])], capture_output=True, text=True, timeout=60, env=GIT_ENV)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(D.read_cursor(self.cfg.state), since)    # the store cursor did not move
        self.assertEqual(D.watch(self.cfg.inbox, since, rx_since=D.read_rx_cursor(self.cfg.state), timeout=0), [])

    # -- property 4 and the doors: nothing changes until the owner presses ------------------------------------

    def test_advice_changes_nothing_and_neither_door_gains_the_others_act(self):
        # Catches: advice that withdraws or keeps the ruling by itself, a scan the agent can ask (its own
        # doorbell), advice the owner door takes (an owner "recommendation" is a ruling), and advice on a
        # ruling that is not stale.
        code, out = self.advise(self.QID)
        self.assertEqual(code, 409, out)
        self.assertIn("is not stale", out["error"])
        RefactorTests.go_stale(self)
        store_before = self.cfg.store.read_bytes()
        code, adv = self.advise(self.QID, star="withdraw")
        self.assertEqual(code, 200, adv)
        self.assertEqual(self.cfg.store.read_bytes(), store_before)
        _, q = self.view_q()
        self.assertEqual(q["state"], "stale")
        self.assertNotIn("outcome", q["refactor"])
        self.assertEqual({k: q["refactor"]["advice"][k] for k in ("star", "evidence")},
                         {"star": "withdraw",
                          "evidence": "RULE-ALPHA was dropped in the rules rewrite; nothing replaced it"})
        code, out = self.advise(self.QID, star="drop")
        self.assertEqual(code, 400, out)   # the request's shape, not a rule of the log
        self.assertIn("star 'drop'", out["error"])
        code, out = self.advise(self.QID, evidence="e" * (SV.S.MAX_TEXT + 1))
        self.assertEqual(code, 400, out)
        self.assertIn(f"the limit is {SV.S.MAX_TEXT}", out["error"])
        code, out = SV.agent_request(self.cfg.socket, "POST", "/refactor", {"action": "scan", "qids": [self.QID],
                                                                             "locks": [self.lock_id], "nonce": "x" * 9})
        self.assertEqual(code, 404, out)
        code, out = self.req("POST", "/api/refactor-advice", {"qid": self.QID, "star": "keep", "evidence": "e",
                                                              "nonce": "rxownadv01"}, tok=token())
        self.assertIn(code, (403, 404), out)
        self.assertEqual([r["type"] for r in self.rx_lines()], ["advice"])

    # -- review MEDIUM: a done scan never reopens, and the steward always has a move ---------------------

    def test_a_ruling_that_holds_again_waits_for_an_answer_and_a_done_scan_never_reopens(self):
        # The reviewer's case. Catches: a done-rule that follows the files (a revert closes the scan, the steward
        # marks its rx, and when the text goes again the scan reopens with nothing to wake anyone, refusing every
        # new scan), and the opposite failure: a ruling that holds for good leaving the scan open with no move.
        from overture import doorbell as D
        RefactorTests.go_stale(self)
        code, sc = self.scan([self.QID], [self.lock_id])
        self.assertEqual(code, 200, sc)
        D.write_cursor(self.cfg.state, self.console.store.seq(), sc["record"]["seq"])   # the steward saw the line
        (self.cfg.root / "rules.md").write_text(RULES)                    # reverted: the ruling holds again
        self.assertEqual(self.view_q()[1]["state"], "locked")
        self.assertEqual(self.todo()["scans"][0]["waiting"], [self.QID])   # still the steward's, not silently done
        code, adv = self.advise(self.QID, star="keep", evidence="The cited line is back word for word; it holds.")
        self.assertEqual(code, 200, adv)                                   # the move a holding ruling has
        self.assertEqual(self.todo()["scans"], [])
        RefactorTests.go_stale(self)                                        # the text goes again, same lock
        self.assertEqual(self.view_q()[1]["state"], "stale")
        self.assertEqual(self.todo()["scans"], [])                          # done stays done
        self.assertEqual(self.scan([self.QID], [self.lock_id])[0], 200)     # and nothing blocks the next scan

    def test_a_ruling_whose_lock_is_gone_or_replaced_does_not_hold_a_scan_open(self):
        # Catches: a scan held open by a ruling the owner answered again (its lock is no longer current, so no
        # advice or proposal can name it) or by one a replacement was asked for before the scan.
        self.second_ruling()
        self.stale_both()
        q = {"qid": "LANE.1/Q4", "item": "LANE.1", "text": "What is the beta rule now?", "kind": "single",
             "options": [{"id": "a", "label": "Runs tests"}, {"id": "b", "label": "Runs nothing"}], "star": "a",
             "valid_if": [], "source": "rules.md:4", "replaces": self.QID3, "nonce": "rxrepl00034"}
        self.assertEqual(self.agent_post("/question", q)[0], 200)          # asked BEFORE the scan
        code, sc = self.scan([self.QID, self.QID3], [self.lock_id, self.lock3])
        self.assertEqual(code, 200, sc)
        sid = sc["record"]["id"]
        self.assertEqual(self.todo()["scans"][0]["waiting"], [self.QID])
        head = self.console.store.head(self.QID)["id"]
        code, a = self.req("POST", "/api/answer", self.answer(qid=self.QID, picks=["b"], nonce="rxreans02",
                                                              supersedes=head, reason="the rule changed"), tok=token())
        self.assertEqual(code, 200, a)                                      # answered again: its lock is gone
        self.assertEqual(self.todo()["scans"], [])
        view, _ = self.view_q()
        self.assertEqual(view["scans"][sid]["rulings"], {self.QID: {"settled": True}, self.QID3: {"settled": True}})

    def test_an_older_agent_py_that_drops_rx_through_costs_one_wake(self):
        # Catches: a cursor an older agent.py rewrites as {"through": N} making the scan line wake on every
        # watch for good, rather than once until the steward marks it again.
        from overture import doorbell as D
        RefactorTests.go_stale(self)
        code, sc = self.scan([self.QID], [self.lock_id])
        rx = sc["record"]["seq"]
        D.write_cursor(self.cfg.state, self.console.store.seq(), rx)
        self.assertEqual(D.watch(self.cfg.inbox, D.read_cursor(self.cfg.state),
                                 rx_since=D.read_rx_cursor(self.cfg.state), timeout=0), [])
        (self.cfg.state / D.CURSOR_FILE).write_text(json.dumps({"through": self.console.store.seq()}) + "\n")
        again = D.watch(self.cfg.inbox, D.read_cursor(self.cfg.state), rx_since=D.read_rx_cursor(self.cfg.state),
                        timeout=0)
        self.assertEqual([b["rx"] for b in again], [rx])                    # the one extra wake
        D.write_cursor(self.cfg.state, D.read_cursor(self.cfg.state), rx)   # the steward marks it again
        self.assertEqual(D.watch(self.cfg.inbox, D.read_cursor(self.cfg.state),
                                 rx_since=D.read_rx_cursor(self.cfg.state), timeout=0), [])

    # -- Q41: an older kit -------------------------------------------------------------------------------

    def test_an_older_kit_reads_the_side_file_as_unreadable_and_still_loads_the_store(self):
        # Q41's accepted cost, measured with the kit as released at v0.8.15. Catches: scan or advice written
        # into store.jsonl (that kit would refuse to start), and a side file the older kit reads PART of (a
        # kept ruling reading "kept" while the scan beside it is silently dropped). Stale stays stale.
        repo = Path(SV.__file__).resolve().parents[3]
        if not (repo / ".git").exists():
            self.skipTest("not a git checkout")
        self.second_ruling()
        self.stale_both()
        self.assertEqual(self.act({"action": "untrack", "qid": self.QID3, "lock": self.lock3})[0], 200)
        self.assertEqual(self.scan([self.QID], [self.lock_id])[0], 200)
        self.assertEqual(self.advise(self.QID)[0], 200)
        old = Path(tempfile.mkdtemp(prefix="ck-old-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(old, ignore_errors=True))
        # 1.20.1: v0.8.15 shipped the package as console_kit (renamed to overture at v1.0.0).
        arc = subprocess.run(["git", "-C", str(repo), "archive", "v0.8.15", "plugin/kit/console_kit"],
                             capture_output=True, timeout=60)
        self.assertEqual(arc.returncode, 0, arc.stderr)
        subprocess.run(["tar", "-x", "-C", str(old)], input=arc.stdout, check=True, timeout=60)
        probe = old / "probe.py"
        probe.write_text(
            "import json, sys, importlib\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "from pathlib import Path\n"
            "try:\n"
            "    ST = importlib.import_module('overture.store'); V = importlib.import_module('overture.view')\n"
            "except ModuleNotFoundError:\n"
            "    ST = importlib.import_module('console_kit.store'); V = importlib.import_module('console_kit.view')\n"
            "st = ST.Store(Path(sys.argv[2]))\n"
            "holds = V.make_evaluator(Path(sys.argv[3]), {})\n"
            "qs = {r['qid']: V.question_state(st, r, holds) for r in st.records() if r['type'] == 'question'}\n"
            "print(json.dumps({'problem': st.refactor.problem, 'states': qs}))\n")
        r = subprocess.run([sys.executable, str(probe), str(old / "plugin" / "kit"), str(self.cfg.store),
                            str(self.cfg.root)], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)   # the store loads: the older kit starts
        got = json.loads(r.stdout)
        self.assertIn("unknown refactor record type 'scan'", got["problem"])
        self.assertEqual((got["states"][self.QID], got["states"][self.QID3]), ("stale", "stale"))
        view, _ = self.view_q()   # this kit: the kept ruling reads kept (locked), the scanned one stale
        self.assertEqual((view["questions"][self.QID]["state"], view["questions"][self.QID3]["state"]),
                         ("stale", "locked"))


# -- Pull requests: pushed by the steward's gh, as data; the server never talks to GitHub -------------------

def pr_row(number: int, **over) -> dict:
    """One PR as `agent.py prs-push` sends it."""
    pr = {"number": number, "title": f"PR {number}", "state": "open", "draft": False, "head": f"lane-{number}",
          "base": "main", "author": "octo", "created_at": "2026-09-30T10:00:00Z", "updated_at": "2026-09-30T11:00:00Z",
          "merged_at": None, "closed_at": None, "url": f"https://github.com/octo/repo/pull/{number}",
          "merge_commit": None, "checks": "success"}
    pr.update(over)
    return pr


PRS_PUSH = {"repo": "octo/repo", "window_days": 30, "prs": [
    pr_row(12, draft=True, checks="pending"),
    pr_row(9, state="merged", merged_at="2026-09-29T08:00:00Z", closed_at="2026-09-29T08:00:00Z",
           merge_commit="a" * 40, checks="failure"),
    pr_row(8, state="closed", closed_at="2026-09-28T08:00:00Z", checks="none")]}

# Each is refused, and named: unknown keys at either level, every cap, and a link that is not this PR on GitHub.
BAD_PRS = [
    {**PRS_PUSH, "extra": 1},
    {k: v for k, v in PRS_PUSH.items() if k != "window_days"},
    {**PRS_PUSH, "prs": [{**pr_row(1), "html": "<b>x</b>"}]},
    {**PRS_PUSH, "prs": [pr_row(n) for n in range(1, PR.MAX_PRS + 2)]},
    {**PRS_PUSH, "prs": [pr_row(1, title="x" * (PR.MAX_TITLE + 1))]},
    {**PRS_PUSH, "prs": [pr_row(1, head="b" * (PR.MAX_REF + 1))]},
    {**PRS_PUSH, "prs": [pr_row(1, head="a\nb")]},
    {**PRS_PUSH, "prs": [pr_row(1, author="x" * (PR.MAX_LOGIN + 1))]},
    {**PRS_PUSH, "prs": [pr_row(1, url="javascript:alert(1)")]},
    {**PRS_PUSH, "prs": [pr_row(1, url="http://github.com/octo/repo/pull/1")]},
    {**PRS_PUSH, "prs": [pr_row(1, url="https://github.com/evil/repo/pull/1")]},
    {**PRS_PUSH, "prs": [pr_row(1, url="https://github.com.evil.example/octo/repo/pull/1")]},
    {**PRS_PUSH, "prs": [pr_row(1, state="merged")]},
    {**PRS_PUSH, "prs": [pr_row(1, checks="green")]},
    {**PRS_PUSH, "prs": [pr_row(1), pr_row(1)]},
    {**PRS_PUSH, "prs": [pr_row(True)]},
    {**PRS_PUSH, "repo": "not a repo"},
    {**PRS_PUSH, "window_days": PR.MAX_DAYS + 1},
]


class PrsPushTests(_Live, unittest.TestCase):
    """The PRs view: pushed only through the agent door, checked against a closed schema, kept in STATE."""

    def prs(self):
        code, out = self.get("/api/prs")
        self.assertEqual(code, 200, out)
        return out

    def test_before_the_first_push_the_view_says_why_it_is_empty(self):
        # Catches: a silent blank list, invented PRs, and a note that outlives the first push.
        self.assertEqual(self.prs(), {"pushed": False, "note": PR.NOT_PUSHED})
        self.assertIn("agent.py prs-push", PR.NOT_PUSHED)
        self.assertFalse((self.cfg.state / PR.PRS).exists())   # reading wrote nothing
        code, out = SV.agent_request(self.cfg.socket, "POST", "/prs", {**PRS_PUSH, "prs": []}, agent="agent-9")
        self.assertEqual(code, 200, out)
        got = self.prs()
        self.assertEqual((got["pushed"], got["note"], got["prs"], got["by"]), (True, None, [], "agent-9"))

    def test_a_push_is_kept_stamped_and_served_to_the_owner(self):
        code, out = SV.agent_request(self.cfg.socket, "POST", "/prs", PRS_PUSH, agent="agent-5")
        self.assertEqual(code, 200, out)
        self.assertEqual((out["prs"], out["repo"]), (3, "octo/repo"))
        got = self.prs()
        self.assertEqual(got["prs"], PRS_PUSH["prs"])
        self.assertEqual((got["repo"], got["window_days"], got["by"]), ("octo/repo", 30, "agent-5"))
        self.assertRegex(got["pushed_at"], PR.TS)
        self.assertEqual(got["pushed_at"], out["pushed_at"])   # the SERVER's clock, not anything the steward sent
        self.assertEqual(os.stat(self.cfg.state / PR.PRS).st_mode & 0o777, 0o600)
        again = SV.Console(self.cfg, FakeAdapter())                # a restart reads it back from STATE
        self.assertEqual(again.prs()["prs"], PRS_PUSH["prs"])

    def test_unknown_keys_and_every_cap_are_refused_by_name_and_the_last_push_stands(self):
        self.assertEqual(SV.agent_request(self.cfg.socket, "POST", "/prs", PRS_PUSH)[0], 200)
        for bad in BAD_PRS:
            with self.subTest(bad=json.dumps(bad)[:120]):
                why = PR.snapshot_problem(bad)
                self.assertIsNotNone(why)
                self.assertEqual(SV.agent_request(self.cfg.socket, "POST", "/prs", bad), (400, {"error": why}))
        self.assertEqual(self.prs()["prs"], PRS_PUSH["prs"])      # every refusal left the push standing
        self.assertIn("unknown fields ['html']", PR.snapshot_problem(BAD_PRS[2]))
        self.assertIn(f"over {PR.MAX_PRS}", PR.snapshot_problem(BAD_PRS[3]))
        self.assertIn(f"over {PR.MAX_TITLE}", PR.snapshot_problem(BAD_PRS[4]))

    def test_the_owner_door_cannot_write_it(self):
        # Catches: a write route on the owner's side (an owner, or anyone holding an Access token, would then
        # put arbitrary links on the page). Every owner method on both paths is refused, and nothing is stored.
        for path in ("/api/prs", "/prs"):
            for method in ("POST", "PUT", "PATCH", "DELETE"):
                with self.subTest(path=path, method=method):
                    code, _ = self.req(method, path, PRS_PUSH, tok=token())
                    self.assertIn(code, (404, 405))
        self.assertFalse((self.cfg.state / PR.PRS).exists())
        self.assertEqual(self.req("GET", "/api/prs")[0], 403)      # the read is behind the Access gate too
        self.assertEqual(self.prs()["pushed"], False)

    def test_a_link_planted_at_prs_json_is_never_followed(self):
        outside = Path(self.tmp.name) / "outside.json"
        outside.write_text(json.dumps({**PRS_PUSH, "pushed_at": "2026-09-30T00:00:00Z", "by": None}))
        (self.cfg.state / PR.PRS).symlink_to(outside)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.prs(), {"pushed": False, "note": PR.UNREADABLE})
        self.assertEqual(SV.agent_request(self.cfg.socket, "POST", "/prs", {**PRS_PUSH, "prs": []})[0], 200)
        self.assertFalse((self.cfg.state / PR.PRS).is_symlink())   # the link was replaced, its target untouched
        self.assertIn("octo/repo", outside.read_text())
        self.assertEqual(self.prs()["prs"], [])

    def test_a_stored_file_that_fails_its_check_is_unreadable_not_half_served(self):
        (self.cfg.state / PR.PRS).write_text(json.dumps({**PRS_PUSH, "pushed_at": "2026-09-30T00:00:00Z",
                                                         "by": None, "extra": True}))
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(self.prs(), {"pushed": False, "note": PR.UNREADABLE})
        self.assertIn("prs.json", err.getvalue())


FAKE_GH = r"""#!{python}
import json, os, sys
args = sys.argv[1:]
log = os.environ.get("FAKE_GH_LOG")
if log:
    with open(log, "a") as f:
        f.write(json.dumps(args) + "\n")
if os.environ.get("FAKE_GH_FAIL"):
    sys.stderr.write("gh: To get started with GitHub CLI, please run:  gh auth login\n")
    sys.exit(4)
row = lambda n, **o: dict({{"number": n, "title": "PR %d" % n, "state": "OPEN", "isDraft": False,
                           "headRefName": "lane-%d" % n, "baseRefName": "main",
                           "author": {{"login": "octo", "is_bot": False}}, "createdAt": "2026-09-30T10:00:00Z",
                           "updatedAt": "2026-09-30T11:00:00Z", "mergedAt": None, "closedAt": None,
                           "url": "https://github.com/octo/repo/pull/%d" % n, "mergeCommit": None,
                           "statusCheckRollup": []}}, **o)
if args[:2] == ["repo", "view"]:
    print(json.dumps({{"nameWithOwner": "octo/repo"}}))
elif "open" in args:
    print(json.dumps([row(12, isDraft=True, statusCheckRollup=[{{"__typename": "CheckRun", "status": "IN_PROGRESS",
                                                                 "conclusion": ""}}])]))
else:
    print(json.dumps([row(9, state="MERGED", mergedAt="2026-09-29T08:00:00Z", closedAt="2026-09-29T08:00:00Z",
                          mergeCommit={{"oid": "{sha}"}})]))
"""


class PrsPushCliTests(_SingleServer, unittest.TestCase):
    """`agent.py prs-push`, a separate process, runs gh (a fake on PATH) in ITS process and pushes the result."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(os.path.realpath(tmp.name))
        self.single(self.base)
        from overture import registry as R
        reg = self.base / "cfg" / "overture" / "projects.json"
        R.register(self.scfg.root, self.scfg.state, HERE / "plugin" / "kit", path=reg)
        bindir = self.base / "bin"
        bindir.mkdir()
        gh = bindir / "gh"
        gh.write_text(FAKE_GH.format(python=sys.executable, sha="b" * 40))
        gh.chmod(0o755)
        self.log = self.base / "gh.log"
        self.env = {**os.environ, "XDG_CONFIG_HOME": str(reg.parent.parent), "FAKE_GH_LOG": str(self.log),
                    "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}"}
        self.env.pop("OVERTURE_AGENT", None)

    def run_cli(self, *extra, **env):
        return subprocess.run([sys.executable, str(HERE / "plugin" / "kit" / "agent.py"), "--state",
                               str(self.scfg.state), "prs-push", "--project", str(self.scfg.root), *extra],
                              capture_output=True, text=True, env={**self.env, **env}, timeout=120,
                              cwd=self.scfg.root)

    def test_the_cli_runs_gh_here_and_the_server_keeps_what_it_sent(self):
        r = self.run_cli("--days", "14", "--limit", "20")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["prs"], 2)
        calls = [json.loads(ln) for ln in self.log.read_text().splitlines()]
        self.assertEqual([c[:2] for c in calls], [["repo", "view"], ["pr", "list"], ["pr", "list"]])
        self.assertIn("20", calls[1])
        self.assertTrue(any(a.startswith("closed:>=") for a in calls[2]))
        got = self.sconsole.prs()
        self.assertEqual([(p["number"], p["state"], p["draft"], p["checks"]) for p in got["prs"]],
                         [(12, "open", True, "pending"), (9, "merged", False, "none")])
        self.assertEqual((got["repo"], got["window_days"], got["prs"][1]["merge_commit"]), ("octo/repo", 14, "b" * 40))

    def test_a_gh_failure_is_named_and_nothing_is_sent(self):
        r = self.run_cli(FAKE_GH_FAIL="1")
        self.assertEqual(r.returncode, 1)
        self.assertIn("refused, nothing sent", r.stderr)
        self.assertIn("gh auth login", r.stderr)
        self.assertEqual(self.sconsole.prs(), {"pushed": False, "note": PR.NOT_PUSHED})

    def test_out_of_range_options_are_refused_before_gh_runs(self):
        for extra in (("--days", "0"), ("--days", str(PR.MAX_DAYS + 1)), ("--limit", str(PR.MAX_LIMIT + 1))):
            r = self.run_cli(*extra)
            self.assertEqual(r.returncode, 1, extra)
            self.assertIn("refused, nothing sent", r.stderr)
        self.assertFalse(self.log.exists())


class OneServerPrsTests(_OneServer, unittest.TestCase):
    """The one server takes the same push on `/p/<project>/prs`, under the project's token (K4), and only there."""

    def test_a_push_reaches_only_its_own_project(self):
        self.spawn()
        self.assertEqual(self.owner("alpha", "GET", "/api/prs"), (200, {"pushed": False, "note": PR.NOT_PUSHED}))
        code, out = self.agent("POST", "/p/alpha/prs", PRS_PUSH)
        self.assertEqual(code, 200, out)
        self.assertEqual(self.owner("alpha", "GET", "/api/prs")[1]["prs"], PRS_PUSH["prs"])
        self.assertEqual(self.owner("beta", "GET", "/api/prs")[1]["pushed"], False)
        bad = BAD_PRS[0]
        self.assertEqual(self.agent("POST", "/p/alpha/prs", bad), (400, {"error": PR.snapshot_problem(bad)}))
        # Another project's token on this project's path is the one 403, and nothing moves.
        self.assertEqual(SV.agent_request(self.sock, "POST", "/p/beta/prs", PRS_PUSH, token=self.tokens["alpha"]),
                         (403, {"error": "forbidden"}))
        self.assertEqual(self.owner("beta", "GET", "/api/prs")[1]["pushed"], False)
        self.assertIn(self.owner("alpha", "POST", "/api/prs", PRS_PUSH)[0], (404, 405))
        self.stop()
        self.assertEqual(self.violations(), [])


if __name__ == "__main__":
    unittest.main(verbosity=1)

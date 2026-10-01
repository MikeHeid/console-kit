"""Tests for the owner console server (spec §4.4, §4.5; slice P3's ACs).

The Access check runs for real: tokens are signed with a throwaway RSA key and
verified by PyJWT through `access_verifier`. Only the key lookup is replaced,
so no test reaches the network.

    python3 tools/console-kit/test_server.py
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
if not os.environ.get("CONSOLE_KIT_TEST_CONFIG"):
    os.environ["CONSOLE_KIT_TEST_CONFIG"] = os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="ck-cfg-")
os.environ.pop("CONSOLE_KIT_AGENT", None)
from console_kit import server as SV  # noqa: E402

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
    from console_kit import gitseam as G
    p = mock.patch.object(G, "_OPEN", False)
    p.start()
    test.addCleanup(p.stop)


def seam_open():
    """The seam as an agent-side caller finds it: open, so git is read."""
    from unittest import mock
    from console_kit import gitseam as G
    return mock.patch.object(G, "_OPEN", True)


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
        self.assertEqual((code, body), (200, {"shape": "s", "values": {"pct": "5%"}}))
        self.assertEqual(len(calls), 1)

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
        self.assertIn('id="console-kit-config"', page)

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
        from console_kit import doorbell as D
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
        head = (f"POST /api/answer HTTP/1.1\r\nHost: x\r\nOrigin: https://{HOSTNAME}\r\n"
                f"Cf-Access-Jwt-Assertion: {token()}\r\nContent-Type: application/json\r\n"
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
        self.assertEqual(SV.agent_request(self.cfg.socket, "POST", "/question", q)[0], 200)
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
        """`plan_reanchor` as an agent-side caller runs it, with git history (what the follow-up lane restores)."""
        real = SV.A.plan_reanchor

        def plan(*a, **kw):
            with seam_open():
                return real(*a, **kw)
        return plan

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
        from console_kit import __version__
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
        from console_kit import doorbell as D
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
        from console_kit import doorbell as D
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
        from console_kit import doorbell as D
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
        from console_kit import anchors as A
        big = self.cfg.root / "logs" / "big.log"
        big.parent.mkdir(parents=True, exist_ok=True)
        with open(big, "wb") as fh:
            fh.write(b"first line\n")
            fh.truncate(A.MAX_READ + 1)
        code, out = self.ask([{"cite": "logs/big.log:1"}], n=50)
        self.assertEqual(code, 400, out)
        self.assertIn("is over 2 MiB, so it is not read", out["error"])


# -- 0.8.0: roar, refine/drill, tags, transcripts and visuals through the real server ------

MOCK = ("<!doctype html><html><head><style>h1{color:#123}</style></head><body><h1>Grid mock</h1>"
        "<script>document.body.setAttribute('data-ran','1')</script></body></html>")


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
        (d / ".console-kit.json").write_text(json.dumps(self.CONFIG))
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
        self.assertEqual(view["config"], {"specs_dir": "specs", "visuals_dir": "visuals"})
        self.assertIn("questions", view["tags"])
        # The seed picked "a" against the ★ "b": deliberate, with its reason.
        [t] = view["tags"]["questions"]["LANE.1/Q1"]
        self.assertEqual(t["step"], "deliberate")
        self.assertIn("went against the ★", t["reason"])

    def test_a_bad_project_config_stops_the_server_by_name(self):
        (self.root / ".console-kit.json").write_text(json.dumps({"visuals_dir": "../out"}))
        from console_kit import projectcfg as PC
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
        (self.root / ".console-kit.json").write_text(json.dumps({}))
        self.console.project = __import__("console_kit.projectcfg", fromlist=["load"]).load(self.root)
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
        (seedwt / ".console-kit.json").write_text(json.dumps({"visuals_dir": "architect/visuals/"}))
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
        self.assertIn(".console-kit.json", before)
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
        """agent.py in-process with a controlled environment (CONSOLE_KIT_AGENT unset unless given)."""
        from unittest import mock
        clean = {k: v for k, v in os.environ.items() if k != "CONSOLE_KIT_AGENT"}
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
                                env={"CONSOLE_KIT_AGENT": "agent-7"})
        self.assertEqual(rc, 0, out + err)
        from_env = json.loads(out)["record"]
        rc, out, err = self.cli("--as", "agent-8", "reply", "LANE", "--as beats the env.",
                                env={"CONSOLE_KIT_AGENT": "agent-7"})
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
                    rc, out, err = self.cli("reply", "LANE.1", "hello", env={"CONSOLE_KIT_AGENT": bad})
                    self.assertEqual(rc, 2, out + err)
                    self.assertIn("CONSOLE_KIT_AGENT", err)
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
        from console_kit import fold as F
        from console_kit import schema as S
        from console_kit.store import Store
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
        from console_kit import registry as R
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
                said = [ln for ln in err.splitlines() if not ln.startswith("console ")]
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
                said = [ln for ln in err.splitlines() if not ln.startswith("console ")]  # the server's own log
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
    (root / ".console-kit.json").write_text(json.dumps({"specs_dir": "specs/"}))
    git(root, "init", "-q")
    git(root, "add", "specs/spec.md", ".console-kit.json")
    git(root, "commit", "-qm", "v1")
    spec.write_text("A new first line.\n" + ServerTests.SPEC)
    git(root, "commit", "-qam", "unrelated")
    os.utime(spec, (1_600_000_000, 1_600_000_000))
    return hashlib.sha256(ServerTests.SPEC.encode()).hexdigest()


def spec_question(v1: str) -> dict:
    return {"qid": "LANE.1/Q2", "item": "LANE.1", "text": "Still?", "kind": "single",
            "options": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}], "star": None,
            "valid_if": [{"kind": "file_sha256", "path": "specs/spec.md", "sha256": v1}],
            "source": "specs/spec.md:5-6", "nonce": "nogitquest01"}


# Modules that may start a process, and why each is allowed (the no-spawn walk skips them):
SPAWN_ALLOWED = {
    "console_kit/gitseam.py": "the one door for git; it starts nothing once the server closes it",
    "agent.py": "run by an agent inside its own jail; the server process never imports it",
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
spawned = []
def hook(event, args):
    if event.startswith(SPAWN):
        spawned.append([event, repr(args)[:300]])
sys.addaudithook(hook)

import http.client, os, socket, threading, time
from pathlib import Path
p = json.loads(sys.argv[1])
sys.path.insert(0, p["kit"])
from console_kit import server as SV

root = Path(p["root"])
s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
cfg = SV.Config(root=root, page=root / "page.html", state=root / "state", adapter=root / "adapter.py",
                team_domain="team.example.cloudflareaccess.com", aud="a" * 64, hostname=p["host"], port=port,
                project="audit")
threading.Thread(target=SV.serve, args=(cfg, lambda _tok: {"email": "owner@example.com"}), daemon=True).start()
for _ in range(200):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
        if cfg.socket.exists():
            break
    except OSError:
        pass
    time.sleep(0.05)

def owner(method, path, body=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    h = {"Cf-Access-Jwt-Assertion": "x", "Origin": "https://" + p["host"]}
    data = None if body is None else json.dumps(body).encode()
    if data is not None:
        h["Content-Type"] = "application/json"
    c.request(method, path, body=data, headers=h)
    r = c.getresponse(); raw = r.read(); c.close()
    try:
        return r.status, json.loads(raw)
    except ValueError:
        return r.status, None

def agent(method, path, body=None):
    return SV.agent_request(cfg.socket, method, path, body)

codes, out = {}, {}
def call(name, fn, *a):
    code, body = fn(*a)
    codes[name] = code
    return body

call("agent POST /question", agent, "POST", "/question", p["question"])
a = call("owner POST /api/answer", owner, "POST", "/api/answer",
         {"qid": "LANE.1/Q2", "picks": ["a"], "own_text": "", "nonce": "nogitanswer1"})
call("owner POST /api/lock", owner, "POST", "/api/lock",
     {"qid": "LANE.1/Q2", "answer": a["record"]["id"], "nonce": "nogitlock001"})
for path in ("/", "/index.html", "/api/board", "/api/usage", "/api/feed", "/api/wait?since=0&timeout=0",
             "/api/evidence?qid=LANE.1/Q2", "/api/visual?id=" + "0" * 24):
    call("owner GET " + path, owner, "GET", path)
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
sys.stdout.write(json.dumps({"spawned": spawned, "codes": codes, "out": out}) + "\n")
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
        self.console = SV.Console(self.cfg, FakeAdapter())
        self.console.seed()
        verify = SV.access_verifier(TEAM, AUD, key_for=lambda _t: KEY.public_key())
        self.owner = SV.owner_server(self.console, verify, 0)
        self.port = self.owner.server_address[1]
        threading.Thread(target=self.owner.serve_forever, daemon=True).start()
        self.agent = SV.agent_server(self.console)
        threading.Thread(target=self.agent.serve_forever, daemon=True).start()
        code, out = self.agent_post("/question", spec_question(self.v1))
        self.assertEqual(code, 200, out)
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
        from console_kit import gitseam as G
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
        self.assertIn("console_kit/server.py", found)
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
        from console_kit import tags as T
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
from console_kit import multiserver as MS
from console_kit import server as SV
from cryptography.hazmat.primitives import serialization
pub = serialization.load_pem_public_key(open(pem, "rb").read())
r = MS.start(server_file, sock, verify_for=lambda aud: SV.access_verifier("team.example.cloudflareaccess.com", aud,
             key_for=lambda t: pub), port_for=lambda h: 0)
armed[0] = True
print(json.dumps({"ports": r.ports(), "refused": r.ms.refused(), "stores": len(r.ms.stores())}), flush=True)
sys.stdin.read()
r.shutdown()
'''


class OneServerTests(unittest.TestCase):
    """K3 step 3: the one server's doors, items-push, and its rules (AC3.3, AC3.5, AC3.6)."""

    def setUp(self):
        import test_kit as TK
        from console_kit import serverfile as SF
        from console_kit import registry as R
        from cryptography.hazmat.primitives import serialization
        self.TK, self.SF, self.R = TK, SF, R
        self.tmp = tempfile.TemporaryDirectory()
        self.t = Path(os.path.realpath(self.tmp.name))
        self.cfg = self.t / "cfg"
        self.reg = self.cfg / "console-kit" / "projects.json"
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
        self.proc = None

    def tearDown(self):
        self.stop()
        self.tmp.cleanup()

    def spawn(self, script=ONE_SERVER):
        roots = ",".join(str(v["root"]) for v in self.p.values())
        self.proc = subprocess.Popen([sys.executable, "-c", script, str(HERE / "plugin" / "kit"),
                                      str(self.sfile), str(self.sock), str(self.pem), str(self.audit), roots],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, env={**os.environ, "XDG_CONFIG_HOME": str(self.cfg)})
        line = self.proc.stdout.readline()
        if not line:
            raise AssertionError(f"the one server did not start: {self.proc.stderr.read()}")
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
        self.proc = None

    def agent(self, method, path, body=None):
        return SV.agent_request(self.sock, method, path, body)

    def owner(self, name, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.info["ports"][name], timeout=30)
        h = {"Cf-Access-Jwt-Assertion": token()}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h.update({"Content-Type": "application/json", "Origin": f"https://{name}.example.com"})
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

    def test_the_seam_is_the_only_admission_point(self):
        # K4 replaces `authorize` alone: refusing there must refuse every route of every project, and an
        # unknown project must look exactly like a refused one.
        self.spawn()
        self.assertEqual(self.agent("GET", "/p/nobody/view"), (403, {"error": "forbidden"}))
        self.assertEqual(self.agent("GET", "/p/alpha/view")[0], 200)
        src = (HERE / "plugin" / "kit" / "console_kit" / "multiserver.py").read_text()
        self.assertEqual(src.count("authorize("), 2)          # its definition and its one call
        body = src.split("def _project(self)")[1].split("def do_GET")[0]
        self.assertIn("authorize(name, self.headers)", body)
        self.stop()
        self.spawn(ONE_SERVER.replace("r = MS.start(", "MS.authorize = lambda project, headers: False\nr = MS.start("))
        for name in ("alpha", "beta"):
            for m, path in (("GET", "/view"), ("GET", "/check"), ("POST", "/items"), ("POST", "/message")):
                self.assertEqual(self.agent(m, f"/p/{name}{path}", {} if m == "POST" else None),
                                 (403, {"error": "forbidden"}), (name, path))
        self.assertEqual(self.agent("GET", "/health")[0], 200)

    def test_ac36_one_bad_store_is_503_there_and_a_kill_loses_nothing_acknowledged(self):
        # Catches: a server that refuses to start for one project's fault, and a restart test that writes nothing.
        (self.p["beta"]["state"] / "store.jsonl").write_text('{"not": "a record"}\n')
        info = self.spawn()
        self.assertEqual(list(info["refused"]), ["beta"])
        for code, out in (self.agent("GET", "/p/beta/view"), self.owner("beta", "GET", "/api/view")):
            self.assertEqual(code, 503)
            self.assertIn("beta's console cannot open", out["error"])
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
        files = ("store.jsonl", "names.jsonl", "inbox.jsonl")
        before = {f: hashlib.sha256((fixture / f).read_bytes()).hexdigest() for f in files if (fixture / f).exists()}
        self.assertIn("store.jsonl", before)
        (self.p["alpha"]["root"] / "console_adapter.py").write_text(
            "def items():\n    return {'LANE': {'title': 'a lane', 'parent': None, 'status': 'open'}}\n"
            "def seed_questions():\n    return []\n"
            "def record(entries, dry_run):\n    return []\n")
        env = {**os.environ, "XDG_CONFIG_HOME": str(self.cfg), "PYTHONDONTWRITEBYTECODE": "1"}
        env.pop("CONSOLE_KIT_AGENT", None)
        agent_py = str(HERE / "plugin" / "kit" / "agent.py")
        r = subprocess.run([sys.executable, agent_py, "--state", str(fixture), "server", "add", "alpha", "--hostname",
                            "alpha.example.com", "--aud", AUD, "--port", "4901", "--team-domain", TEAM],
                           capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.spawn()
        r = subprocess.run([sys.executable, agent_py, "--state", str(fixture), "items-push", "--adapter",
                            "console_adapter.py", "--project", str(self.p["alpha"]["root"])],
                           capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        for path in ("/view", "/check", "/health"):
            self.assertEqual(self.agent("GET", f"/p/alpha{path}")[0], 200, path)
        self.assertEqual(self.owner("alpha", "GET", "/api/view")[0], 200)
        self.stop()
        after = {f: hashlib.sha256((fixture / f).read_bytes()).hexdigest() for f in files if (fixture / f).exists()}
        self.assertEqual(after, before)
        self.assertTrue((fixture / "server.lock").exists())
        self.assertTrue((fixture / "items.json").exists())
        for tag, kit in kits.items():                          # the rollback: every released kit starts on it
            self.TK.start_old_server(kit, self.t / f"work-{tag}", fixture, stay=False)


if __name__ == "__main__":
    unittest.main(verbosity=1)

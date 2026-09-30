"""Tests for the owner console server (spec §4.4, §4.5; slice P3's ACs).

The Access check runs for real: tokens are signed with a throwaway RSA key and
verified by PyJWT through `access_verifier`. Only the key lookup is replaced,
so no test reaches the network.

    python3 tools/console-kit/test_server.py
"""

from __future__ import annotations

import http.client
import json
import os
import stat
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
        # the page (an unlocked or unknown question accepted), and a doorbell line the
        # agent's watch does not wake on, so the follow-up would sit unrun.
        from console_kit import doorbell as D
        body = {"item": "LANE.1", "text": "follow up", "intent": "fork", "mode": "tighten",
                "about_qid": "LANE.1/Q1", "roles": ["devops", "other:Legal"], "nonce": "ownerabout01"}
        code, got = self.req("POST", "/api/message", body, tok=token())
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

    def test_reanchor_dry_run_writes_nothing_and_a_real_run_only_appends(self):
        spec, _, lk = self.locked_on_spec()
        self.git("init", "-q")
        self.git("add", "spec.md")
        self.git("commit", "-qm", "v1")
        spec.write_text("A new first line.\n" + self.SPEC)
        self.git("commit", "-qam", "unrelated")
        before = self.cfg.store.read_bytes()
        rc, out, err = self.agent_cli("reanchor", "--dry-run")
        self.assertEqual(rc, 0, err)
        self.assertIn("LANE.1/Q2: would re-anchor spec.md", out)
        self.assertEqual(self.cfg.store.read_bytes(), before)
        self.assertEqual(self.state_of("LANE.1/Q2")["state"], "stale")
        rc, out, err = self.agent_cli("reanchor")
        self.assertEqual(rc, 0, err)
        after = self.cfg.store.read_bytes()
        self.assertTrue(after.startswith(before))  # nothing already written changed
        [new] = [json.loads(x) for x in after[len(before):].decode().splitlines()]
        self.assertEqual((new["type"], new["by"], new["lock"]), ("anchor", "agent", lk["id"]))
        self.assertEqual(new["anchors"], [{"kind": "excerpt", "path": "spec.md",
                                           "text": "The cited claim, line one.\nThe cited claim, line two."}])
        q = self.state_of("LANE.1/Q2")
        self.assertEqual((q["state"], q["anchored_by"]), ("locked", "reanchor"))
        self.assertEqual(self.agent_cli("reanchor")[0], 0)  # a second run finds nothing stale
        self.assertEqual(self.cfg.store.read_bytes(), after)

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
        real = SV.A.plan_reanchor

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
        real = SV.A.plan_reanchor

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


if __name__ == "__main__":
    unittest.main(verbosity=1)

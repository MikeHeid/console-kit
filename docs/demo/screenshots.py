#!/usr/bin/env python3
"""Render the README screenshots: the real console.js/console.css injected into
docs/demo/showcase.html, against a /view built by the real view.build over a
seeded store. Nothing here is mocked above the HTTP layer, so a screenshot shows
what the console actually draws.

    pip install playwright==1.56.0      # or any version whose Chromium you have
    python3 docs/demo/screenshots.py    # writes docs/screenshots/*.png

The demo project, "Northwind platform rebuild", is planned as 4 waves, 9 phases
and 5 lanes (API, Web UI, Data, Infra, Security); each item carries a
`section` like "wave-2/phase-2.2/lane-api". Its questions cover every state:
waiting for you, answered, locked, and stale (a locked ruling whose item_status
condition no longer holds).
"""
from __future__ import annotations

import json
import secrets
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO / "plugin" / "kit"))
from overture import __version__, publish as P, view as V  # noqa: E402
from overture.store import Store  # noqa: E402

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else REPO / "docs" / "screenshots"

LANES = {"API": "lane-api", "UI": "lane-ui", "DATA": "lane-data", "OPS": "lane-infra", "SEC": "lane-security"}
# id: (title, section, status)
ITEMS = {
    "W1": ("Wave 1 · Foundations", None, "built"),
    "W2": ("Wave 2 · Core services", None, "claimed"),
    "W3": ("Wave 3 · Scale & harden", None, "open"),
    "W4": ("Wave 4 · Launch", None, "open"),
    "API-1": ("Job queue on Postgres", "wave-2/phase-2.1/lane-api", "built"),
    "API-2": ("Retry & dead-letter policy", "wave-2/phase-2.2/lane-api", "claimed"),
    "API-3": ("Public REST v2 versioning", "wave-2/phase-2.2/lane-api", "open"),
    "API-4": ("GraphQL gateway", "wave-3/phase-3.1/lane-api", "deferred"),
    "UI-1": ("Design tokens & dark mode", "wave-1/phase-1.3/lane-ui", "built"),
    "UI-2": ("Invoice screen", "wave-2/phase-2.2/lane-ui", "claimed"),
    "UI-3": ("Onboarding checklist", "wave-2/phase-2.2/lane-ui", "open"),
    "UI-4": ("Team invites", "wave-2/phase-2.2/lane-ui", "blocked"),
    "DATA-1": ("Usage events pipeline", "wave-2/phase-2.1/lane-data", "built"),
    "DATA-2": ("Billing ledger schema", "wave-2/phase-2.2/lane-data", "claimed"),
    "DATA-3": ("Search index choice", "wave-2/phase-2.3/lane-data", "open"),
    "OPS-1": ("Preview environments", "wave-1/phase-1.1/lane-infra", "built"),
    "OPS-2": ("Backups & restore drill", "wave-2/phase-2.2/lane-infra", "claimed"),
    "OPS-3": ("Second region", "wave-3/phase-3.1/lane-infra", "open"),
    "SEC-1": ("Session expiry", "wave-1/phase-1.2/lane-security", "built"),
    "SEC-2": ("Roles & permissions", "wave-2/phase-2.2/lane-security", "claimed"),
    "SEC-3": ("Audit log export", "wave-3/phase-3.3/lane-security", "open"),
}


def _parent(iid: str) -> str | None:
    sec = ITEMS[iid][1]
    return None if sec is None else "W" + sec.split("/")[0].split("-")[1]


items = {i: {"title": t, "parent": _parent(i), "status": st, **({"section": s} if s else {})}
         for i, (t, s, st) in ITEMS.items()}


def _rec(rtype: str, **kw) -> dict:
    return {"type": rtype, "schemaVersion": 1, "nonce": secrets.token_hex(8), **kw}


def ask(qid, text, opts, star, star_by, evidence=None, valid_if=None, agent=None):
    q = _rec("question", qid=qid, item=qid.split("/")[0], text=text, kind="single",
             options=[{"id": k, "label": l, "description": d} for k, l, d in opts], star=star,
             star_by=star_by, valid_if=valid_if or [], source="docs/roadmap.md:1", by="agent")
    if evidence:
        q["evidence"] = evidence
    return q


STALE = "OPS-2"  # its locked ruling assumed OPS-2 "built"; it went back to in progress


def holds(cond: dict) -> bool:
    return cond.get("item") != STALE


def seed(st: Store) -> None:
    waiting = [
        ask("API-2/Q1", "How many times should a failed job retry before a human looks?",
            [("a", "3 times, exponential backoff", "Then park it in a dead-letter table and ping the owner. Matches Stripe's own retry window."),
             ("b", "Retry forever", "Never loses a job, but a broken upstream API gets hammered all night."),
             ("c", "No automatic retries", "Simplest. Every network blip becomes a manual task.")],
            "a", "devops",
            [{"cite": "src/jobs/worker.ts:41-47",
              "text": "catch (err) {\n  await markFailed(job.id, err)\n  // TODO: retry policy (API-2)\n  throw err\n}"}]),
        ask("API-3/Q1", "How should the public API be versioned?",
            [("a", "URL prefix (/v2/...)", "Obvious in logs and docs; cache-friendly."),
             ("b", "Header (Accept-Version)", "Cleaner URLs; easy to forget in curl examples."),
             ("c", "Date-pinned per account", "Stripe-style; most work to build and test.")], "a", "architect"),
        ask("UI-3/Q1", "What should the first screen after sign-up be?",
            [("a", "A 4-step checklist", "Connect repo, invite team, set billing, run first job."),
             ("b", "An empty dashboard with a tour", "Less guided; tours are often skipped."),
             ("c", "A sample project", "Shows value fastest; cleanup later.")], "a", "ux"),
        ask("SEC-2/Q1", "Which roles do we ship first?",
            [("a", "Owner, Admin, Member", "Covers 90% of teams; easy to explain."),
             ("b", "Owner + custom roles", "Flexible; needs a permissions UI now."),
             ("c", "Owner, Member only", "Fastest; admins will ask on day one.")], "a", "security"),
        ask("DATA-2/Q1", "Store money as integer cents or decimals?",
            [("a", "Integer minor units (cents)", "No rounding bugs; matches Stripe."),
             ("b", "NUMERIC(12,2)", "Readable in SQL; easy to misuse in JS.")], "a", "analyst"),
        ask("OPS-3/Q1", "Which second region?",
            [("a", "eu-west-1", "Most EU customers; GDPR story."),
             ("b", "ap-southeast-2", "Two large APAC prospects.")], "a", "devops"),
    ]
    for q in waiting:
        st.append(q)
    locked = [
        (ask("API-1/Q1", "Where should the job queue live?",
             [("a", "Postgres + SKIP LOCKED", "No new service; fine to ~1k jobs/s."),
              ("b", "Redis + BullMQ", "Faster; one more thing to run.")], "a", "architect"),
         "a", "Postgres until it hurts. Revisit at 500 jobs/s."),
        (ask("SEC-1/Q1", "Should sessions expire after inactivity?",
             [("a", "After 30 days", "Common default."), ("b", "After 24 hours", "Safer; more logins.")],
             "a", "security"), "a", ""),
        (ask("UI-1/Q1", "Dark mode: follow the system or ship a toggle?",
             [("a", "Follow system, plus a toggle", "Respects the OS; lets people override."),
              ("b", "Light only for now", "Half the CSS work.")], "a", "ux"), "a", ""),
        (ask("OPS-2/Q1", "Restore drill cadence?",
             [("a", "Monthly, automated", "Proves backups actually restore."),
              ("b", "Quarterly, manual", "Less noise; slower to catch rot.")], "a", "devops",
             valid_if=[{"kind": "item_status", "item": "OPS-2", "status": "built"}]),
         "a", "Monthly. A backup we never restored is a hope, not a backup."),
    ]
    for q, pick, words in locked:
        st.append(q)
        a = st.append(_rec("answer", qid=q["qid"], picks=[pick], own_text=words, by="owner"))
        st.append(_rec("lock", qid=q["qid"], answer=a["id"], by="owner"))
    q = ask("UI-2/Q1", "Show tax as its own line on the invoice screen?",
            [("a", "Yes, always", "Required in the EU anyway."), ("b", "Only when non-zero", "Tidier for US customers.")],
            "a", "ux")
    st.append(q)
    st.append(_rec("answer", qid=q["qid"], picks=["a"], own_text="", by="owner"))


def build_view() -> dict:
    with tempfile.TemporaryDirectory() as td:
        st = Store(Path(td) / "store.jsonl", known_items=items)
        seed(st)
        view = V.build(st, items, holds)
    now = datetime.now(timezone.utc)
    return {"view": view, "items": items,
            "cursor": {"listening": {"state": "listening", "last_seen": now.isoformat()},
                       "last_synced_at": (now - timedelta(minutes=2)).isoformat()}}


STATUS = {"ok": True, "version": __version__, "register": "ok", "agent": "steward", "uptime_s": 86400,
          "waiters": 1, "max_waiters": 32, "cron_last_tick_age_s": 20}


def serve(view: dict) -> ThreadingHTTPServer:
    page = P.inject((HERE / "showcase.html").read_text(), P.console_block('{"api": "/api"}'))

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path.startswith("/api/view"):
                body, ct, code = json.dumps(view), "application/json", 200
            elif self.path.startswith("/api/status"):
                body, ct, code = json.dumps(STATUS), "application/json", 200
            elif self.path.startswith("/api/"):
                body, ct, code = "{}", "application/json", 404
            else:
                body, ct, code = page, "text/html", 200
            data = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def main() -> int:
    from playwright.sync_api import sync_playwright
    OUT.mkdir(parents=True, exist_ok=True)
    srv = serve(build_view())
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    errors: list[str] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for scheme in ("dark", "light"):
            page = browser.new_page(viewport={"width": 1680, "height": 1000}, color_scheme=scheme,
                                    device_scale_factor=2)
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(url)
            page.wait_for_selector(".ck-panel[data-open='true']", timeout=15000)
            page.wait_for_timeout(1500)
            page.screenshot(path=str(OUT / f"overview-{scheme}.png"))
            page.goto(url + "#item=API-2")
            page.wait_for_timeout(1800)
            page.screenshot(path=str(OUT / f"question-{scheme}.png"))
            page.close()
        browser.close()
    srv.shutdown()
    for e in errors:
        print("page error:", e, file=sys.stderr)
    print("wrote", ", ".join(sorted(p.name for p in OUT.glob("*.png"))))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())

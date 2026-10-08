#!/usr/bin/env python3
"""The docked inbox (AB-2/Q2) in a real browser: console.js + console.css injected
into a minimal host page, against a stub /view.

CI has no browser, so this file skips unless OVERTURE_BROWSER=1 is set; with it
set, a missing Playwright or browser is a failure, never a skip. Run locally:

    OVERTURE_BROWSER=1 python3 tools/overture/test_browser.py
    OVERTURE_BROWSERS=chromium,firefox OVERTURE_BROWSER=1 python3 tools/overture/test_browser.py
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
KIT = HERE / "plugin" / "kit"  # the kit ships inside the plugin, so every install carries it
sys.path.insert(0, str(KIT))
from overture import publish as P  # noqa: E402

REQUIRED = os.environ.get("OVERTURE_BROWSER") == "1"
BROWSERS = [b.strip() for b in os.environ.get("OVERTURE_BROWSERS", "chromium").split(",") if b.strip()]

# A host page with nothing of any vendoring project's in it: the kit must bring its own room.
HOST = """<!doctype html><html><head><meta charset="utf-8"><title>host</title>
<style>:root{--c-surface:#fff;--c-surface-alt:#f4f4f4;--c-border:#ccc;--c-fg:#111;
--c-fg-muted:#555;--c-accent:#0057d9;--c-claimed:#7a4b00;--c-claimed-bg:#fff3d6}
body{margin:0;font:14px sans-serif}</style></head><body>
<input id="board-input" aria-label="board input">
<details id="item-LANE.1"><summary>LANE.1 first lane</summary><p>body</p></details>
</body></html>"""


def _view(owner_message: bool = False, second_lane: bool = False, multiline: bool = False) -> dict:
    """/view exactly as the server builds it (server.py: view.build over a store),
    from two questions made by the kit tests' own fixture, so it cannot drift.
    owner_message adds one owner message on LANE.1 with no agent reply, which
    puts that item in awaiting_agent; second_lane adds LANE.2 with one too."""
    import tempfile
    from overture import view as V
    from overture.store import Store
    from test_kit import message, question
    items = {"LANE.1": {"title": "first lane", "parent": None}}
    if second_lane:
        items["LANE.2"] = {"title": "second lane", "parent": None}
    with tempfile.TemporaryDirectory() as td:
        st = Store(Path(td) / "store.jsonl", known_items=items)
        st.append(question("LANE.1/Q1"))
        st.append(question("LANE.1/Q2"))
        if owner_message:
            st.append(message(item="LANE.1"))
        if second_lane:
            st.append(message(item="LANE.2"))
        if multiline:  # 0.8.4: an owner message and an agent reply, each over three lines
            st.append(message(item="LANE.1", text=MULTILINE_OWNER))
            st.append(message(item="LANE.1", by="agent", text=MULTILINE_REPLY))
        return {"view": V.build(st, items, lambda c: True), "items": items, "cursor": {}}


MULTILINE_OWNER = "first owner line\nsecond owner line\nthird owner line"
MULTILINE_REPLY = "Recorded.\n\n- one\n- two"
VIEW = _view()
VIEW_MULTILINE = _view(multiline=True)
VIEW_AGENT = _view(owner_message=True)
VIEW_AGENT2 = _view(owner_message=True, second_lane=True)
assert VIEW_AGENT["view"]["awaiting_agent"] == ["LANE.1"], VIEW_AGENT["view"]["awaiting_agent"]
assert sorted(VIEW_AGENT2["view"]["awaiting_agent"]) == ["LANE.1", "LANE.2"], VIEW_AGENT2["view"]["awaiting_agent"]
assert VIEW["view"]["awaiting_agent"] == [], VIEW["view"]["awaiting_agent"]
PAGE = P.inject(HOST, P.console_block('{"api": "/api"}'))


# The lane board (AB-2/Q4): the REAL page, rendered from the real register, so
# console.js is held to dashboard.apply_live -- the patcher the shape is built on.
# It lives in the project that hosts the kit (OVERTURE_BOARD_DIR, default the
# layout the kit was born in); without it LiveBoardTests skip, by name.
BOARD_DIR = Path(os.environ.get("OVERTURE_BOARD_DIR", HERE.parent.parent / "scripts/register"))
if (BOARD_DIR / "dashboard.py").is_file():
    sys.path.insert(0, str(BOARD_DIR))
    import dashboard as D  # noqa: E402

    REGISTER = D.load()
    # open -> proposed moves no "claimed by" line and no "waits on" link: the shape holds.
    FLIPPED = {i: ({**d, "status": "proposed"} if d["status"] == "open" and n % 7 == 0 else d)
               for n, (i, d) in enumerate(sorted(REGISTER.items()))}
    BOARD_A, BOARD_B = D.board_live(REGISTER), D.board_live(FLIPPED)
    LIVE_PAGES = {"/board-a": P.inject(D.render_html(REGISTER)[0], P.console_block('{"api": "/api"}')),
                  "/board-b": P.inject(D.render_html(FLIPPED)[0], P.console_block('{"api": "/api"}'))}
else:
    D = None
    BOARD_A = BOARD_B = None
    LIVE_PAGES = {}


class _Handler(BaseHTTPRequestHandler):
    view_delay = 0.0   # seconds before /view answers (a slow server)
    view_fails = False  # /view answers 503 (a server that is down)
    view_agent = False  # /view carries one thread waiting on the agent
    view_multiline = False  # /view carries a thread whose messages span several lines (0.8.4)
    view_working = False  # ...and the cursor says an agent is working on it
    view_listening = None  # the cursor's `listening`; None leaves it out, as a pre-0.6.0 server
    board: object = None   # what /api/board answers; None is 404, as a project with no board()
    board_status = 200
    board_hits = 0

    def log_message(self, *args):
        pass

    def do_GET(self):
        status = 200
        if self.path.startswith("/api/view"):
            import time
            time.sleep(_Handler.view_delay)
            status = 503 if _Handler.view_fails else 200
            payload = dict(VIEW_MULTILINE if _Handler.view_multiline else VIEW_AGENT2 if _Handler.view_agent == 2
                           else VIEW_AGENT if _Handler.view_agent else VIEW)
            if _Handler.view_working:  # an agent has marked this item id as in hand
                payload["cursor"] = {"working": {_Handler.view_working: "2026-09-29T10:00:00Z"}}
            if _Handler.view_listening is not None:  # the watch heartbeat, as the server judged it
                payload["cursor"] = {**payload.get("cursor", {}), "listening": _Handler.view_listening}
            body, ctype = json.dumps(payload), "application/json"
        elif self.path.startswith("/api/board"):
            _Handler.board_hits += 1
            status = 404 if _Handler.board is None else _Handler.board_status
            body, ctype = json.dumps(_Handler.board if status == 200 else {"error": "x"}), "application/json"
        elif self.path in LIVE_PAGES:
            body, ctype = LIVE_PAGES[self.path], "text/html"
        else:
            body, ctype = PAGE, "text/html"
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


STATE = """() => {
  const q = s => document.querySelector(s);
  const shown = e => !!e && getComputedStyle(e).display !== 'none' && e.getBoundingClientRect().width > 0;
  const box = e => { const b = e.getBoundingClientRect(); return [Math.round(b.left), Math.round(b.right)]; };
  const p = q('.ck-panel'), strip = q('.ck-dock-strip');
  return {
    strip: shown(strip) ? box(strip) : null,
    badge: strip.querySelector('.ck-inbox-count').textContent,
    expanded: strip.getAttribute('aria-expanded'),
    inboxBtn: shown(q('.ck-inbox-btn')),
    open: p.getAttribute('data-open') === 'true',
    panel: p.getAttribute('data-open') === 'true' ? box(p) : null,
    modal: p.getAttribute('aria-modal'),
    role: p.getAttribute('role'),
    inert: p.inert,
    backdrop: q('.ck-backdrop').getAttribute('data-open') === 'true',
    bodyMargin: getComputedStyle(document.body).marginRight,
    focusInPanel: p.contains(document.activeElement),
    focus: document.activeElement.className || document.activeElement.id || document.activeElement.tagName,
    overflow: document.documentElement.scrollWidth > innerWidth,
  };
}"""


FOCUSABLE = 'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'


def _expand(page, qid=None) -> None:
    """CONSOLE-kit/Q37: a locked question is one line until opened; open it (or every one) to reach its controls."""
    sel = f".ck-q-line[data-qid='{qid}'][aria-expanded='false']" if qid else ".ck-q-line[aria-expanded='false']"
    while page.locator(sel).count():
        page.locator(sel).first.click()


def _playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    return sync_playwright


class DockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not REQUIRED:
            raise unittest.SkipTest("browser tests run only with OVERTURE_BROWSER=1")
        sp = _playwright()
        if sp is None:
            raise RuntimeError("OVERTURE_BROWSER=1 but Playwright is not installed")
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}/"
        cls.pw = sp().start()

    @classmethod
    def tearDownClass(cls):
        cls.pw.stop()
        cls.server.shutdown()

    def setUp(self):
        _Handler.view_delay, _Handler.view_fails, _Handler.view_agent, _Handler.view_working = 0.0, False, False, False
        _Handler.view_listening, _Handler.view_multiline = None, False

    def _page(self, kind: str, width: int, loaded: bool = True):
        browser = getattr(self.pw, kind).launch()
        self.addCleanup(browser.close)
        page = browser.new_page(viewport={"width": width, "height": 800})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        self.addCleanup(lambda: self.assertEqual(errors, [], f"{kind}: page errors"))
        page.goto(self.url)
        if loaded:
            page.wait_for_function("document.querySelector('.ck-dock-strip .ck-inbox-count').textContent === '2'")
        return page

    def _tab_path(self, page, presses: int) -> list[bool]:
        """Press Tab `presses` times; for each stop, is focus inside the panel?"""
        seen = []
        for _ in range(presses):
            page.keyboard.press("Tab")
            seen.append(page.evaluate("document.querySelector('.ck-panel').contains(document.activeElement)"))
        return seen

    def _state(self, page) -> dict:
        page.wait_for_timeout(300)  # the body margin eases over 0.2s
        return page.evaluate(STATE)

    def test_desktop_starts_collapsed_and_opens_a_docked_column(self):
        for kind in BROWSERS:
            with self.subTest(browser=kind):
                page = self._page(kind, 1400)
                s = self._state(page)
                self.assertEqual(s["strip"], [1356, 1400], s)        # 44px strip on the right edge
                self.assertEqual((s["badge"], s["expanded"], s["open"]), ("2", "false", False), s)
                self.assertFalse(s["inboxBtn"], s)                    # the floating button gives way
                self.assertEqual(s["bodyMargin"], "44px", s)          # the board keeps clear of the strip
                page.click(".ck-dock-strip")
                s = self._state(page)
                self.assertEqual(s["panel"], [700, 1400], s)          # one click: a column half the screen
                self.assertEqual((s["modal"], s["backdrop"]), (None, False), s)
                self.assertEqual((s["role"], s["inert"]), ("complementary", False), s)  # a landmark
                self.assertEqual((s["strip"], s["expanded"], s["bodyMargin"]), (None, "true", "700px"), s)
                self.assertTrue(s["focusInPanel"], s)
                self.assertFalse(s["overflow"], s)

    # Owner, 2026-09-29: "on big monitors can you make it 50% of the screen until
    # closed". Catches: a fixed-width column, a column whose width and the page's
    # margin disagree (board text under the column), and a state chip that keeps
    # its own column beside the question and squeezes the text into a strip.
    HALF = """() => { const p = document.querySelector('.ck-panel').getBoundingClientRect();
      const qs = Array.from(document.querySelectorAll('.ck-panel .ck-q-header')).map(h => {
        const c = h.querySelector('.ck-q-state').getBoundingClientRect(), t = h.querySelector('.ck-q-textcol').getBoundingClientRect();
        return { chipAbove: c.bottom <= t.top + 1, textWide: t.width >= h.getBoundingClientRect().width - 30 }; });
      return { panel: [Math.round(p.left), Math.round(p.right)], margin: getComputedStyle(document.body).marginRight,
               overflow: document.documentElement.scrollWidth > innerWidth, qs }; }"""

    def test_an_open_docked_column_is_half_the_screen(self):
        for kind in BROWSERS:
            for width in (1024, 1400, 1920):
                with self.subTest(browser=kind, width=width):
                    page = self._page(kind, width)
                    page.click(".ck-item-btn")  # an item's view, where question cards are drawn
                    page.wait_for_selector(".ck-panel .ck-q-header")
                    # the column slides in: measure once it has arrived at the right edge
                    page.wait_for_function("Math.round(document.querySelector('.ck-panel').getBoundingClientRect().right) === innerWidth")
                    got = page.evaluate(self.HALF)
                    self.assertEqual(got["panel"], [width // 2, width], got)
                    self.assertEqual(got["margin"], f"{width // 2}px", got)
                    self.assertFalse(got["overflow"], got)
                    self.assertTrue(got["qs"], got)  # the inbox really shows questions
                    for q in got["qs"]:
                        self.assertEqual(q, {"chipAbove": True, "textWide": True}, got)

    def test_board_stays_usable_and_escape_belongs_to_the_column(self):
        for kind in BROWSERS:
            with self.subTest(browser=kind):
                page = self._page(kind, 1400)
                page.click(".ck-dock-strip")
                page.click("#board-input")
                page.keyboard.type("abc")
                self.assertEqual(page.input_value("#board-input"), "abc")
                page.keyboard.press("Escape")
                s = self._state(page)
                self.assertTrue(s["open"], s)                          # Escape out on the board: stays
                page.focus(".ck-panel .ck-close-btn")
                page.keyboard.press("Escape")
                s = self._state(page)
                self.assertFalse(s["open"], s)                         # Escape inside: collapses
                self.assertEqual((s["strip"], s["focus"]), ([1356, 1400], "ck-dock-strip"), s)

    def test_item_views_dock_too(self):
        for kind in BROWSERS:
            with self.subTest(browser=kind):
                page = self._page(kind, 1400)
                page.click(".ck-item-btn")
                s = self._state(page)
                self.assertEqual((s["panel"], s["modal"], s["backdrop"]), ([700, 1400], None, False), s)
                self.assertEqual(page.get_attribute(".ck-panel", "aria-label"), "Console: LANE.1")

    # 0.8.4, owner: messages and agent replies lost their line breaks. The text goes in by
    # textContent (never HTML), so only CSS keeps its newlines: without `white-space: pre-wrap`
    # three lines render as one. innerText follows the rendering, so it says which happened.
    LINES = """() => Array.from(document.querySelectorAll('.ck-message-text')).map(e => {
      const lh = parseFloat(getComputedStyle(e).lineHeight);
      return { text: e.innerText, html: e.innerHTML, lines: Math.round(e.getBoundingClientRect().height / lh) }; })"""

    def test_a_multi_line_message_keeps_its_line_breaks(self):
        for kind in BROWSERS:
            with self.subTest(browser=kind):
                _Handler.view_multiline = True
                page = self._page(kind, 1400, loaded=False)
                page.wait_for_selector(".ck-item-btn")
                page.click(".ck-item-btn")
                page.wait_for_selector(".ck-message-text")
                got = {g["text"]: g for g in page.evaluate(self.LINES)}
                self.assertIn(MULTILINE_OWNER, got, list(got))
                self.assertIn(MULTILINE_REPLY, got, list(got))
                self.assertEqual(got[MULTILINE_OWNER]["lines"], 3, got)
                self.assertEqual(got[MULTILINE_REPLY]["lines"], 4, got)   # the blank line is kept too
                # Catches: a fix that turns the text into HTML (<br>) instead of styling it.
                self.assertNotIn("<", got[MULTILINE_REPLY]["html"])

    def test_narrow_window_keeps_the_overlay(self):
        for kind in BROWSERS:
            with self.subTest(browser=kind):
                page = self._page(kind, 1023)
                s = self._state(page)
                self.assertEqual((s["strip"], s["inboxBtn"], s["bodyMargin"]), (None, True, "0px"), s)
                page.click(".ck-inbox-btn")
                s = self._state(page)
                self.assertEqual((s["modal"], s["backdrop"], s["role"]), ("true", True, "dialog"), s)
                page.keyboard.press("Escape")
                self.assertFalse(self._state(page)["open"])
                page.set_viewport_size({"width": 1024, "height": 800})
                s = self._state(page)
                self.assertEqual((s["strip"], s["inboxBtn"]), ([980, 1024], False), s)

    def test_narrowing_an_open_column_makes_a_proper_modal(self):
        for kind in BROWSERS:
            with self.subTest(browser=kind):
                page = self._page(kind, 1400)
                page.click(".ck-dock-strip")
                page.click("#board-input")                         # focus out on the board
                page.set_viewport_size({"width": 800, "height": 800})
                s = self._state(page)
                self.assertEqual((s["modal"], s["backdrop"], s["focusInPanel"]), ("true", True, True), s)
                page.set_viewport_size({"width": 1400, "height": 800})
                s = self._state(page)
                self.assertEqual((s["modal"], s["backdrop"], s["panel"]), (None, False, [700, 1400]), s)


    def test_shift_tab_leaves_an_open_docked_column(self):
        # Docked is not modal: no focus trap, so Shift+Tab from the column's
        # first control steps back onto the page instead of wrapping to its last.
        # (Tested from the start, not the end: the column is last in the DOM, and
        # tabbing off the end of a page goes to browser chrome, where Firefox
        # leaves document.activeElement unchanged.)
        for kind in BROWSERS:
            with self.subTest(browser=kind):
                page = self._page(kind, 1400)
                page.click(".ck-dock-strip")
                self._state(page)
                page.evaluate("""() => document.querySelector('.ck-panel').querySelector(
                  'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])').focus()""")
                page.keyboard.press("Shift+Tab")
                s = self._state(page)
                self.assertFalse(s["focusInPanel"], s)
                self.assertEqual(s["focus"], "ck-item-btn", s)             # the page's last control
                # And forwards: Tab from the column's last control is not held.
                # Off the end of the page focus goes to browser chrome, which
                # headless Firefox does not report, so watch the key itself: a
                # trap cancels the Tab (preventDefault), a non-modal column must not.
                page.evaluate("() => document.addEventListener('keydown', e => {"
                              " if (e.key === 'Tab') window.__tabHeld = e.defaultPrevented; })")
                page.evaluate(f"() => {{ const f = [...document.querySelector('.ck-panel').querySelectorAll({FOCUSABLE!r})]"
                              ".filter(e => e.getClientRects().length); f[f.length - 1].focus(); }")
                page.keyboard.press("Tab")
                self.assertIs(page.evaluate("window.__tabHeld"), False, f"{kind}: the docked column held Tab")

    def test_overlay_trap_skips_the_hidden_back_button(self):
        # Below 1024px the overlay IS modal: Shift+Tab from its first rendered
        # control wraps to its last, never out to the page behind the backdrop.
        for kind in BROWSERS:
            with self.subTest(browser=kind):
                page = self._page(kind, 800)
                page.click(".ck-inbox-btn")
                self._state(page)
                page.focus(".ck-panel .ck-close-btn")              # first rendered control
                page.keyboard.press("Shift+Tab")
                self.assertTrue(self._state(page)["focusInPanel"])

    def test_a_closed_column_is_out_of_the_tab_order(self):
        for kind in BROWSERS:
            with self.subTest(browser=kind):
                page = self._page(kind, 1400)
                page.click(".ck-dock-strip")
                self._state(page)                    # let the open's refresh re-render land
                page.focus(".ck-panel .ck-close-btn")
                page.keyboard.press("Escape")
                s = self._state(page)
                self.assertEqual((s["open"], s["inert"]), (False, True), s)
                page.focus("#board-input")
                self.assertEqual(self._tab_path(page, 6), [False] * 6)

    def test_a_slow_refresh_never_pulls_focus_off_the_board(self):
        # Opening fetches /view again; when it lands late, a docked column must
        # not take focus back from the board the owner has moved on to.
        for kind in BROWSERS:
            with self.subTest(browser=kind):
                page = self._page(kind, 1400)
                _Handler.view_delay = 1.0
                page.click(".ck-dock-strip")
                page.wait_for_timeout(150)
                page.click("#board-input")
                page.wait_for_timeout(1600)                        # the refresh has landed
                self.assertEqual(self._state(page)["focus"], "board-input")
                _Handler.view_delay = 0.0

    def test_badge_says_unknown_until_the_view_loads(self):
        for kind in BROWSERS:
            with self.subTest(browser=kind):
                _Handler.view_fails = True
                page = self._page(kind, 1400, loaded=False)
                page.wait_for_timeout(500)
                self.assertEqual(page.text_content(".ck-dock-strip .ck-inbox-count"), "?")
                self.assertIn("not loaded", page.get_attribute(".ck-dock-strip", "aria-label"))
                self.assertEqual(page.get_attribute(".ck-dock-strip", "aria-controls"), "ck-panel")
                _Handler.view_fails = False

    # Catches: threads waiting on the agent summed into the owner's badge (it would
    # read 3, and "waiting" would include work that is not the owner's), or never
    # shown at all. Checked on both presentations: the strip and the button.
    AGENT = """sel => { const b = document.querySelector(sel), a = b.querySelector('.ck-agent-count');
      return { owner: b.querySelector('.ck-inbox-count').textContent, agent: a.textContent,
               agentShown: getComputedStyle(a).display !== 'none', label: b.getAttribute('aria-label') }; }"""

    def test_threads_waiting_on_the_agent_get_their_own_count(self):
        for kind in BROWSERS:
            for width, sel in ((1400, ".ck-dock-strip"), (800, ".ck-inbox-btn")):
                with self.subTest(browser=kind, width=width):
                    _Handler.view_agent = True
                    page = self._page(kind, width)
                    got = page.evaluate(self.AGENT, sel)
                    self.assertEqual(got["owner"], "2")
                    self.assertEqual(got["agent"], "● 1")
                    self.assertTrue(got["agentShown"])
                    self.assertEqual(got["label"], "Open inbox, 2 waiting for you, 1 waiting on an agent")

    # The inbox's "with the agent" row, as the owner reads it.
    AGENT_ROW = """() => { const s = document.querySelector('.ck-inbox-list .ck-q-state[data-state="agent_active"], .ck-inbox-list .ck-q-state[data-state="awaiting_agent"]');
      const hs = Array.from(document.querySelectorAll('.ck-section-heading')).map(h => h.textContent);
      return { chip: s && s.textContent, state: s && s.getAttribute('data-state'), headings: hs }; }"""

    def test_an_item_an_agent_is_working_on_reads_agent_active(self):
        # Owner, 2026-09-29: "when an agent is working on something, relabel 'awaiting agent' to 'agent active'".
        # Catches: the relabel applied to every agent thread (it would claim work nobody picked up),
        # never applied, or applied whenever ANY item is marked (a mark on OTHER.9 must leave
        # LANE.1 reading "awaiting agent"; review of PR #180). Every state, both presentations.
        for kind in BROWSERS:
            for marked, words, heading in (("LANE.1", "agent active", "Agent active"),
                                           (False, "awaiting agent", "Awaiting agent"),
                                           ("OTHER.9", "awaiting agent", "Awaiting agent")):
                working = marked == "LANE.1"
                for width, sel in ((1400, ".ck-dock-strip"), (800, ".ck-inbox-btn")):
                    with self.subTest(browser=kind, marked=marked, width=width):
                        _Handler.view_agent, _Handler.view_working = True, marked
                        page = self._page(kind, width)
                        label = page.evaluate(self.AGENT, sel)["label"]
                        want = "1 with an agent at work" if working else "1 waiting on an agent"
                        self.assertEqual(label, "Open inbox, 2 waiting for you, " + want)
                        page.click(sel)
                        page.wait_for_selector(".ck-inbox-list .ck-q-state[data-state='agent_active'], .ck-inbox-list .ck-q-state[data-state='awaiting_agent']")
                        got = page.evaluate(self.AGENT_ROW)
                        self.assertEqual(got["chip"], "● " + words, got)
                        self.assertEqual(got["state"], "agent_active" if working else "awaiting_agent", got)
                        self.assertIn(heading, got["headings"], got)

    AGENT_CHIPS = """() => Array.from(document.querySelectorAll(
        '.ck-inbox-list .ck-q-state[data-state="agent_active"], .ck-inbox-list .ck-q-state[data-state="awaiting_agent"]'))
        .map(s => s.getAttribute('data-state')).sort()"""

    def test_two_agent_threads_one_in_hand(self):
        # Review of PR #180: with one agent thread the mark and the thread coincide, so a label
        # that ignores WHICH item is marked still passes. Two threads, LANE.1 marked: one chip
        # each way, the heading stays "Awaiting agent" (not every row is in hand), both counts named.
        for kind in BROWSERS:
            for width, sel in ((1400, ".ck-dock-strip"), (800, ".ck-inbox-btn")):
                with self.subTest(browser=kind, width=width):
                    _Handler.view_agent, _Handler.view_working = 2, "LANE.1"
                    page = self._page(kind, width)
                    label = page.evaluate(self.AGENT, sel)["label"]
                    self.assertEqual(label, "Open inbox, 2 waiting for you, 1 waiting on an agent, "
                                            "1 with an agent at work")
                    page.click(sel)
                    page.wait_for_selector(".ck-inbox-list .ck-q-state[data-state='agent_active']")
                    self.assertEqual(page.evaluate(self.AGENT_CHIPS), ["agent_active", "awaiting_agent"])
                    heads = page.evaluate(self.AGENT_ROW)["headings"]
                    self.assertIn("Awaiting agent", heads)
                    self.assertNotIn("Agent active", heads)

    # Owner, 2026-09-29: "after hitting 'all answers' there is no way to get back to 'inbox'".
    TOGGLE = """() => { const b = document.querySelector('.ck-sheet-toggle'), t = document.querySelector('.ck-title');
      const back = document.querySelector('.ck-back-btn');
      return { text: b && b.textContent, pressed: b && b.getAttribute('aria-pressed'), title: t && t.textContent,
               backShown: !!back && getComputedStyle(back).display !== 'none' }; }"""

    def test_all_answers_is_a_toggle_back_to_the_inbox(self):
        # Catches: a sheet with no way back on a desktop, where the header's Back button is hidden
        # (the reported bug), or a toggle that goes somewhere other than the inbox.
        for kind in BROWSERS:
            for width, opener in ((1400, ".ck-dock-strip"), (800, ".ck-inbox-btn")):
                with self.subTest(browser=kind, width=width):
                    page = self._page(kind, width)
                    page.click(opener)
                    page.wait_for_selector(".ck-sheet-toggle")
                    got = page.evaluate(self.TOGGLE)
                    self.assertEqual((got["text"], got["pressed"], got["title"]), ("All answers", "false", "Inbox"), got)
                    page.click(".ck-sheet-toggle")
                    page.wait_for_function("document.querySelector('.ck-title').textContent.startsWith('Answers')")
                    got = page.evaluate(self.TOGGLE)
                    self.assertEqual((got["text"], got["pressed"]), ("Inbox", "true"), got)
                    if width >= 1024:
                        self.assertFalse(got["backShown"], "the toggle is the only way back here")
                    page.click(".ck-sheet-toggle")
                    page.wait_for_function("document.querySelector('.ck-title').textContent === 'Inbox'")
                    got = page.evaluate(self.TOGGLE)
                    self.assertEqual((got["text"], got["pressed"]), ("All answers", "false"), got)
                    focused = page.evaluate("document.activeElement === document.querySelector('.ck-title')")
                    self.assertTrue(focused, "focus lands on the inbox title, not on a removed button")

    def test_no_agent_count_when_nothing_waits_on_the_agent(self):
        for kind in BROWSERS:
            for width, sel in ((1400, ".ck-dock-strip"), (800, ".ck-inbox-btn")):
                with self.subTest(browser=kind, width=width):
                    page = self._page(kind, width)
                    got = page.evaluate(self.AGENT, sel)
                    self.assertEqual((got["owner"], got["agent"], got["agentShown"]), ("2", "", False))
                    self.assertEqual(got["label"], "Open inbox, 2 waiting for you")

    LISTENING = """() => { const s = document.querySelector('.ck-status-bar .ck-listening');
      const g = s && s.querySelector('.ck-listening-glyph');
      return s && { text: s.textContent, state: s.getAttribute('data-state'), title: s.title,
                    anim: getComputedStyle(g).animationName }; }"""

    def test_agent_listening_is_said_in_words(self):
        # 0.6.0. Catches: a state shown by colour alone (the words must differ per state), an
        # old server with no `listening` read as listening, and the pulse ignoring reduced motion.
        cases = (({"state": "listening", "last_seen": "2026-09-29T10:00:00Z"}, "listening", "● Agent listening"),
                 ({"state": "idle", "last_seen": "2020-01-02T03:04:05Z"}, "idle", "○ Agent idle since "),
                 ({"state": "never", "last_seen": None}, "never", "○ No agent has listened yet"),
                 (None, "unknown", "? Agent: not known"))
        for kind in BROWSERS:
            for listening, state, words in cases:
                with self.subTest(browser=kind, state=state):
                    _Handler.view_listening = listening
                    page = self._page(kind, 1400)
                    page.click(".ck-dock-strip")
                    page.wait_for_selector(".ck-status-bar .ck-listening")
                    page.wait_for_function("document.querySelector('.ck-listening').getAttribute('data-state') === %r"
                                           % state)
                    got = page.evaluate(self.LISTENING)
                    self.assertTrue(got["text"].startswith(words), got)
                    self.assertTrue(got["title"], got)
                    if state == "idle":
                        self.assertIn("2020", got["text"], "an older day shows its date")
                    self.assertEqual(got["anim"] != "none", state == "listening", got)
            with self.subTest(browser=kind, reduced_motion=True):
                _Handler.view_listening = cases[0][0]
                page = self._page(kind, 1400)
                page.emulate_media(reduced_motion="reduce")
                page.click(".ck-dock-strip")
                page.wait_for_function("(document.querySelector('.ck-listening') || {}).getAttribute && "
                                       "document.querySelector('.ck-listening').getAttribute('data-state') === 'listening'")
                self.assertEqual(page.evaluate(self.LISTENING)["anim"], "none")


LIVE_MARKS = "[data-live],[data-live-title],[data-live-width],[data-live-status]"
# Every live-marked element as the browser now shows it: key, text, title, width, status class.
SIGNATURE = """() => Array.from(document.querySelectorAll('%s')).map(e => [
  e.getAttribute('data-live') || e.getAttribute('data-live-title') ||
    e.getAttribute('data-live-width') || e.getAttribute('data-live-status'),
  e.textContent, e.title, e.style.width,
  Array.from(e.classList).filter(c => c.startsWith('s-')).join(' ')])""" % LIVE_MARKS
COUNT_UPDATES = """window.__boardChanged = 0;
document.addEventListener('ck:board-updated', e => { window.__boardChanged += e.detail.changed; });"""


class LiveBoardTests(unittest.TestCase):
    """AB-2/Q4 in a real browser: the committed page, caught up from /api/board."""

    # The same server and Playwright as DockTests, without inheriting its tests.
    @classmethod
    def setUpClass(cls):
        if D is None:
            raise unittest.SkipTest(f"no lane board at {BOARD_DIR} (set OVERTURE_BOARD_DIR)")
        DockTests.__dict__["setUpClass"].__func__(cls)

    tearDownClass = DockTests.__dict__["tearDownClass"]

    def setUp(self):
        _Handler.view_delay, _Handler.view_fails, _Handler.view_agent, _Handler.view_working = 0.0, False, False, False
        _Handler.view_listening = None
        _Handler.board, _Handler.board_status, _Handler.board_hits = None, 200, 0

    def _board_page(self, kind, path, board, width=1280):
        _Handler.board = board
        browser = getattr(self.pw, kind).launch()
        self.addCleanup(browser.close)
        page = browser.new_page(viewport={"width": width, "height": 800})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        self.addCleanup(lambda: self.assertEqual(errors, [], f"{kind}: page errors"))
        page.add_init_script(COUNT_UPDATES)
        page.goto(self.url.rstrip("/") + path)
        return page

    def _settle(self, page, hits):
        # Wait until the board has been fetched `hits` times and applied (fetch is async).
        for _ in range(100):
            if _Handler.board_hits >= hits:
                break
            page.wait_for_timeout(50)
        page.wait_for_timeout(300)

    def test_a_status_change_patches_the_page_into_the_new_page(self):
        self.assertEqual(BOARD_A["shape"], BOARD_B["shape"])
        self.assertNotEqual(BOARD_A["values"], BOARD_B["values"])
        for kind in BROWSERS:
            with self.subTest(browser=kind):
                want = self._board_page(kind, "/board-b", BOARD_B).evaluate(SIGNATURE)
                page = self._board_page(kind, "/board-a", BOARD_A)
                self._settle(page, 1)
                before = page.evaluate(SIGNATURE)
                self.assertNotEqual(before, want)  # the change is there to be made
                _Handler.board = BOARD_B
                page.evaluate("window.ConsoleKit.refreshBoard()")
                page.wait_for_function("window.__boardChanged > 0")
                self.assertEqual(page.evaluate(SIGNATURE), want)

    def test_an_unchanged_board_touches_nothing(self):
        for kind in BROWSERS:
            with self.subTest(browser=kind):
                page = self._board_page(kind, "/board-a", BOARD_A)
                before = page.evaluate(SIGNATURE)
                self._settle(page, 1)
                self.assertGreaterEqual(_Handler.board_hits, 1)  # it did ask
                self.assertEqual(page.evaluate("window.__boardChanged"), 0)
                self.assertEqual(page.evaluate(SIGNATURE), before)

    def test_a_different_shape_says_what_fixes_it_and_stops(self):
        for kind in BROWSERS:
            with self.subTest(browser=kind):
                _Handler.board_hits = 0
                other = {"shape": "0" * 16, "values": dict(BOARD_B["values"])}
                page = self._board_page(kind, "/board-a", other)
                page.wait_for_selector(".ck-board-stale", timeout=5000)
                self.assertEqual(page.evaluate("window.__boardChanged"), 0)  # nothing patched
                # A published snapshot reloads as itself, so no Reload; nothing staged here, so the steward.
                self.assertNotIn("Reload", page.inner_text(".ck-board-stale"))
                self.assertIn("agent.py page-snapshot", page.inner_text(".ck-board-stale"))
                hits = _Handler.board_hits
                page.evaluate("window.ConsoleKit.refreshBoard()")
                page.wait_for_timeout(300)
                self.assertEqual(_Handler.board_hits, hits)  # polling stopped

    # Catches: a stale bar that exists but cannot be seen. Polling stops when it
    # appears, so a covered bar is a stale board with no sign of it. The real
    # lane board has a sticky header at top: 0, z-index 100, which covered a
    # sticky bar once the page scrolled (PR #177 review). Checked scrolled, at
    # the overlay width and at both docked states, by what the browser would
    # actually hit at the bar's message (it carries no button with nothing staged).
    HIT = """() => { const b = document.querySelector('.ck-board-stale span'), r = b.getBoundingClientRect();
      const x = r.left + r.width / 2, y = r.top + r.height / 2;
      const bar = document.querySelector('.ck-board-stale'), s = bar.getBoundingClientRect(), my = s.top + s.height / 2;
      const ends = [s.left + 4, s.right - 4].map(px => bar.contains(document.elementFromPoint(px, my)));
      return { inView: r.top >= 0 && r.bottom <= innerHeight && r.left >= 0 && r.right <= innerWidth,
               hit: b.contains(document.elementFromPoint(x, y)), barEnds: ends, scrolled: scrollY }; }"""

    def test_the_stale_bar_is_visible_on_a_scrolled_board(self):
        other = {"shape": "0" * 16, "values": dict(BOARD_B["values"])}
        for kind in BROWSERS:
            for width, dock_open in ((800, False), (1400, False), (1400, True)):
                with self.subTest(browser=kind, width=width, dock_open=dock_open):
                    page = self._board_page(kind, "/board-a", other, width)
                    page.wait_for_selector(".ck-board-stale", timeout=5000)
                    if dock_open:
                        page.click(".ck-dock-strip")
                        page.wait_for_function("document.documentElement.classList.contains('ck-dock-open')")
                    # The lane board sets `scroll-behavior: smooth`, so a plain scrollTo animates:
                    # jump instead, then wait for the scroll itself, never a fixed time (PR #177 review).
                    page.evaluate("window.scrollTo({top: 2500, behavior: 'instant'})")
                    page.wait_for_function("scrollY > 1000", timeout=5000)
                    page.wait_for_timeout(250)  # the docked column's margin transition
                    got = page.evaluate(self.HIT)
                    self.assertGreater(got["scrolled"], 1000)  # the page really scrolled
                    self.assertTrue(got["inView"], got)
                    self.assertTrue(got["hit"], got)
                    self.assertEqual(got["barEnds"], [True, True], got)  # no end tucked under the strip or column

    def test_no_board_route_stops_polling(self):
        for kind in BROWSERS:
            with self.subTest(browser=kind):
                _Handler.board_hits = 0
                page = self._board_page(kind, "/board-a", None)
                self._settle(page, 1)
                hits = _Handler.board_hits
                page.evaluate("window.ConsoleKit.refreshBoard()")
                page.wait_for_timeout(300)
                self.assertEqual((hits, _Handler.board_hits), (1, 1))
                self.assertIsNone(page.query_selector(".ck-board-stale"))

    def test_a_transient_failure_keeps_polling(self):
        for kind in BROWSERS:
            with self.subTest(browser=kind):
                _Handler.board_hits, _Handler.board_status = 0, 503
                page = self._board_page(kind, "/board-a", BOARD_B)
                self._settle(page, 1)
                self.assertEqual(page.evaluate("window.__boardChanged"), 0)
                _Handler.board_status = 200
                page.evaluate("window.ConsoleKit.refreshBoard()")
                page.wait_for_function("window.__boardChanged > 0")

    def test_a_value_that_is_not_a_percentage_never_reaches_a_style(self):
        # "150" is valid CSS, so only console.js's own check stops it; "50%;..." the
        # browser's CSSOM would refuse anyway; "7" proves the path is live at all.
        get = "document.querySelector('[data-live-width=\"bar\"]').style.width"
        kept = BOARD_A["values"]["bar"]
        for kind in BROWSERS:
            for value, want in (("150", kept), ("50%;background:red", kept), ("7", "7")):
                with self.subTest(browser=kind, value=value):
                    bad = {"shape": BOARD_A["shape"], "values": {**BOARD_A["values"], "bar": value}}
                    page = self._board_page(kind, "/board-a", bad)
                    self._settle(page, 1)
                    self.assertEqual(page.evaluate(get), f"{want}%")

    def test_an_active_status_filter_follows_a_patched_status(self):
        flipped = sorted(i for i in REGISTER if FLIPPED[i]["status"] != REGISTER[i]["status"])[0]
        anchor = D.slugify(flipped)
        for kind in BROWSERS:
            with self.subTest(browser=kind):
                page = self._board_page(kind, "/board-a", BOARD_A)
                self._settle(page, 1)
                page.click('.status-chip[data-status="proposed"]')
                hidden = f"document.getElementById('{anchor}').classList.contains('hidden')"
                self.assertTrue(page.evaluate(hidden))
                _Handler.board = BOARD_B
                page.evaluate("window.ConsoleKit.refreshBoard()")
                page.wait_for_function("window.__boardChanged > 0")
                self.assertFalse(page.evaluate(hidden))

    def test_a_page_without_live_marks_never_asks(self):
        for kind in BROWSERS:
            with self.subTest(browser=kind):
                _Handler.board_hits = 0
                page = self._board_page(kind, "/", BOARD_A)
                page.wait_for_timeout(500)
                page.evaluate("window.ConsoleKit.refreshBoard()")  # the public hook asks nothing either
                page.wait_for_timeout(300)
                self.assertEqual(_Handler.board_hits, 0)


def _locked_view() -> dict:
    """/view with LANE.1/Q1 answered and locked, and LANE.1/Q2 still unanswered."""
    import tempfile
    from overture import view as V
    from overture.store import Store
    from test_kit import answer, lock, question
    items = {"LANE.1": {"title": "first lane", "parent": None}}
    with tempfile.TemporaryDirectory() as td:
        st = Store(Path(td) / "store.jsonl", known_items=items)
        st.append(question("LANE.1/Q1"))
        st.append(question("LANE.1/Q2"))
        st.append(lock(st.append(answer("LANE.1/Q1", own_text="B, for the small rig"))))
        return {"view": V.build(st, items, lambda c: True), "items": items, "cursor": {}}


class AnswerFollowUpTests(unittest.TestCase):
    """0.4.0 (owner, 2026-09-29): a "Follow up" button beside each locked answer.

    OVERTURE_SHOTS=<dir> also saves the open panel at desktop and phone width.
    """

    @classmethod
    def setUpClass(cls):
        DockTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        DockTests.tearDownClass.__func__(cls)

    def _open(self, kind: str, width: int):
        browser = getattr(self.pw, kind).launch()
        self.addCleanup(browser.close)
        page = browser.new_page(viewport={"width": width, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        self.addCleanup(lambda: self.assertEqual(errors, [], f"{kind}: page errors"))
        posted: list[dict] = []
        view = json.dumps(_locked_view())
        page.route("**/api/view*", lambda r: r.fulfill(status=200, content_type="application/json", body=view))

        def on_message(route):
            posted.append(route.request.post_data_json)
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"record": {}}))
        page.route("**/api/message", on_message)
        page.goto(self.url)
        page.click(".ck-item-btn")
        page.wait_for_selector(".ck-question")
        return page, posted

    def test_only_a_locked_answer_offers_it_and_it_sends_one_scoped_fork(self):
        for kind in BROWSERS:
            for width in (1280, 375):
                with self.subTest(browser=kind, width=width):
                    page, posted = self._open(kind, width)
                    _expand(page)
                    # 0.8.0: the follow-up is one of three next steps, behind "Next step ▾".
                    nxt = page.locator("button[aria-label^='Next step for']")
                    self.assertEqual(nxt.count(), 1)  # Q1 is locked; Q2 is unanswered and offers none
                    nxt.focus()
                    page.keyboard.press("Enter")
                    btns = page.locator("button[aria-label^='Follow up with other seats on']")
                    self.assertEqual(btns.count(), 1)
                    btns.focus()
                    page.keyboard.press("Enter")  # keyboard, not a mouse
                    self.assertEqual(btns.get_attribute("aria-expanded"), "true")
                    form = page.locator(".ck-followup")
                    self.assertTrue(form.locator("input[value='tighten']").is_checked())
                    # No seat picked: refused on the page, named visibly, nothing sent.
                    form.get_by_role("button", name="Send follow-up").click()
                    self.assertIn("Pick 1 to 3 seats", form.locator(".ck-error-msg").text_content())
                    self.assertEqual(posted, [])
                    for label in ("DevOps", "Security"):
                        form.get_by_label(label).focus()
                        page.keyboard.press("Space")
                    self.assertEqual(form.locator(".ck-error-msg").text_content(), "")  # a new pick clears it
                    form.get_by_label("Other seat (optional)").fill("Legal")
                    self.assertTrue(form.get_by_label("UX").is_disabled())  # three is the limit
                    form.get_by_label("Note for the seats (optional)").fill("Does B hold on tour?")
                    if os.environ.get("OVERTURE_SHOTS"):
                        card = page.locator(".ck-question", has=page.locator(".ck-followup"))
                        card.scroll_into_view_if_needed()
                        card.screenshot(path=str(Path(os.environ["OVERTURE_SHOTS"]) / f"followup-{kind}-{width}.png"))
                    self.assertFalse(page.evaluate("document.documentElement.scrollWidth > innerWidth"))
                    form.get_by_role("button", name="Send follow-up").click()
                    page.wait_for_function("document.querySelectorAll('.ck-followup').length === 0")
                    self.assertEqual(len(posted), 1)
                    body = dict(posted[0])
                    self.assertTrue(body.pop("nonce"))
                    self.assertEqual(body, {"item": "LANE.1", "intent": "fork", "mode": "tighten",
                                            "about_qid": "LANE.1/Q1", "roles": ["devops", "security", "other:Legal"],
                                            "text": "Does B hold on tour?"})



def _open_view(reply: str | None = None, lock_q3: bool = False) -> dict:
    """/view with LANE.1/Q1 locked, Q2 unanswered, and Q3 answered (not locked) with a deliberation waiting on it.

    `reply` adds an agent reply to that deliberation; `lock_q3` locks Q3 after it was asked."""
    import tempfile
    from overture import view as V
    from overture.store import Store
    from test_kit import answer, fork, lock, message, question
    items = {"LANE.1": {"title": "first lane", "parent": None}}
    with tempfile.TemporaryDirectory() as td:
        st = Store(Path(td) / "store.jsonl", known_items=items)
        for n in (1, 2, 3):
            st.append(question(f"LANE.1/Q{n}"))
        st.append(lock(st.append(answer("LANE.1/Q1", own_text="B, for the small rig"))))
        a3 = st.append(answer("LANE.1/Q3", picks=("a",)))
        f = st.append(fork(about_qid="LANE.1/Q3", roles=["analyst"], mode="explore"))
        if reply is not None:
            st.append(message(item="LANE.1", by="agent", text=reply, reply_to=f["id"]))
        if lock_q3:
            st.append(lock(a3))
        return {"view": V.build(st, items, lambda c: True), "items": items, "cursor": {}}


class DeliberateOpenQuestionTests(unittest.TestCase):
    """Owner ruling build_reply: "Deliberate before answering" on each OPEN question."""

    @classmethod
    def setUpClass(cls):
        DockTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        DockTests.tearDownClass.__func__(cls)

    def _open(self, kind: str, width: int, view: dict | None = None):
        browser = getattr(self.pw, kind).launch()
        self.addCleanup(browser.close)
        page = browser.new_page(viewport={"width": width, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        self.addCleanup(lambda: self.assertEqual(errors, [], f"{kind}: page errors"))
        posted: list[dict] = []
        view = json.dumps(view or _open_view())
        page.route("**/api/view*", lambda r: r.fulfill(status=200, content_type="application/json", body=view))

        def on_message(route):
            posted.append(route.request.post_data_json)
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"record": {}}))
        page.route("**/api/message", on_message)
        page.goto(self.url)
        page.click(".ck-item-btn")
        page.wait_for_selector(".ck-question")
        return page, posted

    def card(self, page, n):
        return page.locator(".ck-question", has_text=f"Which for LANE.1/Q{n}?")

    def test_only_open_questions_offer_it_and_the_cost_tracks_the_seats(self):
        # Catches: the button on a locked answer (which keeps "Follow up"), no button on an
        # open question (the pre-change page), a cost line that does not move with the seats,
        # and a request that is not one owner fork about that question.
        for kind in BROWSERS:
            for width in (1280, 375):
                with self.subTest(browser=kind, width=width):
                    page, posted = self._open(kind, width)
                    _expand(page)
                    sel = "button[aria-label^='Deliberate before answering:']"
                    self.assertEqual(page.locator(sel).count(), 2)
                    self.assertEqual(self.card(page, 1).locator(sel).count(), 0)
                    self.assertEqual(self.card(page, 1).locator("button[aria-label^='Next step for']").count(), 1)
                    # The waiting deliberation on Q3 is shown on its card.
                    asked = self.card(page, 3).locator(".ck-asked")
                    self.assertIn("Deliberation before answering with Analyst", asked.text_content())
                    self.assertIn("waiting for the seats", asked.text_content())
                    btn = self.card(page, 2).locator(sel)
                    btn.focus()
                    page.keyboard.press("Enter")
                    self.assertEqual(btn.get_attribute("aria-expanded"), "true")
                    form = page.locator(".ck-deliberate")
                    self.assertEqual(form.count(), 1)
                    self.assertTrue(form.locator("input[value='explore']").is_checked())
                    cost = form.locator(".ck-cost")
                    self.assertIn("100k tokens per seat", cost.text_content())
                    form.get_by_role("button", name="Send deliberation before answering").click()
                    self.assertIn("Pick 1 to 3 seats", form.locator(".ck-error-msg").text_content())
                    self.assertEqual(posted, [])
                    form.get_by_label("Analyst").focus()
                    page.keyboard.press("Space")
                    self.assertEqual(cost.text_content(), "Cost: ≈ 1 seat × ~100k tokens = ~100k tokens.")
                    form.get_by_label("Security").check()
                    form.get_by_label("Other seat (optional)").fill("Legal")
                    self.assertEqual(cost.text_content(), "Cost: ≈ 3 seats × ~100k tokens = ~300k tokens.")
                    form.get_by_label("Analyst").uncheck()
                    self.assertEqual(cost.text_content(), "Cost: ≈ 2 seats × ~100k tokens = ~200k tokens.")
                    form.get_by_label("Note for the seats (optional)").fill("Which survives a second site?")
                    self.assertFalse(page.evaluate("document.documentElement.scrollWidth > innerWidth"))
                    form.get_by_role("button", name="Send deliberation before answering").click()
                    page.wait_for_function("document.querySelectorAll('.ck-deliberate').length === 0")
                    self.assertEqual(len(posted), 1)
                    body = dict(posted[0])
                    self.assertTrue(body.pop("nonce"))
                    self.assertEqual(body, {"item": "LANE.1", "intent": "fork", "mode": "explore",
                                            "about_qid": "LANE.1/Q2", "roles": ["security", "other:Legal"],
                                            "text": "Which survives a second site?"})

    def test_an_early_reply_leaves_it_pending_and_only_the_result_returns_it(self):
        # Review round 1. Catches: a card that calls the deliberation back on any reply to it.
        for kind in BROWSERS:
            with self.subTest(browser=kind):
                page, _ = self._open(kind, 1280, _open_view(reply="Working on it: one seat running."))
                asked = self.card(page, 3).locator(".ck-asked")
                self.assertEqual(asked.get_attribute("data-done"), "false")
                self.assertIn("waiting for the seats", asked.text_content())
                page, _ = self._open(kind, 1280, _open_view(reply="Result: ★ a\nbecause it holds"))
                asked = self.card(page, 3).locator(".ck-asked")
                self.assertEqual(asked.get_attribute("data-done"), "true")
                self.assertIn("back: Result: ★ a", asked.text_content())
                self.assertNotIn("because", asked.text_content())

    def test_the_label_is_the_forks_own_kind_after_a_lock(self):
        # Review round 1, LOW. Catches: a deliberation before answering relabelled
        # "Follow-up" once the owner locks the question.
        for kind in BROWSERS:
            with self.subTest(browser=kind):
                page, _ = self._open(kind, 1280, _open_view(lock_q3=True))
                _expand(page)
                card = self.card(page, 3)
                self.assertEqual(card.locator("button[aria-label^='Deliberate before answering:']").count(), 0)
                text = card.locator(".ck-asked").text_content()
                self.assertIn("Deliberation before answering with Analyst", text)
                self.assertNotIn("Follow-up", text)

    def test_seats_and_mode_survive_a_rebuild_of_the_form(self):
        # Review round 1, LOW. Catches: a re-render (Refresh, a live update) that drops the
        # owner's seat ticks and mode while keeping only the note.
        for kind in BROWSERS:
            with self.subTest(browser=kind):
                page, _ = self._open(kind, 1280)
                btn = self.card(page, 2).locator("button[aria-label^='Deliberate before answering:']")
                btn.click()
                form = page.locator(".ck-deliberate")
                form.get_by_label("Analyst").check()
                form.get_by_label("Other seat (optional)").fill("Legal")
                form.locator("input[value='tighten']").check()
                btn.click()  # closed: the form is gone
                self.assertEqual(page.locator(".ck-deliberate").count(), 0)
                btn.click()  # rebuilt from scratch
                form = page.locator(".ck-deliberate")
                self.assertTrue(form.get_by_label("Analyst").is_checked())
                self.assertFalse(form.get_by_label("Security").is_checked())
                self.assertEqual(form.get_by_label("Other seat (optional)").input_value(), "Legal")
                self.assertTrue(form.locator("input[value='tighten']").is_checked())
                self.assertEqual(form.locator(".ck-cost").text_content(),
                                 "Cost: ≈ 2 seats × ~100k tokens = ~200k tokens.")


def _stale_view() -> tuple[dict, dict]:
    """/view and /check with LANE.1/Q1 locked on an excerpt whose text then changed, both built by the kit."""
    import tempfile
    from overture import anchors as A
    from overture import view as V
    from overture.store import Store
    from test_kit import answer, lock, question
    items = {"LANE.1": {"title": "first lane", "parent": None}}
    with tempfile.TemporaryDirectory() as td:
        spec = Path(td) / "spec.md"
        spec.write_text("Intro.\nThe console refuses a write from any other origin.\nTail.\n")
        st = Store(Path(td) / "store.jsonl", known_items=items)
        st.append(question("LANE.1/Q1", valid_if=[{"kind": "excerpt", "path": "spec.md",
                                                     "text": "The console refuses a write from any other origin."}]))
        st.append(lock(st.append(answer("LANE.1/Q1", own_text="B, for the small rig"))))
        spec.write_text("Intro.\nThe console refuses a write from a different origin.\nTail.\n")
        view = {"view": V.build(st, items, V.make_evaluator(Path(td), {})), "items": items, "cursor": {}}
        return view, {"stale": A.check(st, Path(td), {})}


NOGIT = "unavailable (no git in the server)"


def _seam_closed(test) -> None:
    """Close the git seam for one test, as `server.serve` does for the console's process (CONSOLE-kit/Q23)."""
    from unittest import mock
    from overture import gitseam as G
    p = mock.patch.object(G, "_OPEN", False)
    p.start()
    test.addCleanup(p.stop)


class WhyStaleTests(unittest.TestCase):
    """0.5.0: a stale answer says why, and the owner can re-lock it as it stands."""

    @classmethod
    def setUpClass(cls):
        DockTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        DockTests.tearDownClass.__func__(cls)

    def test_why_stale_shows_the_change_and_relock_sends_only_qid_and_nonce(self):
        view, check = _stale_view()
        self.assertEqual(view["view"]["questions"]["LANE.1/Q1"]["state"], "stale")
        for kind in BROWSERS:
            for width in (1280, 375):
                with self.subTest(browser=kind, width=width):
                    browser = getattr(self.pw, kind).launch()
                    self.addCleanup(browser.close)
                    page = browser.new_page(viewport={"width": width, "height": 900})
                    errors: list[str] = []
                    page.on("pageerror", lambda e: errors.append(str(e)))
                    posted: list[dict] = []
                    page.route("**/api/view*", lambda r: r.fulfill(status=200, content_type="application/json",
                                                                   body=json.dumps(view)))
                    page.route("**/api/check", lambda r: r.fulfill(status=200, content_type="application/json",
                                                                   body=json.dumps(check)))

                    def on_relock(route):
                        posted.append(route.request.post_data_json)
                        route.fulfill(status=200, content_type="application/json", body=json.dumps({"records": []}))
                    page.route("**/api/relock", on_relock)
                    page.goto(self.url)
                    page.click(".ck-item-btn")
                    page.wait_for_selector(".ck-stale-banner")
                    why = page.locator("button[aria-label^='Why is this stale']")
                    why.focus()
                    page.keyboard.press("Enter")
                    page.wait_for_selector(".ck-why-diff")
                    words = page.locator(".ck-why").text_content()
                    self.assertIn("no longer in spec.md", words)
                    diff = page.locator(".ck-why-diff").text_content()
                    self.assertIn("-The console refuses a write from any other origin.", diff)
                    self.assertIn("+The console refuses a write from a different origin.", diff)
                    self.assertFalse(page.evaluate("document.documentElement.scrollWidth > innerWidth"))
                    if os.environ.get("OVERTURE_SHOTS"):
                        page.locator(".ck-question").first.screenshot(
                            path=str(Path(os.environ["OVERTURE_SHOTS"]) / f"why-stale-{kind}-{width}.png"))
                    page.locator("button[aria-label^='Re-lock this answer as it stands']").click()
                    page.get_by_role("button", name="Re-lock as it stands").click()
                    page.wait_for_function("document.querySelectorAll('.ck-confirm').length === 0")
                    self.assertEqual(len(posted), 1)
                    body = dict(posted[0])
                    self.assertTrue(body.pop("nonce"))
                    self.assertEqual(body, {"qid": "LANE.1/Q1"})  # never anchors: the server computes them
                    self.assertEqual(errors, [], f"{kind}: page errors")

    def test_why_stale_says_git_history_is_unavailable_in_the_server(self):
        # CONSOLE-kit/Q23. Catches: a "why stale" that goes blank, or reads as "not in the recent
        # history", when the server simply never asked git.
        import hashlib
        import tempfile
        from overture import anchors as A
        from overture import view as V
        from overture.store import Store
        from test_kit import answer, lock, question
        _seam_closed(self)
        items = {"LANE.1": {"title": "first lane", "parent": None}}
        with tempfile.TemporaryDirectory() as td:
            spec = Path(td) / "spec.md"
            spec.write_text("Intro.\nThe cited claim.\nTail.\n")
            st = Store(Path(td) / "store.jsonl", known_items=items)
            st.append(question("LANE.1/Q1", valid_if=[{"kind": "file_sha256", "path": "spec.md",
                                                         "sha256": hashlib.sha256(spec.read_bytes()).hexdigest()}]))
            st.append(lock(st.append(answer("LANE.1/Q1"))))
            spec.write_text("A new first line.\nIntro.\nThe cited claim.\nTail.\n")
            view = {"view": V.build(st, items, V.make_evaluator(Path(td), {})), "items": items, "cursor": {}}
            check = {"stale": A.check(st, Path(td), {}), "history": NOGIT}
        for kind in BROWSERS:
            with self.subTest(browser=kind):
                browser = getattr(self.pw, kind).launch()
                self.addCleanup(browser.close)
                page = browser.new_page(viewport={"width": 375, "height": 900})
                errors: list[str] = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.route("**/api/view*", lambda r: r.fulfill(status=200, content_type="application/json",
                                                               body=json.dumps(view)))
                page.route("**/api/check", lambda r: r.fulfill(status=200, content_type="application/json",
                                                               body=json.dumps(check)))
                page.goto(self.url)
                page.click(".ck-item-btn")
                page.wait_for_selector(".ck-stale-banner")
                page.locator("button[aria-label^='Why is this stale']").click()
                page.wait_for_selector(".ck-why-list li")
                words = page.locator(".ck-why").text_content()
                self.assertIn(f"spec.md changed since this answer was locked. Git history is {NOGIT}", words)
                self.assertFalse(page.evaluate("document.documentElement.scrollWidth > innerWidth"))
                self.assertEqual(errors, [], f"{kind}: page errors")


# -- 0.7.0: a live console, against the REAL server --------------------------------------
#
# These run the kit's own server (Access gate, Origin check, store, doorbell) on
# loopback. The page cannot hold an Access token, so a Playwright route adds the
# header Cloudflare would, and the Origin the public hostname carries; nothing
# else about a request is touched. The agent writes through its real socket.

LIVE_ITEMS = {"LANE": {"title": "a lane", "parent": None, "status": "open"},
              "LANE.1": {"title": "first lane", "parent": "LANE", "status": "open"}}
LIVE_HOST = HOST.replace('<details id="item-LANE.1">', '<details id="item-LANE"><summary>LANE a lane</summary>'
                         '</details>\n<details id="item-LANE.1">')
OVERFLOW = "document.documentElement.scrollWidth > innerWidth"


class _LiveAdapter:
    def items(self):
        return dict(LIVE_ITEMS)

    def seed_questions(self):
        return []

    def record(self, entries, dry_run):
        return []


class LiveConsoleTests(unittest.TestCase):
    """0.7.0: new activity without reload, the Feed, the review-round form and the chat."""

    @classmethod
    def setUpClass(cls):
        if not REQUIRED:
            raise unittest.SkipTest("browser tests run only with OVERTURE_BROWSER=1")
        sp = _playwright()
        if sp is None:
            raise RuntimeError("OVERTURE_BROWSER=1 but Playwright is not installed")
        cls.pw = sp().start()

    @classmethod
    def tearDownClass(cls):
        cls.pw.stop()

    # -- the real server ------------------------------------------------------------

    def serve(self, snapshot=False, page=LIVE_HOST, publish=True):
        """The real Console and doors; `snapshot` gives it what `serve` gives it since Q24 (pushed items only).

        `page` is staged as the steward's page snapshot (Q28) and, with `publish`, published as the owner's
        "Use this page" would (Q29): only a published page is served.
        """
        import tempfile
        from overture import items as IT
        from overture import server as SV
        from test_server import AUD, HOSTNAME, KEY, TEAM, page_body, token
        td = tempfile.mkdtemp(prefix="ck-live-")
        self.addCleanup(lambda: __import__("shutil").rmtree(td, ignore_errors=True))
        root = Path(td)
        cfg = SV.Config(root=root, page=None, state=root / "state", adapter=root / "unused.py",
                        team_domain=TEAM, aud=AUD, hostname=HOSTNAME, port=0, project="live-test")
        console = SV.Console(cfg, IT.SnapshotAdapter(cfg.state) if snapshot else _LiveAdapter())
        staged = console.push_page_snapshot(page_body(page))   # Q28: the page is the steward's snapshot in STATE
        if publish:   # Q29: what the owner's "Use this page" does
            console.publish_page({"commit": staged["staged"]})
        verify = SV.access_verifier(TEAM, AUD, key_for=lambda _t: KEY.public_key())
        owner = SV.owner_server(console, verify, 0)
        threading.Thread(target=owner.serve_forever, daemon=True).start()
        agent = SV.agent_server(console)
        threading.Thread(target=agent.serve_forever, daemon=True).start()
        self.addCleanup(owner.server_close)
        self.addCleanup(owner.shutdown)
        self.addCleanup(agent.server_close)
        self.addCleanup(agent.shutdown)
        self.tok, self.origin = token(), f"https://{HOSTNAME}"
        self.SV, self.console, self.cfg = SV, console, cfg
        return f"http://127.0.0.1:{owner.server_address[1]}/"

    def agent_post(self, path, body):
        code, out = self.SV.agent_request(self.cfg.socket, "POST", path, body)
        self.assertEqual(code, 200, out)
        return out["record"]

    def ask(self, n, item="LANE.1", **over):
        body = {"qid": f"{item}/Q{n}", "item": item, "text": f"Question {n}: which way?", "kind": "single",
                "options": [{"id": "fix", "label": "Fix now"}, {"id": "record", "label": "Record in findings.md"},
                            {"id": "leave", "label": "Leave it"}],
                "star": "fix", "source": "docs/spec.md:1", "valid_if": [], "nonce": f"liveq{n:04d}" + item.replace(".", "_")}
        body.update(over)
        return self.agent_post("/question", body)

    def bell(self):
        p = self.cfg.inbox
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def page(self, kind, width, url, reduced=False, block_live=False):
        browser = getattr(self.pw, kind).launch()
        self.addCleanup(browser.close)
        ctx = browser.new_context(viewport={"width": width, "height": 900},
                                  reduced_motion="reduce" if reduced else "no-preference")
        page = ctx.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        self.addCleanup(lambda: self.assertEqual(errors, [], f"{kind}: page errors"))
        self.waits: list[float] = []

        def through_access(route):
            if "/api/wait" in route.request.url:
                self.waits.append(time.monotonic())
            h = {**route.request.headers, "cf-access-jwt-assertion": self.tok}
            if route.request.method == "POST":
                # A browser will not let a page's route rewrite Origin, so a write is
                # re-sent from Playwright with the Origin the public hostname carries.
                h["origin"] = self.origin
                route.fulfill(response=route.fetch(headers=h))
                return
            route.continue_(headers=h)
        page.route("**/*", through_access)
        if block_live:  # routes run newest first: every long poll fails, so the page never updates itself
            page.route("**/api/wait*", lambda route: route.abort())
        page.goto(url)
        page.wait_for_function("document.querySelector('.ck-inbox-count').textContent !== '?'")
        page.evaluate("window.__notReloaded = true")
        return page

    def open_inbox(self, page, width):
        page.click(".ck-dock-strip" if width >= 1024 else ".ck-inbox-btn")
        page.wait_for_selector(".ck-tabs")

    def assert_not_reloaded(self, page):
        self.assertTrue(page.evaluate("window.__notReloaded === true"), "the page reloaded")

    # -- tests -------------------------------------------------------------------------

    def test_before_the_first_items_push_the_inbox_says_why_the_board_is_empty(self):
        # Q24. Catches: a silent blank before the steward's first push (the server runs no adapter, so it has
        # no items until then), the note never rendered, and a note that stays after the push arrives.
        from overture import items as IT
        for kind in BROWSERS:
            with self.subTest(browser=kind):
                url = self.serve(snapshot=True)
                page = self.page(kind, 1280, url)
                self.open_inbox(page, 1280)
                note = page.wait_for_selector(".ck-items-note")
                self.assertEqual(note.text_content(), IT.NOT_PUSHED)
                self.assertEqual(note.get_attribute("role"), "status")
                code, out = self.SV.agent_request(self.cfg.socket, "POST", "/items",
                                                  {"items": dict(LIVE_ITEMS), "seed_questions": [], "board": None})
                self.assertEqual(code, 200, out)
                page.wait_for_function("document.querySelector('.ck-items-note') === null", timeout=10000)
                self.assert_not_reloaded(page)

    def test_the_prs_tab_says_why_it_is_empty_then_renders_hostile_titles_as_text(self):
        # Catches: a blank list (or invented PRs) before the steward's first prs-push; a title or branch put in
        # with innerHTML (the img's onerror would run and mark the body); a link that is not the PR's GitHub URL,
        # or one that opens without noopener; a merged PR above an open one; a question the title names that is
        # not linked to its item; and a PRs view that never re-reads after a live push.
        from overture import prs as PR
        hostile = "<img src=x onerror=\"document.body.setAttribute('data-ran','pr')\"> fixes LANE.1/Q1"
        pr = lambda n, **o: {**{"number": n, "title": f"PR {n}", "state": "open", "draft": False,
                                "head": f"lane-{n}", "base": "main", "author": "octo",
                                "created_at": "2026-09-30T10:00:00Z", "updated_at": "2026-09-30T11:00:00Z",
                                "merged_at": None, "closed_at": None,
                                "url": f"https://github.com/octo/repo/pull/{n}", "merge_commit": None,
                                "checks": "success"}, **o}
        push = {"repo": "octo/repo", "window_days": 30, "prs": [
            pr(4, title=hostile, head="<b>x</b>", draft=True, checks="failure"),
            pr(3, title="constructor toString __proto__ hasOwnProperty", head="valueOf", state="merged",
               merged_at="2026-09-29T08:00:00Z", closed_at="2026-09-29T08:00:00Z", merge_commit="a" * 40)]}
        for kind in BROWSERS:
            for width in (1280, 375):
                with self.subTest(browser=kind, width=width):
                    url = self.serve()
                    self.ask(1)
                    page = self.page(kind, width, url)
                    self.open_inbox(page, width)
                    page.click("#ck-tab-prs")
                    note = page.wait_for_selector(".ck-prs-note")
                    self.assertEqual((note.text_content(), note.get_attribute("role")), (PR.NOT_PUSHED, "status"))
                    self.assertEqual(page.locator(".ck-pr-row").count(), 0)
                    code, out = self.SV.agent_request(self.cfg.socket, "POST", "/prs", push, agent="agent-5")
                    self.assertEqual(code, 200, out)
                    page.wait_for_selector(".ck-pr-row", timeout=10000)   # the live wake re-read it
                    self.assertEqual(page.eval_on_selector_all(".ck-pr-row", "rs => rs.map(r => r.dataset.number)"),
                                     ["4", "3"])
                    first = page.locator(".ck-pr-row[data-number='4']")
                    self.assertEqual(first.locator(".ck-pr-title").text_content(), hostile)
                    self.assertIn("<b>x</b> → main", first.locator(".ck-pr-branch").text_content())
                    self.assertEqual(page.locator(".ck-prs img, .ck-prs b").count(), 0)
                    self.assertIsNone(page.evaluate("document.body.getAttribute('data-ran')"))
                    link = first.locator("a.ck-pr-link")
                    self.assertEqual((link.get_attribute("href"), link.get_attribute("target"),
                                      link.get_attribute("rel")),
                                     ("https://github.com/octo/repo/pull/4", "_blank", "noopener noreferrer"))
                    self.assertIn("draft", first.text_content())
                    self.assertIn("checks fail", first.text_content())
                    self.assertIn("merged", page.locator(".ck-pr-row[data-number='3']").text_content())
                    # Object.prototype names are not items: no Open button for them.
                    self.assertEqual(page.locator(".ck-pr-row[data-number='3'] button").count(), 0)
                    self.assertIn("by the steward (agent-5)", page.locator(".ck-prs-pushed").text_content())
                    self.assertFalse(page.evaluate(OVERFLOW))
                    first.locator("button", has_text="Open LANE.1/Q1").click()
                    page.wait_for_selector(".ck-back-btn")   # the item the question belongs to, opened
                    self.assert_not_reloaded(page)

    def test_the_snapshot_pages_own_script_runs_beside_the_console(self):
        # Q28 point 4. Catches: a CSP or an injection change that stops the page's own inline script (the dashboard
        # breaks) or the console's (the owner loses the console), and the page's provenance line not shown.
        html = LIVE_HOST.replace("</body>", '<script>document.body.dataset.pageScript = "ran";</script></body>')
        for kind in BROWSERS:
            with self.subTest(browser=kind):
                url = self.serve(page=html)
                page = self.page(kind, 1280, url)
                page.wait_for_function("document.body.dataset.pageScript === 'ran'", timeout=10000)
                self.open_inbox(page, 1280)   # the console's own script built its dock and inbox
                self.assertIn("(staged by the steward, published by you)",
                              page.locator(".ck-page-source").text_content())

    def test_a_staged_pages_script_never_runs_until_the_owner_publishes_it(self):
        # Q29 point 3, the browser proof. The staged page's script tries to mark its own frame and the console's
        # page. Catches: the preview frame given allow-scripts or allow-same-origin, the preview route served under
        # a CSP that runs script, the staged page served before the click, and a button that does not publish.
        html = LIVE_HOST.replace("</body>", '<p id="staged-mark">STAGED-PAGE</p><script>'
                                 'document.body.dataset.pageScript = "ran";'
                                 'try { top.document.body.dataset.leaked = "yes"; } catch (e) {}</script></body>')
        for kind in BROWSERS:
            with self.subTest(browser=kind):
                url = self.serve(page=html, publish=False)
                page = self.page(kind, 1280, url)
                self.assertEqual(page.locator(".ck-page-proposed-line").count(), 1)
                self.assertEqual(page.locator("#staged-mark").count(), 0)   # proposed, not served
                self.open_page_dialog(page)
                frame_el = page.wait_for_selector("dialog.ck-page-dialog[open] .ck-page-preview iframe")
                self.assertEqual(frame_el.get_attribute("sandbox"), "")
                frame = frame_el.content_frame()
                frame.wait_for_selector("#staged-mark")   # the preview rendered the staged markup
                page.wait_for_timeout(500)   # time for a script that was going to run
                self.assertTrue(frame.evaluate("document.body.dataset.pageScript === undefined"),
                                "the staged page's script ran in the preview")
                self.assertTrue(page.evaluate("document.body.dataset.leaked === undefined"),
                                "the staged page's script reached the console's page")
                self.assertTrue(page.evaluate("document.body.dataset.pageScript === undefined"))
                with page.expect_navigation():
                    page.click("dialog.ck-page-dialog[open] .ck-page-use")   # the dialog's own button publishes
                page.wait_for_function("document.body.dataset.pageScript === 'ran'", timeout=10000)
                self.assertEqual(page.locator("#staged-mark").count(), 1)   # published: served, its script runs
                self.assertEqual(page.locator(".ck-page-proposed-line").count(), 0)
                self.assertEqual(page.locator(".ck-page-waiting, dialog.ck-page-dialog").count(), 0)   # nothing left
                self.assertIn("published by you", page.locator(".ck-page-source").text_content())

    def test_a_refused_publish_names_why_and_publishes_nothing(self):
        # Q29 point 2, the button's other half. Catches: a refusal swallowed (the owner thinks it published), the
        # page reloaded on a refusal, and a stale button publishing a newer staging it never showed.
        from test_server import page_body
        for kind in BROWSERS:
            with self.subTest(browser=kind):
                url = self.serve(publish=False)
                page = self.page(kind, 1280, url)
                newer = page_body(LIVE_HOST.replace("</body>", "<p>NEWER</p></body>"), commit="beef" + "1" * 36)
                self.console.push_page_snapshot(newer)   # staged after the page loaded
                self.open_page_dialog(page)
                page.click("dialog.ck-page-dialog[open] .ck-page-use")
                err = page.wait_for_selector("dialog.ck-page-dialog[open] .ck-page-use-error:not([hidden])")
                self.assertIn("Not published: the staged page is now beef11111111", err.text_content())
                self.assert_not_reloaded(page)
                self.assertFalse((self.cfg.state / "page-snapshot.json").exists())

    # -- the review dialog (owner, 2026-10-02: "can you create a modal rather than place on bottom (button hides
    # under header)"). A staged page is reviewed in a modal <dialog>, opened only by a button the owner presses.

    # A host page like the real lane board: a sticky header at top: 0, z-index 100, over a page that scrolls.
    HEADER_HOST = LIVE_HOST.replace(
        "<body>\n", '<body>\n<header id="host-header" style="position:sticky;top:0;z-index:100;height:72px;'
        'background:#ddd">Host header</header>\n', 1).replace("</body>", '<div style="height:3000px"></div></body>')
    CHIP_HIT = """() => { const c = document.querySelector('.ck-page-waiting'), r = c.getBoundingClientRect();
      const x = r.left + r.width / 2, y = r.top + r.height / 2, at = document.elementFromPoint(x, y);
      return { visible: r.width > 0 && r.height > 0 && getComputedStyle(c).visibility === 'visible',
               inView: r.top >= 0 && r.bottom <= innerHeight && r.left >= 0 && r.right <= innerWidth,
               hit: c.contains(at), at: at ? at.tagName + '#' + at.id + '.' + at.className : null,
               tag: c.tagName, scrolled: scrollY }; }"""
    IN_DIALOG = ("(() => { const d = document.querySelector('dialog.ck-page-dialog');"
                 " return !!d && d.contains(document.activeElement); })()")

    def open_page_dialog(self, page):
        page.click(".ck-page-waiting")
        page.wait_for_selector("dialog.ck-page-dialog[open]")

    def staged_page(self, kind, width, host=LIVE_HOST):
        """`host` published and served, and a newer page staged over it, waiting for the owner."""
        from test_server import page_body
        url = self.serve(page=host)
        self.console.push_page_snapshot(page_body(host.replace("</body>", "<p>NEWER</p></body>"),
                                                  commit="beef" + "2" * 36))
        self.published = (self.cfg.state / "page-snapshot.json").read_bytes()
        return self.page(kind, width, url)

    def test_a_staged_page_shows_a_waiting_button_no_header_covers_and_it_opens_a_modal(self):
        # Catches: the proposal left where a sticky header or the page's length hides it (the owner's report), an
        # indicator that is not a button, one covered at either width or scroll position, and a "dialog" that is
        # not modal (no focus trap, no Esc, no backdrop) or that leaves focus outside it.
        for kind in BROWSERS:
            for width in (1280, 375):
                with self.subTest(browser=kind, width=width):
                    page = self.staged_page(kind, width, self.HEADER_HOST)
                    for top in (0, 1500, 99999):
                        page.evaluate(f"window.scrollTo({{top: {top}, behavior: 'instant'}})")
                        page.wait_for_timeout(100)
                        got = page.evaluate(self.CHIP_HIT)
                        self.assertEqual(got["tag"], "BUTTON", got)
                        self.assertTrue(got["visible"] and got["inView"] and got["hit"], got)
                    self.assertGreater(got["scrolled"], 1000)   # the page really scrolled
                    chip = page.locator(".ck-page-waiting")
                    self.assertIn("New dashboard page waiting", chip.text_content())
                    self.assertEqual(chip.get_attribute("aria-haspopup"), "dialog")
                    self.open_page_dialog(page)
                    self.assertEqual(page.locator("dialog[open]").count(), 1)
                    self.assertTrue(page.evaluate("document.querySelector('dialog.ck-page-dialog').matches(':modal')"))
                    self.assertTrue(page.evaluate(self.IN_DIALOG))
                    dlg = page.locator("dialog.ck-page-dialog")
                    name_id = dlg.get_attribute("aria-labelledby")
                    self.assertEqual(page.locator(f"#{name_id}").text_content(), "New dashboard page")
                    self.assertIn("Proposed dashboard:", dlg.locator(".ck-page-proposed-line").text_content())
                    for b in (".ck-page-use", ".ck-page-not-now"):
                        self.assertTrue(dlg.locator(b).is_visible(), b)
                    self.assertEqual(dlg.locator("iframe").get_attribute("sandbox"), "")
                    self.assertFalse(page.evaluate(OVERFLOW))
                    self.assert_not_reloaded(page)

    def test_esc_and_not_now_close_the_dialog_publish_nothing_and_return_focus(self):
        # Catches: closing the dialog publishing (or reloading), focus dropped on <body> after close, and an Esc
        # that also closes the inbox behind it.
        for kind in BROWSERS:
            with self.subTest(browser=kind):
                page = self.staged_page(kind, 1280)
                for close in ("Escape", "Not now"):
                    self.open_page_dialog(page)
                    if close == "Escape":
                        page.keyboard.press("Escape")
                    else:
                        page.click("dialog.ck-page-dialog[open] .ck-page-not-now")
                    page.wait_for_function("!document.querySelector('dialog.ck-page-dialog').open")
                    self.assertTrue(page.evaluate("document.activeElement.classList.contains('ck-page-waiting')"),
                                    close)
                    self.assertEqual((self.cfg.state / "page-snapshot.json").read_bytes(), self.published,
                                     close)   # nothing published
                    self.assertTrue((self.cfg.state / "page-staged.json").exists(), close)      # still offered
                    self.assert_not_reloaded(page)
                self.open_inbox(page, 1280)
                self.open_page_dialog(page)
                page.keyboard.press("Escape")
                page.wait_for_function("!document.querySelector('dialog.ck-page-dialog').open")
                self.assertEqual(page.locator(".ck-panel").get_attribute("data-open"), "true")   # inbox untouched

    def test_the_review_dialog_never_opens_on_load(self):
        # Catches: a modal that steals focus the moment the page loads.
        for kind in BROWSERS:
            with self.subTest(browser=kind):
                page = self.staged_page(kind, 1280)
                page.wait_for_timeout(500)
                self.assertEqual(page.locator("dialog.ck-page-dialog").count(), 1)
                self.assertEqual(page.locator("dialog[open]").count(), 0)
                self.assertFalse(page.evaluate(self.IN_DIALOG))
                self.assertFalse(page.locator(".ck-page-proposed-line").is_visible())   # held until asked for
                self.assertTrue(page.locator(".ck-page-waiting").is_visible())

    def test_without_the_console_script_the_proposal_is_still_shown(self):
        # Catches: a proposal reachable only through JavaScript (a closed <dialog> is display: none).
        browser = getattr(self.pw, BROWSERS[0]).launch()
        self.addCleanup(browser.close)
        url = self.serve(publish=False)
        ctx = browser.new_context(viewport={"width": 1280, "height": 900}, java_script_enabled=False)
        page = ctx.new_page()
        page.route("**/*", lambda route: route.continue_(
            headers={**route.request.headers, "cf-access-jwt-assertion": self.tok}))
        page.goto(url)
        self.assertTrue(page.locator(".ck-page-proposed-line").is_visible())
        self.assertTrue(page.locator(".ck-page-use").is_visible())
        self.assertTrue(page.locator(".ck-page-preview iframe").is_visible())
        self.assertEqual(page.locator("dialog[open]").count(), 0)
        self.assertFalse(page.locator(".ck-page-waiting").is_visible())   # it would do nothing without the script
        self.assertFalse(page.locator(".ck-page-not-now").is_visible())

    def test_a_new_question_appears_without_reload_and_the_chip_counts_it(self):
        # Catches: a page that only refreshes on open or on Refresh (the owner sees nothing
        # new until they click); an unread chip that counts the owner's own writes; one that
        # never clears once looked at; and an update done by reloading the page.
        for kind in BROWSERS:
            for width in (1280, 375):
                with self.subTest(browser=kind, width=width):
                    url = self.serve()
                    self.ask(1)
                    page = self.page(kind, width, url)
                    chip = ".ck-dock-strip .ck-new-count" if width >= 1024 else ".ck-inbox-btn .ck-new-count"
                    self.assertEqual(page.locator(chip).text_content(), "")  # a first visit starts at nothing new
                    self.console.write("message", {"item": "LANE.1", "text": "owner note", "nonce": "ownernote01"},
                                       "owner")
                    page.wait_for_timeout(1500)
                    self.assertEqual(page.locator(chip).text_content(), "")  # the owner's own write is not "new"
                    self.ask(2)
                    page.wait_for_function(f"document.querySelector('{chip}').textContent === '1 new'", timeout=10000)
                    page.wait_for_function("document.querySelector('.ck-inbox-count').textContent === '2'")
                    label = page.locator(".ck-dock-strip" if width >= 1024 else ".ck-inbox-btn").get_attribute("aria-label")
                    self.assertIn("1 new since you last looked", label)
                    self.open_inbox(page, width)
                    page.wait_for_selector(".ck-inbox-item[data-qid='LANE.1/Q2']")
                    page.wait_for_function(f"document.querySelector('{chip}').textContent === ''")
                    # With the inbox open, the next question is drawn in place, and marked as arriving.
                    self.ask(3)
                    page.wait_for_selector(".ck-inbox-item[data-qid='LANE.1/Q3'].ck-arrived", timeout=10000)
                    self.assertFalse(page.evaluate(OVERFLOW))
                    self.assert_not_reloaded(page)

    def test_the_feed_shows_events_as_they_happen_and_filters_them(self):
        # Catches: a Feed fetched once and never again, one that loses its filter on a live
        # update, and one built from the view (which has no lock records).
        for kind in BROWSERS:
            for width in (1280, 375):
                with self.subTest(browser=kind, width=width):
                    url = self.serve()
                    self.ask(1)
                    page = self.page(kind, width, url)
                    self.open_inbox(page, width)
                    page.click("#ck-tab-feed")
                    page.wait_for_selector(".ck-feed-row[data-kind='question']")
                    self.ask(2)
                    page.wait_for_function(
                        "(() => { const r = document.querySelector('.ck-feed-row');"
                        " return r && r.textContent.includes('LANE.1/Q2') && r.dataset.kind === 'question'; })()",
                        timeout=10000)
                    self.assertEqual(page.locator(".ck-feed-row.ck-feed-new").count(), 1)  # Q2 arrived after opening
                    a = self.console.write("answer", {"qid": "LANE.1/Q1", "picks": ["fix"], "own_text": "",
                                                      "nonce": "feedanswer1"}, "owner")
                    self.console.write("lock", {"qid": "LANE.1/Q1", "answer": a["id"], "nonce": "feedlock001"}, "owner")
                    page.select_option("#ck-feed-kind", "lock")
                    page.wait_for_function("document.querySelectorAll('.ck-feed-row').length === 1"
                                           " && document.querySelector('.ck-feed-row').dataset.kind === 'lock'",
                                           timeout=10000)
                    self.ask(3)  # a live update keeps the filter: still only locks
                    page.wait_for_timeout(1500)
                    self.assertEqual(page.eval_on_selector_all(".ck-feed-row", "rs => rs.map(r => r.dataset.kind)"),
                                     ["lock"])
                    self.assertIn("not listed here", page.locator(".ck-feed-foot").text_content())
                    self.assertFalse(page.evaluate(OVERFLOW))
                    self.assert_not_reloaded(page)

    def test_a_round_is_one_form_drafts_survive_and_lock_all_rings_once(self):
        # Catches: a form that locks as you pick (nothing may lock before the review page);
        # drafts lost on moving between questions, leaving the form or reloading; a review
        # page that shows stale picks after a change; a Lock all that locks the questions
        # left, or rings "process" once per question; and ←/→ or 1-9 keys that do nothing.
        for kind in BROWSERS:
            for width in (1280, 375):
                with self.subTest(browser=kind, width=width):
                    url = self.serve()
                    spec = self.cfg.root / "docs" / "spec.md"
                    spec.parent.mkdir(parents=True)
                    spec.write_text("intro\nThe bundle cap is 64 KiB.\nend\n")
                    f = self.console.write("message", {"item": "LANE.1", "text": "tighten this", "intent": "fork",
                                                       "mode": "tighten", "nonce": "roundfork01"}, "owner")
                    for n in (1, 2, 3):
                        extra = {"evidence": [{"cite": "docs/spec.md:2", "command": "grep -n cap docs/spec.md",
                                               "result": "2:The bundle cap is 64 KiB."}]} if n == 1 else {}
                        self.ask(n, forked_from=f["id"], star_by="panel", **extra)
                    spec.write_text("intro\nThe bundle cap is 32 KiB.\nend\n")  # changed since asked
                    page = self.page(kind, width, url)
                    self.open_inbox(page, width)
                    page.click(".ck-round-card button")
                    page.wait_for_selector(".ck-round-step")
                    # Q1: evidence, read now, says the cited line changed.
                    page.wait_for_selector(".ck-evidence-state[data-state='changed']")
                    self.assertIn("32 KiB", page.locator(".ck-evidence-row .ck-evidence-lines").text_content())
                    if os.environ.get("OVERTURE_SHOTS"):
                        page.locator(".ck-panel").screenshot(
                            path=str(Path(os.environ["OVERTURE_SHOTS"]) / f"round-step-{kind}-{width}.png"))
                    page.keyboard.press("2")  # picks "Record in findings.md"
                    self.assertTrue(page.locator(".ck-round-options input[value='record']").is_checked())
                    page.fill("#ck-round-words", "Record it; the cap moves next quarter.")
                    page.locator(".ck-round-options input[value='record']").focus()
                    page.keyboard.press("ArrowRight")
                    page.wait_for_function("document.querySelector('.ck-round-count').textContent.startsWith('Question 2 of 3')")
                    page.keyboard.press("1")
                    page.keyboard.press("ArrowLeft")  # back: the draft is still there
                    page.wait_for_function("document.querySelector('.ck-round-count').textContent.startsWith('Question 1 of 3')")
                    self.assertTrue(page.locator(".ck-round-options input[value='record']").is_checked())
                    self.assertEqual(page.input_value("#ck-round-words"), "Record it; the cap moves next quarter.")
                    self.assertEqual(self.console.store.head("LANE.1/Q1"), None)  # nothing written by a pick
                    # Leave the form, come back, and reload: the drafts survive all three.
                    page.click(".ck-panel .ck-back-btn" if width < 400 else ".ck-panel .ck-sheet-toggle")
                    page.evaluate("ConsoleKit.openRound(%s)" % json.dumps(f["id"]))
                    page.wait_for_selector(".ck-round-step")
                    self.assertTrue(page.locator(".ck-round-options input[value='record']").is_checked())
                    page.reload()
                    page.wait_for_function("document.querySelector('.ck-inbox-count').textContent !== '?'")
                    page.evaluate("ConsoleKit.openRound(%s)" % json.dumps(f["id"]))
                    page.wait_for_selector(".ck-round-step")
                    self.assertTrue(page.locator(".ck-round-options input[value='record']").is_checked())
                    page.get_by_role("button", name="Review all").click()
                    page.wait_for_selector(".ck-review-heading")
                    rows = page.eval_on_selector_all(".ck-review-row", "rs => rs.map(r => r.dataset.status)")
                    self.assertEqual(rows, ["picked", "picked", "left"])
                    # Going back to change a pick before committing works.
                    page.get_by_role("button", name="Change LANE.1/Q2").click()
                    page.wait_for_selector(".ck-round-step")
                    page.keyboard.press("3")
                    page.get_by_role("button", name="Review all").click()
                    self.assertIn("Leave it", page.locator(".ck-review-row").nth(1).text_content())
                    if os.environ.get("OVERTURE_SHOTS"):
                        page.locator(".ck-panel").screenshot(
                            path=str(Path(os.environ["OVERTURE_SHOTS"]) / f"round-review-{kind}-{width}.png"))
                    self.assertFalse(page.evaluate(OVERFLOW))
                    self.assertEqual([b for b in self.bell() if b.get("intent") == "process"], [])
                    page.click(".ck-lock-all")
                    page.wait_for_selector(".ck-result-heading")
                    st = self.console.store
                    self.assertEqual((st.head("LANE.1/Q1")["picks"], st.head("LANE.1/Q1")["own_text"]),
                                     (["record"], "Record it; the cap moves next quarter."))
                    self.assertEqual(st.head("LANE.1/Q2")["picks"], ["leave"])
                    self.assertIsNotNone(st.lock_of(st.head("LANE.1/Q1")["id"]))
                    self.assertIsNotNone(st.lock_of(st.head("LANE.1/Q2")["id"]))
                    self.assertIsNone(st.head("LANE.1/Q3"))  # left: still unanswered
                    self.assertEqual(len([b for b in self.bell() if b.get("intent") == "process"]), 1)
                    self.assertIsNone(page.evaluate("localStorage.getItem('ck:live-test:round:%s')" % f["id"]))

    def test_a_refused_lock_all_locks_nothing_and_keeps_the_drafts(self):
        # Catches: a form that locks the good half of a refused batch; one that clears the
        # owner's drafts (and comments) on a refusal; one that reports success when the
        # server locked nothing; and a refusal that does not say which question and why.
        for kind in BROWSERS:
            with self.subTest(browser=kind):
                url = self.serve()
                f = self.console.write("message", {"item": "LANE.1", "text": "x", "intent": "fork", "mode": "explore",
                                                   "nonce": "refusefork1"}, "owner")
                self.ask(1, forked_from=f["id"], star_by="panel")
                self.ask(2, forked_from=f["id"], star_by="panel")
                # No live updates in this tab, so it still believes Q1 is open when it presses.
                page = self.page(kind, 1280, url, block_live=True)
                page.evaluate("ConsoleKit.openRound(%s)" % json.dumps(f["id"]))
                page.wait_for_selector(".ck-round-step")
                page.keyboard.press("1")
                page.fill("#ck-round-words", "my reason for Q1")
                page.locator(".ck-round-options input[value='fix']").focus()
                page.keyboard.press("ArrowRight")
                page.wait_for_function("document.querySelector('.ck-round-count').textContent.startsWith('Question 2 of 2')")
                page.keyboard.press("2")
                page.get_by_role("button", name="Review all").click()
                # Meanwhile, another tab locks Q1 with a different answer.
                a = self.console.write("answer", {"qid": "LANE.1/Q1", "picks": ["leave"], "own_text": "",
                                                  "nonce": "othertab01"}, "owner")
                self.console.write("lock", {"qid": "LANE.1/Q1", "answer": a["id"], "nonce": "othertab02"}, "owner")
                seq = self.console.store.seq()
                page.click(".ck-lock-all")
                page.wait_for_selector(".ck-review-heading ~ [role='alert']")
                self.assertIn("Nothing was locked", page.locator("[role='alert']").text_content())
                self.assertIn("supersede", page.locator(".ck-review-row").nth(0).text_content())
                self.assertEqual(self.console.store.seq(), seq)  # not Q2 either
                self.assertIsNone(self.console.store.head("LANE.1/Q2"))
                saved = json.loads(page.evaluate("localStorage.getItem('ck:live-test:round:%s')" % f["id"]))
                self.assertEqual(saved["drafts"]["LANE.1/Q1"]["text"], "my reason for Q1")  # kept
                self.assertEqual(saved["drafts"]["LANE.1/Q2"]["picks"], ["record"])
                # Brought up to date, the form shows Q1 as locked, and Lock all locks the rest.
                page.evaluate("ConsoleKit.refresh()")
                page.wait_for_function("document.querySelector('.ck-review-row').dataset.status === 'locked'")
                page.click(".ck-lock-all")
                page.wait_for_selector(".ck-result-heading")
                self.assertEqual(self.console.store.head("LANE.1/Q2")["picks"], ["record"])
                self.assertEqual(self.console.store.head("LANE.1/Q1")["picks"], ["leave"])  # never superseded here
                self.assertEqual(len([b for b in self.bell() if b.get("intent") == "process"]), 1)

    def test_a_chat_message_wakes_the_watch_and_the_reply_appears(self):
        # Catches: a chat that is stored but rings nothing (no session would wake), and an
        # agent reply that only shows after a reload.
        from overture import doorbell as D
        for kind in BROWSERS:
            for width in (1280, 375):
                with self.subTest(browser=kind, width=width):
                    url = self.serve()
                    page = self.page(kind, width, url)
                    self.open_inbox(page, width)
                    page.click("#ck-tab-chat")
                    page.fill("#ck-chat-input", "Is the project build green?")
                    page.keyboard.press("Enter")
                    page.wait_for_selector(".ck-chat-msg[data-by='owner']")
                    woke = D.watch(self.cfg.inbox, 0, poll=0.05, timeout=5)
                    self.assertEqual([(w["intent"], w["item"]) for w in woke], [("chat", "@chat")])
                    msg = [r for r in self.console.store.records() if r["type"] == "message"][-1]
                    self.assertEqual((msg["by"], msg["intent"], msg["text"]),
                                     ("owner", "chat", "Is the project build green?"))
                    self.agent_post("/message", {"item": "@chat", "text": "Green at abc123.", "reply_to": msg["id"],
                                                 "nonce": "chatreply01"})
                    page.wait_for_selector(".ck-chat-msg[data-by='agent']", timeout=10000)
                    self.assertIn("Green at abc123.", page.locator(".ck-chat-msg[data-by='agent']").text_content())
                    page.wait_for_function("!document.querySelector('#ck-tab-chat .ck-tab-note')")  # no longer waiting
                    self.assertFalse(page.evaluate(OVERFLOW))
                    if os.environ.get("OVERTURE_SHOTS"):
                        page.locator(".ck-panel").screenshot(
                            path=str(Path(os.environ["OVERTURE_SHOTS"]) / f"chat-{kind}-{width}.png"))
                    self.assert_not_reloaded(page)

    def test_a_named_agent_is_shown_by_name_and_an_unnamed_one_as_before(self):
        # 0.8.2. Catches: a name the view carries but the page never shows (the owner still sees
        # "agent" on every session's reply), and an unnamed record whose author text changed.
        for kind in BROWSERS:
            for width in (1280, 375):
                with self.subTest(browser=kind, width=width):
                    url = self.serve()
                    body = {"qid": "LANE.1/Q1", "item": "LANE.1", "text": "Question 1: which way?", "kind": "single",
                            "options": [{"id": "fix", "label": "Fix now"}, {"id": "leave", "label": "Leave it"}],
                            "star": "fix", "source": "docs/spec.md:1", "valid_if": [], "nonce": "namedq0001"}
                    code, out = self.SV.agent_request(self.cfg.socket, "POST", "/question", body, agent="agent-6")
                    self.assertEqual(code, 200, out)
                    for text, nonce, name in (("Named reply.", "namedreply01", "agent-6"),
                                              ("Plain reply.", "plainreply01", None)):
                        code, out = self.SV.agent_request(self.cfg.socket, "POST", "/message",
                                                          {"item": "LANE.1", "text": text, "nonce": nonce}, agent=name)
                        self.assertEqual(code, 200, out)
                        code, out = self.SV.agent_request(self.cfg.socket, "POST", "/message",
                                                          {"item": "@chat", "text": "Chat " + text, "nonce": "c" + nonce},
                                                          agent=name)
                        self.assertEqual(code, 200, out)
                    page = self.page(kind, width, url)
                    self.open_inbox(page, width)
                    page.click(".ck-inbox-item[data-qid='LANE.1/Q1']")
                    page.wait_for_selector(".ck-q-agent")
                    self.assertEqual(page.locator(".ck-q-agent").first.text_content(), "asked by agent-6")
                    bys = page.locator(".ck-message-by").all_text_contents()
                    self.assertIn("agent-6", bys)
                    self.assertIn("agent", bys)          # the unnamed reply reads as it always did
                    self.assertFalse(page.evaluate(OVERFLOW))
                    page = self.page(kind, width, url)       # a fresh page for the chat tab
                    self.open_inbox(page, width)
                    page.click("#ck-tab-chat")
                    page.wait_for_selector(".ck-chat-msg[data-by='agent']")
                    who = page.locator(".ck-chat-msg[data-by='agent'] .ck-chat-who").all_text_contents()
                    self.assertTrue(any(w.startswith("agent-6 · ") for w in who), who)
                    self.assertTrue(any(w.startswith("Agent · ") for w in who), who)
                    self.assertFalse(page.evaluate(OVERFLOW))
                    self.assert_not_reloaded(page)

    def test_polling_pauses_when_hidden_and_backs_off_on_errors(self):
        # Catches: a loop that keeps polling in a background tab (a thread held open for
        # nothing), and one that retries a failing server in a tight loop.
        for kind in BROWSERS:
            with self.subTest(browser=kind):
                url = self.serve()
                page = self.page(kind, 1280, url)
                page.wait_for_timeout(500)
                page.evaluate("Object.defineProperty(document, 'visibilityState', {configurable: true, get: () => 'hidden'});"
                              "document.dispatchEvent(new Event('visibilitychange'))")
                page.wait_for_timeout(300)
                n = len(self.waits)
                self.ask(1)  # a change while hidden starts no poll
                page.wait_for_timeout(2500)
                self.assertEqual(len(self.waits), n)
                page.evaluate("Object.defineProperty(document, 'visibilityState', {configurable: true, get: () => 'visible'});"
                              "document.dispatchEvent(new Event('visibilitychange'))")
                page.wait_for_function("document.querySelector('.ck-inbox-count').textContent === '1'", timeout=10000)
                # A server that fails: the loop backs off (2 s, 4 s, ...), never a tight loop.
                page.route("**/api/wait*", lambda r: r.fulfill(status=503, body="{}", content_type="application/json"))
                page.wait_for_timeout(300)
                start = len(self.waits)
                page.wait_for_timeout(5000)
                self.assertLessEqual(len(self.waits) - start, 3)

    def test_motion_is_off_under_reduced_motion(self):
        # Catches: animations that ignore the owner's reduced-motion setting.
        for kind in BROWSERS:
            with self.subTest(browser=kind):
                url = self.serve()
                page = self.page(kind, 1280, url, reduced=True)
                self.ask(1)
                page.wait_for_function("document.querySelector('.ck-dock-strip .ck-new-count').textContent === '1 new'",
                                       timeout=10000)
                names = page.evaluate("""() => [document.querySelector('.ck-dock-strip .ck-new-count'),
                    document.querySelector('.ck-item-btn .ck-ring-arc')].map(e => {
                      const s = getComputedStyle(e); return [s.animationName, s.transitionDuration]; })""")
                self.assertEqual(names, [["none", "0s"], ["none", "0s"]])

    def test_an_item_opened_from_the_inbox_offers_the_way_back(self):
        # Catches (owner, 2026-09-30: "when diving into an inbox item, there is no button to go
        # back to the inbox"): an item header whose Back is hidden on a wide screen; one that
        # leaves for the board instead of the inbox; focus dropped on the way in or out; and a
        # Back-to-inbox on an item the BOARD opened, where it would lead somewhere the owner
        # never was.
        for kind in BROWSERS:
            for width in (1280, 375):
                with self.subTest(browser=kind, width=width):
                    url = self.serve()
                    self.ask(1)
                    page = self.page(kind, width, url)
                    self.open_inbox(page, width)
                    page.click(".ck-inbox-item[data-qid='LANE.1/Q1']")
                    page.wait_for_selector(".ck-title-id")
                    back = page.locator(".ck-panel .ck-back-btn")
                    self.assertTrue(back.is_visible())
                    self.assertEqual(back.get_attribute("aria-label"), "Back to inbox")
                    self.assertEqual(back.text_content(), "← Inbox")
                    self.assertTrue(page.evaluate("document.activeElement.classList.contains('ck-back-btn')"))
                    back.click()
                    page.wait_for_selector(".ck-tabs")
                    self.assertEqual(page.get_attribute(".ck-panel", "data-open"), "true")
                    self.assertEqual(page.evaluate("document.activeElement.getAttribute('data-qid')"), "LANE.1/Q1")
                    # Opened from the board, the same item keeps the board's Back (narrow screens only).
                    page.evaluate("window.ConsoleKit.open('LANE.1')")
                    page.wait_for_selector(".ck-title-id")
                    back = page.locator(".ck-panel .ck-back-btn")
                    self.assertEqual(back.get_attribute("aria-label"), "Back to board")
                    self.assertEqual(back.is_visible(), width < 400)
                    self.assertFalse(page.evaluate(OVERFLOW))
                    self.assert_not_reloaded(page)

    def test_one_lock_covers_every_answered_question_on_an_item(self):
        # Catches (owner, 2026-09-30: "there should be a 'lock all items' button rather than one
        # for each question being answered"): no such button; one offered for a single answer;
        # one that locks before its confirmation, locks a question nobody answered, or rings
        # "process" (locking is not asking the agent to act).
        def locks():
            return sorted(b["qid"] for b in self.bell() if b.get("type") == "lock")

        for kind in BROWSERS:
            for width in (1280, 375):
                with self.subTest(browser=kind, width=width):
                    url = self.serve()
                    for n in (1, 2, 3):
                        self.ask(n)
                    self.console.write("answer", {"qid": "LANE.1/Q1", "picks": ["fix"], "own_text": "",
                                                  "nonce": "lockallans1"}, "owner")
                    page = self.page(kind, width, url)
                    page.evaluate("window.ConsoleKit.open('LANE.1')")
                    page.wait_for_selector(".ck-question")
                    self.assertEqual(page.locator(".ck-lock-answered-open").count(), 0)  # one answer: its own button
                    self.console.write("answer", {"qid": "LANE.1/Q2", "picks": ["record"], "own_text": "",
                                                  "nonce": "lockallans2"}, "owner")
                    page.click(".ck-status-bar .ck-refresh-btn >> nth=0")
                    page.wait_for_selector(".ck-lock-answered-open", timeout=10000)
                    self.assertEqual(page.locator(".ck-lock-answered-open").text_content(), "Lock all 2 answers…")
                    page.click(".ck-lock-answered-open")
                    page.wait_for_selector(".ck-lock-answered .ck-confirm")
                    self.assertEqual(locks(), [])  # nothing locks before the confirmation
                    self.assertEqual(page.locator(".ck-lock-answered-qid").all_text_contents(),
                                     ["LANE.1/Q1", "LANE.1/Q2"])
                    self.assertFalse(page.evaluate(OVERFLOW))
                    page.click(".ck-lock-answered-go")
                    page.wait_for_function(
                        "document.querySelectorAll(\".ck-q-state[data-state='locked']\").length === 2", timeout=10000)
                    self.assertEqual(locks(), ["LANE.1/Q1", "LANE.1/Q2"])  # Q3, unanswered, is not locked
                    self.assertEqual(page.locator(".ck-lock-answered-open").count(), 0)
                    self.assertEqual(page.locator(".ck-lock-answered .ck-error-msg").count(), 0)
                    self.assertEqual([b for b in self.bell() if b.get("intent") == "process"], [])
                    self.assert_not_reloaded(page)

    def test_a_refused_lock_stops_the_run_names_it_and_can_be_dismissed(self):
        # Catches: a run that goes on past a refusal, or stops silently; an error that does not
        # name the question or the count; one that cannot be cleared; focus left on <body>.
        def locks():
            return sorted(b["qid"] for b in self.bell() if b.get("type") == "lock")

        for kind in BROWSERS:
            with self.subTest(browser=kind):
                url = self.serve()
                for n in (1, 2, 3):
                    self.ask(n)
                for n in (1, 2, 3):
                    self.console.write("answer", {"qid": f"LANE.1/Q{n}", "picks": ["fix"], "own_text": "",
                                                  "nonce": f"refusedans{n}"}, "owner")
                page = self.page(kind, 1280, url, block_live=True)  # the confirm keeps the answers it showed
                page.evaluate("window.ConsoleKit.open('LANE.1')")
                page.click(".ck-lock-answered-open")
                page.wait_for_selector(".ck-lock-answered .ck-confirm")
                # Q2 is answered again after the owner saw it: its shown answer is no longer current.
                self.console.write("answer", {"qid": "LANE.1/Q2", "picks": ["leave"], "own_text": "",
                                              "nonce": "refusedans4"}, "owner")
                page.click(".ck-lock-answered-go")
                page.wait_for_selector(".ck-lock-answered-error", timeout=10000)
                err = page.locator(".ck-lock-answered-error").text_content()
                self.assertIn("Locked 1 of 3.", err)
                self.assertIn("LANE.1/Q2 was not locked", err)
                self.assertEqual(locks(), ["LANE.1/Q1"])  # stopped at Q2: Q3 was not tried
                self.assertTrue(page.evaluate("document.activeElement.classList.contains('ck-lock-answered-error')"))
                self.assertEqual(page.locator("[role='alert'].ck-lock-answered-error").count(), 0)
                page.click(".ck-lock-answered-error button")
                self.assertEqual(page.locator(".ck-lock-answered-error").count(), 0)
                self.assertTrue(page.evaluate("document.activeElement.classList.contains('ck-questions-heading')"))
                self.assert_not_reloaded(page)

    def test_the_usage_footer_shows_usage_and_account_and_makes_room(self):
        # Catches: a footer on a server that set no file; one drawn from anything but the checked
        # fields; one that covers the page's last line; a stale snapshot that reads as current;
        # and one that pushes a phone sideways.
        from datetime import datetime, timedelta, timezone

        def stamp(ago=timedelta(0)):
            return (datetime.now(timezone.utc) - ago).isoformat().replace("+00:00", "Z")

        def write_usage(d, ago=timedelta(0)):
            (d / "usage.json").write_text(json.dumps({
                "updated_at": stamp(ago),
                "five_hour": {"used_percentage": 42, "resets_at": stamp(-timedelta(hours=2))},
                "seven_day": {"used_percentage": 18, "resets_at": None},
                "planted": "planted-usage-marker"}))

        for kind in BROWSERS:
            with self.subTest(browser=kind):
                url = self.serve()
                page = self.page(kind, 1280, url)
                page.evaluate("window.ConsoleKit.refreshUsage()")
                from overture import __version__
                # No usage file: the footer carries the kit version alone, never usage it was not given.
                self.assertEqual(page.locator(".ck-footer").text_content(), f"overture {__version__}")
                d = self.cfg.root
                write_usage(d)
                (d / "acct.json").write_text(json.dumps(
                    {"oauthAccount": {"emailAddress": "owner@example.com", "accountUuid": "planted-uuid"},
                     "primaryApiKey": "planted-secret"}))
                self.console.cfg = self.SV.Config(**{**self.cfg.__dict__, "usage_file": d / "usage.json",
                                                     "account_file": d / "acct.json"})
                page.evaluate("window.ConsoleKit.refreshUsage()")
                page.wait_for_selector(".ck-footer")
                text = page.locator(".ck-footer").text_content()
                for want in ("5-hour 42% (resets", "7-day 18%", "as of just now", "owner@example.com",
                             f"overture {__version__}"):
                    self.assertIn(want, text)
                self.assertNotIn("planted", text)
                self.assertEqual(page.locator(".ck-footer").get_attribute("data-stale"), "false")
                room = page.evaluate("[parseFloat(getComputedStyle(document.body).paddingBottom),"
                                     " document.querySelector('.ck-footer').offsetHeight]")
                self.assertAlmostEqual(room[0], room[1], delta=1)
                write_usage(d, ago=timedelta(hours=3))
                page.evaluate("window.ConsoleKit.refreshUsage()")
                self.assertIn("as of 3 h ago", page.locator(".ck-footer").text_content())
                self.assertEqual(page.locator(".ck-footer").get_attribute("data-stale"), "true")
                page.set_viewport_size({"width": 375, "height": 800})
                page.evaluate("window.ConsoleKit.refreshUsage()")
                self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 375)
                room = page.evaluate("[parseFloat(getComputedStyle(document.body).paddingBottom),"
                                     " document.querySelector('.ck-footer').offsetHeight]")
                self.assertAlmostEqual(room[0], room[1], delta=1)
                self.assert_not_reloaded(page)

    def test_the_footer_names_the_running_kit_version_with_no_usage_at_all(self):
        # Owner, 2026-10-01: "version number should be in footer". Catches: a version shown only when the
        # usage footer is on; one typed into console.js rather than read from the server; and one put in
        # as markup.
        from overture import __version__
        for kind in BROWSERS:
            with self.subTest(browser=kind):
                url = self.serve()
                page = self.page(kind, 1280, url)
                page.wait_for_selector(".ck-footer")
                self.assertEqual(page.locator(".ck-footer").text_content(), f"overture {__version__}")
                self.assertEqual(page.locator(".ck-footer").evaluate("e => e.children.length"), 0)  # text only
                room = page.evaluate("[parseFloat(getComputedStyle(document.body).paddingBottom),"
                                     " document.querySelector('.ck-footer').offsetHeight]")
                self.assertAlmostEqual(room[0], room[1], delta=1)   # it covers no line of the page
                self.assert_not_reloaded(page)

    # -- the board-stale bar (owner, 2026-10-01: "the 'board has changed since page loaded' message is not
    # going away"). The served page is a PUBLISHED SNAPSHOT (Q28/Q29), so reloading serves the same page with
    # the same shape and the bar comes straight back: it may never offer an action that cannot fix it.

    STALE_HOST = LIVE_HOST.replace("<input id=\"board-input\"",
                                   '<p data-live-shape="page-shape">Open: <span data-live="open">3</span></p>\n'
                                   '<input id="board-input"')
    MISMATCH = {"items": dict(LIVE_ITEMS), "seed_questions": [],
                "board": {"shape": "board-shape", "values": {"open": "4"}}}

    def _stale_page(self, kind, staged):
        url = self.serve(snapshot=True, page=self.STALE_HOST)
        if staged:   # the steward staged a newer page; the owner has not pressed "Use this page" yet
            from test_server import page_body
            self.console.push_page_snapshot(page_body(self.STALE_HOST.replace("page-shape", "board-shape"),
                                                      commit="beef" + "0" * 36))
        code, out = self.SV.agent_request(self.cfg.socket, "POST", "/items", self.MISMATCH)
        self.assertEqual(code, 200, out)
        page = self.page(kind, 1280, url)
        page.wait_for_selector(".ck-board-stale", timeout=10000)
        bar = page.locator(".ck-board-stale")
        self.assertEqual(bar.get_attribute("role"), "status")
        self.assertEqual(page.locator("[data-live='open']").text_content(), "3")   # nothing was patched
        self.assertEqual(page.locator(".ck-board-reload").count(), 0)   # a reload serves the same snapshot
        text = bar.text_content()
        self.assertNotIn("Reload", text)
        self.assertIn("since this page was published", text)
        self.assertIn("live numbers are paused", text)
        return page, bar, text

    def test_a_stale_board_with_nothing_staged_names_page_snapshot_and_offers_no_reload(self):
        for kind in BROWSERS:
            with self.subTest(browser=kind):
                page, bar, text = self._stale_page(kind, staged=False)
                self.assertIn("agent.py page-snapshot", text)
                self.assertNotIn("Use this page", text)
                self.assertEqual(bar.locator("button").count(), 0)   # nothing here can fix it
                self.assert_not_reloaded(page)

    def test_a_stale_board_with_a_staged_page_points_at_use_this_page(self):
        for kind in BROWSERS:
            with self.subTest(browser=kind):
                page, bar, text = self._stale_page(kind, staged=True)
                self.assertIn('"Use this page"', text)
                self.assertNotIn("page-snapshot", text)
                for sel in (".ck-board-stale span", ".ck-page-waiting"):   # stacked, neither covers the other
                    self.assertTrue(page.evaluate(
                        f"(() => {{ const e = document.querySelector('{sel}'), r = e.getBoundingClientRect();"
                        f" return e.contains(document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2));"
                        f" }})()"), sel)
                review = bar.locator("button")
                self.assertEqual(review.text_content(), "Review the new page")
                review.click()
                page.wait_for_selector("dialog.ck-page-dialog[open]")   # the same review dialog the chip opens
                self.assertTrue(page.evaluate("document.querySelector('dialog.ck-page-dialog').matches(':modal')"))
                self.assertTrue(page.evaluate(LiveConsoleTests.IN_DIALOG))
                page.keyboard.press("Escape")
                page.wait_for_function("!document.querySelector('dialog.ck-page-dialog').open")
                self.assertTrue(page.evaluate("document.activeElement.classList.contains('ck-board-show-proposal')"))
                self.assert_not_reloaded(page)   # reviewing the proposal publishes nothing


# -- 0.8.0: the Next step menu, tag chips, roar, and visuals, against the REAL server ------

# A mock that tries everything a hostile page would: run a script in its own document,
# reach the console through parent/top, post a message out, fire an inline handler, and
# submit a form away. In an <iframe sandbox=""> served under a `sandbox` CSP, none may work.
EVIL_MOCK = ("<!doctype html><html><head><style>h1{font:20px sans-serif}</style></head><body>"
             "<h1 id='mock'>Grid mock</h1>"
             "<script>document.body.setAttribute('data-ran','script');"
             "try{parent.document.body.setAttribute('data-pwned','parent')}catch(e){}"
             "try{top.document.title='pwned'}catch(e){}"
             "try{parent.postMessage('pwned','*')}catch(e){}</script>"
             "<img src='x' onerror=\"document.body.setAttribute('data-ran','img');parent.postMessage('img','*')\">"
             "<form action='https://example.com/steal'><button>go</button></form></body></html>")
MERMAID = 'graph TD\n  A["<img src=x onerror=alert(1)>"] --> B[Zone]\n'


class NextStepAndVisualTests(unittest.TestCase):
    """0.8.0: Next step ▾ (follow up / refine / drill), tag chips with reasons, roar once, visuals."""

    setUpClass = classmethod(LiveConsoleTests.setUpClass.__func__)
    tearDownClass = classmethod(LiveConsoleTests.tearDownClass.__func__)
    page, agent_post, bell, assert_not_reloaded = (LiveConsoleTests.page, LiveConsoleTests.agent_post,
                                                   LiveConsoleTests.bell, LiveConsoleTests.assert_not_reloaded)

    def serve(self):
        """LiveConsoleTests.serve, with a project that sets specs_dir and visuals_dir."""
        import tempfile
        from overture import server as SV
        from test_server import AUD, HOSTNAME, KEY, TEAM, page_body, token
        td = tempfile.mkdtemp(prefix="ck-p4-")
        self.addCleanup(lambda: __import__("shutil").rmtree(td, ignore_errors=True))
        root = Path(td)
        (root / "specs").mkdir()
        (root / "specs/grid.md").write_text("# Grid\n\nA session block holds the cells.\n")
        os.utime(root / "specs/grid.md", (1_600_000_000, 1_600_000_000))   # long before any lock
        (root / ".overture.json").write_text(json.dumps({"specs_dir": "specs/", "visuals_dir": "visuals/"}))
        cfg = SV.Config(root=root, page=None, state=root / "state", adapter=root / "unused.py",
                        team_domain=TEAM, aud=AUD, hostname=HOSTNAME, port=0, project="p4-test")
        console = SV.Console(cfg, _LiveAdapter())
        staged = console.push_page_snapshot(page_body(LIVE_HOST))   # Q28: the page is the steward's snapshot
        console.publish_page({"commit": staged["staged"]})   # Q29: and the owner published it
        verify = SV.access_verifier(TEAM, AUD, key_for=lambda _t: KEY.public_key())
        owner = SV.owner_server(console, verify, 0)
        threading.Thread(target=owner.serve_forever, daemon=True).start()
        agent = SV.agent_server(console)
        threading.Thread(target=agent.serve_forever, daemon=True).start()
        for srv in (owner, agent):
            self.addCleanup(srv.server_close)
            self.addCleanup(srv.shutdown)
        self.tok, self.origin = token(), f"https://{HOSTNAME}"
        self.SV, self.console, self.cfg = SV, console, cfg
        return f"http://127.0.0.1:{owner.server_address[1]}/"

    def locked_question(self, own_text="", picks=("leave",)):
        """LANE.1/Q1 citing the spec, answered against the ★ in the owner's own words, and locked."""
        self.agent_post("/question", {
            "qid": "LANE.1/Q1", "item": "LANE.1", "text": "Where does the session block live?", "kind": "single",
            "options": [{"id": "fix", "label": "Fix now"}, {"id": "record", "label": "Record in findings.md"},
                        {"id": "leave", "label": "Leave it"}],
            "star": "fix", "source": "specs/grid.md:3", "valid_if": [], "nonce": "p4question01"})
        a = self.console.write("answer", {"qid": "LANE.1/Q1", "picks": list(picks), "own_text": own_text,
                                          "nonce": "p4answer001"}, "owner")
        self.console.write("lock", {"qid": "LANE.1/Q1", "answer": a["id"], "nonce": "p4lock00001"}, "owner")

    def open_item(self, page):
        page.click("details[id='item-LANE.1'] .ck-item-btn")
        page.wait_for_selector(".ck-panel .ck-tools")

    def shot(self, page, name, kind, width):
        if os.environ.get("OVERTURE_SHOTS"):
            page.locator(".ck-panel").screenshot(
                path=str(Path(os.environ["OVERTURE_SHOTS"]) / f"{name}-{kind}-{width}.png"))

    def forks(self):
        return [r for r in self.console.store.records() if r["type"] == "message" and r.get("intent") == "fork"]

    def test_tag_chips_say_why_and_next_step_starts_a_refine(self):
        # Catches: chips worked out on the page (they would drift from the server's rules), a chip
        # whose reason a screen reader never hears, a menu that offers the step but sends a plain
        # follow-up, and a refine that forgets which answer it is about.
        for kind in BROWSERS:
            for width in (1280, 375):
                with self.subTest(browser=kind, width=width):
                    url = self.serve()
                    self.locked_question(own_text="Keep the `ZoneMaster` out of the grid.")
                    page = self.page(kind, width, url)
                    self.open_item(page)
                    _expand(page)
                    page.wait_for_selector(".ck-question .ck-tag[data-step='refine']")
                    chips = page.eval_on_selector_all(".ck-question .ck-tag", """cs => cs.map(c => ({
                        step: c.dataset.step, title: c.title,
                        shown: c.querySelector('[aria-hidden=true]').textContent,
                        said: c.querySelector('.ck-sr-only').textContent }))""")
                    self.assertEqual([c["step"] for c in chips], ["refine", "drill", "deliberate"])
                    for c in chips:
                        self.assertEqual(c["shown"], "→ " + c["step"])
                        self.assertTrue(c["said"].startswith(f"Suggested next step, {c['step']}: "), c)
                        self.assertTrue(c["said"].endswith(c["title"]), c)   # the reason, whole, both ways
                    self.assertIn("cites specs/grid.md, not edited since you locked this", chips[0]["title"])
                    self.assertIn("`ZoneMaster`", chips[1]["title"])
                    self.assertIn("went against the ★ (Fix now)", chips[2]["title"])
                    nxt = page.locator("button[aria-label^='Next step for']")
                    nxt.click()
                    self.assertEqual(nxt.get_attribute("aria-expanded"), "true")
                    self.assertEqual(page.eval_on_selector_all(".ck-next-menu button", "bs => bs.map(b => b.dataset.step)"),
                                     ["follow", "refine", "drill"])
                    self.assertTrue(page.evaluate("document.activeElement.dataset.step === 'follow'"))  # focus moved in
                    page.locator(".ck-next-menu button[data-step='refine']").click()
                    form = page.locator(".ck-step-form[data-step='refine']")
                    self.assertIn("It writes nothing before you lock", form.text_content())
                    form.get_by_label("Note (optional)").fill("The spec still says the block floats.")
                    self.shot(page, "next-step-refine", kind, width)
                    self.assertFalse(page.evaluate(OVERFLOW))
                    form.get_by_role("button", name="Start the refine").click()
                    page.wait_for_function("document.querySelectorAll('.ck-step-form').length === 0")
                    [f] = self.forks()
                    self.assertEqual({k: f.get(k) for k in ("intent", "step", "about_qid", "mode", "roles", "text")},
                                     {"intent": "fork", "step": "refine", "about_qid": "LANE.1/Q1", "mode": "tighten",
                                      "roles": None, "text": "The spec still says the block floats."})
                    page.wait_for_selector(".ck-fork-head:has-text('Refine')")
                    self.assert_not_reloaded(page)

    def test_a_refine_chip_in_the_server_says_which_time_it_shows(self):
        # CONSOLE-kit/Q23: the served console reads no git, so a refine chip shows the file's time
        # and says why it is not the last commit's. Catches: an unlabelled "file time" that reads
        # as if git had been asked.
        _seam_closed(self)
        for kind in BROWSERS:
            with self.subTest(browser=kind):
                url = self.serve()
                self.locked_question()
                page = self.page(kind, 1280, url)
                self.open_item(page)
                _expand(page)
                page.wait_for_selector(".ck-question .ck-tag[data-step='refine']")
                title = page.get_attribute(".ck-question .ck-tag[data-step='refine']", "title")
                said = page.text_content(".ck-question .ck-tag[data-step='refine'] .ck-sr-only")
                for text in (title, said):
                    self.assertIn(f"(file time 2020-09-13T12:26:40Z; the last-commit time is {NOGIT}, lock ", text)

    def test_a_second_roar_is_refused_on_the_page_naming_the_first(self):
        # Catches: a roar allowed beside other seats, and a second roar the server lets through
        # (the page would say "sent" and six agents would run again).
        for kind in BROWSERS:
            for width in (1280, 375):
                with self.subTest(browser=kind, width=width):
                    url = self.serve()
                    self.locked_question(picks=("fix",))
                    page = self.page(kind, width, url)
                    self.open_item(page)

                    def roar_once():
                        if page.locator(".ck-next-menu").count() == 0:
                            _expand(page)
                            page.locator("button[aria-label^='Next step for']").click()
                        page.locator(".ck-next-menu button[data-step='follow']").click()
                        form = page.locator(".ck-followup")
                        roar = form.locator("input[value='roar']")
                        roar.check()
                        self.assertTrue(form.get_by_label("DevOps").is_disabled())   # roar is called alone
                        form.get_by_role("button", name="Send follow-up").click()
                        return form

                    roar_once()
                    page.wait_for_function("document.querySelectorAll('.ck-followup').length === 0")
                    [first] = self.forks()
                    self.assertEqual(first["roles"], ["roar"])
                    form = roar_once()
                    page.wait_for_function("document.querySelector('.ck-followup .ck-error-msg').textContent !== ''")
                    err = form.locator(".ck-error-msg").text_content()
                    self.assertIn(first["id"], err)
                    self.assertIn("at most once per lock", err)
                    self.assertEqual(len(self.forks()), 1)
                    self.shot(page, "roar-refused", kind, width)
                    self.assertFalse(page.evaluate(OVERFLOW))

    def test_a_visual_renders_only_in_a_sandbox_and_its_script_cannot_reach_the_page(self):
        # Catches: a mock injected into the page (its script runs as the owner), an iframe with
        # allow-scripts or allow-same-origin (it could read the console), a mock whose script runs
        # when its URL is opened on its own, and Mermaid source parsed as markup.
        for kind in BROWSERS:
            for width in (1280, 375):
                with self.subTest(browser=kind, width=width):
                    url = self.serve()
                    page = self.page(kind, width, url)
                    page.evaluate("window.__msgs = []; addEventListener('message', e => __msgs.push(String(e.data)))")
                    self.open_item(page)
                    page.locator("button[aria-label='Request a visual of LANE.1']").click()
                    form = page.locator(".ck-visual-form")
                    form.get_by_label("What should it show?").fill("The grid at phone width")
                    form.get_by_role("button", name="Request the visual").click()
                    page.wait_for_selector(".ck-visual-request")
                    [req] = [r for r in self.console.store.records() if r.get("intent") == "visual"]
                    self.assertEqual(self.bell()[-1]["intent"], "visual")
                    html = self.agent_post("/visual", {"request": req["id"], "format": "html", "title": "Grid mock",
                                                       "content": EVIL_MOCK, "text": "One column per zone.",
                                                       "nonce": "p4visual001"})
                    self.agent_post("/visual", {"request": req["id"], "format": "mermaid", "title": "Zones",
                                                "content": MERMAID, "text": "Zones feed the grid.",
                                                "nonce": "p4visual002"})
                    frame_el = page.wait_for_selector("iframe.ck-visual-frame", timeout=10000)
                    self.assertEqual(frame_el.get_attribute("sandbox"), "")          # grants nothing
                    frame = page.frame_locator("iframe.ck-visual-frame")
                    self.assertEqual(frame.locator("#mock").text_content(timeout=10000), "Grid mock")  # it rendered
                    page.wait_for_timeout(500)                                      # time for any script to act
                    self.assertIsNone(frame.locator("body").get_attribute("data-ran"))
                    self.assertIsNone(page.evaluate("document.body.getAttribute('data-pwned')"))
                    self.assertNotEqual(page.title(), "pwned")
                    self.assertEqual(page.evaluate("window.__msgs"), [])
                    self.assertEqual(page.locator("#mock").count(), 0)            # nothing of it in the page itself
                    code = page.locator(".ck-visual[data-format='mermaid'] .ck-visual-code")
                    page.wait_for_function("document.querySelector(\".ck-visual[data-format='mermaid'] "
                                           ".ck-visual-code\").textContent.startsWith('graph TD')")
                    self.assertEqual(code.text_content(), MERMAID)                  # as text, markup and all
                    self.assertEqual(code.locator("img").count(), 0)
                    self.shot(page, "visual", kind, width)
                    self.assertFalse(page.evaluate(OVERFLOW))
                    # Opened on its own, the mock is still sandboxed by the server's CSP: no script runs.
                    alone = page.context.new_page()
                    alone.route("**/*", lambda route: route.continue_(
                        headers={**route.request.headers, "cf-access-jwt-assertion": self.tok}))
                    alone.goto(url + "api/visual?id=" + html["id"])
                    alone.wait_for_selector("#mock")
                    alone.wait_for_timeout(300)
                    self.assertIsNone(alone.locator("body").get_attribute("data-ran"))
                    self.assert_not_reloaded(page)


# A host whose own header is sticky at the top, stacked the way a lane board's is (z-index 100).
STICKY_HOST = LIVE_HOST.replace(
    "<body>", '<body><header id="host-header" style="position:sticky;top:0;z-index:100;height:64px;'
              'background:#222;color:#fff">host header</header><div style="height:2000px">')
STICKY_HOST = STICKY_HOST.replace("</body>", "</div></body>")


class InboxUXTests(unittest.TestCase):
    """Owner rulings CONSOLE-kit/Q33-Q37 (2026-10-02, all ★), and the console half of Q38's motion."""

    setUpClass = classmethod(LiveConsoleTests.setUpClass.__func__)
    tearDownClass = classmethod(LiveConsoleTests.tearDownClass.__func__)
    serve = LiveConsoleTests.serve
    agent_post = LiveConsoleTests.agent_post
    ask = LiveConsoleTests.ask
    bell = LiveConsoleTests.bell
    page = LiveConsoleTests.page
    open_inbox = LiveConsoleTests.open_inbox
    assert_not_reloaded = LiveConsoleTests.assert_not_reloaded

    # -- helpers ------------------------------------------------------------------------

    def answer(self, qid, pick="fix", own_text=""):
        return self.console.write("answer", {"qid": qid, "picks": [pick], "own_text": own_text,
                                             "nonce": "ans" + qid.replace("/", "").replace(".", "")}, "owner")

    def lock(self, qid):
        head = self.console.store.head(qid)
        return self.console.write("lock", {"qid": qid, "answer": head["id"],
                                           "nonce": "lk" + qid.replace("/", "").replace(".", "")}, "owner")

    def locked(self, qid):
        head = self.console.store.head(qid)
        return bool(head) and self.console.store.lock_of(head["id"]) is not None

    def locks(self):
        return [r for r in self.console.store.records() if r["type"] == "lock"]

    def open_item(self, page, item="LANE.1"):
        page.evaluate("ConsoleKit.open(%s)" % json.dumps(item))
        page.wait_for_selector(".ck-questions-heading")

    def said(self, page, text, timeout=3000):
        page.wait_for_function("t => document.querySelector('.ck-live').textContent.includes(t)", arg=text,
                               timeout=timeout)

    def ask_as(self, n, agent, item="LANE.1", at=None, **over):
        """An agent question asked under a session name (kit 0.8.2), at a chosen store time."""
        if at is not None:
            self.console.store.clock = lambda: at
        body = {"qid": f"{item}/Q{n}", "item": item, "text": f"Question {n}: which way?", "kind": "single",
                "options": [{"id": "fix", "label": "Fix now"}, {"id": "record", "label": "Record in findings.md"},
                            {"id": "leave", "label": "Leave it"}],
                "star": "fix", "source": "docs/spec.md:1", "valid_if": [], "nonce": f"uxq{n:04d}{agent or 'none'}"}
        body.update(over)
        return self.console.write("question", body, "agent", agent=agent)

    # -- Q33: one tap, a countdown, then the lock --------------------------------------

    def test_q33_one_tap_locks_only_when_the_countdown_ends(self):
        # Catches: a lock sent at the tap with a cosmetic countdown (nothing may reach the store before it ends);
        # a countdown that never sends; a second confirm step left in (one tap must be enough); and an Undo
        # that is not where focus lands, so a keyboard owner cannot stop it.
        for kind in BROWSERS:
            with self.subTest(browser=kind):
                url = self.serve()
                self.ask(1)
                self.answer("LANE.1/Q1")
                page = self.page(kind, 1280, url)
                self.open_item(page)
                page.click(".ck-lock-one")
                page.wait_for_selector(".ck-lock-countdown")
                self.assertIn("Locking in 5 s", page.locator(".ck-lock-countdown").text_content())
                self.assertTrue(page.evaluate("document.activeElement.classList.contains('ck-lock-undo')"))
                self.said(page, "Locking in 5 seconds")
                self.assertEqual(page.locator(".ck-confirm").count(), 0, "a second confirmation step is left in")
                page.wait_for_timeout(3500)
                self.assertEqual(self.locks(), [], "the lock reached the store before the countdown ended")
                self.assertIn(page.locator(".ck-lock-left").text_content(), ("Locking in 2 s", "Locking in 1 s"))
                page.wait_for_function("document.querySelector('.ck-lock-countdown') === null", timeout=6000)
                self.assertTrue(self.locked("LANE.1/Q1"))
                self.assertEqual(len(self.locks()), 1)
                self.said(page, "Locked.")
                # Focus was on the Undo the lock removed: it lands on the question's own line.
                page.wait_for_selector(".ck-q-line[data-qid='LANE.1/Q1']")
                self.assertTrue(page.evaluate("document.activeElement.classList.contains('ck-q-line')"))

    def test_q33_undo_leaving_the_question_and_leaving_the_page_send_nothing(self):
        # Catches: an Undo that hides the countdown but lets its timer fire; a countdown that keeps running
        # after the owner left the question (a lock from a place they can no longer see); and a page that
        # sends the lock as it unloads (a beacon or fetch keepalive on pagehide).
        for kind in BROWSERS:
            with self.subTest(browser=kind):
                url = self.serve()
                self.ask(1)
                self.answer("LANE.1/Q1")
                page = self.page(kind, 1280, url)
                self.open_item(page)
                # 1. Undo, by keyboard: Enter on the focused Undo.
                page.click(".ck-lock-one")
                page.wait_for_selector(".ck-lock-undo")
                page.keyboard.press("Enter")
                page.wait_for_selector(".ck-lock-one")
                self.said(page, "Nothing was sent")
                self.assertTrue(page.evaluate("document.activeElement.classList.contains('ck-lock-one')"))
                # A second tap after an Undo counts the full five seconds again (no timer left running from the first).
                page.keyboard.press("Enter")
                page.wait_for_selector(".ck-lock-undo")
                page.wait_for_timeout(3200)
                self.assertEqual(self.locks(), [], "a timer left from the undone count ran the new one fast")
                page.keyboard.press("Enter")
                page.wait_for_selector(".ck-lock-one")
                # 2. Leaving the question: another item mid-count.
                page.click(".ck-lock-one")
                page.wait_for_selector(".ck-lock-countdown")
                page.evaluate("ConsoleKit.open('LANE')")
                self.said(page, "you left the question")
                # 3. Leaving the page mid-count and coming back from the back/forward cache. The page and its
                # timers live on (navigating away for real would kill them and prove nothing): only the pagehide
                # handler can stop this lock from landing on return.
                self.open_item(page)
                page.click(".ck-lock-one")
                page.wait_for_selector(".ck-lock-countdown")
                page.evaluate("""() => {
                    window.dispatchEvent(new PageTransitionEvent('pagehide', {persisted: true}));
                    window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}));
                }""")
                self.said(page, "you left the page before it locked")
                page.wait_for_selector(".ck-lock-one")
                page.wait_for_timeout(6500)   # past every countdown above
                self.assertEqual(self.locks(), [], "a lock was sent without the countdown ending on show")
                self.assertEqual(page.locator(".ck-lock-countdown").count(), 0)

    def test_q33_hiding_the_tab_or_closing_the_panel_mid_count_sends_nothing(self):
        # Review MEDIUMs. Catches: a countdown that keeps running in a background tab (the lock would land
        # where the owner cannot see it), and one that survives the panel being closed over it.
        for kind in BROWSERS:
            with self.subTest(browser=kind):
                url = self.serve()
                self.ask(1)
                self.answer("LANE.1/Q1")
                page = self.page(kind, 1280, url)
                self.open_item(page)
                # 1. The tab is hidden (switching tabs, minimising), then shown again.
                page.click(".ck-lock-one")
                page.wait_for_selector(".ck-lock-countdown")
                page.evaluate("""() => {
                    const set = v => Object.defineProperty(document, 'visibilityState', {configurable: true, get: () => v});
                    set('hidden'); document.dispatchEvent(new Event('visibilitychange'));
                    set('visible'); document.dispatchEvent(new Event('visibilitychange'));
                }""")
                self.said(page, "you left the page before it locked")
                page.wait_for_selector(".ck-lock-one")
                page.wait_for_timeout(6500)
                self.assertEqual(self.locks(), [], "a hidden tab kept counting and locked")
                # 2. The panel is closed mid-count, then opened again.
                page.click(".ck-lock-one")
                page.wait_for_selector(".ck-lock-countdown")
                page.click(".ck-close-btn")
                self.said(page, "you left the question before it locked")
                page.wait_for_timeout(6500)
                self.assertEqual(self.locks(), [], "a closed panel kept counting and locked")
                self.open_item(page)
                self.assertEqual(page.locator(".ck-lock-countdown").count(), 0)
                self.assertEqual(page.locator(".ck-lock-one").count(), 1)

    def test_q33_a_live_redraw_mid_count_keeps_the_countdown_and_the_undo_focus(self):
        # Catches: a countdown lost to a live redraw (it would lock with nothing on show, or not at all), focus
        # thrown off the Undo by the redraw, and a lock landing under a box the owner is typing in.
        for kind in BROWSERS:
            with self.subTest(browser=kind):
                url = self.serve()
                self.ask(1)
                self.ask(2)
                self.answer("LANE.1/Q1")
                page = self.page(kind, 1280, url)
                self.open_item(page)
                page.click(".ck-lock-one")
                page.wait_for_selector(".ck-lock-countdown")
                self.ask(3)   # arrives live while the count runs
                page.wait_for_selector(".ck-question:has-text('Question 3')", timeout=6000)
                self.assertEqual(page.locator(".ck-lock-countdown").count(), 1)
                self.assertTrue(page.evaluate("document.activeElement.classList.contains('ck-lock-undo')"))
                # Now type in Q2's own-words box while the lock lands: the box is not redrawn under the owner.
                box = page.locator(".ck-question:has-text('Question 2') textarea").first
                box.click()
                box.type("half a thought")
                deadline = time.monotonic() + 9
                while not self.locked("LANE.1/Q1") and time.monotonic() < deadline:
                    page.wait_for_timeout(200)   # not time.sleep: the sync route relaying POSTs runs only inside a Playwright call
                self.assertTrue(self.locked("LANE.1/Q1"), page.evaluate("document.querySelector('.ck-live').textContent"))
                # Not redrawn, yet not left saying "Locking in 1 s" over a question that is locked.
                page.wait_for_function("document.querySelector('.ck-lock-left').textContent === 'Locked.'")
                self.assertEqual(page.locator(".ck-lock-undo").get_attribute("aria-disabled"), "true")
                self.assertEqual(box.input_value(), "half a thought")
                self.assertTrue(page.evaluate("document.activeElement.tagName === 'TEXTAREA'"))

    # -- Q37: a settled question is one line ------------------------------------------

    def test_q37_locked_questions_roll_up_to_one_line_and_open_on_click(self):
        # Catches: locked questions left at full size; an open question rolled up (it still asks something);
        # a line without the pick; an expand that a live redraw closes again; and a toggle a keyboard or
        # screen reader cannot use (a div, or no aria-expanded).
        for kind in BROWSERS:
            with self.subTest(browser=kind):
                url = self.serve()
                self.ask(1)
                self.ask(2)
                self.ask(3)
                self.answer("LANE.1/Q1", pick="record")
                self.lock("LANE.1/Q1")
                self.answer("LANE.1/Q2")
                page = self.page(kind, 1280, url)
                self.open_item(page)
                line = page.locator(".ck-q-line[data-qid='LANE.1/Q1']")
                self.assertEqual(line.get_attribute("aria-expanded"), "false")
                self.assertEqual(line.evaluate("e => e.tagName"), "BUTTON")
                text = " ".join(line.text_content().split())
                for part in ("LANE.1/Q1", "Record in findings.md", "locked"):
                    self.assertIn(part, text)
                card = page.locator(".ck-question[data-qid='LANE.1/Q1']")
                self.assertEqual(card.locator(".ck-receipt").count(), 0, "a rolled-up question shows its receipt")
                for qid in ("LANE.1/Q2", "LANE.1/Q3"):   # unlocked and unanswered stay whole
                    self.assertEqual(page.locator(f".ck-question-rolled[data-qid='{qid}']").count(), 0, qid)
                line.focus()
                page.keyboard.press("Enter")
                page.wait_for_selector(".ck-question[data-qid='LANE.1/Q1'] .ck-receipt")
                self.assertEqual(page.locator(".ck-q-line[data-qid='LANE.1/Q1']").get_attribute("aria-expanded"),
                                 "true")
                self.assertTrue(page.evaluate("document.activeElement.classList.contains('ck-q-line')"))
                self.ask(4)   # a live redraw keeps it open
                page.wait_for_selector(".ck-question:has-text('Question 4')", timeout=6000)
                self.assertEqual(page.locator(".ck-question[data-qid='LANE.1/Q1'] .ck-receipt").count(), 1)
                page.click(".ck-q-line[data-qid='LANE.1/Q1']")
                page.wait_for_selector(".ck-question-rolled[data-qid='LANE.1/Q1']")

    def test_q37_a_stale_question_stays_whole(self):
        # Catches: rolling up every question with a lock, so a stale ruling (which needs the owner) hides.
        for kind in BROWSERS:
            with self.subTest(browser=kind):
                url = self.serve()
                spec = self.cfg.root / "docs" / "spec.md"
                spec.parent.mkdir(parents=True)
                spec.write_text("The cap is 64 KiB.\n")
                self.ask(1, valid_if=[{"kind": "excerpt", "path": "docs/spec.md", "text": "The cap is 64 KiB."}])
                self.answer("LANE.1/Q1")
                self.lock("LANE.1/Q1")
                spec.write_text("The cap is 32 KiB.\n")
                page = self.page(kind, 1280, url)
                self.open_item(page)
                page.wait_for_selector(".ck-stale-banner")
                self.assertEqual(page.locator(".ck-question-rolled").count(), 0)

    # -- Q36: the send bar -------------------------------------------------------------

    def test_q36_the_send_bar_counts_sends_the_process_signal_and_goes(self):
        # Catches: a bar shown before any answer; counts that are not answered/left; a bar under a host's
        # sticky header or scrolled away with the questions; a different signal from today's button (another
        # text or intent); two send controls at once; and a bar that stays after the send.
        for kind in BROWSERS:
            for width in (1280, 375):
                with self.subTest(browser=kind, width=width):
                    url = self.serve(page=STICKY_HOST)
                    for n in (1, 2, 3):
                        self.ask(n)
                    page = self.page(kind, width, url)
                    self.open_item(page)
                    self.assertEqual(page.locator(".ck-send-bar").count(), 0, "a bar before any answer")
                    self.answer("LANE.1/Q1")
                    page.wait_for_selector(".ck-send-bar", timeout=6000)
                    self.assertEqual(page.locator(".ck-send-bar-count").text_content(), "1 answered · 2 left")
                    self.answer("LANE.1/Q2", pick="leave")
                    page.wait_for_function("document.querySelector('.ck-send-bar-count').textContent === "
                                           "'2 answered · 1 left'", timeout=6000)
                    self.assertEqual(page.get_by_role("button", name="Answers are in: process them").count(), 0)
                    # Under the panel's header, never covered: what is drawn at its centre is the bar.
                    page.evaluate("document.querySelector('.ck-body').scrollTop = 99999; window.scrollTo(0, 400)")
                    geo = page.evaluate("""() => {
                        const b = document.querySelector('.ck-send-bar').getBoundingClientRect();
                        const h = document.querySelector('.ck-panel .ck-header').getBoundingClientRect();
                        const hit = document.elementFromPoint(b.left + 8, b.top + b.height / 2);
                        return {below: b.top >= h.bottom - 1, top: b.top, mine: !!hit.closest('.ck-send-bar')};
                    }""")
                    self.assertTrue(geo["below"], geo)
                    self.assertTrue(geo["mine"], f"something covers the bar: {geo}")
                    self.assertLess(geo["top"], 200, geo)
                    self.assertEqual([b for b in self.bell() if b.get("intent") == "process"], [])
                    page.click(".ck-send-bar-go")
                    page.wait_for_function("document.querySelector('.ck-send-bar') === null", timeout=6000)
                    sent = [r for r in self.console.store.records()
                            if r["type"] == "message" and r.get("intent") == "process"]
                    self.assertEqual([(r["item"], r["text"], r["by"]) for r in sent],
                                     [("LANE.1", "Answers are in: process them.", "owner")])
                    self.assertEqual(len([b for b in self.bell() if b.get("intent") == "process"]), 1)
                    self.said(page, "Sent to the agent")
                    self.assertFalse(page.evaluate("document.activeElement === document.body"))
                    # The next answer brings it back, counting only what came after the send.
                    self.answer("LANE.1/Q3")
                    page.wait_for_function("document.querySelector('.ck-send-bar-count') && document.querySelector("
                                           "'.ck-send-bar-count').textContent === '1 answered · 0 left'", timeout=6000)

    # -- Q34: a single pick moves on ----------------------------------------------------

    def round_of(self, kinds):
        f = self.console.write("message", {"item": "LANE.1", "text": "go", "intent": "fork", "mode": "explore",
                                           "nonce": "uxfork0001"}, "owner")
        for n, k in enumerate(kinds, 1):
            self.ask(n, forked_from=f["id"], star_by="panel", kind=k)
        return f["id"]

    def test_q34_a_single_pick_moves_to_the_next_unanswered_question_and_says_so(self):
        # Catches: no move at all; a move to the next question even when it is already picked (it must be the
        # next UNANSWERED one); a move that is not announced; a move after a multi-choice pick, after a pick
        # on a question being commented on, or for every ↑/↓ step through the options (each is a change);
        # and a move onto the review page, whose Lock button an Enter would then press.
        for kind in BROWSERS:
            with self.subTest(browser=kind):
                url = self.serve()
                fid = self.round_of(["single", "single", "multi", "single", "single"])
                page = self.page(kind, 1280, url)
                page.evaluate("ConsoleKit.openRound(%s)" % json.dumps(fid))
                page.wait_for_selector(".ck-round-step")
                count = "document.querySelector('.ck-round-count').textContent"
                at = lambda n: page.evaluate(count + f".startsWith('Question {n} of 5')")
                page.click(".ck-round-dot >> nth=1")   # pick Q2 first: it moves on to Q3, the next unanswered
                page.wait_for_function(count + ".startsWith('Question 2 of 5')")
                # An ↑/↓ that changed nothing (its default prevented) must not make the next click a "walk".
                page.eval_on_selector(".ck-round-options input[value='fix']", """r => {
                    for (const type of ['keydown', 'keyup'])
                        r.dispatchEvent(new KeyboardEvent(type, {key: 'ArrowDown', bubbles: true}));
                }""")
                page.click(".ck-round-options input[value='leave']")
                page.wait_for_function(count + ".startsWith('Question 3 of 5')", timeout=3000)
                page.click(".ck-round-dot >> nth=0")
                page.wait_for_function(count + ".startsWith('Question 1 of 5')")
                page.click(".ck-round-options input[value='fix']")
                # Q2 is picked already: the next UNANSWERED is Q3.
                page.wait_for_function(count + ".startsWith('Question 3 of 5')", timeout=3000)
                self.said(page, "Moved on to question 3 of 5")
                self.assertTrue(page.evaluate("document.activeElement.classList.contains('ck-round-qtext')"))
                # Q3 is multi: a pick never moves (Q4 and Q5 are still open, so a move had somewhere to go).
                page.click(".ck-round-options input[value='fix']")
                page.wait_for_timeout(900)
                self.assertTrue(at(3))
                page.click(".ck-round-dot >> nth=3")
                page.wait_for_function(count + ".startsWith('Question 4 of 5')")
                # A step change puts focus on the question text in the next animation frame: wait for that, or
                # it lands after the radio is focused below and the ↓ goes to the heading (a flake, 1 in 4).
                page.wait_for_function("document.activeElement && document.activeElement.classList.contains("
                                       "'ck-round-qtext')")
                # ↑/↓ walk Q4's options without moving on, though Q5 is open.
                page.locator(".ck-round-options input[value='fix']").focus()
                page.wait_for_function("document.activeElement && document.activeElement.value === 'fix'")
                page.keyboard.press("ArrowDown")
                page.wait_for_function("document.querySelector(\".ck-round-options input[value='record']\").checked")
                page.wait_for_timeout(900)   # ADVANCE_MS is 400: a move would have happened by now
                self.assertTrue(at(4))
                # Commenting on Q4, then a pick: it stays.
                page.fill("#ck-round-words", "thinking about it")
                page.click(".ck-round-options input[value='leave']")
                page.wait_for_timeout(900)
                self.assertTrue(at(4))
                page.click(".ck-round-dot >> nth=4")
                page.wait_for_function(count + ".startsWith('Question 5 of 5')")
                # Every question picked now: a pick moves nowhere, and never onto the review page.
                page.click(".ck-round-options input[value='fix']")
                self.said(page, "Every question has a pick")
                page.wait_for_timeout(600)
                self.assertTrue(at(5))
                self.assertEqual(page.locator(".ck-review-heading").count(), 0)
                self.assertEqual(self.console.store.head("LANE.1/Q1"), None, "a pick wrote something")

    # -- Q35: loose questions from one agent, together -----------------------------------

    def test_q35_one_agents_loose_questions_on_one_item_are_offered_together(self):
        # Catches: grouping by item alone (two agents' questions merged), grouping unnamed questions (nothing
        # shows they are one agent's), a group spanning a long gap, a card that opens something other than
        # the round form, and a group lock that skips the owner door (it must be /answer then /lock, then
        # one 'process' message), or that locks before the owner presses its Lock.
        for kind in BROWSERS:
            with self.subTest(browser=kind):
                url = self.serve()
                self.ask_as(1, "agent-5", at="2026-10-02T10:00:00Z")
                self.ask_as(2, "agent-5", at="2026-10-02T10:03:00Z")
                self.ask_as(3, "agent-5", at="2026-10-02T10:06:00Z")
                self.ask_as(4, "agent-5", at="2026-10-02T11:30:00Z")   # after a long gap: alone
                self.ask_as(5, "agent-6", at="2026-10-02T10:04:00Z")   # another agent
                self.ask_as(6, None, at="2026-10-02T10:05:00Z")        # no name
                self.ask_as(7, None, at="2026-10-02T10:05:30Z")
                page = self.page(kind, 1280, url)
                self.open_inbox(page, 1280)
                cards = page.locator(".ck-cluster")
                self.assertEqual(cards.count(), 1)
                self.assertEqual(cards.locator(".ck-cluster-go").text_content(), "Answer these 3 together")
                self.assertEqual(cards.locator(".ck-inbox-item").evaluate_all("rs => rs.map(r => r.dataset.qid)"),
                                 ["LANE.1/Q1", "LANE.1/Q2", "LANE.1/Q3"])
                self.assertIn("agent-5", cards.locator(".ck-cluster-head").text_content())
                cards.locator(".ck-cluster-go").click()
                page.wait_for_selector(".ck-round-step")
                self.assertIn("Together: LANE.1", page.locator(".ck-round-title").text_content())
                self.assertTrue(page.evaluate("document.querySelector('.ck-round-count').textContent"
                                              ".startsWith('Question 1 of 3')"))
                for _ in range(3):
                    page.keyboard.press("2")
                    page.wait_for_timeout(600)
                page.get_by_role("button", name="Review all").click()
                page.wait_for_selector(".ck-review-heading")
                self.assertEqual(self.locks(), [], "locked before the owner pressed Lock")
                page.click(".ck-lock-all")
                page.wait_for_selector(".ck-result-heading", timeout=10000)
                for n in (1, 2, 3):
                    head = self.console.store.head(f"LANE.1/Q{n}")
                    self.assertEqual((head["picks"], head["by"]), (["record"], "owner"))
                    self.assertTrue(self.locked(f"LANE.1/Q{n}"))
                for n in (4, 5, 6, 7):
                    self.assertIsNone(self.console.store.head(f"LANE.1/Q{n}"))
                self.assertEqual(len([b for b in self.bell() if b.get("intent") == "process"]), 1)

    # -- Q38 (console half): motion only without a reduced-motion preference ---------------

    def test_q38_motion_runs_only_when_the_viewer_has_not_asked_for_less(self):
        # Catches: animation that ignores prefers-reduced-motion, and the send bar's slide never wired.
        for kind in BROWSERS:
            for reduced in (False, True):
                with self.subTest(browser=kind, reduced=reduced):
                    url = self.serve()
                    self.ask(1)
                    page = self.page(kind, 1280, url, reduced=reduced)
                    self.open_item(page)
                    self.answer("LANE.1/Q1")
                    page.wait_for_selector(".ck-send-bar", timeout=6000)
                    name = page.evaluate("getComputedStyle(document.querySelector('.ck-send-bar')).animationName")
                    self.assertEqual(name, "none" if reduced else "ck-slide-down")


class ScanUITests(unittest.TestCase):
    """CONSOLE-kit/Q40 (button_now) and Q41 (side file): scanning stale rulings for a resolve, on the page."""

    setUpClass = classmethod(LiveConsoleTests.setUpClass.__func__)
    tearDownClass = classmethod(LiveConsoleTests.tearDownClass.__func__)
    serve = LiveConsoleTests.serve
    page = LiveConsoleTests.page
    open_inbox = LiveConsoleTests.open_inbox
    open_item = InboxUXTests.open_item
    said = InboxUXTests.said

    RULES = "# Rules\nRULE-ALPHA: every release is reviewed twice.\nRULE-BETA: the console never runs code.\n" \
            "RULE-DELTA: the owner locks answers.\n"

    def ruling(self, n, text, **over):
        """LANE.1/Q<n>, answered and locked against the excerpt `text` of rules.md."""
        qid = f"LANE.1/Q{n}"
        self.console.write("question", {
            "qid": qid, "item": "LANE.1", "text": f"Does {text.split(':')[0]} still hold?", "kind": "single",
            "options": [{"id": "a", "label": "Yes"}, {"id": "b", "label": "No"}], "star": "a",
            "valid_if": [{"kind": "excerpt", "path": "rules.md", "text": text}], "source": "rules.md:1",
            "nonce": f"scanq{n:04d}", **over}, "agent")
        ans = self.console.write("answer", {"qid": qid, "picks": ["a"], "own_text": "", "nonce": f"scana{n:04d}"},
                                 "owner")
        lk = self.console.write("lock", {"qid": qid, "answer": ans["id"], "nonce": f"scanl{n:04d}"}, "owner")
        return qid, lk["id"]

    def setup_rulings(self):
        """Q1 and Q2 stale (their rules were reworded), Q3 holding."""
        url = self.serve()
        (self.cfg.root / "rules.md").write_text(self.RULES)
        self.q1, self.l1 = self.ruling(1, "RULE-ALPHA: every release is reviewed twice.")
        self.q2, self.l2 = self.ruling(2, "RULE-BETA: the console never runs code.")
        self.q3, self.l3 = self.ruling(3, "RULE-DELTA: the owner locks answers.")
        (self.cfg.root / "rules.md").write_text(self.RULES.replace("reviewed twice", "reviewed once")
                                                .replace("never runs code", "may run tests"))
        return url

    def scans(self):
        p = self.cfg.state / "refactor.jsonl"
        return [r for r in (json.loads(x) for x in p.read_text().splitlines()) if r["type"] == "scan"] \
            if p.exists() else []

    def card(self, page, qid):
        return page.locator(f".ck-question[data-qid='{qid}']")

    def test_q40_each_stale_ruling_offers_a_scan_and_pressing_asks_one_that_changes_nothing(self):
        # Catches: the button on a ruling that holds, a press that writes a ruling (withdraw, keep, lock) or
        # anything in store.jsonl, a scan naming a lock the page was not shown, buttons still offered while a
        # scan is open (the second press would only be refused), and a press nobody hears happened.
        for kind in BROWSERS:
            with self.subTest(browser=kind):
                url = self.setup_rulings()
                store_before = self.cfg.store.read_bytes()
                page = self.page(kind, 1280, url)
                self.open_item(page)
                for qid in (self.q1, self.q2):
                    self.assertEqual(self.card(page, qid).locator(".ck-scan-one").count(), 1, qid)
                self.assertEqual(self.card(page, self.q3).locator(".ck-scan-one").count(), 0)
                self.card(page, self.q1).locator(".ck-scan-one").click()
                self.said(page, f"Scan asked for {self.q1}")
                self.assertEqual([(s["qids"], s["locks"], s["by"]) for s in self.scans()],
                                 [([self.q1], [self.l1], "owner")])
                self.assertEqual(self.cfg.store.read_bytes(), store_before)
                page.wait_for_selector(f".ck-scan-status[data-qid='{self.q1}']")
                self.assertIn("waiting on the steward",
                              self.card(page, self.q1).locator(".ck-scan-status").text_content())
                self.assertIn("A scan is open", self.card(page, self.q2).locator(".ck-scan-status").text_content())
                self.assertEqual(page.locator(".ck-scan-one").count(), 0)
                self.assertGreater(self.card(page, self.q1).locator(".ck-stale-banner").count(), 0)   # still stale

    def test_q40_scan_all_is_one_request_naming_every_stale_ruling_once(self):
        # Catches: "Scan all" sent as one request per ruling, a list that misses a stale ruling or names one that
        # holds, a double press queueing a second scan, and a control left on offer once the scan is open.
        for kind in BROWSERS:
            with self.subTest(browser=kind):
                url = self.setup_rulings()
                page = self.page(kind, 1280, url)
                self.open_inbox(page, 1280)
                go = page.locator(".ck-scan-all-go")
                self.assertEqual(go.text_content(), "Scan all stale (2)")
                go.dblclick()
                page.wait_for_function("!document.querySelector('.ck-scan-all-go')")
                self.said(page, "Scan asked for 2 stale rulings")
                self.assertEqual([(s["qids"], s["locks"]) for s in self.scans()],
                                 [([self.q1, self.q2], [self.l1, self.l2])])
                self.assertIn("2 of 2 rulings still wait on the steward",
                              page.locator(".ck-scan-all .ck-scan-status").text_content())

    def test_q40_the_stewards_advice_shows_with_its_evidence_and_changes_nothing(self):
        # Catches: advice shown as a ruling (the ruling withdrawn or kept by the steward's word), advice with no
        # evidence, and a page that hides the owner's own Withdraw and Keep once advice is in.
        for kind in BROWSERS:
            with self.subTest(browser=kind):
                url = self.setup_rulings()
                self.console.advise({"qid": self.q1, "star": "withdraw",
                                     "evidence": "RULE-ALPHA now says reviewed once; the twice-review premise is gone.",
                                     "nonce": "scanadv0001"})
                page = self.page(kind, 1280, url)
                self.open_item(page)
                adv = self.card(page, self.q1).locator(".ck-advice")
                self.assertIn("The steward recommends: ★ Withdraw", adv.text_content())
                self.assertIn("the twice-review premise is gone", adv.text_content())
                self.assertIn("Nothing changes until you press Withdraw", adv.text_content())
                for label in ("Withdraw this ruling", "Keep this ruling and stop checking it"):
                    self.assertEqual(self.card(page, self.q1).locator(f"button[aria-label^='{label}']").count(), 1,
                                     label)
                code, view = self.SV.agent_request(self.cfg.socket, "GET", "/view")
                self.assertEqual(view["view"]["questions"][self.q1]["state"], "stale")
                self.assertNotIn("outcome", view["view"]["questions"][self.q1]["refactor"])

    # -- a stale ruling in the Inbox, wherever it came from ----------------------------------

    def setup_forked_round(self):
        """A deliberation round on LANE.1: Q1 a locked ruling that has gone stale, Q4 still open.

        Q3 is a stale ruling from no round, so the Inbox holds two stale rows on one item.
        """
        url = self.serve()
        (self.cfg.root / "rules.md").write_text(self.RULES)
        f = self.console.write("message", {"item": "LANE.1", "text": "go", "intent": "fork", "mode": "explore",
                                           "nonce": "stalefork01"}, "owner")
        self.fork = f["id"]
        self.q1, self.l1 = self.ruling(1, "RULE-ALPHA: every release is reviewed twice.",
                                       forked_from=self.fork, star_by="panel")
        self.q3, self.l3 = self.ruling(3, "RULE-DELTA: the owner locks answers.")
        self.q4 = "LANE.1/Q4"
        self.console.write("question", {
            "qid": self.q4, "item": "LANE.1", "text": "Which way now?", "kind": "single",
            "options": [{"id": "a", "label": "This"}, {"id": "b", "label": "That"}], "star": "a",
            "valid_if": [], "source": "rules.md:1", "forked_from": self.fork, "star_by": "panel",
            "nonce": "staleopen04"}, "agent")
        (self.cfg.root / "rules.md").write_text(self.RULES.replace("reviewed twice", "reviewed once")
                                                .replace("the owner locks answers", "the owner signs answers"))
        return url

    def inbox_row(self, page, qid):
        return page.locator(f".ck-inbox-item[data-qid='{qid}']")

    def test_a_stale_ruling_from_a_round_is_reachable_from_the_inbox_and_offers_its_proposal(self):
        # Catches: a stale ruling from a deliberation round filed under its round, whose form walks open
        # questions only, so it shows nowhere in the Inbox; a row that does not say which question it is (two
        # stale rows on one item read the same); a row silent about the steward's proposal or advice; and a
        # row that opens somewhere without the stale card's Confirm.
        for kind in BROWSERS:
            with self.subTest(browser=kind):
                url = self.setup_forked_round()
                self.console.propose_anchor({"qid": self.q1, "cites": ["rules.md:2"],
                                             "basis": "RULE-ALPHA now says reviewed once", "nonce": "staleprop01"})
                self.console.advise({"qid": self.q3, "star": "keep", "evidence": "RULE-DELTA still means the same.",
                                     "nonce": "staleadv003"})
                page = self.page(kind, 1280, url)
                self.open_inbox(page, 1280)
                r1, r3 = self.inbox_row(page, self.q1), self.inbox_row(page, self.q3)
                self.assertEqual(r1.count(), 1, "the stale ruling from the round has no Inbox row")
                self.assertEqual(r3.count(), 1)
                self.assertIn("Q1", r1.locator(".ck-inbox-item-qnum").text_content())
                self.assertIn("Q3", r3.locator(".ck-inbox-item-qnum").text_content())
                self.assertNotEqual(r1.text_content(), r3.text_content())
                self.assertIn("a new anchor is proposed", r1.text_content())
                self.assertNotIn("advice", r1.text_content())
                self.assertIn("advice is waiting", r3.text_content())
                self.assertNotIn("proposed", r3.text_content())
                self.assertIn(self.q1, r1.get_attribute("aria-label"))
                r1.click()
                card = self.card(page, self.q1)
                card.wait_for()
                self.assertGreater(card.locator(".ck-stale-banner").count(), 0)
                self.assertEqual(card.locator("button[aria-label^='Confirm the proposed anchor']").count(), 1)

    def test_a_round_with_one_open_and_one_stale_question_still_counts_one_step(self):
        # Catches: the stale ruling counted into its round (the card says 2 questions, or the form walks a
        # locked ruling), and the round dropped from the Inbox because one of its questions went stale.
        for kind in BROWSERS:
            with self.subTest(browser=kind):
                url = self.setup_forked_round()
                page = self.page(kind, 1280, url)
                self.open_inbox(page, 1280)
                rc = page.locator(f".ck-round-card[data-fork='{self.fork}']")
                self.assertEqual(rc.count(), 1, "the round left the Inbox")
                self.assertIn("1 question ·", rc.text_content())
                self.assertEqual(self.inbox_row(page, self.q4).count(), 0, "an open round question listed loose")
                rc.locator("button").click()
                page.wait_for_selector(".ck-round-title")
                self.assertEqual(page.locator(f".ck-panel .ck-question[data-qid='{self.q1}']").count(), 0)

    def test_a_stale_ruling_is_never_grouped_into_an_answer_together_round(self):
        # Catches: a stale ruling and an open question from one named agent offered as "Answer these 2
        # together", whose round would walk only the open one, so the button promises what the form never shows.
        for kind in BROWSERS:
            with self.subTest(browser=kind):
                url = self.serve()
                (self.cfg.root / "rules.md").write_text(self.RULES)
                text = "RULE-ALPHA: every release is reviewed twice."
                self.console.write("question", {
                    "qid": "LANE.1/Q1", "item": "LANE.1", "text": "Does RULE-ALPHA still hold?", "kind": "single",
                    "options": [{"id": "a", "label": "Yes"}, {"id": "b", "label": "No"}], "star": "a",
                    "valid_if": [{"kind": "excerpt", "path": "rules.md", "text": text}], "source": "rules.md:1",
                    "nonce": "grpq000001"}, "agent", agent="steward-a")
                ans = self.console.write("answer", {"qid": "LANE.1/Q1", "picks": ["a"], "own_text": "",
                                                    "nonce": "grpa000001"}, "owner")
                self.console.write("lock", {"qid": "LANE.1/Q1", "answer": ans["id"], "nonce": "grpl000001"}, "owner")
                self.console.write("question", {
                    "qid": "LANE.1/Q2", "item": "LANE.1", "text": "Which way now?", "kind": "single",
                    "options": [{"id": "a", "label": "This"}, {"id": "b", "label": "That"}], "star": "a",
                    "valid_if": [], "source": "rules.md:1", "nonce": "grpq000002"}, "agent", agent="steward-a")
                (self.cfg.root / "rules.md").write_text(self.RULES.replace("reviewed twice", "reviewed once"))
                page = self.page(kind, 1280, url)
                self.open_inbox(page, 1280)
                self.assertEqual(self.inbox_row(page, "LANE.1/Q1").count(), 1)
                self.assertEqual(page.locator(".ck-cluster").count(), 0, "a stale ruling was grouped")


if __name__ == "__main__":
    unittest.main()

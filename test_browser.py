#!/usr/bin/env python3
"""The docked inbox (AB-2/Q2) in a real browser: console.js + console.css injected
into a minimal host page, against a stub /view.

CI has no browser, so this file skips unless CONSOLE_KIT_BROWSER=1 is set; with it
set, a missing Playwright or browser is a failure, never a skip. Run locally:

    CONSOLE_KIT_BROWSER=1 python3 tools/console-kit/test_browser.py
    CONSOLE_KIT_BROWSERS=chromium,firefox CONSOLE_KIT_BROWSER=1 python3 tools/console-kit/test_browser.py
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
from console_kit import publish as P  # noqa: E402

REQUIRED = os.environ.get("CONSOLE_KIT_BROWSER") == "1"
BROWSERS = [b.strip() for b in os.environ.get("CONSOLE_KIT_BROWSERS", "chromium").split(",") if b.strip()]

# A host page with nothing of Gradiance's in it: the kit must bring its own room.
HOST = """<!doctype html><html><head><meta charset="utf-8"><title>host</title>
<style>:root{--c-surface:#fff;--c-surface-alt:#f4f4f4;--c-border:#ccc;--c-fg:#111;
--c-fg-muted:#555;--c-accent:#0057d9;--c-claimed:#7a4b00;--c-claimed-bg:#fff3d6}
body{margin:0;font:14px sans-serif}</style></head><body>
<input id="board-input" aria-label="board input">
<details id="item-LANE.1"><summary>LANE.1 first lane</summary><p>body</p></details>
</body></html>"""


def _view(owner_message: bool = False, second_lane: bool = False) -> dict:
    """/view exactly as the server builds it (server.py: view.build over a store),
    from two questions made by the kit tests' own fixture, so it cannot drift.
    owner_message adds one owner message on LANE.1 with no agent reply, which
    puts that item in awaiting_agent; second_lane adds LANE.2 with one too."""
    import tempfile
    from console_kit import view as V
    from console_kit.store import Store
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
        return {"view": V.build(st, items, lambda c: True), "items": items, "cursor": {}}


VIEW = _view()
VIEW_AGENT = _view(owner_message=True)
VIEW_AGENT2 = _view(owner_message=True, second_lane=True)
assert VIEW_AGENT["view"]["awaiting_agent"] == ["LANE.1"], VIEW_AGENT["view"]["awaiting_agent"]
assert sorted(VIEW_AGENT2["view"]["awaiting_agent"]) == ["LANE.1", "LANE.2"], VIEW_AGENT2["view"]["awaiting_agent"]
assert VIEW["view"]["awaiting_agent"] == [], VIEW["view"]["awaiting_agent"]
PAGE = P.inject(HOST, P.console_block('{"api": "/api"}'))


# The lane board (AB-2/Q4): the REAL page, rendered from the real register, so
# console.js is held to dashboard.apply_live -- the patcher the shape is built on.
# It lives in the project that hosts the kit (CONSOLE_KIT_BOARD_DIR, default the
# layout the kit was born in); without it LiveBoardTests skip, by name.
BOARD_DIR = Path(os.environ.get("CONSOLE_KIT_BOARD_DIR", HERE.parent.parent / "scripts/register"))
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
            payload = dict(VIEW_AGENT2 if _Handler.view_agent == 2 else VIEW_AGENT if _Handler.view_agent else VIEW)
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
            raise unittest.SkipTest("browser tests run only with CONSOLE_KIT_BROWSER=1")
        sp = _playwright()
        if sp is None:
            raise RuntimeError("CONSOLE_KIT_BROWSER=1 but Playwright is not installed")
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
        _Handler.view_listening = None

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
            raise unittest.SkipTest(f"no lane board at {BOARD_DIR} (set CONSOLE_KIT_BOARD_DIR)")
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

    def test_a_different_shape_offers_a_reload_and_stops(self):
        for kind in BROWSERS:
            with self.subTest(browser=kind):
                _Handler.board_hits = 0
                other = {"shape": "0" * 16, "values": dict(BOARD_B["values"])}
                page = self._board_page(kind, "/board-a", other)
                page.wait_for_selector(".ck-board-stale", timeout=5000)
                self.assertEqual(page.evaluate("window.__boardChanged"), 0)  # nothing patched
                self.assertIn("Reload", page.inner_text(".ck-board-stale"))
                hits = _Handler.board_hits
                page.evaluate("window.ConsoleKit.refreshBoard()")
                page.wait_for_timeout(300)
                self.assertEqual(_Handler.board_hits, hits)  # polling stopped

    # Catches: a Reload bar that exists but cannot be seen. Polling stops when it
    # appears, so a covered bar is a stale board with no sign of it. The real
    # lane board has a sticky header at top: 0, z-index 100, which covered a
    # sticky bar once the page scrolled (PR #177 review). Checked scrolled, at
    # the overlay width and at both docked states, by what the browser would
    # actually hit at the button's centre.
    HIT = """() => { const b = document.querySelector('.ck-board-reload'), r = b.getBoundingClientRect();
      const x = r.left + r.width / 2, y = r.top + r.height / 2;
      const bar = document.querySelector('.ck-board-stale'), s = bar.getBoundingClientRect(), my = s.top + s.height / 2;
      const ends = [s.left + 4, s.right - 4].map(px => bar.contains(document.elementFromPoint(px, my)));
      return { inView: r.top >= 0 && r.bottom <= innerHeight && r.left >= 0 && r.right <= innerWidth,
               hit: b.contains(document.elementFromPoint(x, y)), barEnds: ends, scrolled: scrollY }; }"""

    def test_the_reload_offer_is_visible_on_a_scrolled_board(self):
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
    from console_kit import view as V
    from console_kit.store import Store
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

    CONSOLE_KIT_SHOTS=<dir> also saves the open panel at desktop and phone width.
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
                    btns = page.locator("button[aria-label^='Follow up with other seats on']")
                    self.assertEqual(btns.count(), 1)  # Q1 is locked; Q2 is unanswered and offers none
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
                    if os.environ.get("CONSOLE_KIT_SHOTS"):
                        card = page.locator(".ck-question", has=page.locator(".ck-followup"))
                        card.scroll_into_view_if_needed()
                        card.screenshot(path=str(Path(os.environ["CONSOLE_KIT_SHOTS"]) / f"followup-{kind}-{width}.png"))
                    self.assertFalse(page.evaluate("document.documentElement.scrollWidth > innerWidth"))
                    form.get_by_role("button", name="Send follow-up").click()
                    page.wait_for_function("document.querySelectorAll('.ck-followup').length === 0")
                    self.assertEqual(len(posted), 1)
                    body = dict(posted[0])
                    self.assertTrue(body.pop("nonce"))
                    self.assertEqual(body, {"item": "LANE.1", "intent": "fork", "mode": "tighten",
                                            "about_qid": "LANE.1/Q1", "roles": ["devops", "security", "other:Legal"],
                                            "text": "Does B hold on tour?"})



def _stale_view() -> tuple[dict, dict]:
    """/view and /check with LANE.1/Q1 locked on an excerpt whose text then changed, both built by the kit."""
    import tempfile
    from console_kit import anchors as A
    from console_kit import view as V
    from console_kit.store import Store
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
                    if os.environ.get("CONSOLE_KIT_SHOTS"):
                        page.locator(".ck-question").first.screenshot(
                            path=str(Path(os.environ["CONSOLE_KIT_SHOTS"]) / f"why-stale-{kind}-{width}.png"))
                    page.locator("button[aria-label^='Re-lock this answer as it stands']").click()
                    page.get_by_role("button", name="Re-lock as it stands").click()
                    page.wait_for_function("document.querySelectorAll('.ck-confirm').length === 0")
                    self.assertEqual(len(posted), 1)
                    body = dict(posted[0])
                    self.assertTrue(body.pop("nonce"))
                    self.assertEqual(body, {"qid": "LANE.1/Q1"})  # never anchors: the server computes them
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
            raise unittest.SkipTest("browser tests run only with CONSOLE_KIT_BROWSER=1")
        sp = _playwright()
        if sp is None:
            raise RuntimeError("CONSOLE_KIT_BROWSER=1 but Playwright is not installed")
        cls.pw = sp().start()

    @classmethod
    def tearDownClass(cls):
        cls.pw.stop()

    # -- the real server ------------------------------------------------------------

    def serve(self):
        import tempfile
        from console_kit import server as SV
        from test_server import AUD, HOSTNAME, KEY, TEAM, token
        td = tempfile.mkdtemp(prefix="ck-live-")
        self.addCleanup(lambda: __import__("shutil").rmtree(td, ignore_errors=True))
        root = Path(td)
        (root / "page.html").write_text(LIVE_HOST)
        cfg = SV.Config(root=root, page=root / "page.html", state=root / "state", adapter=root / "unused.py",
                        team_domain=TEAM, aud=AUD, hostname=HOSTNAME, port=0, project="live-test")
        console = SV.Console(cfg, _LiveAdapter())
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
                    if os.environ.get("CONSOLE_KIT_SHOTS"):
                        page.locator(".ck-panel").screenshot(
                            path=str(Path(os.environ["CONSOLE_KIT_SHOTS"]) / f"round-step-{kind}-{width}.png"))
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
                    if os.environ.get("CONSOLE_KIT_SHOTS"):
                        page.locator(".ck-panel").screenshot(
                            path=str(Path(os.environ["CONSOLE_KIT_SHOTS"]) / f"round-review-{kind}-{width}.png"))
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
        from console_kit import doorbell as D
        for kind in BROWSERS:
            for width in (1280, 375):
                with self.subTest(browser=kind, width=width):
                    url = self.serve()
                    page = self.page(kind, width, url)
                    self.open_inbox(page, width)
                    page.click("#ck-tab-chat")
                    page.fill("#ck-chat-input", "Is the gradiance build green?")
                    page.keyboard.press("Enter")
                    page.wait_for_selector(".ck-chat-msg[data-by='owner']")
                    woke = D.watch(self.cfg.inbox, 0, poll=0.05, timeout=5)
                    self.assertEqual([(w["intent"], w["item"]) for w in woke], [("chat", "@chat")])
                    msg = [r for r in self.console.store.records() if r["type"] == "message"][-1]
                    self.assertEqual((msg["by"], msg["intent"], msg["text"]),
                                     ("owner", "chat", "Is the gradiance build green?"))
                    self.agent_post("/message", {"item": "@chat", "text": "Green at abc123.", "reply_to": msg["id"],
                                                 "nonce": "chatreply01"})
                    page.wait_for_selector(".ck-chat-msg[data-by='agent']", timeout=10000)
                    self.assertIn("Green at abc123.", page.locator(".ck-chat-msg[data-by='agent']").text_content())
                    page.wait_for_function("!document.querySelector('#ck-tab-chat .ck-tab-note')")  # no longer waiting
                    self.assertFalse(page.evaluate(OVERFLOW))
                    if os.environ.get("CONSOLE_KIT_SHOTS"):
                        page.locator(".ck-panel").screenshot(
                            path=str(Path(os.environ["CONSOLE_KIT_SHOTS"]) / f"chat-{kind}-{width}.png"))
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


if __name__ == "__main__":
    unittest.main()

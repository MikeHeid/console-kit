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
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
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


def _view(owner_message: bool = False) -> dict:
    """/view exactly as the server builds it (server.py: view.build over a store),
    from two questions made by the kit tests' own fixture, so it cannot drift.
    owner_message adds one owner message on LANE.1 with no agent reply, which
    puts that item in awaiting_agent."""
    import tempfile
    from console_kit import view as V
    from console_kit.store import Store
    from test_kit import message, question
    items = {"LANE.1": {"title": "first lane", "parent": None}}
    with tempfile.TemporaryDirectory() as td:
        st = Store(Path(td) / "store.jsonl", known_items=items)
        st.append(question("LANE.1/Q1"))
        st.append(question("LANE.1/Q2"))
        if owner_message:
            st.append(message(item="LANE.1"))
        return {"view": V.build(st, items, lambda c: True), "items": items, "cursor": {}}


VIEW = _view()
VIEW_AGENT = _view(owner_message=True)
assert VIEW_AGENT["view"]["awaiting_agent"] == ["LANE.1"], VIEW_AGENT["view"]["awaiting_agent"]
assert VIEW["view"]["awaiting_agent"] == [], VIEW["view"]["awaiting_agent"]
PAGE = P.inject(HOST, P.console_block('{"api": "/api"}'))


# The lane board (AB-2/Q4): the REAL page, rendered from the real register, so
# console.js is held to dashboard.apply_live -- the patcher the shape is built on.
sys.path.insert(0, str(HERE.parent.parent / "scripts/register"))
import dashboard as D  # noqa: E402

REGISTER = D.load()
# open -> proposed moves no "claimed by" line and no "waits on" link: the shape holds.
FLIPPED = {i: ({**d, "status": "proposed"} if d["status"] == "open" and n % 7 == 0 else d)
           for n, (i, d) in enumerate(sorted(REGISTER.items()))}
BOARD_A, BOARD_B = D.board_live(REGISTER), D.board_live(FLIPPED)
LIVE_PAGES = {"/board-a": P.inject(D.render_html(REGISTER)[0], P.console_block('{"api": "/api"}')),
              "/board-b": P.inject(D.render_html(FLIPPED)[0], P.console_block('{"api": "/api"}'))}


class _Handler(BaseHTTPRequestHandler):
    view_delay = 0.0   # seconds before /view answers (a slow server)
    view_fails = False  # /view answers 503 (a server that is down)
    view_agent = False  # /view carries one thread waiting on the agent
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
            body, ctype = json.dumps(VIEW_AGENT if _Handler.view_agent else VIEW), "application/json"
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
        _Handler.view_delay, _Handler.view_fails, _Handler.view_agent = 0.0, False, False

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

    def test_no_agent_count_when_nothing_waits_on_the_agent(self):
        for kind in BROWSERS:
            for width, sel in ((1400, ".ck-dock-strip"), (800, ".ck-inbox-btn")):
                with self.subTest(browser=kind, width=width):
                    page = self._page(kind, width)
                    got = page.evaluate(self.AGENT, sel)
                    self.assertEqual((got["owner"], got["agent"], got["agentShown"]), ("2", "", False))
                    self.assertEqual(got["label"], "Open inbox, 2 waiting for you")


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
    setUpClass = DockTests.__dict__["setUpClass"]
    tearDownClass = DockTests.__dict__["tearDownClass"]

    def setUp(self):
        _Handler.view_delay, _Handler.view_fails, _Handler.view_agent = 0.0, False, False
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


if __name__ == "__main__":
    unittest.main()

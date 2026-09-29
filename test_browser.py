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


def _view() -> dict:
    """/view exactly as the server builds it (server.py: view.build over a store),
    from two questions made by the kit tests' own fixture, so it cannot drift."""
    import tempfile
    from console_kit import view as V
    from console_kit.store import Store
    from test_kit import question
    items = {"LANE.1": {"title": "first lane", "parent": None}}
    with tempfile.TemporaryDirectory() as td:
        st = Store(Path(td) / "store.jsonl", known_items=items)
        st.append(question("LANE.1/Q1"))
        st.append(question("LANE.1/Q2"))
        return {"view": V.build(st, items, lambda c: True), "items": items, "cursor": {}}


VIEW = _view()
PAGE = P.inject(HOST, P.console_block('{"api": "/api"}'))


class _Handler(BaseHTTPRequestHandler):
    view_delay = 0.0   # seconds before /view answers (a slow server)
    view_fails = False  # /view answers 503 (a server that is down)

    def log_message(self, *args):
        pass

    def do_GET(self):
        status = 200
        if self.path.startswith("/api/view"):
            import time
            time.sleep(_Handler.view_delay)
            status = 503 if _Handler.view_fails else 200
            body, ctype = json.dumps(VIEW), "application/json"
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
        _Handler.view_delay, _Handler.view_fails = 0.0, False

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
                self.assertEqual(s["panel"], [1020, 1400], s)         # one click: a 380px column
                self.assertEqual((s["modal"], s["backdrop"]), ("false", False), s)
                self.assertEqual((s["role"], s["inert"]), ("complementary", False), s)  # a landmark
                self.assertEqual((s["strip"], s["expanded"], s["bodyMargin"]), (None, "true", "380px"), s)
                self.assertTrue(s["focusInPanel"], s)
                self.assertFalse(s["overflow"], s)

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
                self.assertEqual((s["panel"], s["modal"], s["backdrop"]), ([1020, 1400], "false", False), s)
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
                self.assertEqual((s["modal"], s["backdrop"], s["panel"]), ("false", False, [1020, 1400]), s)


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


if __name__ == "__main__":
    unittest.main()

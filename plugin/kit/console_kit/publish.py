"""Inject the console into a rendered page, and prove that the injection strips out cleanly (spec R8).

The host page (a project's committed dashboard, say) stays a pure render,
which the project may gate byte for byte. The console exists only in the page the server
serves: `inject` inserts one marked block before `</body>`, and `strip`
removes exactly that block. `check` proves that `strip(inject(page)) == page`,
so nothing the console needs can leak into the gated file.

    python3 tools/console-kit/publish.py check docs/dashboard/index.html
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BEGIN = "<!-- BEGIN console-kit (tools/console-kit/publish.py) -->"
END = "<!-- END console-kit -->"


class PublishError(Exception):
    pass


def console_block(config_json: str) -> str:
    """Build the block the server injects. The config is JSON the page reads, never HTML."""
    css = (HERE / "console.css").read_text(encoding="utf-8")
    js = (HERE / "console.js").read_text(encoding="utf-8")
    for name, text in (("console.css", css), ("console.js", js)):
        if BEGIN in text or END in text or "</script" in text.lower() or "</style" in text.lower():
            raise PublishError(f"{name} contains a marker or a closing tag; it would break out of its block")
    safe_cfg = config_json.replace("<", "\\u003c")
    return (f"{BEGIN}\n<style>\n{css}</style>\n"
            f'<script type="application/json" id="console-kit-config">{safe_cfg}</script>\n'
            f"<script>\n{js}</script>\n{END}\n")


def inject(page: str, block: str) -> str:
    if BEGIN in page or END in page:
        raise PublishError("the page already carries a console block; inject into the committed render only")
    if page.count("</body>") != 1:
        raise PublishError(f"the page has {page.count('</body>')} </body> tags; expected exactly one")
    return page.replace("</body>", block + "</body>", 1)


def strip(page: str) -> str:
    b, e = page.find(BEGIN), page.find(END)
    if b == -1 and e == -1:
        return page
    if b == -1 or e == -1 or e < b or page.count(BEGIN) != 1 or page.count(END) != 1:
        raise PublishError("console markers are unpaired or repeated")
    return page[:b] + page[e + len(END) + 1:]


def check(committed: str, block: str) -> list[str]:
    """Return every problem with serving `committed` with the console; an empty list means it is clean."""
    problems = []
    if BEGIN in committed or END in committed:
        problems.append("the committed page carries a console marker; the injection leaked into the gated file")
        return problems
    served = inject(committed, block)
    if strip(served) != committed:
        problems.append("strip(inject(page)) differs from the page; the injection is not cleanly removable")
    return problems


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2 or args[0] != "check":
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    page = Path(args[1]).read_text(encoding="utf-8")
    try:
        problems = check(page, console_block("{}"))
    except PublishError as err:
        problems = [str(err)]
    for p in problems:
        print(f"publish check: {p}", file=sys.stderr)
    if not problems:
        print(f"ok: {args[1]} injects and strips back byte for byte")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

"""Scaffold a dashboard page and a live `board()` for a project.

0.9.3. The console serves whatever `page-snapshot` sent (the HTML page frozen
at a commit) and, beside it, the latest values from the adapter's `board()`
method as data: `view.board = {"shape": str, "values": {str: str}}`. A page
whose elements carry `data-live-shape`, `data-live`, `data-live-title`,
`data-live-width` and `data-live-status` attributes refreshes those elements
from `view.board` without a new snapshot ("live" = as fresh as the last
`items-push`).

This module writes a working dashboard page the project can edit and injects a
matching `board()` into the project's adapter. Re-running adds missing
sections without clobbering edits: every section wraps between

    <!-- scaffold:<name> start -->
    ...
    <!-- scaffold:<name> end -->

and a section whose markers are already present is left alone.

The scaffold does NOT touch items, specs or the project's own records. It only
writes the page and, when `board()` is missing, patches the adapter.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

# Sections the first-run template includes. Order defines the page's order.
SECTIONS = ("header", "items", "rollout", "engine", "spec", "footer")
BOARD_SHAPE = "overture/dashboard/1"
MARK_START = "<!-- scaffold:{name} start -->"
MARK_END = "<!-- scaffold:{name} end -->"
BOARD_MARK = "# scaffold:board start"
BOARD_END = "# scaffold:board end"
BOARD_SHAPE_KEY = "shape"


@dataclass(frozen=True)
class ScaffoldPlan:
    """What `prepare()` would write; `apply()` acts on it. Paths are absolute."""
    project_root: Path
    page_path: Path
    adapter_path: Path
    page_write: str | None            # new page body, or None when every section is already present
    adapter_write: str | None         # new adapter body, or None when board() is already defined
    added_sections: tuple[str, ...]   # section names newly introduced on this run
    skipped_sections: tuple[str, ...] # section names left alone because the markers exist


class ScaffoldError(ValueError):
    """A refusal the operator fixes; carries no project secret."""


def prepare(project_root: str | Path, page_path: str | Path, adapter_path: str | Path,
            project_name: str, sections: Iterable[str] = SECTIONS) -> ScaffoldPlan:
    """Compute what writes would happen without touching the filesystem.

    The caller runs `apply(plan)` to commit, or prints the plan for the operator to review.
    """
    root = Path(project_root).resolve()
    page = (root / page_path).resolve()
    adapter = (root / adapter_path).resolve()
    if root not in page.parents:
        raise ScaffoldError(f"{page} is not inside {root}")
    if root not in adapter.parents:
        raise ScaffoldError(f"{adapter} is not inside {root}")
    wanted = tuple(s for s in sections if s in SECTIONS)
    if not wanted:
        raise ScaffoldError(f"no known sections to write; known: {', '.join(SECTIONS)}")

    page_text = page.read_text(encoding="utf-8") if page.exists() else ""
    adapter_text = adapter.read_text(encoding="utf-8") if adapter.exists() else ""

    page_write, added, skipped = _page_write(page_text, project_name, wanted)
    adapter_write = _adapter_write(adapter_text)

    return ScaffoldPlan(
        project_root=root,
        page_path=page,
        adapter_path=adapter,
        page_write=page_write,
        adapter_write=adapter_write,
        added_sections=added,
        skipped_sections=skipped,
    )


def apply(plan: ScaffoldPlan) -> list[str]:
    """Write the plan's files; return one line per file touched (absent files are created)."""
    out: list[str] = []
    if plan.page_write is not None:
        plan.page_path.parent.mkdir(parents=True, exist_ok=True)
        plan.page_path.write_text(plan.page_write, encoding="utf-8")
        out.append(f"{plan.page_path.relative_to(plan.project_root).as_posix()}: "
                   f"wrote {', '.join(plan.added_sections) or '(no new sections)'}")
    if plan.adapter_write is not None:
        plan.adapter_path.parent.mkdir(parents=True, exist_ok=True)
        plan.adapter_path.write_text(plan.adapter_write, encoding="utf-8")
        out.append(f"{plan.adapter_path.relative_to(plan.project_root).as_posix()}: added board()")
    return out


# -- page body -------------------------------------------------------------------------

def _page_write(current: str, project_name: str, wanted: tuple[str, ...]) -> tuple[str | None, tuple[str, ...], tuple[str, ...]]:
    """Return (new page text or None, newly-added sections, sections left alone).

    None means every requested section already has its markers; nothing to do.
    """
    present = _present_sections(current)
    missing = tuple(s for s in wanted if s not in present)
    skipped = tuple(s for s in wanted if s in present)
    needs_shape = _needs_body_shape(current)
    if not missing and current.strip() and not needs_shape:
        return None, (), skipped
    if not current.strip():
        return _new_page(project_name, wanted), wanted, ()
    # Append new sections before </body> or at the end.
    additions = "\n".join(_section_block(s, project_name) for s in missing)
    needle = "</body>"
    if needle in current:
        new = current.replace(needle, additions + "\n" + needle, 1) if additions else current
    else:
        new = (current.rstrip() + "\n" + additions + "\n") if additions else current
    # 1.11.0: a scaffolded page MUST carry `<body data-live-shape="…">` for the live loop to find
    # it. If a project added sections to a hand-made page that lacks the attribute, add it now.
    if needs_shape:
        new = _add_body_shape(new)
    return new, missing, skipped


_BODY_TAG = re.compile(r"(<body\b)([^>]*)>", re.IGNORECASE)


def _needs_body_shape(page: str) -> bool:
    """True when the page has a `<body>` tag but no `data-live-shape` attribute on it."""
    m = _BODY_TAG.search(page)
    if m is None:
        return False
    return "data-live-shape" not in m.group(2).lower()


def _add_body_shape(page: str) -> str:
    """Insert `data-live-shape="{BOARD_SHAPE}"` into the `<body>` tag. No-op when already present or absent."""
    def _rewrite(m: re.Match) -> str:
        attrs = m.group(2)
        if "data-live-shape" in attrs.lower():
            return m.group(0)
        # Always a space between `<body` and the new attribute; keep remaining attrs verbatim
        # (they already start with their own whitespace when non-empty).
        return f'{m.group(1)} data-live-shape="{BOARD_SHAPE}"{attrs}>'
    return _BODY_TAG.sub(_rewrite, page, count=1)


def _present_sections(page: str) -> set[str]:
    return set(re.findall(r"scaffold:([a-z]+) start", page))


def _new_page(project_name: str, sections: tuple[str, ...]) -> str:
    body = "\n".join(_section_block(s, project_name) for s in sections)
    safe_name = (project_name or "console").replace("<", "&lt;").replace(">", "&gt;").replace("&", "&amp;")
    return (
        "<!doctype html>\n"
        '<html lang="en">\n'
        '<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">\n'
        f"<title>{safe_name} — console</title>\n"
        "<style>\n"
        "  :root { --fg:#111; --bg:#fafbfc; --muted:#5a5f66; --accent:#1f4e8c; --border:#dde0e4; }\n"
        "  html, body { margin: 0; background: var(--bg); color: var(--fg);\n"
        "    font: 14px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif; }\n"
        "  body { padding: 20px; max-width: 1100px; margin: 0 auto; }\n"
        "  h1 { font-size: 22px; margin: 0 0 4px; }\n"
        "  h2 { font-size: 16px; margin: 24px 0 8px; color: var(--accent); }\n"
        "  .scaf-grid { display: grid; grid-template-columns: repeat(auto-fit,minmax(220px,1fr));\n"
        "    gap: 10px; margin: 8px 0; }\n"
        "  .scaf-card { background: #fff; border: 1px solid var(--border); border-radius: 6px;\n"
        "    padding: 10px 12px; }\n"
        "  .scaf-card h3 { margin: 0 0 2px; font-size: 13px; color: var(--muted); font-weight: 600; }\n"
        "  .scaf-value { font-size: 20px; font-weight: 600; }\n"
        "  .scaf-muted { color: var(--muted); font-size: 13px; }\n"
        "  .scaf-items-tree { background: #f6f8fa; padding: 10px 12px; border-radius: 6px;\n"
        "    font: 12.5px/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;\n"
        "    white-space: pre; overflow: auto; max-height: 420px; }\n"
        "  footer { color: var(--muted); font-size: 12px; margin-top: 28px; }\n"
        "</style>\n"
        "</head>\n"
        '<body data-live-shape="' + BOARD_SHAPE + '">\n'
        + body +
        "\n</body>\n</html>\n"
    )


def _section_block(name: str, project_name: str) -> str:
    start = MARK_START.format(name=name)
    end = MARK_END.format(name=name)
    inner = _section_inner(name, project_name)
    return f"{start}\n{inner}\n{end}"


def _section_inner(name: str, project_name: str) -> str:
    if name == "header":
        safe = (project_name or "console").replace("<", "&lt;").replace(">", "&gt;")
        return (
            f"<h1>{safe}</h1>\n"
            '<p class="scaf-muted" data-live="headline" data-live-title="headline">'
            "What the owner needs to see today.</p>"
        )
    if name == "rollout":
        return (
            '<h2>Rollout</h2>\n'
            '<div class="scaf-grid">\n'
            '  <div class="scaf-card"><h3>Open questions</h3>'
            '<div class="scaf-value" data-live="open_questions">—</div></div>\n'
            '  <div class="scaf-card"><h3>Open PRs</h3>'
            '<div class="scaf-value" data-live="open_prs">—</div></div>\n'
            '  <div class="scaf-card"><h3>Last fold</h3>'
            '<div class="scaf-value" data-live="last_fold">—</div></div>\n'
            '  <div class="scaf-card"><h3>Status</h3>'
            '<div class="scaf-value" data-live="status" data-live-status="status">—</div></div>\n'
            '</div>'
        )
    if name == "engine":
        return (
            '<h2>Engine</h2>\n'
            '<p class="scaf-muted">What this project does, who it serves, and how it stays honest. '
            'Fill this in by hand; the scaffold does not overwrite it on re-runs.</p>'
        )
    if name == "spec":
        return (
            '<h2>Spec</h2>\n'
            '<p class="scaf-muted" data-live="spec_summary">—</p>\n'
            '<ul data-live="spec_links" data-live-width="40"><li class="scaf-muted">'
            'Links to the spec documents the agents read. Point at files under `specs_dir`.</li></ul>'
        )
    if name == "items":
        # 1.9.0: `items_tree` is a kit-computed live value — one <pre> is enough. The old
        # `items_rows` HTML table (and the second `data-live-shape` under it) relied on inner-HTML
        # replacement that `data-live` cannot do; the plain-text tree works everywhere.
        return (
            '<h2>Items</h2>\n'
            '<p class="scaf-muted">Numbered, nested by parent. Overture keeps it current '
            'on every <code>items-push</code>.</p>\n'
            '<pre class="scaf-items-tree" data-live="items_tree">The items tree fills in '
            'after the first <code>items-push</code>.</pre>'
        )
    if name == "footer":
        return (
            '<footer>\n'
            '  <span data-live="footer_note">Served by overture.</span>\n'
            '</footer>'
        )
    return f"<!-- scaffold: unknown section {name} -->"


# -- adapter patch ---------------------------------------------------------------------

_BOARD_SNIPPET = '''

{mark}
def board() -> dict:
    """Live values for a scaffolded dashboard page.

    Values appear in the page where its elements carry `data-live="..."`. The shape key
    pins this adapter to a scaffold version so a page template can refuse an older adapter.
    Edit the counters below (and add more) as the project evolves. The console reads this
    on every `items-push`; see `agent.py items-watch` for a loop that re-pushes on change.
    """
    import datetime as _dt
    counts = items()
    open_items = sum(1 for v in counts.values() if (v.get("status") or "open").lower() == "open")
    return {{
        "shape": "{shape}",
        "values": {{
            "headline": f"{{open_items}} open items.",
            "open_questions": "—",   # items-push re-reads this; wire up to your own counter when ready
            "open_prs": "—",
            "last_fold": "—",
            "status": "ok" if open_items else "quiet",
            "spec_summary": "No specs linked yet.",
            "footer_note": "Updated " + _dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z"),
        }},
    }}
{end}
'''


def _adapter_write(current: str) -> str | None:
    if not current.strip():
        raise ScaffoldError("the adapter file is empty; onboard the project first "
                            "(onboard.py writes a working adapter_template.py).")
    if re.search(r"^def\s+board\s*\(", current, re.M):
        return None
    snippet = _BOARD_SNIPPET.format(mark=BOARD_MARK, end=BOARD_END, shape=BOARD_SHAPE)
    return current.rstrip() + "\n" + snippet


# -- items tree sync -------------------------------------------------------------------
# Insert a <details id="item-X" data-ck-item="X"> for every item not already on the page, nesting children
# under their parent. Per-item markers (ck:item X start/end) let a re-sync preserve hand-edits inside each
# node AND nest a late-arriving child under its existing parent.

ITEMS_START = "<!-- ck:items start -->"
ITEMS_END = "<!-- ck:items end -->"
_ITEM_MARK = re.compile(r"<!--\s*ck:item\s+([A-Za-z0-9][A-Za-z0-9_.\-]{0,127})\s+start\s*-->")
_SECTION_MARK = re.compile(r'<!--\s*ck:section\s+([A-Za-z0-9][A-Za-z0-9/._-]{0,255})\s+start\s*-->')


def sync_items_block(page_text: str, items: dict) -> tuple[str, list[str]]:
    """Return (new page text, list of newly inserted item ids). Idempotent.

    `items` is {item_id: {"title": str, "parent": id|None, "section": "wave/phase/lane"|None, ...}}.

    An item with a `section` is nested under a chain of `<details data-ck-section="…">` blocks: one per
    path segment, created in order so the full "wave-1 → phase-2 → lane-ui" tree is visible on the page
    as items arrive. Missing segments are created the first time any item below them lands; later items
    in the same section slot straight into the existing node.

    An item with a `parent` (and no section, or a section matching its parent's) still nests under its
    parent's `<details>`. Items with neither land at the top of the ck:items block.
    """
    s, e = page_text.find(ITEMS_START), page_text.find(ITEMS_END)
    if s == -1 or e == -1 or e < s:
        page_text = _insert_items_block(page_text)
        s, e = page_text.find(ITEMS_START), page_text.find(ITEMS_END)
    present_items = set(_ITEM_MARK.findall(page_text[s:e]))
    present_sections = set(_SECTION_MARK.findall(page_text[s:e]))
    # Order: parents before children; among roots, shallower-section before deeper-section. Each insert
    # then sees its parent/section already materialised.
    missing = [k for k in items if k not in present_items]
    missing.sort(key=lambda k: (
        _depth(k, items),
        _section_depth(items[k] or {}),
    ))
    added: list[str] = []
    for item_id in missing:
        data = items[item_id] or {}
        page_text = _ensure_section_path(page_text, data.get("section"), present_sections)
        page_text = _insert_item(page_text, item_id, data, items, present_items, present_sections)
        present_items.add(item_id)
        added.append(item_id)
    return page_text, added


def _section_depth(data: dict) -> int:
    sec = data.get("section")
    return len(sec.split("/")) if isinstance(sec, str) and sec else 0


def _ensure_section_path(page: str, section: str | None, present_sections: set) -> str:
    """Materialise every path segment of `section` as nested <details>, if not already there."""
    if not section:
        return page
    segs = section.split("/")
    for i in range(1, len(segs) + 1):
        slug = "/".join(segs[:i])
        if slug in present_sections:
            continue
        page = _insert_section(page, slug, "/".join(segs[: i - 1]) or None, present_sections)
        present_sections.add(slug)
    return page


def _section_html(slug: str) -> str:
    label = slug.split("/")[-1].replace("-", " ").replace("_", " ")
    return (
        f"<!-- ck:section {slug} start -->\n"
        f'<details class="ck-section" data-ck-section="{slug}" open>\n'
        f'  <summary><span class="ck-section-label">{label}</span>'
        f'  <code class="ck-section-slug">{slug}</code></summary>\n'
        f"  <!-- ck:section-children {slug} -->\n"
        f"</details>\n"
        f"<!-- ck:section {slug} end -->\n"
    )


def _insert_section(page: str, slug: str, parent_slug: str | None, present_sections: set) -> str:
    html = _section_html(slug)
    if parent_slug and parent_slug in present_sections:
        hole = f"<!-- ck:section-children {parent_slug} -->"
        at = page.find(hole)
        if at != -1:
            return page[:at] + html + page[at:]
    at = page.find(ITEMS_END)
    return page[:at] + html + page[at:]


def _depth(item_id: str, items: dict, seen: tuple = ()) -> int:
    if item_id in seen:
        return 0
    parent = (items.get(item_id) or {}).get("parent")
    if not parent or parent not in items:
        return 0
    return 1 + _depth(parent, items, seen + (item_id,))


def _node_html(item_id: str, data: dict) -> str:
    title = (data.get("title") or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    # The ck:children marker inside each parent's <details> body is where children's blocks get inserted,
    # so a child really nests inside its parent's details element. Hand edits around it survive re-syncs.
    return (
        f"<!-- ck:item {item_id} start -->\n"
        f'<details id="item-{item_id}" data-ck-item="{item_id}">\n'
        f'  <summary><code>{item_id}</code> — {title}</summary>\n'
        f"  <!-- ck:children {item_id} -->\n"
        f"</details>\n"
        f"<!-- ck:item {item_id} end -->\n"
    )


def _insert_item(page: str, item_id: str, data: dict, items: dict,
                 present_items: set, present_sections: set) -> str:
    html = _node_html(item_id, data)
    # 1.4.0: parent still wins when the parent is already on the page. Otherwise, drop into the deepest
    # existing section slot. Fall back to the top of the items block.
    parent = data.get("parent")
    if parent and parent in present_items:
        hole = f"<!-- ck:children {parent} -->"
        at = page.find(hole)
        if at != -1:
            return page[:at] + html + page[at:]
    section = data.get("section")
    if section and section in present_sections:
        hole = f"<!-- ck:section-children {section} -->"
        at = page.find(hole)
        if at != -1:
            return page[:at] + html + page[at:]
    at = page.find(ITEMS_END)
    return page[:at] + html + page[at:]


def _insert_items_block(page: str) -> str:
    block = f"\n{ITEMS_START}\n{ITEMS_END}\n"
    at = page.find("</body>")
    if at == -1:
        return page.rstrip() + block
    return page[:at] + block + page[at:]

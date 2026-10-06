"""Build a Mermaid status flowchart for one item from the view's own data.

0.8.19. The console renders this inside the same sandboxed iframe used for
agent-drawn visuals: it is Mermaid source, no HTML, no scripts, produced from
`view.questions`, `view.forks` and the item register. Nothing in the project
is read; nothing is written anywhere.

Nodes:
- the item itself (one, at the top-left)
- every question that lives on the item, labelled with its number, state and
  a short slice of its text
- every round (fork message with `intent == "fork"`) on the item or on its
  descendants, labelled with its kind and when it was asked

Edges:
- item -> each question not inside a round
- round -> each of its questions (`forked_from`)
- older question --> newer question when the newer one supersedes it
  (`supersedes`: the newer answer displaces the older ruling)
- older round --> newer round when the newer round follows up on it
  (`follow_up_of`)
- item -> each round

A cap keeps the picture readable: at most MAX_NODES question nodes make the
chart; past that an ellipsis node names how many more exist.
"""

from __future__ import annotations

import re
from typing import Iterable, Mapping

MAX_NODES = 40
MAX_LABEL = 56


# Shades picked to work under both light and dark backgrounds in the console's
# sandboxed iframe (which paints its own white body). Each state has a distinct
# fill AND a distinct stroke, so a viewer who cannot read colour can still tell
# them apart by border.
STATE_CLASS = {
    "awaiting_you": "awaiting",
    "unlocked": "unlocked",
    "stale": "stale",
    "locked": "locked",
    "superseded": "superseded",
    "withdrawn": "withdrawn",
}
STATE_PREFIX = {
    "awaiting_you": "? ",
    "unlocked": "~ ",
    "stale": "! ",
    "locked": "o ",
    "superseded": "x ",
    "withdrawn": "x ",
}

_UNSAFE = re.compile(r'[\x00-\x1f"`\\]')


def _safe(text: str, limit: int = MAX_LABEL) -> str:
    """A one-line label for a Mermaid node: no control chars, no backticks, quotes replaced; capped."""
    s = _UNSAFE.sub(" ", (text or "").strip())
    s = " ".join(s.split())
    if len(s) > limit:
        s = s[: limit - 1] + "…"
    return s


def _qnum(qid: str) -> str:
    """A short label for a question id, like 'Q7', else the id itself."""
    if "/Q" in qid:
        tail = qid.rsplit("/Q", 1)[1]
        return "Q" + tail
    return qid


def _node_id(prefix: str, raw: str) -> str:
    """A Mermaid-safe node id: letters, digits and underscores only."""
    return prefix + re.sub(r"[^A-Za-z0-9]", "_", raw)[:48]


def build(view: Mapping, items: Mapping[str, Mapping], item: str, clickable: bool = False) -> str:
    """A Mermaid flowchart (LR) for `item`, as source text.

    Raises KeyError when `item` is not in `items`. When `clickable` is True, every question, round and the
    item itself get a `click` directive that calls `window.ckClick(kind, target)` in the iframe; the parent
    turns that message into a scroll to the question card or an item open.
    """
    if item not in items:
        raise KeyError(f"no item {item!r} in the item register")

    item_data = items[item] or {}
    title = _safe(item_data.get("title") or "", MAX_LABEL)

    questions = _questions_on(view, item)
    forks = _forks_on(view, item)

    lines: list[str] = ["flowchart LR"]
    lines.extend(_classdefs())

    # Root item node.
    item_node = _node_id("I_", item)
    item_label = _safe(item, 48) + ("<br/>" + title if title else "")
    lines.append(f'  {item_node}(["{item_label}"]):::item')
    if clickable:
        lines.append(f'  click {item_node} call ckClick("item", "{_js(item)}")')

    # Fork (round) nodes.
    fork_nodes: dict[str, str] = {}
    for fid, f in forks.items():
        msg = f.get("message") or {}
        kind = f.get("kind") or msg.get("intent") or "round"
        when = _rel_time(msg.get("ts") or "")
        label = f"round / {_safe(str(kind), 24)}"
        if when:
            label += "<br/>" + _safe(when, 24)
        node_id = _node_id("F_", fid)
        fork_nodes[fid] = node_id
        lines.append(f'  {node_id}{{{{"{label}"}}}}:::round')
        lines.append(f"  {item_node} --> {node_id}")
        if clickable:
            lines.append(f'  click {node_id} call ckClick("fork", "{_js(fid)}")')

    # Question nodes, capped. "In cluster" means it already hangs off a round.
    kept = list(questions.values())[:MAX_NODES]
    skipped = len(questions) - len(kept)
    question_nodes: dict[str, str] = {}
    for q in kept:
        qd = q["question"]
        qid = qd["qid"]
        state = q.get("state") or "unlocked"
        cls = STATE_CLASS.get(state, "unlocked")
        prefix = STATE_PREFIX.get(state, "")
        head = prefix + _qnum(qid) + " " + state.replace("_", " ")
        text = _safe(qd.get("text") or "", MAX_LABEL)
        label = head + ("<br/>" + text if text else "")
        node_id = _node_id("Q_", qid)
        question_nodes[qid] = node_id
        lines.append(f'  {node_id}["{label}"]:::{cls}')
        fid = qd.get("forked_from")
        parent = fork_nodes.get(fid) if fid else None
        lines.append(f"  {parent or item_node} --> {node_id}")
        if clickable:
            lines.append(f'  click {node_id} call ckClick("question", "{_js(qid)}")')

    if skipped > 0:
        more_id = _node_id("MORE_", item)
        lines.append(f'  {more_id}(["… and {skipped} more"]):::muted')
        lines.append(f"  {item_node} --> {more_id}")

    # Supersedes edges: an answer on question B that supersedes question A's lock.
    for q in kept:
        qd = q["question"]
        other = qd.get("supersedes") or qd.get("replaces")
        if isinstance(other, str) and other in question_nodes and qd["qid"] in question_nodes:
            lines.append(f"  {question_nodes[other]} -. supersedes .-> {question_nodes[qd['qid']]}")

    # Follow-up round edges: a round that follows an earlier round.
    for fid, f in forks.items():
        msg = f.get("message") or {}
        prev = msg.get("follow_up_of")
        if isinstance(prev, str) and prev in fork_nodes and fid in fork_nodes:
            lines.append(f"  {fork_nodes[prev]} -. follow-up .-> {fork_nodes[fid]}")

    return "\n".join(lines) + "\n"


# -- whole-project chart ----------------------------------------------------------------

PROJECT_MAX_NODES = 120   # items on one project chart; past this a group is shown as "+N more under X"


def build_project(view: Mapping, items: Mapping[str, Mapping], *, clickable: bool = False,
                  state_filter: str | None = None) -> str:
    """A Mermaid flowchart (TD) of every item in the project, parent -> child, coloured by status roll-up.

    The roll-up on an item uses its own questions only (not its descendants'): awaiting_you dominates,
    then stale, then unlocked, then locked, then nothing. Clicking an item node opens it in the console.

    `state_filter` keeps items whose own roll-up equals the filter, plus every ancestor on the way to the
    root (so the tree stays connected); ancestors that do not themselves match are drawn muted. `None` or
    an unknown value disables the filter and shows every item.
    """
    if not items:
        return "flowchart TD\n" + "\n".join(_classdefs()) + '\n  EMPTY(["No items yet"]):::muted\n'

    # Group each item's questions by state.
    by_item: dict[str, dict[str, int]] = {k: {} for k in items}
    for q in (view.get("questions") or {}).values():
        qd = q.get("question") or {}
        it = qd.get("item")
        st = q.get("state") or "unlocked"
        if it in by_item:
            by_item[it][st] = by_item[it].get(st, 0) + 1

    rollups = {k: _rollup(by_item.get(k) or {}) for k in items}
    valid_filter = state_filter if state_filter in STATE_CLASS else None

    if valid_filter is None:
        visible_ids = set(items.keys())
        ancestor_only: set[str] = set()
    else:
        matched = {k for k, s in rollups.items() if s == valid_filter}
        # Add every ancestor on the way to the root so the tree stays connected.
        ancestors: set[str] = set()
        for k in matched:
            cur = (items.get(k) or {}).get("parent")
            while isinstance(cur, str) and cur in items and cur not in matched and cur not in ancestors:
                ancestors.add(cur)
                cur = (items.get(cur) or {}).get("parent")
        visible_ids = matched | ancestors
        ancestor_only = ancestors - matched

    lines: list[str] = ["flowchart TD"]
    lines.extend(_classdefs())

    if not visible_ids:
        lines.append(f'  EMPTY(["No items match {_js(valid_filter or "")}"]):::muted')
        return "\n".join(lines) + "\n"

    ordered = [(k, items[k]) for k in items if k in visible_ids]
    skipped = max(0, len(ordered) - PROJECT_MAX_NODES)
    kept = ordered[:PROJECT_MAX_NODES]
    kept_ids = {k for k, _ in kept}

    for key, data in kept:
        data = data or {}
        title = _safe(data.get("title") or "", 44)
        counts = by_item.get(key) or {}
        state = rollups.get(key, "muted")
        cls = STATE_CLASS.get(state, "muted") if key not in ancestor_only else "muted"
        suffix = _state_badge(counts)
        label = _safe(key, 44) + ("<br/>" + title if title else "") + (("<br/>" + suffix) if suffix else "")
        node_id = _node_id("I_", key)
        lines.append(f'  {node_id}(["{label}"]):::{cls}')
        if clickable:
            lines.append(f'  click {node_id} call ckClick("item", "{_js(key)}")')

    for key, data in kept:
        data = data or {}
        parent = data.get("parent")
        if isinstance(parent, str) and parent in kept_ids and parent != key:
            lines.append(f"  {_node_id('I_', parent)} --> {_node_id('I_', key)}")

    if skipped > 0:
        more_id = "MORE_project"
        lines.append(f'  {more_id}(["… and {skipped} more items"]):::muted')

    return "\n".join(lines) + "\n"


def _rollup(counts: Mapping[str, int]) -> str:
    """The dominant state for an item's own questions, in the order the owner cares about."""
    for s in ("awaiting_you", "stale", "unlocked", "locked", "superseded", "withdrawn"):
        if counts.get(s):
            return s
    return "muted"


def _state_badge(counts: Mapping[str, int]) -> str:
    """A short per-state tally for an item's node label, like '? 2 ~ 1 o 3'. Empty when there are no questions."""
    parts = []
    for s, glyph in (("awaiting_you", "?"), ("unlocked", "~"), ("stale", "!"), ("locked", "o")):
        n = counts.get(s) or 0
        if n:
            parts.append(f"{glyph} {n}")
    return " · ".join(parts)


def _js(s: str) -> str:
    """A JavaScript-safe string literal body for a Mermaid `click` call argument (double-quoted)."""
    out = []
    for ch in s:
        if ch == '\\' or ch == '"':
            out.append('\\' + ch)
        elif ord(ch) < 0x20:
            out.append(f'\\u{ord(ch):04x}')
        else:
            out.append(ch)
    return "".join(out)


def _questions_on(view: Mapping, item: str) -> dict:
    """Every question on `item`, keyed by qid, sorted by seq (lowest first)."""
    out = {}
    for qid, q in (view.get("questions") or {}).items():
        qd = q.get("question") or {}
        if qd.get("item") == item:
            out[qid] = q
    return dict(sorted(out.items(), key=lambda kv: kv[1]["question"].get("seq") or 0))


def _forks_on(view: Mapping, item: str) -> dict:
    """Every round whose message lives on `item`, keyed by fork id, sorted by seq."""
    out = {}
    for fid, f in (view.get("forks") or {}).items():
        msg = f.get("message") or {}
        if msg.get("item") == item:
            out[fid] = f
    return dict(sorted(out.items(), key=lambda kv: kv[1]["message"].get("seq") or 0))


def _rel_time(iso: str) -> str:
    """A short label like '2h ago', '3d ago'. Empty when the timestamp cannot be read."""
    if not isinstance(iso, str) or not iso:
        return ""
    import datetime as dt
    try:
        when = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return ""
    now = dt.datetime.now(when.tzinfo) if when.tzinfo else dt.datetime.now()
    delta = now - when
    s = int(delta.total_seconds())
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def _classdefs() -> Iterable[str]:
    """Mermaid classDef lines for every state and the item/round shapes."""
    return [
        "  classDef item fill:#e7eef7,stroke:#1f4e8c,color:#111,font-weight:bold",
        "  classDef round fill:#efe6fa,stroke:#5a2a9b,color:#111",
        "  classDef awaiting fill:#fff3cd,stroke:#856404,color:#111",
        "  classDef unlocked fill:#ffeeba,stroke:#8a6d1a,color:#111",
        "  classDef stale fill:#f8d7da,stroke:#842029,color:#111",
        "  classDef locked fill:#d1e7dd,stroke:#0f5132,color:#111",
        "  classDef superseded fill:#e9ecef,stroke:#495057,color:#495057",
        "  classDef withdrawn fill:#e9ecef,stroke:#495057,color:#495057,stroke-dasharray:4 2",
        "  classDef muted fill:#f8f9fa,stroke:#6c757d,color:#495057",
    ]

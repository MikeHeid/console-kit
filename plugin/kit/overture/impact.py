"""Build an impact graph for one item: nodes for questions/items/sections/files, edges for the
relationships that connect them. The server emits Cytoscape-shaped JSON; the browser-side wrapper
in server.py renders it inside a sandboxed iframe with the vendored Cytoscape lib inlined (0.14.0).

Node kinds:
- item: the focal item + its ancestors on the way to root.
- question: every question on the focal item.
- section: dashboard section anchors (from `.overture.json` `sections`).
- file: files cited by evidence rows on the focal item's questions, deduplicated.

Edge kinds:
- parent: item -> child item.
- fork: round message -> every question it produced.
- supersedes: older question -> newer question that replaced it.
- in_section: item -> section (deep-link).
- cites: question -> file.

The output is a dict `{nodes: [...], edges: [...]}` where each node is
`{id, label, kind, state?}` and each edge is `{id, source, target, kind}`. The server inlines this as
JSON in the wrapper HTML; Cytoscape converts to its native shape via a trivial `data` map.
"""

from __future__ import annotations

import re
from typing import Mapping

MAX_NODES = 120  # cap so a huge item does not blow up the wrapper response


def build_item(view: Mapping, items: Mapping[str, Mapping], item: str, sections: Mapping[str, list] | None = None,
               ) -> dict:
    """Impact graph scoped to `item`: its ancestors, its questions, cited files, mapped sections."""
    if item not in items:
        raise KeyError(f"no item {item!r} in the item register")
    sections = sections or {}

    nodes: list[dict] = []
    edges: list[dict] = []
    seen: set[str] = set()

    def add_node(nid: str, label: str, kind: str, state: str | None = None) -> None:
        if nid in seen or len(seen) >= MAX_NODES:
            return
        seen.add(nid)
        node: dict = {"id": nid, "label": label, "kind": kind}
        if state:
            node["state"] = state
        nodes.append(node)

    def add_edge(source: str, target: str, kind: str) -> None:
        if source in seen and target in seen:
            edges.append({"id": f"{kind}:{source}->{target}", "source": source, "target": target, "kind": kind})

    # Item + its ancestors (parent chain).
    chain: list[str] = []
    cur: str | None = item
    while isinstance(cur, str) and cur in items and cur not in chain:
        chain.append(cur)
        parent = (items.get(cur) or {}).get("parent")
        cur = parent if isinstance(parent, str) else None
    for i, it in enumerate(reversed(chain)):
        data = items[it] or {}
        label = _label(it, data.get("title", ""))
        add_node("item:" + it, label, "item")
        if i > 0:
            prev = list(reversed(chain))[i - 1]
            add_edge("item:" + prev, "item:" + it, "parent")

    # Sections mapped to the focal item.
    for anchor in sections.get(item, []) or []:
        add_node("section:" + anchor, anchor, "section")
        add_edge("item:" + item, "section:" + anchor, "in_section")

    # Questions on the focal item.
    questions = {qid: q for qid, q in (view.get("questions") or {}).items()
                 if (q.get("question") or {}).get("item") == item}
    forks_cited: set[str] = set()
    for qid, q in questions.items():
        qd = q.get("question") or {}
        state = q.get("state") or "unlocked"
        add_node("q:" + qid, _qlabel(qid, qd.get("text", "")), "question", state)
        parent_id = qd.get("forked_from")
        if parent_id:
            forks_cited.add(parent_id)
            add_node("fork:" + parent_id, "round " + parent_id[:8], "fork")
            add_edge("fork:" + parent_id, "q:" + qid, "fork")
        else:
            add_edge("item:" + item, "q:" + qid, "parent")

    # supersedes edges (within the focal item).
    for qid, q in questions.items():
        other = (q.get("question") or {}).get("supersedes") or (q.get("question") or {}).get("replaces")
        if isinstance(other, str) and ("q:" + other) in seen:
            add_edge("q:" + other, "q:" + qid, "supersedes")

    # file citations from evidence rows.
    file_rx = re.compile(r"^([^:]+):")
    for q in questions.values():
        qid = (q.get("question") or {}).get("qid") or ""
        for row in (q.get("question") or {}).get("evidence") or []:
            cite = row.get("cite") if isinstance(row, dict) else None
            m = file_rx.match(cite or "")
            if m:
                path = m.group(1)
                add_node("file:" + path, path, "file")
                add_edge("q:" + qid, "file:" + path, "cites")

    return {"nodes": nodes, "edges": edges, "focus": item}


def _label(iid: str, title: str) -> str:
    t = _safe(title, 48)
    return f"{iid}\n{t}" if t else iid


def _qlabel(qid: str, text: str) -> str:
    tail = qid.rsplit("/Q", 1)[-1] if "/Q" in qid else qid
    return f"Q{tail}\n{_safe(text, 48)}" if text else "Q" + tail


_UNSAFE = re.compile(r'[\x00-\x1f]')


def _safe(s: str, limit: int) -> str:
    s = _UNSAFE.sub(" ", (s or "").strip())
    s = " ".join(s.split())
    return s if len(s) <= limit else s[: limit - 1] + "…"

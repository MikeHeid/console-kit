"""What the page shows, computed from the store on every request and never stored (spec R5 and R6).

This is the only place these states are worked out. The page renders this
view instead of re-deriving it, so "stale", "unlocked" and "awaiting you"
cannot mean one thing in Python and another in JavaScript.

Question states:

    awaiting_you    the agent asked; there is no answer yet
    unlocked        answered, not locked
    locked          locked, and every `valid_if` condition still holds
    stale           locked, but a condition no longer holds; supersede it (D3)

A thread is `awaiting_agent` when the owner's latest message on an item is
newer than the agent's latest message there.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Callable, Mapping

from .store import Store

STATES = ("awaiting_you", "unlocked", "locked", "stale")
COUNTED = ("awaiting_you", "awaiting_agent", "unlocked", "stale")


def make_evaluator(root: Path, item_status: Mapping[str, str]) -> Callable[[dict], bool]:
    """Return a function that says whether a `valid_if` condition holds for the project as it is now."""
    root = Path(root).resolve()

    def holds(c: dict) -> bool:
        if c["kind"] == "item_status":
            return item_status.get(c["item"]) == c["status"]
        p = (root / c["path"]).resolve()
        if root not in p.parents or not p.is_file():
            return False
        return hashlib.sha256(p.read_bytes()).hexdigest() == c["sha256"]

    return holds


def question_state(store: Store, q: dict, holds: Callable[[dict], bool]) -> str:
    head = store.head(q["qid"])
    if head is None:
        return "awaiting_you"
    if store.lock_of(head["id"]) is None:
        return "unlocked"
    return "locked" if all(holds(c) for c in q["valid_if"]) else "stale"


def build(store: Store, items: Mapping[str, dict], holds: Callable[[dict], bool]) -> dict:
    """Build the whole page view: each item's questions and threads, counts rolled up to ancestors, and the Inbox.

    `items` maps id -> {"title": str, "parent": id | None}, as the project adapter supplies it.
    """
    recs = store.records()
    questions: dict[str, dict] = {}
    for r in recs:
        if r["type"] != "question":
            continue
        answers = store.answers(r["qid"])
        questions[r["qid"]] = {
            "question": r,
            "state": question_state(store, r, holds),
            "answers": [dict(a, locked=store.lock_of(a["id"]) is not None) for a in answers],
            "failing": [c for c in r["valid_if"] if not holds(c)],
        }

    threads: dict[str, list[dict]] = {}
    for r in recs:
        if r["type"] == "message":
            threads.setdefault(r["item"], []).append(r)

    own: dict[str, dict[str, int]] = {i: dict.fromkeys(COUNTED, 0) for i in items}
    for q in questions.values():
        s = q["state"]
        if s in COUNTED and q["question"]["item"] in own:
            own[q["question"]["item"]][s] += 1
    waiting_agent = []
    for item, msgs in threads.items():
        last_owner = max((m["seq"] for m in msgs if m["by"] == "owner"), default=0)
        last_agent = max((m["seq"] for m in msgs if m["by"] == "agent"), default=0)
        if last_owner > last_agent and item in own:
            own[item]["awaiting_agent"] += 1
            waiting_agent.append(item)

    total = _roll_up(items, own)
    # A question whose item left the register (a rename with no alias yet, R7) is
    # named under `orphaned` rather than counted: the Inbox badge counts only
    # questions the page can show against an item.
    orphaned = sorted(qid for qid, q in questions.items() if q["question"]["item"] not in own)
    inbox = sorted((q for q in questions.values()
                    if q["state"] != "locked" and q["question"]["item"] in own),
                   key=lambda q: (STATES.index(q["state"]), q["question"]["seq"]))
    return {
        "items": {i: {"own": own[i], "total": total[i]} for i in items},
        "questions": questions,
        "threads": threads,
        "inbox": [q["question"]["qid"] for q in inbox],
        "awaiting_agent": sorted(waiting_agent),
        "orphaned": orphaned,
    }


def _roll_up(items: Mapping[str, dict], own: dict[str, dict[str, int]]) -> dict[str, dict[str, int]]:
    """Add each item's own counts to itself and to each of its ancestors exactly once, even when parent links form a cycle."""
    total = {i: dict.fromkeys(COUNTED, 0) for i in items}
    for i in items:
        seen: set[str] = set()
        node: str | None = i
        while node is not None and node in items and node not in seen:
            seen.add(node)
            for k in COUNTED:
                total[node][k] += own[i][k]
            node = items[node].get("parent")
    return total

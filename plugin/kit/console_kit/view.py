"""What the page shows, computed from the store on every request and never stored (spec R5 and R6).

This is the only place these states are worked out. The page renders this
view instead of re-deriving it, so "stale", "unlocked" and "awaiting you"
cannot mean one thing in Python and another in JavaScript.

Question states:

    awaiting_you    the agent asked; there is no answer yet
    unlocked        answered, not locked
    locked          locked, and every condition still holds
    stale           locked, but a condition no longer holds; supersede it (D3), or re-lock it

A locked answer is decided by its lock's anchors when it has them, and by the
question's `valid_if` otherwise (0.5.0; see `anchors.py`).

A thread is `awaiting_agent` when the owner's latest message on an item is
newer than the agent's latest message there.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Mapping

from . import anchors as A
from .store import Store

STATES = ("awaiting_you", "unlocked", "locked", "stale")
COUNTED = ("awaiting_you", "awaiting_agent", "unlocked", "stale")


def make_evaluator(root: Path, item_status: Mapping[str, str]) -> Callable[[dict], bool]:
    """Return a function that says whether a condition holds for the project as it is now.

    Each file is read and hashed at most once per evaluator, and a view builds
    one evaluator per request.
    """
    return A.evaluator(root, item_status)


def question_state(store: Store, q: dict, holds: Callable[[dict], bool]) -> str:
    head = store.head(q["qid"])
    if head is None:
        return "awaiting_you"
    if store.lock_of(head["id"]) is None:
        return "unlocked"
    conds, _ = A.conditions_for(store, q)
    return "locked" if all(holds(c) for c in conds) else "stale"


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
        conds, origin = A.conditions_for(store, r)
        questions[r["qid"]] = {
            "question": r,
            "state": question_state(store, r, holds),
            "answers": [dict(a, locked=store.lock_of(a["id"]) is not None) for a in answers],
            "failing": [c for c in conds if not holds(c)],
            # Where the conditions deciding it came from: the question's valid_if,
            # the re-lock's own anchors, or an `agent.py reanchor` record.
            "anchored_by": origin,
        }

    threads: dict[str, list[dict]] = {}
    forks: dict[str, dict] = {}
    for r in recs:
        if r["type"] == "message":
            threads.setdefault(r["item"], []).append(r)
            if r.get("intent") == "fork":
                forks[r["id"]] = {"message": r, "questions": []}
    # Forked questions are grouped under their fork (§6.5 F1). `append` refuses a
    # `forked_from` that is not an owner fork message, but loading a file does not
    # re-run that rule, so one that names no fork is simply left ungrouped.
    for q in questions.values():
        fid = q["question"].get("forked_from")
        if fid in forks:
            forks[fid]["questions"].append(q["question"]["qid"])

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
        "forks": forks,
        "inbox": [q["question"]["qid"] for q in inbox],
        "awaiting_agent": sorted(waiting_agent),
        "orphaned": orphaned,
    }


def subtree(items: Mapping[str, dict], root: str) -> set[str]:
    """`root` and every item under it (D14's scope), terminating even when parent links form a cycle."""
    out = set()
    for i in items:
        seen: set[str] = set()
        node: str | None = i
        while node is not None and node in items and node not in seen:
            if node == root:
                out.add(i)
                break
            seen.add(node)
            node = items[node].get("parent")
    return out


def tree_order(items: Mapping[str, dict]) -> list[str]:
    """Items depth first from the roots, children in the adapter's order; an item only a cycle reaches comes last."""
    kids: dict[str | None, list[str]] = {}
    for i, d in items.items():
        p = d.get("parent")
        kids.setdefault(p if p in items else None, []).append(i)
    out: list[str] = []
    seen: set[str] = set()
    stack = list(reversed(kids.get(None, [])))
    while stack:
        i = stack.pop()
        if i in seen:
            continue
        seen.add(i)
        out.append(i)
        stack.extend(reversed(kids.get(i, [])))
    return out + [i for i in items if i not in seen]


def answers_sheet(view: dict, items: Mapping[str, dict], item: str | None = None,
                  fork: str | None = None) -> dict:
    """Every question in a scope with EVERY answer it got, in tree order (spec §7.6).

    `item` narrows to that item and all under it; `fork` narrows to one round's
    questions. Unanswered and stale questions stay in, because leaving them out
    would make a round look finished when it is not. With no `item`, a question
    whose item left the register is listed last, named `orphaned`.
    """
    order = {i: n for n, i in enumerate(tree_order(items))}
    scope = subtree(items, item) if item is not None else None
    wanted = set(view["forks"].get(fork, {}).get("questions", [])) if fork is not None else None
    rows = []
    for qid, q in view["questions"].items():
        rec = q["question"]
        known = rec["item"] in items
        if scope is not None and rec["item"] not in scope:
            continue
        if wanted is not None and qid not in wanted:
            continue
        rows.append({
            "qid": qid, "item": rec["item"], "orphaned": not known,
            "text": rec["text"], "kind": rec["kind"], "state": q["state"], "failing": q["failing"],
            "star": rec["star"], "options": rec["options"],
            **{k: rec[k] for k in ("star_by", "forked_from") if k in rec},
            "answers": q["answers"],
        })
    rows.sort(key=lambda r: (r["orphaned"], order.get(r["item"], len(order)), r["item"],
                             int(r["qid"].rsplit("/Q", 1)[1])))
    counts = dict.fromkeys(STATES, 0)
    for r in rows:
        counts[r["state"]] += 1
    return {"item": item, "fork": fork, "rows": rows, "counts": counts,
            "answers": sum(len(r["answers"]) for r in rows)}


STATE_WORDS = {"awaiting_you": "unanswered", "unlocked": "answered, not locked",
               "locked": "locked", "stale": "stale"}


def condition_words(c: dict) -> str:
    """A `valid_if` condition as a reader would say it."""
    if c["kind"] == "item_status":
        return f"item `{c['item']}` has status `{c['status']}`"
    if c["kind"] == "excerpt":
        return f"`{c['path']}` still contains the text the question cites"
    return f"`{c['path']}` is unchanged (a whole-file check)"


def sheet_markdown(sheet: dict) -> str:
    """The sheet as one Markdown block, for a PR or a chat (§7.6). Every answer appears, oldest first."""
    c = sheet["counts"]
    scope = f"`{sheet['item']}` and all under it" if sheet["item"] else "the whole console"
    if sheet["fork"]:
        scope += f", fork `{sheet['fork']}`"
    out = [f"# Answers: {scope}", "",
           f"{len(sheet['rows'])} questions: {c['awaiting_you']} unanswered, {c['unlocked']} answered, "
           f"{c['locked']} locked, {c['stale']} stale. {sheet['answers']} "
           f"answer{'' if sheet['answers'] == 1 else 's'} in all.", ""]
    for r in sheet["rows"]:
        labels = {o["id"]: o["label"] for o in r["options"]}
        out.append(f"## {r['qid']}: {STATE_WORDS[r['state']]}" + (" (item no longer in the register)"
                                                                     if r["orphaned"] else ""))
        out += [f"> {line}" for line in r["text"].splitlines()] + [""]
        if r.get("star"):
            whose = f", {r['star_by']}'s" if r.get("star_by") else ""
            out.append(f"★{whose}: {labels.get(r['star'], r['star'])}")
        for cnd in r["failing"]:
            out.append(f"Stale because this no longer holds: {condition_words(cnd)}")
        if not r["answers"]:
            out.append("No answer yet.")
        for n, a in enumerate(r["answers"], 1):
            tag = "current" if n == len(r["answers"]) else "earlier"
            picks = ", ".join(labels.get(p, p) for p in a["picks"]) or "(no pick)"
            out.append(f"{n}. {a['ts']} ({tag}{', locked' if a.get('locked') else ''}): {picks}")
            out += [f"   > {line}" for line in a["own_text"].splitlines() if a["own_text"].strip()]
            if a.get("reason"):
                out.append(f"   Replaced the answer before it, because: {a['reason']}")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


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

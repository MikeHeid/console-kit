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

Agent names (0.8.2): a record an agent wrote under a name carries `agent`
here, joined from the names file (`names.py`); a record with no name is the
store's own record, exactly as before.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Mapping

from . import anchors as A
from . import schema as S
from .names import named
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


def build(store: Store, items: Mapping[str, dict], holds: Callable[[dict], bool],
          names: Mapping[str, str] | None = None) -> dict:
    """Build the whole page view: each item's questions and threads, counts rolled up to ancestors, and the Inbox.

    `items` maps id -> {"title": str, "parent": id | None}, as the project adapter supplies it.
    `names` maps record id -> agent name (0.8.2); a named record gains `agent`, no other changes.
    """
    recs = store.records()
    questions: dict[str, dict] = {}
    for r in recs:
        if r["type"] != "question":
            continue
        answers = store.answers(r["qid"])
        conds, origin = A.conditions_for(store, r)
        questions[r["qid"]] = {
            "question": named(r, names),
            "state": question_state(store, r, holds),
            "answers": [dict(a, locked=store.lock_of(a["id"]) is not None) for a in answers],
            "failing": [c for c in conds if not holds(c)],
            # Where the conditions deciding it came from: the question's valid_if,
            # the re-lock's own anchors, or an `agent.py reanchor` record.
            "anchored_by": origin,
        }

    threads: dict[str, list[dict]] = {}
    forks: dict[str, dict] = {}
    transcripts: dict[str, dict] = {}
    visuals: dict[str, list[dict]] = {}
    for r in recs:
        if r["type"] == "message":
            threads.setdefault(r["item"], []).append(named(r, names))
            if r.get("intent") == "fork":
                forks[r["id"]] = {"message": r, "questions": [], "transcript": None}
        elif r["type"] == "transcript":
            # A roar's transcript (0.8.0), shown collapsed on its fork and that fork's questions.
            transcripts[r["fork"]] = named({"id": r["id"], "fork": r["fork"], "ts": r["ts"], "text": r["text"],
                                            "bytes": len(r["text"].encode("utf-8"))}, names)
        elif r["type"] == "visual":
            visuals.setdefault(r["item"], []).append(named(
                {k: r[k] for k in ("id", "seq", "ts", "item", "request", "format", "title", "text",
                                   "path", "doc_path", "bytes")}, names))
    for fid, t in transcripts.items():
        if fid in forks:
            forks[fid]["transcript"] = t["id"]
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
        # A visual (0.8.0) is the agent answering on that item, as a reply is.
        last_agent = max([m["seq"] for m in msgs if m["by"] == "agent"]
                         + [v["seq"] for v in visuals.get(item, [])], default=0)
        if last_owner > last_agent and item in own:
            own[item]["awaiting_agent"] += 1
            waiting_agent.append(item)

    # The chat (0.7.0) is not a register item, so it is never in `own`: whether
    # it waits on an agent is said on its own, never folded into an item's count.
    chat = threads.get(S.CHAT_ITEM, [])
    chat_owner = max((m["seq"] for m in chat if m["by"] == "owner"), default=0)
    chat_agent = max((m["seq"] for m in chat if m["by"] == "agent"), default=0)

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
        "chat": {"item": S.CHAT_ITEM, "awaiting_agent": chat_owner > chat_agent, "messages": len(chat)},
        # 0.8.0: roar transcripts by fork id, and visuals by item (the file is served by /api/visual).
        "transcripts": transcripts,
        "visuals": visuals,
        # The store's sequence number this view was built at (0.7.0): the page's
        # live loop and unread count compare against it.
        "seq": recs[-1]["seq"] if recs else 0,
    }


# -- the Feed (0.7.0) ----------------------------------------------------------------
#
# A view over the store, newest first, never stored. Folds and PR merges are
# not store records (a fold lands through a PR the store never sees), so they
# are not in the feed; the page says so and links out.

FEED_KINDS = ("question", "answer", "lock", "reanchor", "fork", "process", "chat", "reply", "note",
              "transcript", "visual")
FEED_MAX = 200
FEED_DEFAULT = 50
FEED_SNIPPET = 240


def _snip(text: str) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= FEED_SNIPPET else t[:FEED_SNIPPET - 1] + "…"


def feed_event(rec: dict, qitems: Mapping[str, str], relocks: set[str], agent: str | None = None) -> dict:
    """One store record as a feed event: what happened, where, and a short line of what was said."""
    t = rec["type"]
    ev = {"seq": rec["seq"], "ts": rec["ts"], "by": rec["by"], "id": rec["id"]}
    if agent:  # 0.8.2: which agent wrote it, when it gave a name
        ev["agent"] = agent
    if t == "question":
        ev.update(kind="question", item=rec["item"], qid=rec["qid"], text=_snip(rec["text"]))
        if "forked_from" in rec:
            ev["round"] = rec["forked_from"]
    elif t == "answer":
        ev.update(kind="answer", item=qitems.get(rec["qid"]), qid=rec["qid"], picks=rec["picks"],
                  text=_snip(rec["own_text"]), supersedes="supersedes" in rec)
        if "reason" in rec:
            ev["reason"] = _snip(rec["reason"])
    elif t == "lock":
        ev.update(kind="lock", item=qitems.get(rec["qid"]), qid=rec["qid"], relock=rec["id"] in relocks)
    elif t == "anchor":
        ev.update(kind="reanchor", item=qitems.get(rec["qid"]), qid=rec["qid"], text=_snip(rec["basis"]))
    elif t == "transcript":  # 0.8.0: the fork's item is filled in by `feed`, which knows the fork
        ev.update(kind="transcript", fork=rec["fork"], text=_snip(rec["text"]))
    elif t == "visual":      # 0.8.0: an agent's answer to a visual request
        ev.update(kind="visual", item=rec["item"], request=rec["request"], format=rec["format"],
                  text=_snip(rec["title"]))
    else:  # message
        intent = rec.get("intent")
        kind = (intent if intent in ("fork", "process", "chat", "visual")
                else "chat" if rec["item"] == S.CHAT_ITEM
                else "reply" if rec["by"] == "agent" else "note")
        ev.update(kind=kind, item=rec["item"], text=_snip(rec["text"]))
        for k in ("mode", "focus", "roles", "about_qid", "follow_up_of", "reply_to", "step"):
            if k in rec:
                ev[k] = rec[k]
    return ev


def feed(store: Store, items: Mapping[str, dict], *, kinds: set[str] | None = None, item: str | None = None,
         before: int | None = None, limit: int = FEED_DEFAULT, names: Mapping[str, str] | None = None) -> dict:
    """The newest `limit` events (optionally of `kinds`, on `item` and all under it, before seq `before`).

    `next_before` is the seq to pass as `before` for the page after this one,
    or None when there is nothing older.
    """
    recs = store.records()
    qitems = {r["qid"]: r["item"] for r in recs if r["type"] == "question"}
    relocks, seen = set(), set()
    for r in recs:  # a lock on a question that was locked before is a re-lock
        if r["type"] == "lock":
            (relocks.add(r["id"]) if r["qid"] in seen else seen.add(r["qid"]))
    scope = (subtree(items, item) if item in items else {item}) if item is not None else None
    out: list[dict] = []
    more = None
    for r in reversed(recs):
        if before is not None and r["seq"] >= before:
            continue
        ev = feed_event(r, qitems, relocks, (names or {}).get(r["id"]))
        if ev["kind"] == "transcript":
            fork = store.get(ev["fork"])
            ev["item"] = fork["item"] if fork is not None else None
        if kinds is not None and ev["kind"] not in kinds:
            continue
        if scope is not None and ev.get("item") not in scope:
            continue
        if len(out) == limit:
            more = out[-1]["seq"]
            break
        out.append(ev)
    return {"events": out, "next_before": more, "kinds": list(FEED_KINDS),
            "seq": recs[-1]["seq"] if recs else 0}


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
            **{k: rec[k] for k in ("star_by", "forked_from", "agent") if k in rec},
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
        if r.get("agent"):  # 0.8.2: only a question an agent asked under a name
            out.append(f"Asked by {r['agent']}.")
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

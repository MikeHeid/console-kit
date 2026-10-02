"""What the page shows, computed from the store on every request and never stored (spec R5 and R6).

This is the only place these states are worked out. The page renders this
view instead of re-deriving it, so "stale", "unlocked" and "awaiting you"
cannot mean one thing in Python and another in JavaScript.

Question states:

    awaiting_you    the agent asked; there is no answer yet
    unlocked        answered, not locked
    locked          locked, and every condition still holds
    stale           locked, but a condition no longer holds; supersede it (D3), re-lock it, or refactor it
    withdrawn       the owner withdrew a stale ruling: it no longer applies (CONSOLE-kit/Q30)
    superseded      a question asked to replace a stale ruling was locked (CONSOLE-kit/Q32)

A stale ruling the owner chose to keep without checking (Q30 "keep, stop
checking") reads `locked`, with `refactor.outcome.kind` "untracked". Whatever
was done about a stale answer is under `refactor` (`refactor.summary`).

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
from . import refactor as RX
from . import schema as S
from .names import named
from .store import Store

STATES = ("awaiting_you", "unlocked", "locked", "stale", "withdrawn", "superseded")
# Not waiting on the owner: these never enter the inbox.
SETTLED = ("locked", "withdrawn", "superseded")
OUTCOME_STATE = {"withdrawn": "withdrawn", "untracked": "locked", "superseded": "superseded"}
# When a fork is done (owner ruling build_reply, review rounds 1-2): an agent message replying
# to the fork whose first line starts with RESULT, or - for any fork but an open-question round -
# its questions. Progress notes never reply to the fork, so they cannot hide a pending round.
RESULT = "Result:"
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
    out = RX.outcome(store, q["qid"])
    if out is not None:
        return OUTCOME_STATE[out["kind"]]
    conds, _ = A.conditions_for(store, q)
    return "locked" if all(holds(c) for c in conds) else "stale"


def fork_kind(store: Store, msg: dict) -> str:
    """What a fork is, fixed when it was asked: open (seats on a question not locked THEN),
    follow_up (seats on a locked answer), roar, refine, drill, round (a later round) or item."""
    if msg.get("step"):
        return msg["step"]
    if "roar" in (msg.get("roles") or ()):
        return "roar"
    qid = msg.get("about_qid")
    if qid:
        before = [r for r in store.records() if r["seq"] < msg["seq"] and r.get("qid") == qid]
        answers = [r for r in before if r["type"] == "answer"]
        head = answers[-1]["id"] if answers else None
        locked = any(r["type"] == "lock" and r["answer"] == head for r in before)
        return "follow_up" if head is not None and locked else "open"
    return "round" if msg.get("follow_up_of") else "item"


def fork_result(msgs: list[dict], fid: str) -> dict | None:
    """The agent's result reply to fork `fid` (first line starts with RESULT), the latest if several."""
    hits = [m for m in msgs if m["by"] == "agent" and m.get("reply_to") == fid
            and m["text"].lstrip().startswith(RESULT)]
    return max(hits, key=lambda m: m["seq"]) if hits else None


def fork_done(f: dict) -> bool:
    """Done = a RESULT reply exists, OR the fork is not an open-question round and its questions exist.

    Open-question rounds are new, so they always need the RESULT line. Every older kind keeps the
    rule forks were finished under before it (questions back), so an upgrade re-runs nothing."""
    if f["result"] is not None:
        return True
    return f["kind"] != "open" and bool(f["questions"])


def build(store: Store, items: Mapping[str, dict], holds: Callable[[dict], bool],
          names: Mapping[str, str] | None = None) -> dict:
    """Build the whole page view: each item's questions and threads, counts rolled up to ancestors, and the Inbox.

    `items` maps id -> {"title": str, "parent": id | None}, as the project adapter supplies it.
    `names` maps record id -> agent name (0.8.2); a named record gains `agent`, no other changes.
    """
    recs = store.records()
    # The newest record that can change what a question shows (K1, for `since`): the question,
    # an answer, a lock, a re-anchor. Staleness itself has no record: it is a file or a status.
    last_seq: dict[str, int] = {}
    for r in recs:
        if r["type"] in ("question", "answer", "lock", "anchor"):
            last_seq[r["qid"]] = r["seq"]
    questions: dict[str, dict] = {}
    for r in recs:
        if r["type"] != "question":
            continue
        answers = store.answers(r["qid"])
        conds, origin = A.conditions_for(store, r)
        state = question_state(store, r, holds)
        questions[r["qid"]] = {
            "question": named(r, names),
            "state": state,
            "answers": [dict(a, locked=store.lock_of(a["id"]) is not None) for a in answers],
            # A ruling kept without checking, withdrawn or superseded is no longer checked: nothing "fails".
            "failing": [c for c in conds if not holds(c)] if state in ("stale", "unlocked", "awaiting_you") else [],
            # Where the conditions deciding it came from: the question's valid_if,
            # the re-lock's own anchors, or an `agent.py reanchor` record.
            "anchored_by": origin,
            "last_seq": last_seq[r["qid"]],
        }
        rx = RX.summary(store, r["qid"])   # CONSOLE-kit/Q30-Q32: only when something was done or proposed
        if rx:
            questions[r["qid"]]["refactor"] = rx
        if state == "stale":   # the lock an owner act names, so one taken on a page that is out of date is refused
            questions[r["qid"]]["lock"] = RX.current_lock(store, r["qid"])["id"]

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
            transcripts[r["fork"]] = named({"id": r["id"], "seq": r["seq"], "fork": r["fork"], "ts": r["ts"],
                                            "text": r["text"], "bytes": len(r["text"].encode("utf-8"))}, names)
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
    for fid, f in forks.items():
        f["kind"] = fork_kind(store, f["message"])
        f["result"] = fork_result(threads.get(f["message"]["item"], []), fid)
        f["done"] = fork_done(f)

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
                    if q["state"] not in SETTLED and q["question"]["item"] in own),
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
        # Slim reads (K1): the visual requests nothing answers yet, oldest first; `todo` reads it from here.
        "waiting_visuals": waiting_visuals(threads, visuals),
        # The store's sequence number this view was built at (0.7.0): the page's
        # live loop and unread count compare against it.
        "seq": recs[-1]["seq"] if recs else 0,
    }


# -- slim reads (K1) -----------------------------------------------------------------
#
# Each of these FILTERS a view `build` already made; none decides anything on its
# own. "Waiting" is therefore worked out once, in `build`, and `todo` cannot drift
# from what the page and `view` say.

FORK_KEYS = ("mode", "focus", "roles", "about_qid", "follow_up_of", "step")


def waiting_visuals(threads: Mapping[str, list[dict]], visuals: Mapping[str, list[dict]]) -> list[dict]:
    """The visual requests (0.8.0) no stored visual names as its `request`, oldest first, as {id, item}.

    The one rule for "a visual request waits": `build` calls it, and so does
    `todo` on a view from a server built before this field existed.
    """
    drawn = {v["request"] for vs in visuals.values() for v in vs}
    reqs = [m for msgs in threads.values() for m in msgs
            if m["by"] == "owner" and m.get("intent") == "visual" and m["id"] not in drawn]
    return [{"id": m["id"], "item": m["item"]} for m in sorted(reqs, key=lambda m: m["seq"])]


# What the slim reads need from a server's view. A field added in K1 (waiting_visuals)
# is not listed: a server older than K1 does not send it, and it is derived from these.
NEEDED = ("items", "questions", "threads", "forks", "inbox", "awaiting_agent", "orphaned", "chat",
          "transcripts", "visuals", "seq")
NEEDED_FORK = ("message", "questions", "kind", "result", "done")


class ViewTooOld(ValueError):
    """The server's view lacks something the slim reads need: it runs an older kit than this client."""


def check_view(view: object) -> None:
    """Raise ViewTooOld naming every field a slim read needs and this view lacks; return when it has them."""
    if not isinstance(view, dict):
        raise ViewTooOld("the view is not a JSON object")
    missing = [k for k in NEEDED if k not in view]
    if not missing:
        forks = view["forks"] if isinstance(view["forks"], dict) else {}
        missing = sorted({f"forks.*.{k}" for f in forks.values() for k in NEEDED_FORK
                          if not isinstance(f, dict) or k not in f})
        if not isinstance(view["chat"], dict) or not {"item", "awaiting_agent"} <= set(view["chat"]):
            missing.append("chat.awaiting_agent")
    if missing:
        raise ViewTooOld(f"the view has no {', '.join(missing)}")


def _waiting(view: dict) -> list[dict]:
    """`view.waiting_visuals`, or the same rule run on the view's own records when an older server left it out."""
    if "waiting_visuals" in view:
        return view["waiting_visuals"]
    return waiting_visuals(view["threads"], view["visuals"])


def _fork_row(f: dict) -> dict:
    m = f["message"]
    return {"id": m["id"], "item": m["item"], "kind": f["kind"], **{k: m[k] for k in FORK_KEYS if k in m}}


def todo(view: dict) -> dict:
    """Exactly what a session processing the console needs to know is waiting, filtered from `view`.

    forks           every fork that is not done, oldest first: its id, item, kind, mode and seats
    awaiting_agent  the items whose thread waits on the agent
    chat            whether the chat waits, and the owner message to reply to
    visuals         the visual requests nothing answers yet (id, item)
    inbox           the qids in the owner's inbox
    seq             the store seq the view was built at
    """
    forks = sorted((f for f in view["forks"].values() if not f["done"]), key=lambda f: f["message"]["seq"])
    chat = view["chat"]
    owner_chat = [m for m in view["threads"].get(chat["item"], []) if m["by"] == "owner"]
    return {
        "forks": [_fork_row(f) for f in forks],
        "awaiting_agent": list(view["awaiting_agent"]),
        "chat": {"awaiting_agent": chat["awaiting_agent"],
                 "reply_to": owner_chat[-1]["id"] if chat["awaiting_agent"] and owner_chat else None},
        "visuals": [dict(w) for w in _waiting(view)],
        "inbox": list(view["inbox"]),
        "seq": view["seq"],
    }


def _filter_tags(view: dict, qids, fids) -> dict:
    tags = view.get("tags")
    if not isinstance(tags, dict):
        return {}
    return {"tags": {**tags, "questions": {q: t for q, t in tags.get("questions", {}).items() if q in qids},
                     "forks": {f: t for f, t in tags.get("forks", {}).items() if f in fids}}}


def item_view(payload: dict, item: str) -> dict:
    """The agent door's payload narrowed to `item` and everything under it (D14's scope).

    `@chat` narrows to the chat thread. Raises KeyError naming an item the
    register does not hold. The shape is the payload's own, so a reader of
    `view` reads this the same way; `view.scope` names the item.
    """
    view, items = payload["view"], payload["items"]
    if item == S.CHAT_ITEM:
        scope: set[str] = set()
    elif item in items:
        scope = subtree(items, item)
    else:
        raise KeyError(f"no item {item!r} in the project's item list")
    keep = scope | ({S.CHAT_ITEM} if item == S.CHAT_ITEM else set())
    questions = {q: v for q, v in view["questions"].items() if v["question"]["item"] in keep}
    forks = {f: v for f, v in view["forks"].items() if v["message"]["item"] in keep}
    out = {**view,
           "scope": item,
           "items": {i: c for i, c in view["items"].items() if i in scope},
           "questions": questions,
           "threads": {i: m for i, m in view["threads"].items() if i in keep},
           "forks": forks,
           "inbox": [q for q in view["inbox"] if q in questions],
           "awaiting_agent": [i for i in view["awaiting_agent"] if i in keep],
           "orphaned": [q for q in view["orphaned"] if q in questions],
           "transcripts": {f: t for f, t in view["transcripts"].items() if f in forks},
           "visuals": {i: v for i, v in view["visuals"].items() if i in keep},
           "waiting_visuals": [w for w in _waiting(view) if w["item"] in keep],
           **_filter_tags(view, questions, forks)}
    return {**payload, "view": out, "items": {i: d for i, d in items.items() if i in scope}}


def check_since(view: dict) -> None:
    """Raise ViewTooOld when `view` lacks the seqs `since` keeps records by (a server older than K1)."""
    missing = sorted({"questions.*.last_seq" for q in view["questions"].values() if "last_seq" not in q}
                     | {"transcripts.*.seq" for t in view["transcripts"].values() if "seq" not in t})
    if missing:
        raise ViewTooOld(f"the view has no {', '.join(missing)}")


def since(view: dict, seq: int) -> dict:
    """`view` with only what changed after store seq `seq`: the records-bearing parts are filtered.

    A question is kept when any record about it is after `seq` (the question,
    an answer, a lock or a re-anchor: its `last_seq`), and whenever it is
    stale: staleness comes from a file or an item's status, which carry no
    seq, so a stale question is always shown rather than silently missed. A
    thread keeps its messages after `seq`; a fork is kept when its message,
    result or transcript is after `seq` or one of its kept questions came from
    it; a visual by its own seq. The summaries (inbox, awaiting_agent, chat,
    waiting_visuals, seq) stay whole: they are state, not history. `items`
    keeps the counts of the items the kept records are on.

    Limit: a question that went from stale back to valid (its file restored)
    with no record after `seq` is not shown. Raises ViewTooOld for a view
    from a server older than K1, which carries no `last_seq`.
    """
    check_since(view)
    questions = {q: v for q, v in view["questions"].items() if v["last_seq"] > seq or v["state"] == "stale"}
    threads = {i: kept for i, msgs in view["threads"].items() if (kept := [m for m in msgs if m["seq"] > seq])}
    transcripts = {f: t for f, t in view["transcripts"].items() if t["seq"] > seq}
    forks = {f: v for f, v in view["forks"].items()
             if v["message"]["seq"] > seq or (v["result"] is not None and v["result"]["seq"] > seq)
             or f in transcripts or any(q in questions for q in v["questions"])}
    visuals = {i: kept for i, vs in view["visuals"].items() if (kept := [v for v in vs if v["seq"] > seq])}
    touched = ({v["question"]["item"] for v in questions.values()} | set(threads) | set(visuals)
               | {v["message"]["item"] for v in forks.values()})
    return {**view, "since": seq,
            "items": {i: c for i, c in view["items"].items() if i in touched},
            "questions": questions, "threads": threads, "forks": forks, "visuals": visuals,
            "transcripts": {f: t for f, t in view["transcripts"].items() if f in forks},
            **_filter_tags(view, questions, forks)}


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
            **({"refactor": q["refactor"]} if "refactor" in q else {}),
            "answers": q["answers"],
        })
    rows.sort(key=lambda r: (r["orphaned"], order.get(r["item"], len(order)), r["item"],
                             int(r["qid"].rsplit("/Q", 1)[1])))
    counts = dict.fromkeys(STATES[:4], 0)   # withdrawn / superseded only when some are, as before Q30
    for r in rows:
        counts[r["state"]] = counts.get(r["state"], 0) + 1
    return {"item": item, "fork": fork, "rows": rows, "counts": counts,
            "answers": sum(len(r["answers"]) for r in rows)}


STATE_WORDS = {"awaiting_you": "unanswered", "unlocked": "answered, not locked",
               "locked": "locked", "stale": "stale", "withdrawn": "withdrawn", "superseded": "superseded"}


def refactor_lines(rx: dict) -> list[str]:
    """What was done about a stale ruling (CONSOLE-kit/Q30-Q32), as lines of the sheet; [] when nothing was."""
    out = []
    o = rx.get("outcome")
    if o is not None:
        why = f": {o['reason']}" if o.get("reason") else ""
        out.append({"withdrawn": f"Withdrawn by the {o['by']} on {o['at']}{why}",
                    "untracked": f"Kept by the {o['by']} on {o['at']}, no longer checked against the files{why}",
                    "superseded": f"Superseded on {o['at']} by {o.get('replaced_by')}, locked by the {o['by']}"}
                   [o["kind"]])
    if rx.get("replaced_by") and (o is None or o["kind"] != "superseded"):
        out.append(f"A replacement is open: {rx['replaced_by']}. This ruling stands until it is locked.")
    if rx.get("replaces"):
        out.append(f"Asked to replace {rx['replaces']}.")
    if rx.get("proposal"):
        out.append("The steward proposed a new anchor; it waits for the owner to confirm it.")
    if rx.get("confirmed"):
        out.append(f"Re-anchored on {rx['confirmed']['at']} from a proposal the {rx['confirmed']['by']} confirmed.")
    return out


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
           f"answer{'' if sheet['answers'] == 1 else 's'} in all."
           + "".join(f" {c[s]} {s}." for s in ("withdrawn", "superseded") if c.get(s)), ""]
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
        out += refactor_lines(r.get("refactor") or {})
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

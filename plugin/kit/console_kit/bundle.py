"""What a deliberation's committee reads: the round's bundle (spec §6.3, D14, §7.5 F3).

The bundle is the fork request, its earlier rounds, the item and every item
under it, every question and answer in that scope, and the threads there. The
project's own sources (specs, findings) are found by the session from the item
ids; this module knows nothing about any project.

A follow-up on one locked answer (a fork with `about_qid`, 0.4.0) leads with
that question: its text and options, every answer with the owner's own words,
and which answer holds the lock. The item context follows.

Seats called on an OPEN question (a deliberation before answering) lead the
same way, under a heading that says the question is open and what the round
returns: one reply with a recommendation, never an answer or a lock.

A bundle over MAX_BUNDLE bytes (64 KiB, §6.6) is refused by name, listing its
largest parts, never trimmed (D14): a trimmed bundle would hand the committee a
partial picture that looks whole.
"""

from __future__ import annotations

from typing import Mapping

from . import view as V

MAX_BUNDLE = 64 * 1024  # bytes of UTF-8 (§6.6)


class BundleTooLarge(Exception):
    pass


def rounds(view: dict, fork_id: str) -> list[dict]:
    """The fork message and every round before it, first round first."""
    chain, seen = [], set()
    node = view["forks"].get(fork_id, {}).get("message")
    while node is not None and node["id"] not in seen:
        seen.add(node["id"])
        chain.append(node)
        node = view["forks"].get(node.get("follow_up_of"), {}).get("message")
    return list(reversed(chain))


def _seats(m: dict) -> str:
    if m.get("step"):  # 0.8.0: a refine or drill runs the project's skill, not seats
        return f"none: a {m['step']}, run by the project's {m['step']} skill"
    if m.get("roles") == ["roar"]:  # 0.8.0
        return "a roar panel (independent reads, deliberation, synthesis)"
    if not m.get("roles"):
        return "the default committee"
    return ", ".join(r.removeprefix("other:") for r in m["roles"])


def _quote(text: str) -> list[str]:
    return [f"> {line}" for line in text.splitlines()] or [">"]


def about_qid(chain: list[dict]) -> str | None:
    """The question a follow-up on one answer is about: the latest round's, else the nearest earlier one's."""
    return next((m["about_qid"] for m in reversed(chain) if m.get("about_qid")), None)


OPEN_RESULT = ("The question is not locked, so the seats deliberate BEFORE the owner answers. The round "
               "returns ONE reply on this question's item, replying to the fork: a ★ recommendation (one option "
               "id) with its reasons and what the seats found. Only if the seats show the options themselves "
               "are wrong does it also ask a replacement question (the next free qid, forked from this fork), "
               "and the reply names it. The round never answers or locks: those are the owner's.")


def _is_open(view: dict, qid: str, fork: dict) -> bool:
    """Whether a fork about `qid` is a deliberation before answering: seats, no step, no roar, and no lock now."""
    if fork.get("step") or "roar" in (fork.get("roles") or ()):
        return False
    q = view["questions"].get(qid)
    return q is not None and q["state"] in ("awaiting_you", "unlocked")


def _about_section(view: dict, qid: str) -> str:
    """The question, every answer it got (oldest first, the owner's own words in full) and which one is locked."""
    q = view["questions"].get(qid)
    if q is None:  # the store refuses this on append; a hand-edited file is named, not guessed at
        return f"`{qid}` is not in the console's store.\n"
    rec = q["question"]
    labels = {o["id"]: o["label"] for o in rec["options"]}
    out = [f"On item `{rec['item']}`, now {V.STATE_WORDS[q['state']]}. Source: `{rec['source']}`.", ""]
    out += _quote(rec["text"]) + [""]
    if rec["options"]:
        out.append("Options:")
        for o in rec["options"]:
            star = (" (★ recommended" + (f" by {rec['star_by']})" if rec.get("star_by") else ")")
                    if o["id"] == rec.get("star") else "")
            desc = f": {o['description']}" if o.get("description") else ""
            out.append(f"- `{o['id']}` {o['label']}{star}{desc}")
        out.append("")
    for c in q["failing"]:
        out.append(f"Stale because this no longer holds: {V.condition_words(c)}")
    out.append("Every answer, oldest first:" if q["answers"] else "No answer yet.")
    for n, a in enumerate(q["answers"], 1):
        tag = "current" if n == len(q["answers"]) else "earlier"
        lock = ", LOCKED" if a.get("locked") else ""
        picks = ", ".join(labels.get(p, p) for p in a["picks"]) or "(no pick)"
        out.append(f"{n}. {a['ts']} ({tag}{lock}): {picks}")
        if a["own_text"].strip():
            out.append("   The owner's own words:")
            out += [f"   > {line}" for line in a["own_text"].splitlines()]
        if a.get("reason"):
            out.append(f"   Replaced the answer before it, because: {a['reason']}")
    locked = [a for a in q["answers"] if a.get("locked")]
    out += ["", f"The lock stands on answer {len(q['answers'])} (`{locked[-1]['id']}`)."
            if locked and locked[-1] is q["answers"][-1] else "The current answer is not locked."]
    return "\n".join(out)


def fork_context(view: dict, items: Mapping[str, dict], fork_id: str, max_bytes: int = MAX_BUNDLE) -> str:
    if fork_id not in view["forks"]:
        raise KeyError(f"no fork {fork_id!r}")
    chain = rounds(view, fork_id)
    fork = chain[-1]
    item = fork["item"]
    scope = V.subtree(items, item)
    parts: list[tuple[str, str]] = []  # (what it is, its text), so a refusal can name the largest
    about = about_qid(chain)
    if about is not None:
        # A follow-up on one answer (0.4.0) leads with that answer: the seats
        # read what was decided before they read the item around it.
        what = {"refine": "Refine", "drill": "Drill"}.get(fork.get("step"), "Follow-up")
        if _is_open(view, about, fork):
            # Seats called on an OPEN question: a deliberation before answering. Its result is
            # one reply on the question (a recommendation), never an answer or a lock.
            lead = (f"# Deliberation bundle: the open question `{about}`, before the owner answers it\n\n"
                    f"{OPEN_RESULT}\n\n")
        else:
            lead = f"# {what} bundle: the locked answer to `{about}`\n\n"
        parts.append((f"the question `{about}`", lead + _about_section(view, about)))
        title = f"## The request, then the item context: `{item}` and all under it"
    else:
        title = f"# Deliberation bundle: `{item}` and all under it"
    head = [title, "",
            f"Round {len(chain)}, fork `{fork_id}`: mode {fork['mode']}, focus {fork.get('focus', 'whole')}, "
            f"seats: {_seats(fork)}.", ""]
    parts.append(("the request", "\n".join(head + _quote(fork["text"]))))
    if len(chain) > 1:
        earlier = ["## Earlier rounds", ""]
        for n, m in enumerate(chain[:-1], 1):
            qs = view["forks"][m["id"]]["questions"]
            earlier.append(f"{n}. `{m['id']}` ({m['ts']}), {m['mode']}, seats: {_seats(m)}; "
                           f"questions: {', '.join(qs) or 'none'}")
        parts.append(("the earlier rounds", "\n".join(earlier)))
    listed = ["## Items in scope", ""]
    for i in V.tree_order(items):
        if i in scope:
            d = items[i]
            status = f" ({d['status']})" if d.get("status") else ""
            listed.append(f"- `{i}` {d.get('title', '')}{status}")
    parts.append(("the item list", "\n".join(listed)))
    sheet = V.sheet_markdown(V.answers_sheet(view, items, item=item)).replace("# Answers:", "## Answers:", 1)
    parts.append(("the answers sheet", sheet.rstrip()))
    for i in V.tree_order(items):
        msgs = view["threads"].get(i, []) if i in scope else []
        if msgs:
            body = [f"## Thread on `{i}`", ""]
            for m in sorted(msgs, key=lambda m: m["seq"]):
                tag = f", intent {m['intent']}" if m.get("intent") else ""
                body.append(f"**{m['by']}**, {m['ts']}{tag}:")
                body += _quote(m["text"]) + [""]
            parts.append((f"the thread on `{i}`", "\n".join(body).rstrip()))
    text = "\n\n".join(t for _, t in parts) + "\n"
    size = len(text.encode("utf-8"))
    if size > max_bytes:
        biggest = sorted(parts, key=lambda p: -len(p[1].encode("utf-8")))[:3]
        named = ", ".join(f"{name} ({len(t.encode('utf-8'))} bytes)" for name, t in biggest)
        raise BundleTooLarge(f"the bundle for `{item}` is {size} bytes, over the {max_bytes}-byte limit. "
                             f"Largest parts: {named}. It is refused, not trimmed: deliberate on a narrower item")
    return text

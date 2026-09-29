"""What a deliberation's committee reads: the round's bundle (spec §6.3, D14, §7.5 F3).

The bundle is the fork request, its earlier rounds, the item and every item
under it, every question and answer in that scope, and the threads there. The
project's own sources (specs, findings) are found by the session from the item
ids; this module knows nothing about any project.

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
    if not m.get("roles"):
        return "the default committee"
    return ", ".join(r.removeprefix("other:") for r in m["roles"])


def _quote(text: str) -> list[str]:
    return [f"> {line}" for line in text.splitlines()] or [">"]


def fork_context(view: dict, items: Mapping[str, dict], fork_id: str, max_bytes: int = MAX_BUNDLE) -> str:
    if fork_id not in view["forks"]:
        raise KeyError(f"no fork {fork_id!r}")
    chain = rounds(view, fork_id)
    fork = chain[-1]
    item = fork["item"]
    scope = V.subtree(items, item)
    parts: list[tuple[str, str]] = []  # (what it is, its text), so a refusal can name the largest
    head = [f"# Deliberation bundle: `{item}` and all under it", "",
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

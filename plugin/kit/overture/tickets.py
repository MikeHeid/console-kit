"""Tickets — lightweight work units hanging off items .

A ticket is a child issue scoped to an item. It is not a Linear/Jira clone: no
workflow engine, no custom fields, no saved views. Overture's tickets exist as the
downstream artifact of Launch Idea and as a place to pin bugs / research / grilling
notes against an item.

Storage shape — a single `STATE/tickets.json` written whole, O_NOFOLLOW, atomic:

    {
      "tickets": {
        "T-aAb3": {
          "id": "T-aAb3",
          "parent_item": "A.1",
          "kind": "task"       | "bug" | "research" | "grilling",
          "title": "...",
          "body": "...",       (may be empty)
          "status": "open"     | "blocked" | "closed",
          "blocked_by": ["T-xYz1", ...],
          "created_at": "...",
          "created_by": "owner" | "agent",
          "agent": "optional agent name on an agent write",
          "closed_at": "...",   (set when status == closed)
          "updated_at": "..."
        }
      }
    }

Trust: this module writes under STATE only. The server never reads a ticket's text
and never exposes it beyond the owner door; a ticket is never emitted to a peer.
Caps keep the file bounded against an agent that writes in a loop:

- Up to `MAX_TICKETS` live rows; above this a create raises `TicketError` by name.
- Each title / body capped at `MAX_TITLE` / `MAX_BODY` characters.
- Blocking is acyclic: a `blocked_by` closure that reaches `id` is refused.
- A write that would make the file exceed `MAX_FILE` is refused by name.
"""

from __future__ import annotations

import json
import os
import re
import secrets
from pathlib import Path

from . import atfile as AF

FILE = "tickets.json"
MAX_FILE = 2 << 20       # 2 MiB on disk — ~10k rows at the title/body average
MAX_TICKETS = 10_000
MAX_TITLE = 500
MAX_BODY = 20_000
MAX_BLOCKED_BY = 32      # per ticket

KINDS = ("task", "bug", "research", "grilling")
STATUSES = ("open", "blocked", "closed")
WRITERS = ("owner", "agent")
# Ticket id: T- + base64url chars. bumped from 4 → 8 random bytes (64-bit) after the
# adversarial review noted the 2^32 variant had a birthday-collision risk around 93k tickets.
ID_RE = re.compile(r"^T-[A-Za-z0-9_-]{4,20}\Z")
# Item id: a repo-shaped identifier. Looser than schema.ITEM but tighter than "anything".
ITEM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-]{0,127}\Z")

STATE_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC


class TicketError(ValueError):
    """A refusal the owner sees; carries no project secret."""


def _new_id(existing: dict) -> str:
    """Generate a fresh ticket id; retries up to 8 times on a collision.

    bumped from `token_urlsafe(4)[:6]` (32 bits of entropy, ~93k birthday collision)
    to `token_urlsafe(8)[:10]` (~64 bits), which pushes the collision horizon well past the
    MAX_TICKETS=10_000 cap. The retry loop stays as a safety net; it should essentially never fire.
    """
    for _ in range(8):
        tid = "T-" + secrets.token_urlsafe(8)[:10]
        if tid not in existing:
            return tid
    raise TicketError("could not pick a fresh ticket id after 8 tries")


def load(state: Path) -> dict:
    """Read `STATE/tickets.json`, returning `{"tickets": {id: ticket}}`. Missing → empty.

    pre-1.30-dot-1 this silently DROPPED rows whose shape didn't match the current schema;
    the next save() then rewrote the file WITHOUT them — permanent data loss on any schema
    drift. The reviewer flagged this as HIGH. Now `load` preserves every row with a well-formed
    id key (and whose value is a dict); filtering by `kind`/`status`/`title` happens at
    `as_view` time instead, so a bad row is just skipped in the live view and not erased from
    disk. One `_unknown` key counts rows we had to drop at parse time; stderr logs it so the
    operator sees the drift without losing anything.
    """
    sfd = os.open(state, STATE_FLAGS)
    try:
        raw = AF.read_at(sfd, FILE, MAX_FILE)
    finally:
        os.close(sfd)
    if raw is None:
        return {"tickets": {}}
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise TicketError(f"{FILE} is not JSON: {e}") from None
    if not isinstance(doc, dict) or not isinstance(doc.get("tickets"), dict):
        raise TicketError(f"{FILE} must be an object with a 'tickets' map")
    out: dict = {"tickets": {}}
    dropped = 0
    for k, v in doc["tickets"].items():
        if not isinstance(k, str) or not ID_RE.match(k):
            dropped += 1
            continue
        if not isinstance(v, dict):
            dropped += 1
            continue
        out["tickets"][k] = v
    if dropped:
        import sys as _sys
        _sys.stderr.write(f"console tickets: {dropped} row(s) with malformed id/shape ignored (not deleted)\n")
    return out


def _row_is_viewable(v: object) -> bool:
    """Does this ticket row have the fields the view renderer needs?

    this check moved out of `load` so a row that fails it is skipped in the view but
    kept on disk. If a kind/status enum ever grows, old rows survive the next save.
    """
    if not isinstance(v, dict):
        return False
    if v.get("kind") not in KINDS:
        return False
    if v.get("status") not in STATUSES:
        return False
    if not isinstance(v.get("title"), str):
        return False
    if not isinstance(v.get("body", ""), str):
        return False
    if not isinstance(v.get("parent_item"), str) or not ITEM_RE.match(v["parent_item"]):
        return False
    return True


def save(state: Path, doc: dict) -> None:
    """Replace `STATE/tickets.json` whole, O_NOFOLLOW. Atomic via `atfile.write_at`."""
    data = json.dumps({"tickets": doc.get("tickets", {})}, sort_keys=True, ensure_ascii=False).encode("utf-8")
    if len(data) > MAX_FILE:
        raise TicketError(f"tickets: file would be {len(data)} bytes; limit is {MAX_FILE}")
    sfd = os.open(state, STATE_FLAGS)
    try:
        AF.write_at(sfd, FILE, data)
    finally:
        os.close(sfd)


def _check_title(v: object) -> str:
    if not isinstance(v, str):
        raise TicketError("title must be a string")
    v = v.strip()
    if not v:
        raise TicketError("title must not be empty")
    if len(v) > MAX_TITLE:
        raise TicketError(f"title is {len(v)} characters; limit is {MAX_TITLE}")
    if "\n" in v or "\r" in v:
        raise TicketError("title must be one line")
    return v


def _check_body(v: object) -> str:
    if v is None:
        return ""
    if not isinstance(v, str):
        raise TicketError("body must be a string")
    if len(v) > MAX_BODY:
        raise TicketError(f"body is {len(v)} characters; limit is {MAX_BODY}")
    return v


def _check_parent(item: object) -> str:
    if not isinstance(item, str) or not ITEM_RE.match(item):
        raise TicketError("parent_item must be a repo-shaped id")
    return item


def _check_blocked_by(blocked_by: object, tickets: dict, self_id: str | None) -> list[str]:
    if blocked_by is None:
        return []
    if not isinstance(blocked_by, list):
        raise TicketError("blocked_by must be a list of ticket ids")
    if len(blocked_by) > MAX_BLOCKED_BY:
        raise TicketError(f"blocked_by has {len(blocked_by)} entries; limit is {MAX_BLOCKED_BY}")
    out: list[str] = []
    for b in blocked_by:
        if not isinstance(b, str) or not ID_RE.match(b):
            raise TicketError(f"blocked_by entry {b!r} is not a ticket id")
        if b == self_id:
            raise TicketError("a ticket cannot block itself")
        if b not in tickets:
            raise TicketError(f"blocked_by references unknown ticket {b!r}")
        out.append(b)
    # Acyclic check: walk the blocker chain; if we re-reach self_id, it's a cycle.
    if self_id is not None:
        seen: set[str] = set()
        stack = list(out)
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            if cur == self_id:
                raise TicketError("blocked_by would create a cycle")
            cur_t = tickets.get(cur)
            if isinstance(cur_t, dict):
                stack.extend(cur_t.get("blocked_by") or [])
    return out


def create(state: Path, body: dict, by: str, now_iso: str, agent: str | None = None) -> dict:
    """Create a ticket. Returns the stored row."""
    if by not in WRITERS:
        raise TicketError(f"ticket writer {by!r} must be owner or agent")
    if not isinstance(body, dict):
        raise TicketError("ticket create takes an object")
    parent = _check_parent(body.get("parent_item"))
    kind = body.get("kind", "task")
    if kind not in KINDS:
        raise TicketError(f"kind {kind!r} must be one of {', '.join(KINDS)}")
    title = _check_title(body.get("title"))
    text = _check_body(body.get("body"))
    doc = load(state)
    tickets = doc["tickets"]
    if len(tickets) >= MAX_TICKETS:
        raise TicketError(f"ticket count would exceed {MAX_TICKETS}")
    blocked = _check_blocked_by(body.get("blocked_by"), tickets, None)
    tid = _new_id(tickets)
    row = {
        "id": tid, "parent_item": parent, "kind": kind, "title": title, "body": text,
        "status": "blocked" if blocked else "open",
        "blocked_by": blocked,
        "created_at": now_iso, "updated_at": now_iso, "created_by": by,
    }
    if agent is not None and by == "agent":
        row["agent"] = agent
    tickets[tid] = row
    save(state, {"tickets": tickets})
    return row


def update(state: Path, tid: str, body: dict, now_iso: str) -> dict:
    """Patch a ticket's title, body, or blocked_by. Returns the updated row.

    Status is NOT patched here (use `close` or `reopen`); writing status directly
    would skip the acyclic check and the closed_at bookkeeping.
    """
    if not ID_RE.match(tid):
        raise TicketError(f"ticket id {tid!r} is malformed")
    doc = load(state)
    tickets = doc["tickets"]
    row = tickets.get(tid)
    if row is None:
        raise TicketError(f"no ticket {tid!r}")
    allowed = {"title", "body", "blocked_by"}
    unknown = set(body) - allowed
    if unknown:
        raise TicketError(f"unknown update field(s) {', '.join(sorted(unknown))}")
    patched = dict(row)
    if "title" in body:
        patched["title"] = _check_title(body["title"])
    if "body" in body:
        patched["body"] = _check_body(body["body"])
    if "blocked_by" in body:
        new_blocked = _check_blocked_by(body["blocked_by"], tickets, tid)
        patched["blocked_by"] = new_blocked
        # Status follows blocking: a blocked ticket stays blocked while anything blocks it.
        if patched["status"] != "closed":
            patched["status"] = "blocked" if new_blocked else "open"
    patched["updated_at"] = now_iso
    tickets[tid] = patched
    save(state, {"tickets": tickets})
    return patched


def close(state: Path, tid: str, now_iso: str) -> dict:
    """Close a ticket. Dependents of this ticket drop it from their blocked_by on next update."""
    if not ID_RE.match(tid):
        raise TicketError(f"ticket id {tid!r} is malformed")
    doc = load(state)
    tickets = doc["tickets"]
    row = tickets.get(tid)
    if row is None:
        raise TicketError(f"no ticket {tid!r}")
    patched = dict(row)
    patched["status"] = "closed"
    patched["closed_at"] = now_iso
    patched["updated_at"] = now_iso
    tickets[tid] = patched
    # Unblock dependents whose only blocker was this one.
    for other_id, other in tickets.items():
        if other.get("status") == "blocked" and tid in (other.get("blocked_by") or []):
            new_blocks = [b for b in other["blocked_by"] if b != tid]
            tickets[other_id] = {**other, "blocked_by": new_blocks,
                                 "status": "blocked" if new_blocks else "open",
                                 "updated_at": now_iso}
    save(state, {"tickets": tickets})
    return patched


def as_view(doc: dict) -> dict:
    """Shape for inclusion in `view.tickets`: grouped by parent item, with counts.

    filters by `_row_is_viewable` so a row the `load` relaxation preserved on disk but
    that doesn't fit the current view schema is skipped here without KeyError.
    """
    tickets = doc.get("tickets") or {}
    by_item: dict[str, list[dict]] = {}
    for t in tickets.values():
        if not _row_is_viewable(t):
            continue
        by_item.setdefault(t["parent_item"], []).append(t)
    # Sort each group newest-first.
    for lst in by_item.values():
        lst.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    counts: dict[str, dict] = {}
    for item_id, lst in by_item.items():
        c = {"open": 0, "blocked": 0, "closed": 0}
        for t in lst:
            s = t.get("status")
            if s in c:
                c[s] += 1
        counts[item_id] = c
    return {"by_item": by_item, "counts": counts}

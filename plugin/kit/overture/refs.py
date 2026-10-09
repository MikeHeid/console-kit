r"""Dotted-number references for items (1.7.0).

An item in `items.json` can carry an optional `ref` field: a dotted number like
`"1"`, `"1.2"`, `"1.2.1"`. The owner uses these in chat, on calls and in notes
to point at specific work. The kit owns their assignment and persistence so
numbers do not drift or collide across pushes.

### Rules

- A ref is dotted non-zero digits:  ``^[1-9][0-9]*(\.[1-9][0-9]*)*$``.
- The ref's parent prefix equals the parent item's ref. So an item with ref
  `1.2` has `parent` whose ref is `1`. An item with no parent (a *topic*) has
  a single-segment ref like `1` or `2`.
- A ref, once given to an id, is permanent for that id. It never changes
  under the item, and never gets reused by another id.
- When the item leaves `items.json`, the ref is **retired**, not freed. The
  number stays reserved so the next push does not reuse it.

### Where the state lives

`STATE/refs.json`:

    {
      "assignments": {"AB-1": "1", "AB-2": "1.1", "XX-7": "2"},
      "retired":     {"1.3": "AB-9"}
    }

Keys of `assignments` are current item ids. Keys of `retired` are refs whose
id has left the register. `retired` is append-only; the server never deletes
rows there.

### Flow at items-push

- The caller passes `assign_refs(state, items)` the resolved {id: data} map.
- Any item whose row carries a `ref` is checked: it must match the stored
  assignment (if any) and its prefix must equal its parent's ref. A mismatch
  is a `RefError` whose message names the item + the stored ref.
- Any item without a `ref` whose auto-assign is on gets the next free number
  under its parent. The free number is one past the max currently used under
  that parent (across both current and retired).
- The write is JSON-whole, dir-relative, O_NOFOLLOW. A read failure keeps
  everything in memory and surfaces the error — no partial state.

### What this module does NOT do

- No cycle checking. `items.snapshot_problem` already catches a parent chain
  the view cannot walk; this module just reads parents.
- No `item-move`. That is a 1.8.0 command.
- No UI. The browser renders the ref from `items[id].ref` once the server
  writes it back into the live view (see `items.SnapshotAdapter`).
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from . import atfile as AF

FILE = "refs.json"
MAX_FILE = 1 << 20
REF_RE = re.compile(r"^[1-9][0-9]*(?:\.[1-9][0-9]*)*$")
STATE_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC


class RefError(ValueError):
    """An item's own ref is inconsistent with the stored assignment, its parent's ref, or its shape."""


def load(state: Path) -> dict:
    """Read `STATE/refs.json`, returning `{"assignments": {...}, "retired": {...}}`.

    An absent file returns the empty shape. A malformed file raises `RefError` with the first bad field;
    `assign_refs` lets that propagate so the owner sees the problem instead of a silent reset.
    """
    sfd = os.open(state, STATE_FLAGS)
    try:
        raw = AF.read_at(sfd, FILE, MAX_FILE)
    except FileNotFoundError:
        return {"assignments": {}, "retired": {}}
    finally:
        os.close(sfd)
    if raw is None:
        return {"assignments": {}, "retired": {}}
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise RefError(f"{FILE} is not JSON: {e}") from None
    if not isinstance(doc, dict):
        raise RefError(f"{FILE} is not an object")
    a = doc.get("assignments", {})
    r = doc.get("retired", {})
    if not (isinstance(a, dict) and isinstance(r, dict)):
        raise RefError(f"{FILE}: assignments and retired must be objects")
    for k, v in a.items():
        if not isinstance(k, str) or not isinstance(v, str) or not REF_RE.match(v):
            raise RefError(f"{FILE}: assignments[{k!r}] = {v!r} is not a dotted-number ref")
    for k, v in r.items():
        if not isinstance(k, str) or not REF_RE.match(k) or not isinstance(v, str):
            raise RefError(f"{FILE}: retired[{k!r}] = {v!r} is not a dotted-number key")
    return {"assignments": a, "retired": r}


def save(state: Path, doc: dict) -> None:
    """Replace `STATE/refs.json` whole, O_NOFOLLOW. Raises OSError on an unwritable dir."""
    data = json.dumps({"assignments": doc.get("assignments", {}),
                       "retired": doc.get("retired", {})}, sort_keys=True).encode()
    sfd = os.open(state, STATE_FLAGS)
    try:
        AF.write_at(sfd, FILE, data)
    finally:
        os.close(sfd)


def assign_refs(state: Path, items: dict, auto: bool = True) -> dict[str, str]:
    """Reconcile `items` against stored assignments. Returns {id: ref} for every item that now has a ref.

    - Items with a stored ref keep it; a disagreeing `ref` in the push is refused.
    - Items pushed with an explicit `ref` that matches the stored assignment (or has none) are
      recorded.
    - Items with no stored ref and no pushed ref are auto-assigned when `auto` is true; otherwise
      left unassigned (and absent from the return).
    - Items that have left the register move to `retired`. Nothing new is freed.
    - The file is rewritten whole on any change.

    Raises `RefError` on the first conflict; nothing is written in that case.
    """
    doc = load(state)
    assignments: dict[str, str] = dict(doc["assignments"])
    retired: dict[str, str] = dict(doc["retired"])

    # Build a parent lookup: id -> parent-id-or-None.
    parent_of: dict[str, str | None] = {k: (v or {}).get("parent") if isinstance(v, dict) else None
                                        for k, v in items.items()}
    # Pass 1: verify every pushed ref against the stored row + the parent prefix.
    for item_id, data in items.items():
        pushed = (data or {}).get("ref") if isinstance(data, dict) else None
        if pushed is not None:
            if not isinstance(pushed, str) or not REF_RE.match(pushed):
                raise RefError(f"items.{item_id}.ref {pushed!r} is not a dotted number like 1 or 1.2.1")
            stored = assignments.get(item_id)
            if stored and stored != pushed:
                raise RefError(f"item {item_id} has ref {stored!r}; refs do not change; "
                               f"use `agent.py item-move` to move the item instead")
            want = _parent_ref(parent_of, assignments, item_id)
            if want is not None:
                # Prefix must equal the parent's ref AND the ref must be exactly one segment deeper.
                if not pushed.startswith(want + ".") or "." in pushed[len(want) + 1:]:
                    raise RefError(f"items.{item_id}.ref {pushed!r} must be exactly one segment under "
                                   f"the parent's ref {want!r} (e.g. {want}.1, {want}.2, …)")
            elif "." in pushed:
                # No parent → ref must be a single segment.
                raise RefError(f"items.{item_id}.ref {pushed!r} has no parent, so its ref must be a "
                               f"single segment like 1 or 2")
            # Check uniqueness within this push.
            for other_id, other_ref in assignments.items():
                if other_ref == pushed and other_id != item_id:
                    raise RefError(f"items.{item_id}.ref {pushed!r} is already assigned to {other_id!r}")
            # 1.19.1: refs are "never reused" — if `pushed` is a retired ref for a different id,
            # refuse rather than silently reclaim it.
            if pushed in retired and retired[pushed] != item_id:
                raise RefError(f"items.{item_id}.ref {pushed!r} was retired from {retired[pushed]!r}; "
                               f"refs are not reused — pick a different number")
            # If the id is a returning retired one keeping its own number, restore it.
            if pushed in retired and retired[pushed] == item_id:
                del retired[pushed]
            assignments[item_id] = pushed

    # Pass 2: pre-build the already-used set for free-number scanning (current + retired).
    used_by_parent: dict[str | None, set[str]] = {}
    for iid, r in assignments.items():
        used_by_parent.setdefault(_parent_segment_key(r), set()).add(r.rsplit(".", 1)[-1])
    for r in retired:
        used_by_parent.setdefault(_parent_segment_key(r), set()).add(r.rsplit(".", 1)[-1])

    # Pass 3: auto-assign the still-missing ones. Walk parents-first so a child sees its parent's ref.
    # 1.19.1: a retired id that comes back gets its old ref restored (refs are permanent per id).
    retired_by_id: dict[str, str] = {rid: ref for ref, rid in retired.items()}
    if auto:
        for item_id in _topological(items):
            if item_id in assignments:
                continue
            # If this id was retired before, restore its old number — never re-generate.
            old = retired_by_id.get(item_id)
            if old is not None:
                assignments[item_id] = old
                del retired[old]
                continue
            parent = parent_of.get(item_id)
            parent_ref = assignments.get(parent) if parent else None
            if parent and parent_ref is None:
                # Parent has no ref yet (maybe disabled on purpose, or missing). Skip for now.
                continue
            next_seg = _next_segment(used_by_parent.get(parent_ref, set()))
            new_ref = f"{parent_ref}.{next_seg}" if parent_ref else next_seg
            assignments[item_id] = new_ref
            used_by_parent.setdefault(parent_ref, set()).add(next_seg)

    # Pass 4: retire assignments whose id has left the register.
    for stale_id in list(assignments.keys()):
        if stale_id not in items:
            retired[assignments[stale_id]] = stale_id
            del assignments[stale_id]

    new_doc = {"assignments": assignments, "retired": retired}
    if new_doc != doc:
        save(state, new_doc)
    return dict(assignments)


def _parent_ref(parent_of: dict, assignments: dict, item_id: str) -> str | None:
    parent = parent_of.get(item_id)
    return assignments.get(parent) if parent else None


def _parent_segment_key(ref: str) -> str | None:
    """The parent-ref that this ref sits under, or None for a top-level number."""
    i = ref.rfind(".")
    return ref[:i] if i >= 0 else None


def _next_segment(used: set[str]) -> str:
    """The smallest positive integer, as a string, not already in `used`."""
    n = 1
    while str(n) in used:
        n += 1
    return str(n)


def _topological(items: dict) -> list[str]:
    """Parent-before-child order. Items whose parent is missing from items[] are treated as roots.

    1.19.1: iterative — a 1000-item parent chain was hitting Python's recursion limit via the old
    recursive visit (snapshot_problem doesn't cap chain depth).
    """
    out: list[str] = []
    seen: set[str] = set()
    for start in items:
        if start in seen:
            continue
        # Walk the parent chain of `start` into `stack` in reverse so we can emit parent-first;
        # a cycle is terminated by the `on_path` set.
        stack: list[str] = []
        on_path: set[str] = set()
        cur = start
        while cur is not None and cur in items and cur not in seen and cur not in on_path:
            stack.append(cur)
            on_path.add(cur)
            cur = (items[cur] or {}).get("parent") if isinstance(items[cur], dict) else None
        while stack:
            iid = stack.pop()
            if iid not in seen:
                out.append(iid)
                seen.add(iid)
    return out

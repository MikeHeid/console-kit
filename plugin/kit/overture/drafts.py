"""Server-backed draft text for the owner (1.19.0).

A draft is unsent compose text — an answer the owner was writing, a chat message half-typed,
a visual brief in progress. Up to now these lived in each browser's `localStorage`, so a draft
written at home never reached the office browser. This module stores them server-side under
`STATE/drafts.json`, so an owner across multiple browsers sees the same drafts.

Shape of the file:

    {
      "keys": {
        "chat:": {"text": "half-finished question", "ts": "2026-10-08T14:12:00Z"},
        "answer:ABC/Q3": {"text": "my own words...", "ts": "..."}
      }
    }

Each key is a shaped string from the browser side; the server does not interpret it — it just
mirrors the owner's text under that key. An empty text removes the entry (so a lock or send clears
the draft everywhere).

Limits:

- Up to `MAX_KEYS` live entries (older-ts entries are pruned at save time when the cap is hit).
- Each text is capped at `MAX_TEXT` characters, matching the schema cap for a question's text.
- The whole file is capped at `MAX_FILE` bytes; a write that would exceed it is refused by name.
- The file is written whole, dir-relative, O_NOFOLLOW — same path as `items.json` / `refs.json`.

Trust: this module is owner-privileged. The server never reads or writes it based on an agent
request — only the owner-door (`/api/draft`) touches it. Nothing in the drafts file reaches a
peer or any outbound channel.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from . import atfile as AF

FILE = "drafts.json"
MAX_FILE = 1 << 20       # 1 MiB on disk
MAX_TEXT = 20_000        # characters per draft (matches schema.MAX_TEXT)
MAX_KEYS = 500           # live entries; older-ts entries pruned above this
MAX_KEY_LEN = 256
# A draft key is a shaped tag like "chat:", "answer:AB-1/Q3", "delegate:text". Shape it so a bad
# string cannot escape the drafts namespace or embed anything that becomes a path or URL.
KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@\-]{0,255}$")
STATE_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC


class DraftError(ValueError):
    """A refusal the owner sees; carries no project secret."""


def load(state: Path) -> dict:
    """Read `STATE/drafts.json`, returning `{"keys": {key: {text, ts}}}`.

    An absent or empty file returns the empty shape. A malformed file raises `DraftError`.
    """
    sfd = os.open(state, STATE_FLAGS)
    try:
        raw = AF.read_at(sfd, FILE, MAX_FILE)
    except FileNotFoundError:
        return {"keys": {}}
    finally:
        os.close(sfd)
    if raw is None:
        return {"keys": {}}
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise DraftError(f"{FILE} is not JSON: {e}") from None
    if not isinstance(doc, dict) or not isinstance(doc.get("keys"), dict):
        raise DraftError(f"{FILE} must be an object with a 'keys' map")
    out: dict = {"keys": {}}
    for k, v in doc["keys"].items():
        if not isinstance(k, str) or not KEY_RE.match(k):
            continue
        if not isinstance(v, dict):
            continue
        text = v.get("text")
        ts = v.get("ts")
        if not isinstance(text, str) or not isinstance(ts, str):
            continue
        out["keys"][k] = {"text": text, "ts": ts}
    return out


def save(state: Path, doc: dict) -> None:
    """Replace `STATE/drafts.json` whole, O_NOFOLLOW. Raises OSError on an unwritable dir."""
    data = json.dumps({"keys": doc.get("keys", {})}, sort_keys=True).encode()
    if len(data) > MAX_FILE:
        raise DraftError(f"drafts: file would be {len(data)} bytes; limit is {MAX_FILE}")
    sfd = os.open(state, STATE_FLAGS)
    try:
        AF.write_at(sfd, FILE, data)
    finally:
        os.close(sfd)


def set_draft(state: Path, key: str, text: str, now_iso: str) -> dict:
    """Set the draft for `key` to `text`, timestamped `now_iso`. Empty text removes the key.

    Returns the resulting `view.drafts` map ({key: text}). Prunes oldest entries when `MAX_KEYS`
    is exceeded. Raises `DraftError` on a bad key or an over-cap text.
    """
    if not isinstance(key, str) or not KEY_RE.match(key):
        raise DraftError(f"draft key {key!r} must be alnum + _.:/@-, up to {MAX_KEY_LEN} chars")
    if not isinstance(text, str):
        raise DraftError("draft text must be a string")
    if len(text) > MAX_TEXT:
        raise DraftError(f"draft text is {len(text)} characters; limit is {MAX_TEXT}")
    doc = load(state)
    keys = dict(doc["keys"])
    if text == "":
        keys.pop(key, None)
    else:
        keys[key] = {"text": text, "ts": now_iso}
    if len(keys) > MAX_KEYS:
        # Prune oldest-ts entries down to the cap.
        ordered = sorted(keys.items(), key=lambda kv: kv[1].get("ts") or "")
        drop = len(keys) - MAX_KEYS
        for k, _ in ordered[:drop]:
            keys.pop(k, None)
    save(state, {"keys": keys})
    return as_view(keys)


def as_view(keys: dict) -> dict:
    """Shape for inclusion in the live view: just `{key: text}`."""
    return {k: v["text"] for k, v in keys.items() if isinstance(v, dict) and isinstance(v.get("text"), str)}

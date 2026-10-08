"""The owner's starred visuals and items, kept in STATE/favorites.json.

The owner stars something ("Favorite" tab, 0.8.19) so it stays easy to find.
Only the owner door writes; no agent route touches it. The file is a tiny JSON
array of keys of the form `visual:<record id>` or `item:<item id>`, with a
newest-first order; a toggle moves the key to the front or removes it.

The file is read and written through a held descriptor of STATE, with the same
guarantees items.json uses: O_NOFOLLOW opens, O_EXCL temporary and rename on
write, a directory in its place renamed aside and kept.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
from pathlib import Path

from . import atfile as AF
from . import schema as S

FILE = "favorites.json"
MAX_BYTES = 1 << 20          # a star is a short key; 1 MiB is already thousands of them
MAX_COUNT = 10_000
STATE_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
SET_ASIDE = ".favorites.json.set-aside."

# a record id is lowercase hex (store.jsonl); an item id is schema.ITEM_ID.
_VISUAL = re.compile(r"^visual:[0-9a-f]{8,64}$")
_ITEM = re.compile(r"^item:(.+)$")


def problem(key: object) -> str | None:
    """Why `key` is not a favorite target; None when it is one.

    The console accepts only `visual:<record id>` and `item:<item id>`: a key
    the server does not store, like a question id, is refused so the file
    cannot be filled with keys the view cannot resolve.
    """
    if not isinstance(key, str) or len(key) > 256:
        return "a favorite is a string of at most 256 characters"
    if _VISUAL.match(key):
        return None
    m = _ITEM.match(key)
    if m and len(m.group(1)) <= 128 and S.ITEM_ID.match(m.group(1)):
        return None
    return (f"{key[:60]!r} is not a favorite target: expected visual:<record id> "
            "or item:<item id>")


class Favorites:
    """The owner's starred list, newest first; read as data by the view."""

    def __init__(self, state: Path) -> None:
        self.state = Path(state)
        self._lock = threading.Lock()
        self._seen: tuple[int, int] | None = None
        self._keys: list[str] = []

    def _load_locked(self) -> list[str]:
        sfd = os.open(self.state, STATE_FLAGS)
        try:
            try:
                st = os.stat(FILE, dir_fd=sfd, follow_symlinks=False)
            except FileNotFoundError:
                self._seen, self._keys = None, []
                return self._keys
            key = (st.st_mtime_ns, st.st_size)
            if key != self._seen:
                raw = AF.read_at(sfd, FILE, MAX_BYTES)
                if raw is None:  # a symlink, a FIFO, or similar: treat as empty, log once
                    if self._seen is not None or self._keys:
                        sys.stderr.write(
                            f"console favorites: {Path(self.state, FILE)} is not a plain file; "
                            "serving no favorites until a star replaces it\n")
                    self._seen, self._keys = None, []
                    return self._keys
                try:
                    doc = json.loads(raw.decode("utf-8"))
                except (ValueError, UnicodeDecodeError, RecursionError):
                    sys.stderr.write(
                        f"console favorites: {Path(self.state, FILE)} is not JSON; "
                        "serving no favorites until a star replaces it\n")
                    self._seen, self._keys = None, []
                    return self._keys
                keys = doc.get("favorites") if isinstance(doc, dict) else None
                if not isinstance(keys, list):
                    self._seen, self._keys = None, []
                    return self._keys
                # Drop any key the schema no longer accepts; de-duplicate preserving order.
                seen: set[str] = set()
                out: list[str] = []
                for k in keys:
                    if isinstance(k, str) and problem(k) is None and k not in seen:
                        seen.add(k)
                        out.append(k)
                    if len(out) >= MAX_COUNT:
                        break
                self._keys = out
                self._seen = key
            return self._keys
        finally:
            os.close(sfd)

    def list(self) -> list[str]:
        with self._lock:
            return list(self._load_locked())

    def toggle(self, key: str) -> dict:
        """Add `key` to the front, or remove it if already starred. Returns the new state and the keys.

        Raises ValueError when `key` is not a favorite target or the list is at MAX_COUNT.
        """
        why = problem(key)
        if why:
            raise ValueError(why)
        with self._lock:
            cur = list(self._load_locked())
            if key in cur:
                cur.remove(key)
                state = "off"
            else:
                if len(cur) >= MAX_COUNT:
                    raise ValueError(f"the favorites list is at {MAX_COUNT}; remove one to add another")
                cur.insert(0, key)
                state = "on"
            self._write_locked(cur)
            self._keys = cur
            return {"state": state, "favorites": list(cur)}

    def _write_locked(self, keys: list[str]) -> None:
        data = json.dumps({"favorites": keys}, sort_keys=False).encode("utf-8")
        if len(data) > MAX_BYTES:
            raise ValueError(f"the favorites file would be over {MAX_BYTES} bytes")
        sfd = os.open(self.state, STATE_FLAGS)
        try:
            aside = AF.set_aside_dir(sfd, FILE)
            if aside:
                sys.stderr.write(
                    f"console favorites: a directory at {Path(self.state, FILE)} was set aside as {aside}\n")
            AF.write_at(sfd, FILE, data)
            # Invalidate the mtime/size cache so the next load re-reads.
            self._seen = None
        finally:
            os.close(sfd)

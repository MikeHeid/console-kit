"""The doorbell, from the agent's side: read it, wait on it, and remember how far the agent got (spec §7.3).

The server appends one JSON line to `inbox.jsonl` for every owner write. An
open session runs `agent.py watch`, which blocks until a line that should wake
it arrives, then exits; the exit is what wakes the session. Only the owner's
two requests wake it: `process` (answers are in) and `fork` (deliberate).

The agent's own cursor, the highest doorbell seq it has processed, is kept in
`agent-cursor.json` beside the doorbell. It only moves forward.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Callable

from .registry import read_regular

WAKE_INTENTS = ("process", "fork")
CURSOR_FILE = "agent-cursor.json"
MAX_DOORBELL = 64 << 20  # one short line per owner write; far past any real console
MAX_CURSOR = 4096


def read_lines(path: Path) -> list[dict]:
    """Every complete doorbell line. A final line with no newline is still being written, so it waits.

    Only a regular file is read, and never by blocking on it: a FIFO or device at
    the path is refused by name (`RegistryError`) rather than hanging a watch.
    """
    data = read_regular(Path(path), MAX_DOORBELL)
    if data is None:
        return []
    raw = data.decode("utf-8", errors="replace")  # a bad byte spoils only its own line, which is then skipped
    out = []
    for line in raw.split("\n")[:-1]:  # the piece after the last newline is incomplete or empty
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue  # a damaged line is skipped, never guessed at; the store is the record
        if isinstance(rec, dict) and isinstance(rec.get("seq"), int) and not isinstance(rec["seq"], bool):
            out.append(rec)
    return out


def pending(path: Path, since: int, intents: tuple[str, ...] | None = WAKE_INTENTS) -> list[dict]:
    """Doorbell lines after `since`, oldest first; with `intents`, only the lines carrying one of them."""
    return [r for r in read_lines(path)
            if r["seq"] > since and (intents is None or r.get("intent") in intents)]


def watch(path: Path, since: int, *, poll: float = 2.0, timeout: float | None = None,
          sleep: Callable[[float], None] = time.sleep,
          clock: Callable[[], float] = time.monotonic) -> list[dict]:
    """Block until a wake line arrives after `since`, and return every wake line waiting.

    A line already there when the watch starts returns at once, so a signal sent
    while no session was watching is never missed. With `timeout`, an empty list
    means it ran out.
    """
    start = clock()
    while True:
        found = pending(path, since)
        if found:
            return found
        if timeout is not None and clock() - start >= timeout:
            return []
        sleep(poll)


def read_cursor(state: Path) -> int:
    try:
        data = read_regular(Path(state) / CURSOR_FILE, MAX_CURSOR)
        v = json.loads(data.decode("utf-8")).get("through", 0) if data is not None else 0
    except (OSError, ValueError, AttributeError):
        return 0
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0


def write_cursor(state: Path, through: int) -> int:
    """Record that the agent has processed up to `through`; never moves backwards. Returns the cursor now held."""
    now = max(read_cursor(state), through)
    p = Path(state) / CURSOR_FILE
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".agent-cursor.", suffix=".tmp")  # 0600, unique, from creation
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"through": now}) + "\n")
    os.replace(tmp, p)
    return now

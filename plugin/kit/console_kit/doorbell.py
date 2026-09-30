"""The doorbell, from the agent's side: read it, wait on it, and remember how far the agent got (spec §7.3).

The server appends one JSON line to `inbox.jsonl` for every owner write. An
open session runs `agent.py watch`, which blocks until a line that should wake
it arrives, then exits; the exit is what wakes the session. Only the owner's
requests wake it: `process` (answers are in), `fork` (deliberate), `chat` (0.7.0) and
`visual` (0.8.0, draw a visual of an item).

The agent's own cursor, the highest doorbell seq it has processed, is kept in
`agent-cursor.json` beside the doorbell. It only moves forward.
"""

from __future__ import annotations

import calendar
import json
import os
import stat
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable

from .registry import read_regular

WAKE_INTENTS = ("process", "fork", "chat", "visual")
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
          clock: Callable[[], float] = time.monotonic,
          heartbeat: Callable[[], None] | None = None) -> list[dict]:
    """Block until a wake line arrives after `since`, and return every wake line waiting.

    A line already there when the watch starts returns at once, so a signal sent
    while no session was watching is never missed. With `timeout`, an empty list
    means it ran out. `heartbeat`, if given, is called once per poll while the
    watch waits (see `Heartbeat`).
    """
    start = clock()
    while True:
        found = pending(path, since)
        if found:
            return found
        if timeout is not None and clock() - start >= timeout:
            return []
        if heartbeat is not None:
            heartbeat()
        sleep(poll)


# "Agent listening" (0.6.0). A watch that is waiting on the doorbell says so in
# `watch.json` beside it: `watching`, when it last said so (`at`), and the time
# by which it promises to say so again (`until`). The server reads it and shows
# the owner "listening" only while that promise is kept, so a watch killed
# without a word (SIGKILL, a closed laptop) lapses to "idle" on its own, and a
# watch that exits normally says so at once. It is a hint for the owner, never
# something the doorbell depends on: a failed write is reported and the watch
# goes on.
WATCH_FILE = "watch.json"
BEAT_EVERY = 10.0     # seconds between writes while waiting; a 2 s poll does not write every 2 s
BEAT_GRACE = 20.0     # slack past the promised next beat before "listening" lapses
MAX_WATCH = 4096
MAX_PROMISE = 86400   # an `until` further ahead than this is not believed
TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _ts(t: float) -> str:
    return time.strftime(TS_FORMAT, time.gmtime(t))


def _parse_ts(v: object) -> float | None:
    if not isinstance(v, str) or len(v) > 40:
        return None
    try:
        return float(calendar.timegm(time.strptime(v, TS_FORMAT)))
    except ValueError:
        return None


def write_watch(state: Path, watching: bool, *, every: float = BEAT_EVERY, now: float | None = None) -> None:
    """Record whether a watch is waiting now; `every` is how soon it promises to write again."""
    t = time.time() if now is None else now
    rec = {"watching": watching, "at": _ts(t), "until": _ts(t + every + BEAT_GRACE) if watching else None}
    p = Path(state) / WATCH_FILE
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".watch.", suffix=".tmp")  # 0600 from creation
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, sort_keys=True) + "\n")
        os.replace(tmp, p)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _read_watch(path: Path) -> bytes | None:
    """The heartbeat's bytes, or None when absent.

    Trust: the state dir is the server's own, mode 0700, so only this user can
    put anything in it. Even so, the file is opened with O_NOFOLLOW (a symlink
    raises ELOOP) and O_NONBLOCK, and must be a small regular file: a link
    planted to make some other file read as a heartbeat reads as "never".
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_WATCH:
            raise OSError(f"{path} is not a small regular file")
        return os.read(fd, MAX_WATCH + 1)[:MAX_WATCH]
    finally:
        os.close(fd)


def listening(state: Path, *, now: float | None = None) -> dict:
    """What the owner is told: {"state": "listening"|"idle"|"never", "last_seen": ts|None}.

    Anything unreadable, malformed or not a regular file reads as "never", by
    name of state, never as "listening": the safe error is to under-claim.
    """
    t = time.time() if now is None else now
    try:
        data = _read_watch(Path(state) / WATCH_FILE)
        rec = json.loads(data.decode("utf-8")) if data is not None else None
    except (OSError, ValueError):
        rec = None
    if not isinstance(rec, dict):
        return {"state": "never", "last_seen": None}
    at, until = _parse_ts(rec.get("at")), _parse_ts(rec.get("until"))
    if at is None or at > t + 60:  # a stamp from the future is a broken clock or a forged file
        return {"state": "never", "last_seen": None}
    if (rec.get("watching") is True and until is not None
            and at <= until <= at + MAX_PROMISE and t <= until):
        return {"state": "listening", "last_seen": rec["at"]}
    return {"state": "idle", "last_seen": rec["at"]}


class Heartbeat:
    """Write `watch.json` on the first call, then at most once per `every` seconds.

    A write that fails is reported once on stderr and never stops the watch.
    """

    def __init__(self, state: Path, poll: float, *, clock: Callable[[], float] = time.monotonic,
                 warn: Callable[[str], None] | None = None) -> None:
        self.state = Path(state)
        self.every = max(BEAT_EVERY, poll)  # a slow poll promises its own, longer, interval
        self.clock = clock
        self.warn = warn or (lambda m: sys.stderr.write(m + "\n"))
        self._last: float | None = None
        self._warned = False

    def _write(self, watching: bool) -> None:
        try:
            write_watch(self.state, watching, every=self.every)
        except OSError as e:
            if not self._warned:
                self._warned = True
                self.warn(f"console-kit: cannot record the watch in {self.state / WATCH_FILE}: {e}; "
                          "the owner will not see 'agent listening'")

    def __call__(self) -> None:
        now = self.clock()
        if self._last is None or now - self._last >= self.every:
            self._last = now
            self._write(True)

    def stop(self) -> None:
        """The watch is over (woken, timed out or interrupted): say so now, not after the promise lapses."""
        self._write(False)


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

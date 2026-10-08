"""Agent names (0.8.2): which agent session wrote a record, when several share one console.

Every write through the agent's door is stored `by: "agent"`, so with several
sessions on one console the owner could not tell them apart. An agent may now
say who it is: `agent.py --as agent-6` (or OVERTURE_AGENT=agent-6), which
sends the name in the `X-Console-Agent` header. The name is optional; a
session that gives none writes exactly what 0.8.1 wrote.

**Why the name is not a field of the store record.** `schema.validate`
refuses a field it does not know, BY NAME ("question: unknown field(s)
agent"), and `Store._load` refuses the WHOLE store on the first line that
fails validation. A name written into `store.jsonl` would therefore stop a
0.8.1 kit from starting on that store at all: a rollback would lose the
console, not only the names. So the store records stay exactly the 0.8.1
shape, SCHEMA_VERSION stays 1, and each name lives in a sidecar beside the
store:

    STATE/names.jsonl    one line per named record: {"agent": NAME, "id": RECORD_ID}

A 0.8.1 kit never opens it and reads the store as it always did. The view,
the feed and `fold.py export` join the two by record id, adding `agent` only
to records that have a name, so an unnamed record renders exactly as before.

The file is append-only, like the store. A record keeps the first name it
was given: a retried write (same nonce, same content, so the same record id)
never renames it. A line that is not a well-formed name for a record id is
refused on load, by line number, never skipped.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import threading
from pathlib import Path

from . import schema as S

FILE = "names.jsonl"
HEADER = "X-Console-Agent"
ENV = "OVERTURE_AGENT"
# A short plain token: a lowercase letter, then lowercase letters, digits and
# single hyphens, at most 32 characters ("agent-6", "review-bot"). It is shown
# on the owner's page and printed into the rulings record, so it is held to
# exactly this shape.
NAME = re.compile(r"^[a-z](?:[a-z0-9]|-(?=[a-z0-9])){0,31}\Z")
MAX_NAME = 32
# "agent" is the shared bucket of every session that gives no name (the working
# marks, `server.working`), and "owner" is the owner's own author word: an agent
# named either would read as someone else.
RESERVED = frozenset({"agent", "owner"})
UNNAMED = "agent"


class NamesError(Exception):
    """A names file the kit refuses to read; the message names the line."""


def problem(v: object) -> str | None:
    """Why `v` is not an agent name, or None when it is one."""
    if not isinstance(v, str):
        return "an agent name must be a string"
    if v in RESERVED:
        return (f"agent name {v!r} is reserved ({'the unnamed sessions share it' if v == UNNAMED else 'it is the owner'}"
                f"); pick another, such as agent-6")
    if len(v) > MAX_NAME or not NAME.fullmatch(v):   # fullmatch: `$` alone lets a trailing newline through
        return (f"agent name {v!r} is refused: use 1 to {MAX_NAME} characters of lowercase letters, digits and "
                f"single hyphens, starting with a letter (e.g. agent-6)")
    return None


class Names:
    """The names sidecar of one state directory: record id -> agent name."""

    def __init__(self, state: Path) -> None:
        self.path = Path(state) / FILE
        self._lock = threading.Lock()
        self._by_id: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        raw = self.path.read_text(encoding="utf-8")
        lines = raw.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        elif lines:
            raise NamesError(f"{self.path}: line {len(lines)} has no newline, so a write was cut short. Move that "
                             f"line aside by hand; the kit does not guess what it said")
        for n, line in enumerate(lines, 1):
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as e:
                raise NamesError(f"{self.path}:{n}: not JSON ({e.msg})") from None
            if not isinstance(rec, dict) or set(rec) != {"id", "agent"}:
                raise NamesError(f"{self.path}:{n}: a line is exactly {{\"agent\": NAME, \"id\": RECORD_ID}}")
            if not isinstance(rec["id"], str) or not S.RECORD_ID.match(rec["id"]):
                raise NamesError(f"{self.path}:{n}: id {rec['id']!r} is not a record id")
            why = problem(rec["agent"])
            if why:
                raise NamesError(f"{self.path}:{n}: {why}")
            self._by_id.setdefault(rec["id"], rec["agent"])  # the first name a record got is its name

    def get(self, rid: object) -> str | None:
        return self._by_id.get(rid) if isinstance(rid, str) else None

    def mapping(self) -> dict[str, str]:
        """A copy of record id -> name."""
        with self._lock:
            return dict(self._by_id)

    def add(self, rid: str, name: str) -> bool:
        """Name record `rid`, unless it already has a name (a retried write keeps the first); True when written."""
        why = problem(name)
        if why:
            raise NamesError(why)
        if not isinstance(rid, str) or not S.RECORD_ID.match(rid):
            raise NamesError(f"{rid!r} is not a record id")
        with self._lock:
            if rid in self._by_id:
                return False
            line = json.dumps({"agent": name, "id": rid}, sort_keys=True, separators=(",", ":")) + "\n"
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as fh:
                fcntl.flock(fh, fcntl.LOCK_EX)
                try:
                    fh.write(line)
                    fh.flush()
                    os.fsync(fh.fileno())
                finally:
                    fcntl.flock(fh, fcntl.LOCK_UN)
            self._by_id[rid] = name
            return True


def load_beside(store_path: Path) -> dict[str, str]:
    """Record id -> name from the names file beside a store file (`fold.py export`); {} when there is none."""
    return Names(Path(store_path).parent).mapping()


def named(rec: dict, names: dict[str, str] | None) -> dict:
    """`rec` with its agent name added, or `rec` itself (the same object) when it has none."""
    n = names.get(rec.get("id")) if names else None
    return {**rec, "agent": n} if n else rec

"""Append-only JSONL store for the owner console (spec §4.1; rules R3 and R4; decision D3).

This class is the only writer to its file. A record goes in once and is never
edited or removed. `append` enforces the rules that need the other records:

- a qid is minted once;
- an answer names an existing question and picks only that question's options;
- a lock locks the question's CURRENT answer, and locks it only once;
- once an answer is locked, the next answer must name it in `supersedes` and
  give a `reason`, because a lock is superseded, never undone (D3);
- an `anchor` re-anchors only the question's CURRENT lock (0.5.0);
- a roar runs at most once per lock (a new lock after a supersede may roar again), and its fork carries one transcript (0.8.0);
- a `visual` answers an owner's visual request on that request's item (0.8.0).

A retried write (same content, same nonce) returns the stored record and
writes nothing, so a flaky network cannot duplicate a message.

**One writer process per file.** The rule checks run under a threading lock
against this process's in-memory index; `flock` guards only the physical
append. Two processes appending to one file could both pass a check (say,
both lock the same answer) before either write lands. The console server is
the file's only writer (spec §4.4), and the agent writes through it, never
by opening a second `Store` on the live file.
"""

from __future__ import annotations

import datetime as _dt
import fcntl
import json
import os
import threading
from pathlib import Path
from typing import Callable, Iterable, Mapping

from . import refactor as RF
from . import schema as S


# At most this many questions come out of one fork (spec §6.6, an architect
# default the owner may veto).
MAX_FORK_QUESTIONS = 5
# A deliberation is its first round plus follow-ups; each round starts an agent run (§7.7).
MAX_ROUNDS = 4


class StoreError(Exception):
    """Raised for a record the store refuses, or a file it cannot read. The message says why."""


def utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Store:
    def __init__(self, path: Path, *, known_items: Iterable[str] | Mapping[str, dict] | None = None,
                 clock: Callable[[], str] = utc_now) -> None:
        self.path = Path(path)
        self.known_items = None if known_items is None else frozenset(known_items)
        # item -> parent, when the register is known as a mapping: a fork's scope is
        # its item and everything under it (D14), which a flat id set cannot say.
        self.item_parents: dict[str, str | None] | None = None
        if isinstance(known_items, Mapping):
            self.set_items(known_items)
        self.clock = clock
        self._lock = threading.Lock()
        self._records: list[dict] = []
        self._by_id: dict[str, dict] = {}
        self._load()
        # CONSOLE-kit/Q30-Q32: what the owner did about stale answers, in a file of its own beside this one,
        # so a kit that predates it never meets a record kind it would refuse the whole store over.
        self.refactor = RF.Log(self.path.parent, clock)

    def set_items(self, items: Mapping[str, dict]) -> None:
        """Take the register as it is now: the ids a write may name (R7) and each item's parent."""
        self.known_items = frozenset(items)
        self.item_parents = {i: (d.get("parent") if isinstance(d, Mapping) else None) for i, d in items.items()}

    # -- reading -------------------------------------------------------------

    def _load(self) -> None:
        if not self.path.exists():
            return
        raw = self.path.read_text(encoding="utf-8")
        lines = raw.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        elif lines:
            raise StoreError(f"{self.path}: line {len(lines)} has no newline, so a write was cut short. "
                             f"Move that line aside by hand; the store does not guess what it said")
        for n, line in enumerate(lines, 1):
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as e:
                raise StoreError(f"{self.path}:{n}: not JSON ({e.msg})") from None
            if isinstance(rec, dict) and rec.get("schemaVersion") != S.SCHEMA_VERSION:
                raise StoreError(f"{self.path}:{n}: schemaVersion {rec.get('schemaVersion')!r}; "
                                 f"this kit reads only {S.SCHEMA_VERSION}")
            errs = S.validate(rec)
            if errs:
                raise StoreError(f"{self.path}:{n}: " + "; ".join(errs))
            if rec.get("seq") != n or rec.get("id") != S.record_id(rec):
                raise StoreError(f"{self.path}:{n}: seq or id does not match the line; the file was edited")
            self._index(rec)

    def _index(self, rec: dict) -> None:
        self._records.append(rec)
        self._by_id[rec["id"]] = rec

    def records(self) -> list[dict]:
        """Return every record in append order. The list is a copy; the records are shared, so don't mutate them."""
        return list(self._records)

    def seq(self) -> int:
        """The sequence number of the newest record; 0 for an empty store."""
        return len(self._records)

    def get(self, rid: str) -> dict | None:
        """The record with id `rid`, or None."""
        return self._by_id.get(rid) if isinstance(rid, str) else None

    def question(self, qid: str) -> dict | None:
        return next((r for r in self._records if r["type"] == "question" and r["qid"] == qid), None)

    def answers(self, qid: str) -> list[dict]:
        return [r for r in self._records if r["type"] == "answer" and r["qid"] == qid]

    def head(self, qid: str) -> dict | None:
        """The question's current answer, which is its most recent one."""
        a = self.answers(qid)
        return a[-1] if a else None

    def lock_of(self, answer_id: str) -> dict | None:
        return next((r for r in self._records if r["type"] == "lock" and r["answer"] == answer_id), None)

    def locks(self, qid: str) -> list[dict]:
        return [r for r in self._records if r["type"] == "lock" and r["qid"] == qid]

    def anchor_of(self, lock_id: str) -> dict | None:
        """The latest `anchor` record for this lock, or None (0.5.0)."""
        return next((r for r in reversed(self._records) if r["type"] == "anchor" and r["lock"] == lock_id), None)

    # -- writing -------------------------------------------------------------

    def append(self, rec: dict) -> dict:
        """Check `rec`'s shape and the store's rules, then write it and return the stored record."""
        rec = dict(rec)
        rec.setdefault("schemaVersion", S.SCHEMA_VERSION)
        for f in ("id", "seq", "ts"):
            if f in rec:
                raise StoreError(f"{f} is the store's to assign, not the writer's")
        errs = S.validate(rec)
        if errs:
            raise StoreError("; ".join(errs))
        rid = S.record_id(rec)
        with self._lock:
            if rid in self._by_id:
                return self._by_id[rid]
            self._check_rules(rec)
            rec["id"] = rid
            rec["seq"] = len(self._records) + 1
            rec["ts"] = self.clock()
            self._write(rec)
            self._index(rec)
            return rec

    def existing(self, rec: dict) -> dict | None:
        """The stored record `rec` would dedupe to (same fields, same nonce), or None."""
        full = dict(rec)
        full.setdefault("schemaVersion", S.SCHEMA_VERSION)
        return self._by_id.get(S.record_id(full))

    def trial(self) -> "Store":
        """A throwaway copy of this store that checks every rule but writes nothing (0.7.0).

        "Lock all & process" appends the whole batch here first, so a batch
        the rules refuse anywhere is refused before any of it is written. The
        copy shares the (never mutated) records; appends to it touch no file
        and are invisible to this store.
        """
        t = Store.__new__(Store)
        t.path = self.path
        t.known_items = self.known_items
        t.item_parents = self.item_parents
        t.clock = self.clock
        t.refactor = self.refactor   # read only here: a trial writes no refactor record
        t._lock = threading.Lock()
        with self._lock:
            t._records = list(self._records)
            t._by_id = dict(self._by_id)
        t._write = lambda rec: None
        return t

    def _check_rules(self, rec: dict) -> None:
        kind = rec["type"]
        item = rec.get("item")
        # The chat's thread (0.7.0) is not a register item, and the schema admits it
        # only on a message; every other record still names a register item (R7).
        if item == S.CHAT_ITEM and kind == "message":
            pass
        elif item is not None and self.known_items is not None and item not in self.known_items:
            raise StoreError(f"item {item!r} is not in the project's item list")
        if kind == "question":
            if self.question(rec["qid"]):
                raise StoreError(f"qid {rec['qid']} already exists; a qid is minted once. Ask a new Q<n>")
            if "forked_from" in rec:
                self._check_forked(rec)
        elif kind == "message":
            if "reply_to" in rec and rec["reply_to"] not in self._by_id:
                raise StoreError(f"reply_to {rec['reply_to']!r} names no record")
            if "follow_up_of" in rec:
                self._check_follow_up(rec)
            if "about_qid" in rec:
                self._check_about(rec)
            if S.ROAR in (rec.get("roles") or ()):
                self._check_roar_once(rec)
        elif kind == "answer":
            self._check_answer(rec)
        elif kind == "lock":
            self._check_lock(rec)
        elif kind == "anchor":
            self._check_anchor(rec)
        elif kind == "transcript":
            self._check_transcript(rec)
        elif kind == "visual":
            self._check_visual(rec)

    def lock_at(self, qid: str, seq: int) -> dict | None:
        """The lock on `qid` that was current just before store seq `seq`: the latest lock written before it.

        A lock is only ever superseded by a later answer AND its own later lock
        (D3), so the latest lock before a record is the one that record saw.
        """
        return next((r for r in reversed(self._records[:max(0, seq - 1)])
                     if r["type"] == "lock" and r["qid"] == qid), None)

    def roar_of(self, qid: str, lock_id: str | None = None) -> dict | None:
        """The roar fork already run on `qid` while `lock_id` was its lock (default: its current lock), or None.

        The fork record needs no lock field: the lock it was about is derived
        from the store, as the lock current when the fork was appended.
        """
        if lock_id is None:
            lk = self.locks(qid)
            lock_id = lk[-1]["id"] if lk else None
        if lock_id is None:
            return None
        for r in self._records:
            if (r["type"] == "message" and r.get("intent") == "fork" and S.ROAR in (r.get("roles") or ())
                    and r.get("about_qid") == qid):
                seen = self.lock_at(qid, r["seq"])
                if seen is not None and seen["id"] == lock_id:
                    return r
        return None

    def _check_roar_once(self, rec: dict) -> None:
        """A roar runs at most once per LOCK (owner ruling "Once per lock ★", 0.8.0).

        A second roar while the same lock stands is refused, naming the first.
        Once the answer is superseded and locked again, the new lock may have
        its own roar. (`_check_about` has already required a locked answer.)
        """
        qid = rec["about_qid"]
        locks = self.locks(qid)
        if not locks:
            return
        current = locks[-1]
        first = self.roar_of(qid, current["id"])
        if first is not None:
            raise StoreError(f"{qid}'s lock {current['id']} already had its roar: fork {first['id']}, requested "
                             f"{first['ts']}. A roar runs at most once per lock; supersede and re-lock the answer "
                             f"to roar again, or follow up with other seats")

    def transcript_of(self, fork_id: str) -> dict | None:
        return next((r for r in self._records if r["type"] == "transcript" and r["fork"] == fork_id), None)

    def _check_transcript(self, rec: dict) -> None:
        """A transcript belongs to a roar fork, one per fork (0.8.0)."""
        fork = self._by_id.get(rec["fork"])
        if (fork is None or fork["type"] != "message" or fork.get("intent") != "fork"
                or S.ROAR not in (fork.get("roles") or ())):
            raise StoreError(f"fork {rec['fork']!r} is not a roar fork; a transcript is a roar panel's")
        first = self.transcript_of(fork["id"])
        if first is not None:
            raise StoreError(f"fork {fork['id']} already has its transcript ({first['id']}); a fork keeps one")

    def _check_visual(self, rec: dict) -> None:
        """A visual answers an owner visual request on the same item, at most MAX_VISUALS_PER_REQUEST each (0.8.0)."""
        req = self._by_id.get(rec["request"])
        if req is None or req["type"] != "message" or req.get("intent") != "visual":
            raise StoreError(f"request {rec['request']!r} is not an owner visual request")
        if req["item"] != rec["item"]:
            raise StoreError(f"request {req['id']} is on item {req['item']!r}; its visual goes on the same item")
        n = sum(1 for r in self._records if r["type"] == "visual" and r["request"] == req["id"])
        if n >= S.MAX_VISUALS_PER_REQUEST:
            raise StoreError(f"request {req['id']} already has {n} visuals; the limit is "
                             f"{S.MAX_VISUALS_PER_REQUEST}. Ask the owner for a new request")

    def _check_answer(self, rec: dict) -> None:
        q = self.question(rec["qid"])
        if q is None:
            raise StoreError(f"no question {rec['qid']}")
        ids = [o["id"] for o in q["options"]]
        stray = [p for p in rec["picks"] if p not in ids]
        if stray:
            raise StoreError(f"pick(s) {stray} are not options of {rec['qid']}; say it in your own words instead")
        if q["kind"] == "single" and len(rec["picks"]) > 1:
            raise StoreError(f"{rec['qid']} takes one pick, got {len(rec['picks'])}")
        head = self.head(rec["qid"])
        if "supersedes" in rec and (head is None or rec["supersedes"] != head["id"]):
            raise StoreError(f"supersedes {rec['supersedes']!r} is not the current answer of {rec['qid']}")
        if head is not None and self.lock_of(head["id"]) and "supersedes" not in rec:
            raise StoreError(f"{rec['qid']} is locked. A new answer supersedes the lock: "
                             f"name it (supersedes={head['id']}) and give a reason. A lock is never undone")

    def _check_forked(self, rec: dict) -> None:
        """A forked question names an owner fork message, and a fork writes at most MAX_FORK_QUESTIONS (§6.3, §6.6)."""
        fork = self._by_id.get(rec["forked_from"])
        # "Owner" needs no check here: the schema refuses intent 'fork' from any
        # other writer, on append and on load, so no agent fork can be in the file.
        if fork is None or fork["type"] != "message" or fork.get("intent") != "fork":
            raise StoreError(f"forked_from {rec['forked_from']!r} is not an owner fork message")
        n = sum(1 for r in self._records if r["type"] == "question" and r.get("forked_from") == fork["id"])
        if n >= MAX_FORK_QUESTIONS:
            raise StoreError(f"fork {fork['id']} already has {n} questions; the limit is {MAX_FORK_QUESTIONS}")

    def _check_follow_up(self, rec: dict) -> None:
        """A follow-up names an earlier owner fork message on the same item (D13, §7.5)."""
        fork = self._by_id.get(rec["follow_up_of"])
        if fork is None or fork["type"] != "message" or fork.get("intent") != "fork":
            raise StoreError(f"follow_up_of {rec['follow_up_of']!r} is not an owner fork message")
        if fork["item"] != rec["item"]:
            raise StoreError(f"follow_up_of names a fork on {fork['item']!r}; a follow-up stays on its fork's item")
        rounds, node = 1, fork
        while node.get("follow_up_of") in self._by_id and rounds <= MAX_ROUNDS:
            node, rounds = self._by_id[node["follow_up_of"]], rounds + 1
        if rounds >= MAX_ROUNDS:
            raise StoreError(f"this deliberation already has {rounds} rounds; the limit is {MAX_ROUNDS}. "
                             f"Start a new deliberation instead")

    def _check_about(self, rec: dict) -> None:
        """A fork about one question names a question in the fork's scope (0.4.0; an open one since 0.8.8).

        The page is untrusted, so each of these is checked here, not assumed
        from the button: the question exists, and it sits on the fork's item or
        an item under it (D14). Seats and their count are the schema's check,
        which `append` has already run.

        Seats called on an OPEN question (unanswered, or answered and not
        locked) are a deliberation before answering (owner ruling
        "build_reply"): allowed, and a fork is only a message, so it changes
        nothing about the question; its answer and lock stay the owner's.
        A roar, a refine and a drill still work on a LOCKED answer only.
        """
        qid = rec["about_qid"]
        q = self.question(qid)
        if q is None:
            raise StoreError(f"about_qid {qid} names no question")
        if not self._in_scope(q["item"], rec["item"]):
            raise StoreError(f"about_qid {qid} is on item {q['item']!r}, outside this fork's scope "
                             f"({rec['item']!r} and everything under it)")
        if "step" not in rec and S.ROAR not in (rec.get("roles") or ()):
            return
        head = self.head(qid)
        if head is None or self.lock_of(head["id"]) is None:
            what = f"a {rec['step']}" if "step" in rec else "a roar"
            raise StoreError(f"{qid} has no locked answer; {what} on an answer comes after it is locked. "
                             f"To deliberate before answering, call seats instead")

    def _in_scope(self, item: str, root: str) -> bool:
        """Whether `item` is `root` or under it, walking `item_parents`; with no tree known, only `root` itself."""
        seen: set[str] = set()
        node: str | None = item
        while node is not None and node not in seen:
            if node == root:
                return True
            seen.add(node)
            node = (self.item_parents or {}).get(node)
        return False

    def _check_lock(self, rec: dict) -> None:
        a = self._by_id.get(rec["answer"])
        if a is None or a["type"] != "answer" or a["qid"] != rec["qid"]:
            raise StoreError(f"answer {rec['answer']!r} is not an answer to {rec['qid']}")
        head = self.head(rec["qid"])
        if head is None or head["id"] != a["id"]:
            raise StoreError(f"answer {a['id']} is not the current answer of {rec['qid']}; lock the current one")
        if self.lock_of(a["id"]):
            raise StoreError(f"answer {a['id']} is already locked")

    def _check_anchor(self, rec: dict) -> None:
        lk = self._by_id.get(rec["lock"])
        if lk is None or lk["type"] != "lock" or lk["qid"] != rec["qid"]:
            raise StoreError(f"lock {rec['lock']!r} is not a lock on {rec['qid']}")
        head = self.head(rec["qid"])
        if head is None or head["id"] != lk["answer"]:
            raise StoreError(f"lock {lk['id']} is not on the current answer of {rec['qid']}; "
                             f"only the current lock is re-anchored")

    def _write(self, rec: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(rec, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n"
        with open(self.path, "a", encoding="utf-8") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

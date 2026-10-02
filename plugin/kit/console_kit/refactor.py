"""What the owner did about a stale answer: refactor records, beside the store (CONSOLE-kit/Q30, Q31, Q32).

A locked answer goes stale when a condition it was locked against no longer
holds. Before this, the owner's only moves were to supersede it (a new
answer, D3) or re-lock it as it stands. This module records the three owner
rulings on what else can happen to one:

    withdraw   owner  the ruling no longer applies: it leaves the inbox, marked withdrawn, with the owner's reason
    untrack    owner  "keep, stop checking": the ruling stands, leaves the inbox, and is no longer checked
                      against the files; a reason is optional
    proposal   agent  the steward PROPOSES a new anchor: the conditions that still hold, plus excerpts the
                      SERVER read from `path:a-b` cites. It changes nothing
    confirm    owner  the owner confirms one proposal, seen side by side with the old anchor; from then on the
                      proposal's anchors decide the lock
    replaces   agent  a new question asked to replace a stale ruling (written by the server when `ask` names
                      `replaces`). The old ruling stays in force, marked stale and linked, until the new one is
                      locked; then it is SUPERSEDED. That state is derived, never written: the replacement's
                      lock is the record

Every record names the lock it acted on, so a later supersede and re-lock of
the same question makes it stop applying (a new lock is a new ruling).

**Why a file of its own.** A kit loads `store.jsonl` by validating every line
and refuses the whole store on the first record it does not know
(`store.Store._load`), so an older kit would not start on a store holding any
of these. Kept here, an older kit never opens them: a withdrawn, kept or
replaced answer reads stale again, and a confirmed anchor is not seen, so the
answer reads stale, never fresh. Nothing is lost on disk.

**What a bad file does.** Any line that fails its check (a hand edit, a
torn write) makes the WHOLE file unreadable, named in `problem`: every
refactor action is refused, naming it, and every answer reads as if no
refactor record existed (stale stays stale). The console itself keeps
running: a stuck refactor is better than a stopped console, and the safe
direction is "stale", never "fresh".

The file is read and replaced whole through `atfile` (held directory
descriptor, O_NOFOLLOW, regular files only, O_EXCL temporary, fsync,
rename), so a write is all or nothing.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

from . import atfile as AF
from . import schema as S

FILE = "refactor.jsonl"
SCHEMA_VERSION = 1
MAX_FILE = 8 << 20            # bytes; a larger file is unreadable, by name
MAX_CITES = 4                 # excerpts one proposal may cite
KINDS = ("withdraw", "untrack", "proposal", "confirm", "replaces")
OWNER_KINDS = frozenset({"withdraw", "untrack", "confirm"})
AGENT_KINDS = frozenset({"proposal", "replaces"})
WRITERS = {k: "owner" for k in OWNER_KINDS} | {k: "agent" for k in AGENT_KINDS}
REQUIRED = {
    "withdraw": ("qid", "lock", "reason", "by", "nonce"),
    "untrack": ("qid", "lock", "by", "nonce"),
    "proposal": ("qid", "lock", "base", "anchors", "cites", "basis", "by", "nonce"),
    "confirm": ("qid", "lock", "proposal", "by", "nonce"),
    "replaces": ("qid", "replaces", "lock", "by", "nonce"),
}
OPTIONAL = {"withdraw": frozenset(), "untrack": frozenset({"reason"}), "proposal": frozenset(),
            "confirm": frozenset(), "replaces": frozenset()}
STORE_FIELDS = frozenset({"id", "seq", "ts", "schemaVersion", "type"})
OUTCOMES = ("withdrawn", "untracked", "superseded")


class RefactorError(Exception):
    """A refactor record the log refuses, or a log that cannot be read. The message says why."""


def validate(rec: object) -> list[str]:
    """Every problem with `rec`'s shape; an empty list means well-formed. The rules needing the store are `Log`'s."""
    if not isinstance(rec, dict):
        return ["a refactor record must be a JSON object"]
    kind = rec.get("type")
    if kind not in KINDS:
        return [f"unknown refactor record type {kind!r}; expected one of {', '.join(KINDS)}"]
    errs = []
    if rec.get("schemaVersion") != SCHEMA_VERSION:
        errs.append(f"schemaVersion {rec.get('schemaVersion')!r} is not {SCHEMA_VERSION}")
    missing = [k for k in REQUIRED[kind] if k not in rec]
    if missing:
        return errs + [f"{kind}: missing {', '.join(missing)}"]
    unknown = sorted(set(rec) - set(REQUIRED[kind]) - OPTIONAL[kind] - STORE_FIELDS)
    if unknown:
        errs.append(f"{kind}: unknown field(s) {', '.join(unknown)}")
    if rec["by"] != WRITERS[kind]:
        errs.append(f"{kind}: written by {rec['by']!r}, but only the {WRITERS[kind]} may write one")
    if not isinstance(rec["nonce"], str) or not S.NONCE.match(rec["nonce"]):
        errs.append(f"{kind}: nonce must be 8-64 of [A-Za-z0-9_-]")
    for f in ("qid",) + (("replaces",) if kind == "replaces" else ()):
        if not isinstance(rec[f], str) or not S.QID.match(rec[f]):
            errs.append(f"{kind}: {f} {rec[f]!r} is not <itemId>/Q<n>")
    for f in ("lock",) + (("proposal",) if kind == "confirm" else ()):
        if not isinstance(rec[f], str) or not S.RECORD_ID.match(rec[f]):
            errs.append(f"{kind}: {f} must be a store record id (24 lowercase hex)")
    if kind == "withdraw" or (kind == "untrack" and "reason" in rec):
        errs += [f"{kind}: {m}" for m in S._text(rec, "reason")]
    if kind == "proposal":
        errs += [f"proposal: {m}" for m in S.check_conditions(rec["base"], "base", allow_empty=False)]
        errs += [f"proposal: {m}" for m in S.check_conditions(rec["anchors"], "anchors", allow_empty=False)]
        errs += [f"proposal: {m}" for m in cites_problem(rec["cites"])]
        errs += [f"proposal: {m}" for m in S._text(rec, "basis")]
    if kind == "replaces" and rec["qid"] == rec["replaces"]:
        errs.append("replaces: a question cannot replace itself")
    return errs


def cites_problem(cites: object) -> list[str]:
    """Why a proposal's cites are not 1 to MAX_CITES distinct well-formed `path:a-b` cites, or []."""
    if not isinstance(cites, list) or not 1 <= len(cites) <= MAX_CITES:
        return [f"cites must be a list of 1 to {MAX_CITES} cites of the form path:line or path:first-last"]
    errs = []
    for c in cites:
        parts = S.cite_parts(c)
        if parts is None:
            errs.append(f"cite {c!r} is not path:line or path:first-last inside the project")
        elif parts[2] < parts[1]:
            errs.append(f"cite {c!r} ends before it starts")
        elif S.secret_path(parts[0]):
            errs.append(f"cite {c!r} names {S.secret_path(parts[0])}; a proposal never reads a secrets file")
    if len(set(map(str, cites))) != len(cites):
        errs.append("cites repeat")
    return errs


class Log:
    """The refactor records beside one store: loaded at start, appended whole-file through `atfile`."""

    def __init__(self, folder: Path, clock) -> None:
        self.folder = Path(folder)
        self.clock = clock
        self._lock = threading.Lock()
        self._records: list[dict] = []
        self._by_id: dict[str, dict] = {}
        self._raw = b""
        self.problem: str | None = None
        self._load()

    def _load(self) -> None:
        try:
            fd = os.open(self.folder, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        except FileNotFoundError:
            return
        try:
            raw = AF.read_at(fd, FILE, MAX_FILE)
            if raw is None:
                try:
                    os.stat(FILE, dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    return
                return self._refuse("is not a plain file (a link, a folder or a FIFO)")
        except OSError as e:
            return self._refuse(f"cannot be read ({e.strerror or type(e).__name__})")
        finally:
            os.close(fd)
        if len(raw) > MAX_FILE:
            return self._refuse(f"is over {MAX_FILE} bytes")
        lines = raw.split(b"\n")
        if lines[-1] != b"":
            return self._refuse(f"line {len(lines)} has no newline")
        recs = []
        for n, line in enumerate(lines[:-1], 1):
            try:
                rec = json.loads(line.decode("utf-8"))
            except (ValueError, RecursionError):
                return self._refuse(f"line {n} is not JSON")
            errs = validate(rec)
            if errs:
                return self._refuse(f"line {n}: " + "; ".join(errs))
            if rec.get("seq") != n or rec.get("id") != S.record_id(rec):
                return self._refuse(f"line {n}: seq or id does not match the line; the file was edited")
            recs.append(rec)
        self._raw = raw
        for rec in recs:
            self._index(rec)

    def _refuse(self, why: str) -> None:
        self.problem = (f"{FILE} {why}, so it is not read: withdraw, keep, confirm and replace are refused "
                        f"until it is fixed or moved aside, and every answer reads as if it held nothing")
        sys.stderr.write(f"console refactor: {self.folder / FILE}: {why}\n")

    def _index(self, rec: dict) -> None:
        self._records.append(rec)
        self._by_id[rec["id"]] = rec

    def records(self) -> list[dict]:
        return list(self._records)

    def get(self, rid: str) -> dict | None:
        return self._by_id.get(rid) if isinstance(rid, str) else None

    def existing(self, rec: dict) -> dict | None:
        full = {**rec, "schemaVersion": SCHEMA_VERSION}
        return self._by_id.get(S.record_id(full))

    def append(self, rec: dict, store) -> dict:
        """Check `rec`'s shape and the rules that need the store, then write it; return the stored record."""
        if self.problem:
            raise RefactorError(self.problem)
        rec = {**rec, "schemaVersion": SCHEMA_VERSION}
        for f in ("id", "seq", "ts"):
            if f in rec:
                raise RefactorError(f"{f} is the log's to assign, not the writer's")
        errs = validate(rec)
        if errs:
            raise RefactorError("; ".join(errs))
        rid = S.record_id(rec)
        with self._lock:
            if rid in self._by_id:
                return self._by_id[rid]
            self._check_rules(rec, store)
            rec["id"] = rid
            rec["seq"] = len(self._records) + 1
            rec["ts"] = self.clock()
            data = self._raw + json.dumps(rec, sort_keys=True, ensure_ascii=False,
                                          separators=(",", ":")).encode("utf-8") + b"\n"
            self.folder.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.folder, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            try:
                AF.write_at(fd, FILE, data)
            finally:
                os.close(fd)
            self._raw = data
            self._index(rec)
            return rec

    # -- the rules that need the store ---------------------------------------------------

    def _check_rules(self, rec: dict, store) -> None:
        kind, qid = rec["type"], rec["qid"]
        if kind == "replaces":
            return self._check_replaces(rec, store)
        lk = current_lock(store, qid)
        if lk is None or lk["id"] != rec["lock"]:
            raise RefactorError(f"lock {rec['lock']} is not the current lock of {qid}; reload, the answer changed")
        out = outcome(store, qid)
        if out is not None:
            raise RefactorError(f"{qid} is already {out['kind']} ({out['at']}); nothing more is done to this lock")
        if kind == "confirm":
            p = self._by_id.get(rec["proposal"])
            if p is None or p["type"] != "proposal" or p["qid"] != qid or p["lock"] != rec["lock"]:
                raise RefactorError(f"proposal {rec['proposal']} is not a proposal for {qid}'s current lock")
            done = confirmed(store, rec["lock"])
            if done is not None and done[1]["id"] == p["id"]:
                raise RefactorError(f"proposal {p['id'][:8]} for {qid} is already confirmed ({done[0]['ts']})")
            latest = proposal_of(store, rec["lock"])
            if latest is None or latest["id"] != p["id"]:
                raise RefactorError(f"proposal {p['id'][:8]} is no longer the latest for {qid}; reload, check the "
                                    f"newer one, and confirm again")

    def _check_replaces(self, rec: dict, store) -> None:
        if store.question(rec["qid"]) is None:
            raise RefactorError(f"no question {rec['qid']}")
        old = rec["replaces"]
        lk = current_lock(store, old)
        if lk is None or lk["id"] != rec["lock"]:
            raise RefactorError(f"{old} has no current lock {rec['lock']}; only a locked, stale ruling is replaced")
        out = outcome(store, old)
        if out is not None:
            raise RefactorError(f"{old} is already {out['kind']}; there is nothing in force to replace")
        if any(r["type"] == "replaces" and r["qid"] == rec["qid"] for r in self._records):
            raise RefactorError(f"{rec['qid']} already replaces a ruling")
        other = replacement_of(store, old)
        if other is not None:
            raise RefactorError(f"{old} is already being replaced by {other['qid']}; answer that question instead")


# -- reading what happened (every function takes a store; `store.refactor` is its Log) ---

def _log(store) -> Log | None:
    log = getattr(store, "refactor", None)
    return None if log is None or log.problem else log


def current_lock(store, qid: str) -> dict | None:
    """The lock on `qid`'s current answer, or None."""
    head = store.head(qid)
    return store.lock_of(head["id"]) if head is not None else None


def _latest(store, kind: str, **match) -> dict | None:
    log = _log(store)
    if log is None:
        return None
    return next((r for r in reversed(log._records)
                 if r["type"] == kind and all(r.get(k) == v for k, v in match.items())), None)


def proposal_of(store, lock_id: str) -> dict | None:
    """The latest proposal for this lock, confirmed or not."""
    return _latest(store, "proposal", lock=lock_id)


def confirmed(store, lock_id: str) -> tuple[dict, dict] | None:
    """(confirm record, its proposal) when the owner confirmed a proposal for this lock; else None."""
    c = _latest(store, "confirm", lock=lock_id)
    if c is None:
        return None
    return c, _log(store).get(c["proposal"])


def replacement_of(store, qid: str) -> dict | None:
    """The `replaces` record naming `qid`'s CURRENT lock, if a replacement was asked for it."""
    lk = current_lock(store, qid)
    return None if lk is None else _latest(store, "replaces", replaces=qid, lock=lk["id"])


def replaces_of(store, qid: str) -> dict | None:
    """The `replaces` record that says `qid` was asked to replace another ruling, if any."""
    return _latest(store, "replaces", qid=qid)


def outcome(store, qid: str) -> dict | None:
    """What became of `qid`'s current lock, or None: {kind, by, at, record, reason? | replaced_by?}.

    withdrawn and untracked are the owner's records. superseded is derived: a
    replacement was asked for this lock and its own current answer is locked.
    An earlier withdraw or keep wins over a later supersede.
    """
    lk = current_lock(store, qid)
    if lk is None:
        return None
    for kind, word in (("withdraw", "withdrawn"), ("untrack", "untracked")):
        r = _latest(store, kind, qid=qid, lock=lk["id"])
        if r is not None:
            out = {"kind": word, "by": r["by"], "at": r["ts"], "record": r["id"]}
            if "reason" in r:
                out["reason"] = r["reason"]
            return out
    link = replacement_of(store, qid)
    if link is not None:
        new_lock = current_lock(store, link["qid"])
        if new_lock is not None:
            return {"kind": "superseded", "by": new_lock["by"], "at": new_lock["ts"], "record": new_lock["id"],
                    "replaced_by": link["qid"]}
    return None


def summary(store, qid: str) -> dict:
    """What the page and the export show about `qid`'s refactor state: only the keys that apply."""
    out: dict = {}
    o = outcome(store, qid)
    if o is not None:
        out["outcome"] = o
    lk = current_lock(store, qid)
    if lk is not None:
        p = proposal_of(store, lk["id"])
        c = confirmed(store, lk["id"])
        if p is not None and (c is None or c[1]["id"] != p["id"]):
            out["proposal"] = {k: p[k] for k in ("id", "ts", "base", "anchors", "cites", "basis")}
        if c is not None:
            out["confirmed"] = {"confirm": c[0]["id"], "proposal": c[1]["id"], "by": c[0]["by"],
                                "at": c[0]["ts"], "basis": c[1]["basis"]}
        link = replacement_of(store, qid)
        if link is not None:
            out["replaced_by"] = link["qid"]
    back = replaces_of(store, qid)
    if back is not None:
        out["replaces"] = back["replaces"]
    log = getattr(store, "refactor", None)
    if log is not None and log.problem:
        out["problem"] = log.problem
    return out

"""Turn locked answers into repo records (spec R1, §4.2). This is the gate between the console and the record.

It runs in two steps, on purpose:

    export   store -> committed `console/locked/<item>__Q<n>.json`, one file per
             locked question, carrying the question's whole locked history
             (supersessions included). The agent runs it, and the files go
             through a PR like any other change.
    fold     committed files -> the project adapter's `record()`. This step
             never reads the live store (R1), only files a reviewer has seen.

`fold` is all-or-nothing: if any file is refused, nothing is recorded, and
every refusal is reported by name. A lock that was already folded is skipped,
not refused, so re-running over the whole directory is safe.

    python3 tools/console-kit/fold.py export --store PATH --out console/locked
    python3 tools/console-kit/fold.py fold --locked console/locked --ledger console/folded.txt \
        --adapter scripts/register/console_adapter.py [--dry-run]
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
from pathlib import Path
from typing import Protocol

from . import anchors as A
from . import names as N
from . import registry as R
from . import schema as S
from .store import Store


class ProjectAdapter(Protocol):
    """The project seam (spec §4.3). A project supplies one module that exposes these three functions."""

    def items(self) -> dict[str, dict]:
        """Return a map of id -> {"title": str, "parent": id | None, "status": str}."""

    def record(self, entries: list[dict], dry_run: bool) -> list[str]:
        """Write each folded entry into the project's record, and return one line per file touched."""

    def seed_questions(self) -> list[dict]:
        """Return the agent's foreseen questions, as `question` records without the store's fields."""

    # Optional, and so not part of the Protocol a checker enforces: board() -> {"shape": str,
    # "values": {key: str}}, the page's live values (AB-2/Q4), served at /api/board. Each key
    # names an element the page marks with data-live*; "shape" fingerprints everything else.


class FoldError(Exception):
    pass


RECORD_ID = S.RECORD_ID
# An answer changed this many times before locking is carried whole; past it the
# file is refused rather than bloating the record. Generous on purpose.
MAX_EARLIER = 100
MAX_FORK_QUESTIONS = 5  # the store's cap, re-checked across committed files (§6.6)
TS = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\Z")


def locked_filename(qid: str) -> str:
    return qid.replace("/", "__") + ".json"


def names_beside(store_path: Path) -> dict[str, str]:
    """Record id -> agent name from the names file beside a store (0.8.2); {} when there is none."""
    return N.load_beside(store_path)


def export(store: Store, names: dict[str, str] | None = None) -> dict[str, dict]:
    """Return filename -> entry for every question whose CURRENT answer is locked.

    `names` (0.8.2) maps record id -> agent name: a question an agent asked
    under a name carries `asked_by_agent`, so a ruling can say which agent
    asked. A question with no name gets no such key, so its file is byte for
    byte what 0.8.1 exported.
    """
    out: dict[str, dict] = {}
    for q in (r for r in store.records() if r["type"] == "question"):
        answers = store.answers(q["qid"])
        locks = [(a, store.lock_of(a["id"])) for a in answers]
        locked = [(a, lk) for a, lk in locks if lk is not None]
        if not locked or locked[-1][0]["id"] != answers[-1]["id"]:
            continue
        history = [_entry_answer(store, q, a, lk) for a, lk in locked]
        # An answer changed before it was locked is still what the owner said.
        # Dropping it lost the owner's own words from the record (2026-09-28,
        # TC-lane/Q1), so every unlocked answer is carried as `earlier`.
        earlier = [_entry_earlier(q, a) for a, lk in locks if lk is None]
        entry = {
            "schemaVersion": S.SCHEMA_VERSION,
            "qid": q["qid"],
            "item": q["item"],
            "question": q["text"],
            "kind": q["kind"],
            "options": q["options"],
            "star": q["star"],
            "source": q["source"],
            "asked_by": q["by"],
            "asked_at": q["ts"],
            "valid_if": q["valid_if"],
            "locked": history[-1],
            "history": history[:-1],
            "earlier": earlier,
            # Every answer id in store order, so fold can replay the store's rule.
            "order": [a["id"] for a in answers],
        }
        # `evidence` (0.7.0) only when present, so a file for a question without it is byte-identical to 0.6.0.
        entry.update({k: q[k] for k in ("forked_from", "star_by", "evidence") if k in q})
        if names and names.get(q["id"]):
            entry["asked_by_agent"] = names[q["id"]]
        if "forked_from" in q:
            # The fork message travels with the file, so `fold`, which never reads
            # the store (R1), can still check the id against the message it names.
            entry["fork"] = next(r for r in store.records() if r["id"] == q["forked_from"])
        out[locked_filename(q["qid"])] = entry
    return out


def _entry_earlier(q: dict, a: dict) -> dict:
    labels = {o["id"]: o["label"] for o in q["options"]}
    e = {"answer_id": a["id"], "picks": a["picks"],
         "picked_labels": [labels[p] for p in a["picks"] if p in labels],
         "own_text": a["own_text"], "by": a["by"], "answered_at": a["ts"]}
    # An answer that superseded a lock and was then changed before its own lock
    # still said why it superseded; that reason is the owner's words too.
    if "supersedes" in a:
        e["supersedes"], e["reason"] = a["supersedes"], a["reason"]
    return e


def _entry_answer(store: Store, q: dict, a: dict, lk: dict) -> dict:
    labels = {o["id"]: o["label"] for o in q["options"]}
    e = {
        "answer_id": a["id"],
        "picks": a["picks"],
        "picked_labels": [labels[p] for p in a["picks"] if p in labels],
        "picked_star": q["star"] is not None and q["star"] in a["picks"],
        "rejected_labels": [o["label"] for o in q["options"] if o["id"] not in a["picks"]],
        "own_text": a["own_text"],
        "by": a["by"],
        "answered_at": a["ts"],
        "lock_id": lk["id"],
        "locked_by": lk["by"],
        "locked_at": lk["ts"],
    }
    if "supersedes" in a:
        e["supersedes"] = a["supersedes"]
        e["reason"] = a["reason"]
    e.update(_entry_anchors(store, q, lk))
    return e


def _entry_anchors(store: Store, q: dict, lk: dict) -> dict:
    """What this lock is actually checked against, when that is not the question's `valid_if` (0.5.0).

    The file keeps `valid_if` as the question put it. A lock re-anchored on
    re-lock carries `anchored_by: lock` and its `anchors`; one re-anchored by
    `agent.py reanchor` also names the `anchor` record, its evidence (`basis`)
    and when it was written. A lock decided by `valid_if` adds nothing, so its
    file is byte for byte what 0.4.0 exported.
    """
    conds, origin, rec = A.lock_conditions(store, q, lk)
    if origin == "question":
        return {}
    out = {"anchored_by": origin, "anchors": conds}
    if rec is not None:
        out.update({"anchor_id": rec["id"], "anchor_basis": rec["basis"], "anchored_at": rec["ts"]})
    return out


def write_export(entries: dict[str, dict], out: Path) -> list[str]:
    """Write only the files that changed, in a stable byte form, and return the names written."""
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for name, entry in sorted(entries.items()):
        text = json.dumps(entry, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        p = out / name
        if not p.exists() or p.read_text(encoding="utf-8") != text:
            p.write_text(text, encoding="utf-8")
            written.append(name)
    return written


def check_entry(name: str, e: object, items: dict[str, dict]) -> list[str]:
    """Return every reason `fold` must refuse this file; an empty list means it may be folded."""
    if not isinstance(e, dict):
        return [f"{name}: not a JSON object"]
    if e.get("schemaVersion") != S.SCHEMA_VERSION:
        return [f"{name}: schemaVersion {e.get('schemaVersion')!r} is not {S.SCHEMA_VERSION}"]
    errs = []
    qid = e.get("qid")
    if not isinstance(qid, str) or locked_filename(qid) != name:
        errs.append(f"{name}: file name does not match qid {qid!r}")
    if e.get("item") not in items:
        errs.append(f"{name}: item {e.get('item')!r} does not resolve to a project item")
    for k in ("question", "source", "kind"):
        if not isinstance(e.get(k), str) or not e[k]:
            errs.append(f"{name}: missing {k}: the question as put is part of the record")
    # The committed file arrives through a PR, not through the store, so it is
    # checked again with the SAME rules the store applied to the question.
    # Every field the rulings record prints inline (qid, source, labels) must
    # pass them, or a crafted file could print a fake ruling.
    as_record = {"type": "question", "schemaVersion": S.SCHEMA_VERSION, "qid": qid, "item": e.get("item"),
                 "text": e.get("question"), "kind": e.get("kind"), "options": e.get("options"),
                 "star": e.get("star"), "valid_if": e.get("valid_if"), "source": e.get("source"),
                 "by": e.get("asked_by"), "nonce": "export-check"}
    as_record.update({k: e[k] for k in ("forked_from", "star_by", "evidence") if k in e})
    errs += [f"{name}: {m}" for m in S.validate(as_record)]
    if not isinstance(e.get("asked_at"), str) or not TS.match(e["asked_at"]):
        errs.append(f"{name}: asked_at {e.get('asked_at')!r} is not a store timestamp")
    if "asked_by_agent" in e:  # 0.8.2: printed into the rulings record, so held to the name's shape
        why = N.problem(e["asked_by_agent"])
        if why:
            errs.append(f"{name}: asked_by_agent: {why}")
        elif e.get("asked_by") != "agent":
            errs.append(f"{name}: asked_by_agent names an agent, but asked_by is {e.get('asked_by')!r}")
    opts = e.get("options")
    ok_opts = isinstance(opts, list) and all(isinstance(o, dict) for o in opts)
    ids = [o.get("id") for o in opts] if ok_opts else []
    labels = {o.get("id"): o.get("label") for o in opts} if ok_opts else {}
    chain = ([*e["history"], e["locked"]]
             if isinstance(e.get("history"), list) and isinstance(e.get("locked"), dict) else None)
    if chain is None:
        return errs + [f"{name}: not locked: no `locked` answer"]
    for a in chain:
        errs += _check_locked_answer(name, a, ids, labels, e.get("star"))
    # `earlier` is absent from files exported before it existed; they were
    # folded then, and read as having no earlier answers.
    earlier = e.get("earlier", [])
    if not isinstance(earlier, list) or len(earlier) > MAX_EARLIER:
        errs.append(f"{name}: earlier must be a list of at most {MAX_EARLIER} answers")
    else:
        for a in earlier:
            errs += _check_earlier_answer(name, a, ids, labels)
    errs += _check_fork(name, e)
    errs += _check_sequence(name, e, chain, earlier if isinstance(earlier, list) else [])
    return errs


def _check_sequence(name: str, e: dict, chain: list, earlier: list) -> list[str]:
    """Replay the store's answer rule (`Store._check_answer`) over the question's answers in order.

    The rule, answer by answer: the first supersedes nothing; one that follows a
    LOCKED answer must name it in `supersedes` (a lock is superseded, never undone,
    D3); one that follows an unlocked answer may name only that answer. Checking
    anything looser, such as "some answer supersedes each lock", let a decoy
    earlier answer carry the real link while the locked answer printed a made-up
    supersession (PR #168 security review, HIGH).

    `order` lists every answer id in store order. A file exported before it
    existed has no `earlier` either, so its answers are exactly the locked chain,
    in order, and each lock after the first must name the one before it.
    """
    if not all(isinstance(a, dict) for a in chain + earlier):
        return []  # already reported by the per-answer checks
    order = e.get("order")
    if order is None:
        if earlier:
            return [f"{name}: a file with earlier answers must carry their order"]
        order = [a.get("answer_id") for a in chain]
    by_id = {a.get("answer_id"): a for a in chain + earlier}
    if (not isinstance(order, list) or len(order) != len(chain) + len(earlier)
            or len(by_id) != len(order) or set(order) != set(by_id)):
        return [f"{name}: order must list every answer exactly once"]
    locked_ids = [a.get("answer_id") for a in chain]
    if [i for i in order if i in set(locked_ids)] != locked_ids or order[-1] != locked_ids[-1]:
        return [f"{name}: order puts the locks out of sequence, or ends on an unlocked answer"]
    errs = []
    prev = None
    for aid in order:
        a = by_id[aid]
        sup = a.get("supersedes")
        if prev is None:
            if sup is not None:
                errs.append(f"{name}: the first answer {aid} supersedes {sup}, but nothing came before it")
        elif prev in locked_ids and sup != prev:
            errs.append(f"{name}: answer {aid} follows lock {prev} without superseding it; "
                        f"a lock is superseded, never replaced silently (D3)")
        elif prev not in locked_ids and sup is not None and sup != prev:
            errs.append(f"{name}: answer {aid} claims to supersede {sup}, but the answer before it was {prev}")
        prev = aid
    return errs


def _check_fork(name: str, e: dict) -> list[str]:
    """A forked question's file carries the fork message it names, checked as a stored record.

    What this proves: `forked_from` is the content hash of a well-formed owner
    fork message that the file shows in full. What it cannot prove, because
    `fold` never reads the store (R1): that the message is in the store. That
    is the PR reviewer's to see, as it is for every answer and lock id here.
    """
    if "forked_from" not in e:
        return [f"{name}: a fork message without forked_from"] if "fork" in e else []
    f = e.get("fork")
    if not isinstance(f, dict):
        return [f"{name}: forked_from names a fork, but the file does not carry the fork message"]
    errs = [f"{name}: fork message: {m}" for m in S.validate(f)]
    if errs:
        return errs
    if f.get("intent") != "fork":
        errs.append(f"{name}: the message forked_from names is not a fork")
    if f.get("id") != S.record_id(f) or f["id"] != e["forked_from"]:
        errs.append(f"{name}: forked_from does not match the fork message's own id")
    return errs


def _check_earlier_answer(name: str, a: object, option_ids: list, labels: dict) -> list[str]:
    """An earlier (never locked) answer prints the owner's words too, so it is checked as strictly."""
    base = {"answer_id", "picks", "picked_labels", "own_text", "by", "answered_at"}
    if not isinstance(a, dict) or set(a) not in (base, base | {"supersedes", "reason"}):
        return [f"{name}: an earlier answer carries exactly {sorted(base)}, plus supersedes and reason together"]
    errs = []
    if "supersedes" in a:
        if not isinstance(a["supersedes"], str) or not RECORD_ID.match(a["supersedes"]):
            errs.append(f"{name}: earlier supersedes {a['supersedes']!r} is not a store record id")
        if not isinstance(a["reason"], str) or not a["reason"].strip() or len(a["reason"]) > S.MAX_TEXT:
            errs.append(f"{name}: earlier reason must be non-empty text of at most {S.MAX_TEXT} characters")
    if not isinstance(a["answer_id"], str) or not RECORD_ID.match(a["answer_id"]):
        errs.append(f"{name}: earlier answer_id {a['answer_id']!r} is not a store record id")
    if not isinstance(a["answered_at"], str) or not TS.match(a["answered_at"]):
        errs.append(f"{name}: earlier answered_at {a['answered_at']!r} is not a store timestamp")
    if a["by"] != "owner":
        errs.append(f"{name}: earlier answer {a['answer_id']} has by={a['by']!r}; only the owner answers")
    if not isinstance(a["own_text"], str) or len(a["own_text"]) > S.MAX_TEXT:
        errs.append(f"{name}: earlier own_text must be a string of at most {S.MAX_TEXT} characters")
    if not isinstance(a["picks"], list) or not all(p in option_ids for p in a["picks"]):
        errs.append(f"{name}: earlier answer {a['answer_id']} picks something that is not an option")
    elif a["picked_labels"] != [labels[p] for p in a["picks"]]:
        errs.append(f"{name}: earlier picked_labels does not match the options and picks")
    return errs


def _check_locked_answer(name: str, a: object, option_ids: list, labels: dict, star: object) -> list[str]:
    if not isinstance(a, dict):
        return [f"{name}: an answer in the chain is not an object"]
    errs = []
    for k in ("answer_id", "lock_id") + (("supersedes",) if "supersedes" in a else ()):
        if not isinstance(a.get(k), str) or not RECORD_ID.match(a[k]):
            errs.append(f"{name}: {k} {a.get(k)!r} is not a store record id")
    for k in ("answered_at", "locked_at"):
        if not isinstance(a.get(k), str) or not TS.match(a[k]):
            errs.append(f"{name}: {k} {a.get(k)!r} is not a store timestamp")
    for k in ("own_text",) + (("reason",) if "reason" in a else ()):
        if not isinstance(a.get(k), str) or len(a[k]) > S.MAX_TEXT:
            errs.append(f"{name}: {k} must be a string of at most {S.MAX_TEXT} characters")
    picks_ok = isinstance(a.get("picks"), list) and all(p in option_ids for p in a["picks"])
    if picks_ok:
        # Derived fields are recomputed, never trusted: they are what the record prints.
        want = {"picked_labels": [labels[p] for p in a["picks"]],
                "rejected_labels": [labels[i] for i in option_ids if i not in a["picks"]],
                "picked_star": star is not None and star in a["picks"]}
        for k, v in want.items():
            if a.get(k) != v:
                errs.append(f"{name}: {k} does not match the options and picks")
    if not a.get("lock_id") or a.get("locked_by") != "owner":
        errs.append(f"{name}: answer {a.get('answer_id')} is not locked by the owner")
    errs += _check_anchor_fields(name, a)
    if a.get("by") != "owner":
        errs.append(f"{name}: answer {a.get('answer_id')} has by={a.get('by')!r}; only the owner answers")
    picks = a.get("picks")
    if not isinstance(picks, list):
        errs.append(f"{name}: answer {a.get('answer_id')}: picks must be a list")
    else:
        stray = [p for p in picks if p not in option_ids]
        if stray:
            errs.append(f"{name}: answer {a.get('answer_id')} picks {stray}, which are not options; "
                        f"the owner's own words belong in own_text")
        if not picks and not (isinstance(a.get("own_text"), str) and a["own_text"].strip()):
            errs.append(f"{name}: answer {a.get('answer_id')} has neither a pick nor own words")
    return errs


def _check_anchor_fields(name: str, a: dict) -> list[str]:
    """A lock's anchors and their provenance (0.5.0), checked with the store's own rules; absent is fine."""
    keys = {"anchored_by", "anchors", "anchor_id", "anchor_basis", "anchored_at"}
    if not keys & set(a):
        return []
    aid = a.get("answer_id")
    origin = a.get("anchored_by")
    want = {"anchored_by", "anchors"} | ({"anchor_id", "anchor_basis", "anchored_at"} if origin == "reanchor" else set())
    if origin not in ("lock", "reanchor") or keys & set(a) != want:
        return [f"{name}: answer {aid}: anchored_by is 'lock' (with anchors) or 'reanchor' (with anchors, "
                f"anchor_id, anchor_basis and anchored_at)"]
    errs = [f"{name}: answer {aid}: {m}" for m in S.check_conditions(a["anchors"], "anchors", allow_empty=False)]
    if origin == "reanchor":
        if not isinstance(a["anchor_id"], str) or not RECORD_ID.match(a["anchor_id"]):
            errs.append(f"{name}: answer {aid}: anchor_id {a['anchor_id']!r} is not a store record id")
        if not isinstance(a["anchored_at"], str) or not TS.match(a["anchored_at"]):
            errs.append(f"{name}: answer {aid}: anchored_at {a['anchored_at']!r} is not a store timestamp")
        b = a["anchor_basis"]
        if not isinstance(b, str) or not b.strip() or len(b) > S.MAX_TEXT:
            errs.append(f"{name}: answer {aid}: anchor_basis must be non-empty text of at most {S.MAX_TEXT} characters")
    return errs


def read_ledger(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {line.split()[0] for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}


def fold(locked_dir: Path, ledger: Path, adapter: ProjectAdapter, *, dry_run: bool) -> tuple[list[str], list[str]]:
    """Fold every committed locked file not yet folded, and return (lines written, lines skipped).

    If any file is refused, raises FoldError naming every refusal and writes nothing.
    """
    items = adapter.items()
    done = read_ledger(ledger)
    todo, skipped, refusals = [], [], []
    forks: dict[str | None, list[str]] = {}
    for p in sorted(locked_dir.glob("*.json")):
        try:
            e = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as err:
            refusals.append(f"{p.name}: not JSON ({err.msg})")
            continue
        errs = check_entry(p.name, e, items)
        if errs:
            refusals += errs
            continue
        # Every file counts toward its fork's cap, folded before or not.
        forks.setdefault(e.get("forked_from"), []).append(p.name)
        if e["locked"]["lock_id"] in done:
            skipped.append(f"{p.name}: lock {e['locked']['lock_id']} already folded")
            continue
        todo.append(e)
    forks.pop(None, None)
    for fid, names in sorted(forks.items()):
        if len(names) > MAX_FORK_QUESTIONS:
            refusals.append(f"fork {fid}: {len(names)} questions ({', '.join(names)}); the limit is {MAX_FORK_QUESTIONS}")
    if refusals:
        raise FoldError("refused, nothing folded:\n  " + "\n  ".join(refusals))
    written = adapter.record(todo, dry_run) if todo else []
    if todo and not dry_run:
        with open(ledger, "a", encoding="utf-8") as fh:
            for e in todo:
                fh.write(f"{e['locked']['lock_id']} {e['qid']}\n")
    return written, skipped


def load_adapter(path: Path) -> ProjectAdapter:
    spec = importlib.util.spec_from_file_location("console_adapter", path)
    if spec is None or spec.loader is None:
        raise FoldError(f"cannot load adapter {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for fn in ("items", "record", "seed_questions"):
        if not callable(getattr(mod, fn, None)):
            raise FoldError(f"adapter {path} lacks {fn}()")
    return mod  # type: ignore[return-value]


def inside(root: Path, raw: str, what: str) -> Path:
    """A project-relative path that stays inside `root`, or FoldError naming why not (spec §7.7).

    These paths reach the command line from the repository's `.console-kit.json`,
    and `--adapter` is imported and run, so the boundary is enforced here, in the
    code that acts on it, and not only in the skill that builds the command.
    """
    if not raw or raw.startswith(("/", "~")) or "\\" in raw or ".." in Path(raw).parts:
        raise FoldError(f"{what} {raw!r} must be a relative path inside the project (no leading / or ~, no ..)")
    base = os.path.realpath(root)
    full = os.path.realpath(os.path.join(base, raw))
    if os.path.commonpath([base, full]) != base:
        raise FoldError(f"{what} {raw!r} resolves outside the project, to {full}")
    return Path(full)


def _steward_refusal(a) -> str | None:
    """The steward lock (0.8.3), as `agent.py` applies it to `watch` and `synced`.

    The lock is keyed to what the command TOUCHES, not to how it was called:
    `export` to the console whose store it reads (the store's directory is the
    console's state) and to the project its `--out` lands in; `fold` to the
    project of `--root` and of each of `--locked`, `--ledger` and `--adapter`.
    Each path is resolved and walked up to the NEAREST registered project
    (`R.enclosing`), so neither an ancestor `--root` above a registered project
    nor a path reaching into a project registered below `--root` escapes the
    lock. If any of them lands on a console with a steward that is not this
    session, it is refused. Paths in no registered project carry no lock.
    """
    root = Path(a.root)
    touched = [root, root / a.out] if a.cmd == "export" else \
        [root, root / a.locked, root / a.ledger, root / a.adapter]
    projects = R.load()
    states: dict[Path, str] = {}
    if a.cmd == "export":
        states[Path(a.store).resolve().parent] = f"the store {a.store}"
    for t in touched:
        hit = R.enclosing(t, projects)
        if hit is not None:
            states.setdefault(Path(hit[1]["state"]), f"{t} (in {hit[0]})")
    for state, what in states.items():
        name = R.steward_for_state(state)
        if name is None or a.agent == name:
            continue
        who = f"this session ({a.agent})" if a.agent else "this session (unnamed)"
        return (f"refused: the fold belongs to this console's steward, {name}, and {who} is not it ({what} is "
                f"on its console). Only the steward watches the doorbell, marks it synced and folds answers. "
                f"Use `ask`, `reply` and `working` instead; the steward folds what the owner locks. (A session "
                f"named {name} runs as it: --as {name}, or {N.ENV}={name}.)")
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", type=Path, default=Path("."),
                    help="the project root every other path must stay inside (default: the current directory)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ex = sub.add_parser("export", help="write committed locked files from the store")
    ex.add_argument("--store", type=Path, required=True)
    ex.add_argument("--out", required=True)
    fo = sub.add_parser("fold", help="fold committed locked files into the project's record")
    fo.add_argument("--locked", required=True)
    fo.add_argument("--ledger", required=True)
    fo.add_argument("--adapter", required=True)
    fo.add_argument("--dry-run", action="store_true")
    ap.add_argument("--as", dest="agent", metavar="NAME",
                    help=f"this session's agent name (default: ${N.ENV}); checked against the console's steward")
    a = ap.parse_args(argv)
    if a.agent is None and os.environ.get(N.ENV):
        a.agent = os.environ[N.ENV]
    if a.agent is not None and N.problem(a.agent):
        print(f"fold: agent name: {N.problem(a.agent)}; nothing was done", file=sys.stderr)
        return 2
    try:
        why = _steward_refusal(a)
    except R.RegistryError as err:
        print(f"fold: {err}", file=sys.stderr)
        return 1
    if why:
        print(f"fold: {why}", file=sys.stderr)
        return 1
    try:
        if a.cmd == "export":
            a.out = inside(a.root, a.out, "--out")
        else:  # checked before anything is imported: the adapter is code
            a.locked, a.ledger, a.adapter = (inside(a.root, a.locked, "--locked"),
                                             inside(a.root, a.ledger, "--ledger"),
                                             inside(a.root, a.adapter, "--adapter"))
        if a.cmd == "export":
            for name in write_export(export(Store(a.store), names_beside(a.store)), a.out):
                print(f"wrote {a.out / name}")
            return 0
        written, skipped = fold(a.locked, a.ledger, load_adapter(a.adapter), dry_run=a.dry_run)
    except (FoldError, Exception) as err:  # noqa: BLE001 - every failure is reported, none swallowed
        print(f"fold: {err}", file=sys.stderr)
        return 1
    for line in skipped:
        print(f"skip  {line}")
    for line in written:
        print(("would " if a.dry_run else "") + f"write {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

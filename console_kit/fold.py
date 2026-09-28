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
import re
import sys
from pathlib import Path
from typing import Protocol

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


class FoldError(Exception):
    pass


RECORD_ID = re.compile(r"^[0-9a-f]{24}$")
TS = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")


def locked_filename(qid: str) -> str:
    return qid.replace("/", "__") + ".json"


def export(store: Store) -> dict[str, dict]:
    """Return filename -> entry for every question whose CURRENT answer is locked."""
    out: dict[str, dict] = {}
    for q in (r for r in store.records() if r["type"] == "question"):
        answers = store.answers(q["qid"])
        locks = [(a, store.lock_of(a["id"])) for a in answers]
        locked = [(a, lk) for a, lk in locks if lk is not None]
        if not locked or locked[-1][0]["id"] != answers[-1]["id"]:
            continue
        history = [_entry_answer(q, a, lk) for a, lk in locked]
        out[locked_filename(q["qid"])] = {
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
        }
    return out


def _entry_answer(q: dict, a: dict, lk: dict) -> dict:
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
    return e


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
    errs += [f"{name}: {m}" for m in S.validate(as_record)]
    if not isinstance(e.get("asked_at"), str) or not TS.match(e["asked_at"]):
        errs.append(f"{name}: asked_at {e.get('asked_at')!r} is not a store timestamp")
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
    for prev, nxt in zip(chain, chain[1:]):
        if nxt.get("supersedes") != prev.get("answer_id"):
            errs.append(f"{name}: answer {nxt.get('answer_id')} does not supersede {prev.get('answer_id')}; "
                        f"a lock is superseded, never replaced silently (D3)")
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
        if e["locked"]["lock_id"] in done:
            skipped.append(f"{p.name}: lock {e['locked']['lock_id']} already folded")
            continue
        todo.append(e)
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    ex = sub.add_parser("export", help="write committed locked files from the store")
    ex.add_argument("--store", type=Path, required=True)
    ex.add_argument("--out", type=Path, required=True)
    fo = sub.add_parser("fold", help="fold committed locked files into the project's record")
    fo.add_argument("--locked", type=Path, required=True)
    fo.add_argument("--ledger", type=Path, required=True)
    fo.add_argument("--adapter", type=Path, required=True)
    fo.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "export":
            for name in write_export(export(Store(a.store)), a.out):
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

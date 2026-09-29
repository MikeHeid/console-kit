"""Record shapes for the owner console (spec `architect/40-specs/owner-console.md` §4.1).

This module is project-neutral: nothing here knows about Gradiance.

Every record is one JSON object on one line of the store, and every record
carries `schemaVersion`. A record from another schema version is refused BY
NAME, never coerced.

There are four kinds of record, and each has fixed writers (spec R4):

    question  agent   `qid` is `<itemId>/Q<n>`, minted once and never reused
    message   either  a thread entry on an item; a message the owner writes is "author input"
    answer    owner   picks from the question's options, own words, or both
    lock      owner   locks one answer; a later answer SUPERSEDES it, never undoes it (D3)
"""

from __future__ import annotations

import hashlib
import json
import re

SCHEMA_VERSION = 1

KINDS = ("question", "message", "answer", "lock")
WRITERS = {
    "question": frozenset({"agent"}),
    "message": frozenset({"agent", "owner"}),
    "answer": frozenset({"owner"}),
    "lock": frozenset({"owner"}),
}
QUESTION_KINDS = ("single", "multi", "free")
# The owner's two requests to the agent: "fork" asks it to deliberate (§6), and
# "process" says the answers are in and it should process them (§7.3).
INTENTS = ("fork", "process")
OWNER_INTENTS = frozenset(INTENTS)
FOCUSES = ("code", "design", "ui", "backend", "whole")
MODES = ("explore", "tighten")
# The seats a follow-up round may call (D13), and a typed seat: `other:<role>`,
# 1-40 characters of letters, digits, spaces and hyphens. It is shown on the
# page and handed to an agent, so it is held to exactly this shape.
ROSTER = ("devops", "ux", "adversarial", "security", "architect", "analyst")
OTHER_ROLE = re.compile(r"^other:[A-Za-z0-9][A-Za-z0-9 \-]{0,39}$")
MAX_ROLES = 3
# Whose recommendation a forked question's ★ is: the whole panel, one of the
# default committee's seats (§6.6), a roster seat (D13), or a typed `other:` seat.
STAR_BY = ("panel", "architect", "ux", "security", "determinism", "devops", "adversarial", "analyst")
VALID_IF_KINDS = ("file_sha256", "item_status")

# The fields the WRITER supplies; `id`, `seq` and `ts` belong to the store.
REQUIRED = {
    "question": ("qid", "item", "text", "kind", "options", "star", "valid_if", "source", "by", "nonce"),
    "message": ("item", "text", "by", "nonce"),
    "answer": ("qid", "picks", "own_text", "by", "nonce"),
    "lock": ("qid", "answer", "by", "nonce"),
}
# Fork deliberations (spec §6.4) add optional fields, so SCHEMA_VERSION stays 1:
# every existing record stays valid, and a kit from before them refuses a
# record that carries them BY NAME (`unknown field(s)`) rather than dropping them.
OPTIONAL = {
    "question": frozenset({"forked_from", "star_by"}),
    "message": frozenset({"reply_to", "intent", "focus", "mode", "roles", "follow_up_of"}),
    "answer": frozenset({"supersedes", "reason"}),
    "lock": frozenset(),
}
STORE_FIELDS = frozenset({"id", "seq", "ts", "schemaVersion", "type"})

ITEM_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]*$")
# A store record id (`record_id`). A field holding one is printed inline in the
# rulings record, so it is held to exactly this shape, never "any string".
RECORD_ID = re.compile(r"^[0-9a-f]{24}$")
QID = re.compile(r"^(?P<item>[A-Za-z0-9][A-Za-z0-9_.\-]*)/Q(?P<n>[1-9][0-9]*)$")
OPTION_ID = re.compile(r"^[a-z][a-z0-9_]*$")
NONCE = re.compile(r"^[A-Za-z0-9_\-]{8,64}$")
# `source` is a path, optionally with `:line` or `:line-line`. It is rendered
# inline into the rulings record, so it must never carry markup or a newline.
SOURCE = re.compile(r"^[A-Za-z0-9_.\-/]+(:[0-9]+(-[0-9]+)?)?$")
MAX_TEXT = 20_000
MAX_LINE = 300       # a label, a description, or any other one-line field
MAX_OPTIONS = 12
MAX_VALID_IF = 16


def validate(rec: object) -> list[str]:
    """List every problem with `rec` as its author wrote it; an empty list means well-formed.

    This checks shape only. Whether the qid exists, whether the answer is the
    latest, and whether the item resolves are the STORE's job, because those
    checks need the other records.
    """
    if not isinstance(rec, dict):
        return ["a record must be a JSON object"]
    kind = rec.get("type")
    if kind not in KINDS:
        return [f"unknown record type {kind!r}; expected one of {', '.join(KINDS)}"]
    errs: list[str] = []
    if rec.get("schemaVersion") != SCHEMA_VERSION:
        errs.append(f"schemaVersion {rec.get('schemaVersion')!r} is not {SCHEMA_VERSION}; refused, not coerced")
    missing = [k for k in REQUIRED[kind] if k not in rec]
    if missing:
        errs.append(f"{kind}: missing {', '.join(missing)}")
    unknown = sorted(set(rec) - set(REQUIRED[kind]) - OPTIONAL[kind] - STORE_FIELDS)
    if unknown:
        errs.append(f"{kind}: unknown field(s) {', '.join(unknown)}")
    if missing:
        return errs
    if not isinstance(rec["by"], str) or rec["by"] not in WRITERS[kind]:  # a JSON list is unhashable
        errs.append(f"{kind}: written by {rec['by']!r}, but only {sorted(WRITERS[kind])} may write one")
    if not isinstance(rec["nonce"], str) or not NONCE.match(rec["nonce"]):
        errs.append(f"{kind}: nonce must be 8-64 of [A-Za-z0-9_-]")
    errs += _TYPE_CHECKS[kind](rec)
    return errs


def _text(rec: dict, field: str, *, allow_empty: bool = False) -> list[str]:
    v = rec.get(field)
    if not isinstance(v, str):
        return [f"{field} must be a string"]
    if not allow_empty and not v.strip():
        return [f"{field} must not be empty"]
    if len(v) > MAX_TEXT:
        return [f"{field} is {len(v)} characters; the limit is {MAX_TEXT}"]
    return []


def one_line(v: object, field: str, *, allow_empty: bool = False) -> list[str]:
    """Check a field that is rendered inline: a string of one line, at most MAX_LINE characters."""
    if not isinstance(v, str):
        return [f"{field} must be a string"]
    if not allow_empty and not v.strip():
        return [f"{field} must not be empty"]
    if "\n" in v or "\r" in v or " " in v or " " in v:
        return [f"{field} must be one line"]
    if len(v) > MAX_LINE:
        return [f"{field} is {len(v)} characters; the limit is {MAX_LINE}"]
    return []


def _check_question(rec: dict) -> list[str]:
    errs = _text(rec, "text")
    if not isinstance(rec["source"], str) or not SOURCE.match(rec["source"]) or len(rec["source"]) > MAX_LINE:
        errs.append(f"source {rec['source']!r} must be a repo path, optionally with :line or :line-line")
    m = QID.match(rec["qid"]) if isinstance(rec["qid"], str) else None
    if not m:
        errs.append(f"qid {rec['qid']!r} is not <itemId>/Q<n>")
    elif m.group("item") != rec["item"]:
        errs.append(f"qid {rec['qid']!r} names item {m.group('item')!r}, but the record says {rec['item']!r}")
    if rec["kind"] not in QUESTION_KINDS:
        errs.append(f"kind {rec['kind']!r} is not one of {', '.join(QUESTION_KINDS)}")
    opts = rec["options"]
    if not isinstance(opts, list):
        return errs + ["options must be a list"]
    if len(opts) > MAX_OPTIONS:
        return errs + [f"{len(opts)} options; the limit is {MAX_OPTIONS}"]
    ids = []
    for o in opts:
        if not isinstance(o, dict) or not isinstance(o.get("id"), str) or not OPTION_ID.match(o["id"]):
            errs.append(f"option {o!r} needs an id matching {OPTION_ID.pattern}")
            continue
        errs += one_line(o.get("label"), f"option {o['id']!r} label")
        if "description" in o:
            errs += one_line(o["description"], f"option {o['id']!r} description", allow_empty=True)
        if set(o) - {"id", "label", "description"}:
            errs.append(f"option {o['id']!r}: unknown field(s) {sorted(set(o) - {'id', 'label', 'description'})}")
        ids.append(o["id"])
    if len(ids) != len(set(ids)):
        errs.append("option ids repeat")
    if rec["kind"] == "free" and opts:
        errs.append("a free question offers no options")
    if rec["kind"] in ("single", "multi") and len(opts) < 2:
        errs.append(f"a {rec['kind']} question offers at least two options")
    star = rec["star"]
    if star is not None and star not in ids:
        errs.append(f"star {star!r} is not one of the options")
    errs += _check_fork_fields(rec, star)
    vi = rec["valid_if"]
    if not isinstance(vi, list):
        errs.append("valid_if must be a list")
    elif len(vi) > MAX_VALID_IF:
        errs.append(f"{len(vi)} valid_if conditions; the limit is {MAX_VALID_IF}")
    else:
        for c in vi:
            errs += _check_condition(c)
    return errs


def _check_fork_fields(rec: dict, star: object) -> list[str]:
    """A forked question names its fork, and a starred one says whose ★ it is (§6.4).

    That the fork exists and is an owner fork message is the STORE's check.
    """
    errs = []
    if "forked_from" in rec and (not isinstance(rec["forked_from"], str) or not RECORD_ID.match(rec["forked_from"])):
        errs.append("forked_from must be the id of the fork message (24 lowercase hex)")
    if "star_by" in rec:
        if not is_star_by(rec["star_by"]):
            errs.append(f"star_by {rec['star_by']!r} is not one of {', '.join(STAR_BY)}, "
                        f"or other:<role> (1-40 letters, digits, spaces, hyphens)")
        if star is None:
            errs.append("star_by names whose ★ it is, and this question has no ★")
    if "forked_from" in rec and star is not None and "star_by" not in rec:
        errs.append("a forked question with a ★ says whose it is: add star_by")
    return errs


def _check_condition(c: object) -> list[str]:
    if not isinstance(c, dict) or c.get("kind") not in VALID_IF_KINDS:
        return [f"valid_if condition {c!r} needs kind in {', '.join(VALID_IF_KINDS)}"]
    want = {"file_sha256": {"kind", "path", "sha256"}, "item_status": {"kind", "item", "status"}}[c["kind"]]
    if set(c) != want or not all(isinstance(c[k], str) and c[k] for k in want):
        return [f"valid_if {c['kind']} takes exactly {sorted(want)}, all non-empty strings"]
    if c["kind"] == "file_sha256" and (c["path"].startswith("/") or ".." in c["path"].split("/")):
        return [f"valid_if path {c['path']!r} must be relative and stay inside the project"]
    return []


def _check_message(rec: dict) -> list[str]:
    errs = _text(rec, "text")
    if not isinstance(rec["item"], str) or not ITEM_ID.match(rec["item"]):
        errs.append(f"item {rec['item']!r} is not an item id")
    if "reply_to" in rec and not isinstance(rec["reply_to"], str):
        errs.append("reply_to must be a record id")
    intent = rec.get("intent")
    if "intent" in rec and intent not in INTENTS:
        errs.append(f"intent {intent!r} is not one of {', '.join(INTENTS)}")
    # isinstance first: JSON can carry a list here, and a list is unhashable in a frozenset test.
    if isinstance(intent, str) and intent in OWNER_INTENTS and rec["by"] != "owner":
        # An agent that could write either could start its own deliberation or processing run.
        errs.append(f"intent {intent!r} is the owner's request; only the owner writes it")
    if intent == "fork":
        if "mode" not in rec:
            errs.append("a fork says its mode: explore or tighten")
    else:
        present = [f for f in ("focus", "mode", "roles", "follow_up_of") if f in rec]
        if present:
            errs.append(f"{', '.join(present)} belong(s) to a fork: set intent 'fork' or leave them out")
    if "focus" in rec and rec["focus"] not in FOCUSES:
        errs.append(f"focus {rec['focus']!r} is not one of {', '.join(FOCUSES)}")
    if "mode" in rec and rec["mode"] not in MODES:
        errs.append(f"mode {rec['mode']!r} is not one of {', '.join(MODES)}")
    errs += _check_follow_up(rec)
    return errs


def _check_follow_up(rec: dict) -> list[str]:
    """A follow-up round names the fork it follows and the 1-3 seats it calls (D13, §7.5).

    The first round of a fork runs the default committee and names no seats,
    so `roles` comes only with `follow_up_of`. That the named fork exists is
    the STORE's check.
    """
    errs = []
    if "follow_up_of" in rec:
        if not isinstance(rec["follow_up_of"], str) or not RECORD_ID.match(rec["follow_up_of"]):
            errs.append("follow_up_of must be the id of the earlier fork message (24 lowercase hex)")
        if "roles" not in rec:
            errs.append(f"a follow-up names the seats it calls: roles, 1 to {MAX_ROLES}")
    elif "roles" in rec:
        errs.append("roles belong to a follow-up; the first round runs the default committee. Add follow_up_of")
    if "roles" in rec:
        roles = rec["roles"]
        if not isinstance(roles, list) or not 1 <= len(roles) <= MAX_ROLES:
            errs.append(f"roles must be a list of 1 to {MAX_ROLES} seats")
        else:
            bad = [r for r in roles if not is_role(r)]
            if bad:
                errs.append(f"role(s) {bad!r} are not one of {', '.join(ROSTER)}, "
                            f"or other:<role> (1-40 letters, digits, spaces, hyphens)")
            elif len(roles) != len(set(roles)):
                errs.append("roles repeat")
    return errs


def is_role(v: object) -> bool:
    """A seat a follow-up may call: a roster seat, or a typed `other:<role>`."""
    return isinstance(v, str) and (v in ROSTER or bool(OTHER_ROLE.match(v)))


def is_star_by(v: object) -> bool:
    """Whose ★ a forked question carries: a committee or roster seat, or a typed `other:<role>`."""
    return isinstance(v, str) and (v in STAR_BY or bool(OTHER_ROLE.match(v)))


def _check_answer(rec: dict) -> list[str]:
    errs = _text(rec, "own_text", allow_empty=True)
    picks = rec["picks"]
    if not isinstance(picks, list) or not all(isinstance(p, str) for p in picks) or len(picks) > MAX_OPTIONS:
        errs.append(f"picks must be a list of at most {MAX_OPTIONS} option ids")
    elif len(picks) != len(set(picks)):
        errs.append("picks repeat")
    elif not picks and isinstance(rec["own_text"], str) and not rec["own_text"].strip():
        errs.append("an answer needs at least one pick or its own words")
    if ("supersedes" in rec) != ("reason" in rec):
        errs.append("supersedes and reason go together: a superseding answer says why (D3)")
    if "reason" in rec:
        errs += _text(rec, "reason")
    return errs


def _check_lock(rec: dict) -> list[str]:
    return [] if isinstance(rec["answer"], str) and rec["answer"] else ["answer must be the id of the answer being locked"]


_TYPE_CHECKS = {"question": _check_question, "message": _check_message,
                "answer": _check_answer, "lock": _check_lock}


def record_id(rec: dict) -> str:
    """Hash the author's fields into the record's id, leaving out the store's own fields.

    A retried write (same nonce, same content) hashes to the same id, so the
    store keeps one copy. Two separate writes carry different nonces, so two
    owner messages that both say "yes" on one item are two records, not one.
    """
    body = {k: v for k, v in rec.items() if k not in STORE_FIELDS or k == "type"}
    canon = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:24]

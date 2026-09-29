#!/usr/bin/env python3
"""Tests for the owner-console kit (`schema`, `store`, `view`, `fold`, `publish`).

    python3 tools/console-kit/test_kit.py

Each test names the wrong implementation it exists to catch, per the project's
rule: "what would pass this while missing the point?". Every write happens in
a temporary directory.
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from console_kit import fold as F  # noqa: E402
from console_kit import publish as P  # noqa: E402
from console_kit import schema as S  # noqa: E402
from console_kit import view as V  # noqa: E402
from console_kit.store import Store, StoreError  # noqa: E402

REPO = HERE.parent.parent
ITEMS = {
    "LANE": {"title": "a lane", "parent": None, "status": "open"},
    "LANE.1": {"title": "a phase", "parent": "LANE", "status": "open"},
    "LANE.1.a": {"title": "a topic", "parent": "LANE.1", "status": "proposed"},
}

_n = 0


def nonce() -> str:
    global _n
    _n += 1
    return f"nonce{_n:06d}"


def question(qid="LANE.1/Q1", kind="single", star="b", valid_if=None, **kw):
    item = qid.split("/")[0]
    opts = [] if kind == "free" else [
        {"id": "a", "label": "Option A", "description": "the first"},
        {"id": "b", "label": "Option B ★"},
        {"id": "c", "label": "Option C"},
    ]
    r = {"type": "question", "schemaVersion": 1, "qid": qid, "item": item, "text": f"Which for {qid}?",
         "kind": kind, "options": opts, "star": None if kind == "free" else star,
         "valid_if": valid_if or [], "source": "architect/40-specs/owner-console.md:1",
         "by": "agent", "nonce": nonce()}
    r.update(kw)
    return r


def answer(qid="LANE.1/Q1", picks=("b",), own_text="", **kw):
    r = {"type": "answer", "schemaVersion": 1, "qid": qid, "picks": list(picks), "own_text": own_text,
         "by": "owner", "nonce": nonce()}
    r.update(kw)
    return r


def lock(answer_rec, **kw):
    r = {"type": "lock", "schemaVersion": 1, "qid": answer_rec["qid"], "answer": answer_rec["id"],
         "by": "owner", "nonce": nonce()}
    r.update(kw)
    return r


def message(item="LANE.1", by="owner", text="guidance", **kw):
    r = {"type": "message", "schemaVersion": 1, "item": item, "text": text, "by": by, "nonce": nonce()}
    r.update(kw)
    return r


class Tmp(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.dir = Path(self._td.name)
        self.path = self.dir / "store.jsonl"
        self.tick = 0

    def tearDown(self):
        self._td.cleanup()

    def clock(self):
        self.tick += 1
        return f"2026-09-28T00:00:{self.tick:02d}Z"

    def store(self, **kw):
        return Store(self.path, known_items=ITEMS, clock=self.clock, **kw)


class SchemaTests(unittest.TestCase):
    def test_well_formed_records_pass(self):
        for r in (question(), question(kind="free"), question(kind="multi"), answer(), message()):
            self.assertEqual(S.validate(r), [], r["type"])

    def test_only_the_named_writer_may_write_each_kind(self):
        # Catches: an agent answering or locking for the owner (R4).
        self.assertTrue(S.validate(answer(by="agent")))
        self.assertTrue(S.validate(question(by="owner")))
        self.assertTrue(any("only" in e for e in S.validate(dict(answer(), type="lock", answer="x", by="agent"))))

    def test_other_schema_version_is_refused_by_name(self):
        # Catches: a reader that coerces or ignores a newer record's shape.
        errs = S.validate(question(schemaVersion=2))
        self.assertTrue(any("schemaVersion 2" in e for e in errs), errs)

    def test_unknown_field_is_refused(self):
        # Catches: a writer smuggling `id`-like or future fields past the schema.
        self.assertTrue(any("unknown field" in e for e in S.validate(message(colour="red"))))

    def test_qid_must_name_its_item(self):
        self.assertTrue(S.validate(question(qid="LANE.1/Q1", item="LANE")))
        self.assertTrue(S.validate(question(qid="LANE.1/Q0")))

    def test_star_must_be_an_offered_option(self):
        self.assertTrue(S.validate(question(star="z")))

    def test_valid_if_path_cannot_leave_the_project(self):
        # Catches: a staleness check that reads /etc/passwd or ../ outside the repo.
        for p in ("/etc/passwd", "../x", "a/../../x"):
            c = {"kind": "file_sha256", "path": p, "sha256": "0" * 64}
            self.assertTrue(S.validate(question(valid_if=[c])), p)

    def test_supersedes_needs_a_reason(self):
        # Catches: a lock replaced with no stated reason (D3).
        self.assertTrue(S.validate(answer(supersedes="abc")))
        self.assertTrue(S.validate(answer(reason="because")))
        self.assertEqual(S.validate(answer(supersedes="abc", reason="because")), [])

    def test_inline_fields_are_one_line_and_bounded(self):
        # Catches: a label, source or list that is unbounded, or carries a newline into an inline field.
        opts = [{"id": f"o{i}", "label": f"L{i}"} for i in range(S.MAX_OPTIONS + 1)]
        self.assertTrue(S.validate(question(options=opts, star=None)))
        long = [{"id": "a", "label": "x" * (S.MAX_LINE + 1)}, {"id": "b", "label": "y"}]
        self.assertTrue(S.validate(question(options=long, star=None)))
        self.assertTrue(S.validate(question(source="a.md:1\n## fake")))
        self.assertTrue(S.validate(question(source="**bold**.md")))
        many = [{"kind": "item_status", "item": "LANE", "status": "open"}] * (S.MAX_VALID_IF + 1)
        self.assertTrue(S.validate(question(valid_if=many)))

    def test_empty_answer_is_refused(self):
        self.assertTrue(S.validate(answer(picks=(), own_text="  ")))
        self.assertEqual(S.validate(answer(picks=(), own_text="none of these")), [])


class StoreTests(Tmp):
    def test_retry_with_same_nonce_writes_once(self):
        # Catches: a network retry duplicating an owner message.
        st = self.store()
        m = message()
        a, b = st.append(m), st.append(m)
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(len(self.path.read_text().splitlines()), 1)

    def test_two_identical_messages_with_different_nonces_are_two(self):
        # Catches: an id that hashes content only, so a second "yes" vanishes.
        st = self.store()
        st.append(message(text="yes"))
        st.append(message(text="yes"))
        self.assertEqual(len(st.records()), 2)

    def test_qid_is_minted_once(self):
        st = self.store()
        st.append(question())
        with self.assertRaisesRegex(StoreError, "minted once"):
            st.append(question())

    def test_pick_outside_options_is_refused(self):
        st = self.store()
        st.append(question())
        with self.assertRaisesRegex(StoreError, "not options"):
            st.append(answer(picks=("z",)))

    def test_single_question_takes_one_pick(self):
        st = self.store()
        st.append(question())
        with self.assertRaisesRegex(StoreError, "one pick"):
            st.append(answer(picks=("a", "b")))

    def test_locked_answer_is_superseded_never_undone(self):
        # Catches: D3 broken, so a new answer quietly replaces a locked one.
        st = self.store()
        st.append(question())
        a1 = st.append(answer())
        st.append(lock(a1))
        with self.assertRaisesRegex(StoreError, "supersedes the lock"):
            st.append(answer(picks=("a",)))
        with self.assertRaisesRegex(StoreError, "not the current answer"):
            st.append(answer(picks=("a",), supersedes="deadbeef", reason="changed"))
        a2 = st.append(answer(picks=("a",), supersedes=a1["id"], reason="spec moved"))
        self.assertEqual(st.head("LANE.1/Q1")["id"], a2["id"])
        self.assertEqual(len(st.answers("LANE.1/Q1")), 2, "the locked answer stays on record")

    def test_lock_only_the_current_answer_once(self):
        st = self.store()
        st.append(question())
        a1 = st.append(answer())
        a2 = st.append(answer(picks=("c",)))
        with self.assertRaisesRegex(StoreError, "not the current answer"):
            st.append(lock(a1))
        st.append(lock(a2))
        with self.assertRaisesRegex(StoreError, "already locked"):
            st.append(lock(a2))

    def test_writer_cannot_assign_store_fields(self):
        st = self.store()
        with self.assertRaisesRegex(StoreError, "store's to assign"):
            st.append(dict(message(), seq=1))

    def test_unknown_item_is_refused(self):
        with self.assertRaisesRegex(StoreError, "not in the project"):
            self.store().append(message(item="NOPE"))

    def test_reload_reproduces_the_records(self):
        st = self.store()
        st.append(question())
        st.append(answer())
        self.assertEqual(Store(self.path).records(), st.records())

    def test_hand_edited_line_is_refused_on_load(self):
        # Catches: a store that trusts a tampered line (an edited pick, a moved seq).
        st = self.store()
        st.append(question())
        st.append(answer())
        lines = self.path.read_text().splitlines()
        rec = json.loads(lines[1])
        rec["picks"] = ["a"]
        lines[1] = json.dumps(rec)
        self.path.write_text("\n".join(lines) + "\n")
        with self.assertRaisesRegex(StoreError, "was edited"):
            Store(self.path)

    def test_torn_final_line_is_refused_not_guessed(self):
        self.store().append(message())
        with open(self.path, "a") as fh:
            fh.write('{"type":"mess')
        with self.assertRaisesRegex(StoreError, "cut short"):
            Store(self.path)

    def test_other_schema_version_on_disk_is_refused_by_name(self):
        self.store().append(message())
        rec = json.loads(self.path.read_text())
        rec["schemaVersion"] = 2
        self.path.write_text(json.dumps(rec) + "\n")
        with self.assertRaisesRegex(StoreError, "schemaVersion 2"):
            Store(self.path)


class ViewTests(Tmp):
    def status_holds(self, statuses):
        return V.make_evaluator(self.dir, statuses)

    def test_four_states(self):
        st = self.store()
        cond = {"kind": "item_status", "item": "LANE.1", "status": "open"}
        for n in (1, 2, 3, 4):
            st.append(question(qid=f"LANE.1/Q{n}", valid_if=[cond]))
        st.append(answer(qid="LANE.1/Q2"))
        a3 = st.append(answer(qid="LANE.1/Q3"))
        st.append(lock(a3))
        a4 = st.append(answer(qid="LANE.1/Q4"))
        st.append(lock(a4))
        holds = self.status_holds({"LANE.1": "open"})
        got = {q: V.question_state(st, st.question(q), holds) for q in ("LANE.1/Q1", "LANE.1/Q2", "LANE.1/Q3")}
        self.assertEqual(got, {"LANE.1/Q1": "awaiting_you", "LANE.1/Q2": "unlocked", "LANE.1/Q3": "locked"})
        moved = self.status_holds({"LANE.1": "built"})
        self.assertEqual(V.question_state(st, st.question("LANE.1/Q4"), moved), "stale")

    def test_file_condition_goes_stale_when_the_file_changes(self):
        f = self.dir / "spec.md"
        f.write_text("v1")
        cond = {"kind": "file_sha256", "path": "spec.md", "sha256": hashlib.sha256(b"v1").hexdigest()}
        holds = self.status_holds({})
        self.assertTrue(holds(cond))
        f.write_text("v2")
        self.assertFalse(holds(cond))

    def test_counts_roll_up_to_ancestors_not_down(self):
        # Catches: a roll-up that shows a child's pending question on the child only,
        # or pushes a parent's count down onto its children.
        st = self.store()
        st.append(question(qid="LANE.1.a/Q1"))
        v = V.build(st, ITEMS, self.status_holds({}))
        self.assertEqual(v["items"]["LANE.1.a"]["own"]["awaiting_you"], 1)
        self.assertEqual(v["items"]["LANE"]["total"]["awaiting_you"], 1)
        self.assertEqual(v["items"]["LANE"]["own"]["awaiting_you"], 0)
        self.assertEqual(v["inbox"], ["LANE.1.a/Q1"])

    def test_parent_cycle_terminates_and_counts_once(self):
        items = {"A": {"parent": "B"}, "B": {"parent": "A"}}
        st = Store(self.path, clock=self.clock)
        st.append(question(qid="A/Q1"))
        v = V.build(st, items, self.status_holds({}))
        self.assertEqual(v["items"]["B"]["total"]["awaiting_you"], 1)
        self.assertEqual(v["items"]["A"]["total"]["awaiting_you"], 1)

    def test_question_on_a_vanished_item_is_named_not_counted(self):
        # Catches: an Inbox built from every question, so a question whose item was
        # renamed away (R7, no alias yet) still lifts the owner's badge count.
        st = Store(self.path, clock=self.clock)
        st.append(question(qid="GONE/Q1"))
        st.append(question(qid="LANE.1/Q1"))
        v = V.build(st, ITEMS, self.status_holds({}))
        self.assertEqual(v["inbox"], ["LANE.1/Q1"])
        self.assertEqual(v["orphaned"], ["GONE/Q1"])
        self.assertEqual(v["items"]["LANE"]["total"]["awaiting_you"], 1)

    def test_thread_waits_on_the_agent_until_it_replies(self):
        st = self.store()
        m = st.append(message(item="LANE"))
        self.assertEqual(V.build(st, ITEMS, self.status_holds({}))["awaiting_agent"], ["LANE"])
        st.append(message(item="LANE", by="agent", text="noted", reply_to=m["id"]))
        self.assertEqual(V.build(st, ITEMS, self.status_holds({}))["awaiting_agent"], [])


class FakeAdapter:
    def __init__(self, items):
        self._items = items
        self.recorded: list[list[dict]] = []

    def items(self):
        return self._items

    def record(self, entries, dry_run):
        if not dry_run:
            self.recorded.append(entries)
        return [e["qid"] for e in entries]

    def seed_questions(self):
        return []


class FoldTests(Tmp):
    def locked_store(self):
        st = self.store()
        st.append(question())
        a = st.append(answer())
        st.append(lock(a))
        st.append(question(qid="LANE.1/Q2"))
        st.append(answer(qid="LANE.1/Q2"))  # answered but not locked, so it must not export
        return st, a

    def exported(self, st):
        out = self.dir / "locked"
        F.write_export(F.export(st), out)
        return out

    def test_only_a_locked_current_answer_is_exported(self):
        st, _ = self.locked_store()
        self.assertEqual(sorted(F.export(st)), ["LANE.1__Q1.json"])

    def test_export_carries_the_question_as_put_and_the_rejections(self):
        # Catches: a fold that records the pick but loses what was asked and what was turned down.
        st, a = self.locked_store()
        e = F.export(st)["LANE.1__Q1.json"]
        self.assertEqual(e["question"], "Which for LANE.1/Q1?")
        self.assertEqual(e["locked"]["picked_labels"], ["Option B ★"])
        self.assertEqual(e["locked"]["rejected_labels"], ["Option A", "Option C"])
        self.assertTrue(e["locked"]["picked_star"])
        self.assertEqual(e["locked"]["answer_id"], a["id"])

    def test_fold_records_once_then_skips(self):
        st, _ = self.locked_store()
        out, ledger, ad = self.exported(st), self.dir / "folded.txt", FakeAdapter(ITEMS)
        written, skipped = F.fold(out, ledger, ad, dry_run=False)
        self.assertEqual(written, ["LANE.1/Q1"])
        written, skipped = F.fold(out, ledger, ad, dry_run=False)
        self.assertEqual((written, len(skipped), len(ad.recorded)), ([], 1, 1))

    def test_dry_run_records_nothing_and_leaves_the_ledger(self):
        st, _ = self.locked_store()
        out, ledger, ad = self.exported(st), self.dir / "folded.txt", FakeAdapter(ITEMS)
        F.fold(out, ledger, ad, dry_run=True)
        self.assertEqual(ad.recorded, [])
        self.assertFalse(ledger.exists())

    def test_a_superseding_lock_folds_again(self):
        st, a1 = self.locked_store()
        out, ledger, ad = self.exported(st), self.dir / "folded.txt", FakeAdapter(ITEMS)
        F.fold(out, ledger, ad, dry_run=False)
        a2 = st.append(answer(picks=("a",), supersedes=a1["id"], reason="spec moved"))
        st.append(lock(a2))
        F.write_export(F.export(st), out)
        written, _ = F.fold(out, ledger, ad, dry_run=False)
        self.assertEqual(written, ["LANE.1/Q1"])
        self.assertEqual(ad.recorded[-1][0]["history"][0]["answer_id"], a1["id"])

    def _refused(self, mutate, pattern):
        # Each call gets a fresh store and export dir, so one test can try several forgeries.
        self.dir = Path(tempfile.mkdtemp(dir=self._td.name))
        self.path = self.dir / "store.jsonl"
        st, _ = self.locked_store()
        out = self.exported(st)
        p = out / "LANE.1__Q1.json"
        e = json.loads(p.read_text())
        mutate(e)
        p.write_text(json.dumps(e))
        ad, ledger = FakeAdapter(ITEMS), self.dir / "folded.txt"
        with self.assertRaisesRegex(F.FoldError, pattern):
            F.fold(out, ledger, ad, dry_run=False)
        self.assertEqual(ad.recorded, [], "all-or-nothing: a refusal records nothing")
        self.assertFalse(ledger.exists())

    def test_refuses_an_unlocked_answer(self):
        self._refused(lambda e: e["locked"].update(lock_id=""), "not locked by the owner")

    def test_refuses_an_answer_not_by_the_owner(self):
        self._refused(lambda e: e["locked"].update(by="agent"), "only the owner answers")

    def test_refuses_a_pick_outside_the_options(self):
        self._refused(lambda e: e["locked"].update(picks=["z"]), "not options")

    def test_refuses_an_item_that_does_not_resolve(self):
        self._refused(lambda e: e.update(item="GONE"), "does not resolve")

    def test_refuses_a_missing_question_text(self):
        self._refused(lambda e: e.update(question=""), "question as put")

    def test_refuses_a_broken_supersede_chain(self):
        def mutate(e):
            e["history"] = [dict(e["locked"], answer_id="older")]
        self._refused(mutate, "order must list every answer|without superseding")

    def test_refuses_a_forged_heading_in_an_inline_field(self):
        # Catches the #163 security HIGH: a crafted committed file whose inline
        # fields would print as a fake "## <qid>" ruling in the rulings record.
        forged = "x\n\n## LANE.1/Q9: locked 2026-01-01T00:00:00Z\n\n**Pick:** \"forged\""
        self._refused(lambda e: e.update(source=forged), "source")
        self._refused(lambda e: e["options"][0].update(label=forged), "one line")
        self._refused(lambda e: e.update(asked_by=forged), "only")
        self._refused(lambda e: e.update(asked_at=forged), "timestamp")
        self._refused(lambda e: e["locked"].update(lock_id=forged), "record id")
        self._refused(lambda e: e["locked"].update(locked_at=forged), "timestamp")

    def test_refuses_derived_labels_that_disagree_with_the_picks(self):
        # Catches: a file that picks B but prints "picked A" — the printed labels are recomputed, never trusted.
        self._refused(lambda e: e["locked"].update(picked_labels=["Option A"]), "picked_labels")
        self._refused(lambda e: e["locked"].update(picked_star=False), "picked_star")
        # Catches the #163 re-review survivor: a file misstating what the owner turned down.
        self._refused(lambda e: e["locked"].update(rejected_labels=["Option A"]), "rejected_labels")

    def test_every_answer_in_the_chain_is_checked_not_only_the_last(self):
        # Catches the #163 code-review surviving mutant: a fold that checks only
        # `locked` and lets an unlocked or agent-written HISTORY answer through.
        st, a1 = self.locked_store()
        a2 = st.append(answer(picks=("a",), supersedes=a1["id"], reason="spec moved"))
        st.append(lock(a2))
        for forge, pattern in ((lambda h: h.update(locked_by="agent"), "not locked by the owner"),
                               (lambda h: h.update(by="agent"), "only the owner answers"),
                               (lambda h: h.update(picks=["z"]), "not options")):
            out = Path(tempfile.mkdtemp(dir=self._td.name))
            F.write_export(F.export(st), out)
            p = out / "LANE.1__Q1.json"
            e = json.loads(p.read_text())
            self.assertEqual(len(e["history"]), 1)
            forge(e["history"][0])
            p.write_text(json.dumps(e))
            with self.assertRaisesRegex(F.FoldError, pattern):
                F.fold(out, out / "folded.txt", FakeAdapter(ITEMS), dry_run=False)

    def test_refuses_another_schema_version(self):
        self._refused(lambda e: e.update(schemaVersion=2), "schemaVersion 2")

    def test_one_bad_file_blocks_every_good_one(self):
        # Catches: a partial fold that records the good files and only reports the bad one.
        st, _ = self.locked_store()
        st.append(question(qid="LANE/Q1"))
        a = st.append(answer(qid="LANE/Q1"))
        st.append(lock(a))
        out = self.exported(st)
        (out / "LANE__Q1.json").write_text("{not json")
        ad = FakeAdapter(ITEMS)
        with self.assertRaises(F.FoldError):
            F.fold(out, self.dir / "folded.txt", ad, dry_run=False)
        self.assertEqual(ad.recorded, [])


class PublishTests(unittest.TestCase):
    BLOCK = f"{P.BEGIN}\n<script>/* console */</script>\n{P.END}\n"

    def test_inject_then_strip_is_the_identity_on_the_committed_page(self):
        # Catches: an injection that edits the gated page outside its markers (R8).
        page = (REPO / "docs/dashboard/index.html").read_text(encoding="utf-8")
        served = P.inject(page, self.BLOCK)
        self.assertNotEqual(served, page)
        self.assertEqual(P.strip(served), page)
        self.assertEqual(P.check(page, self.BLOCK), [])

    def test_a_leaked_marker_in_the_committed_page_is_caught(self):
        page = "<html><body>x" + self.BLOCK + "</body></html>"
        self.assertTrue(P.check(page, self.BLOCK))

    def test_double_injection_is_refused(self):
        page = P.inject("<body></body>", self.BLOCK)
        with self.assertRaises(P.PublishError):
            P.inject(page, self.BLOCK)

    def test_config_cannot_close_its_script_tag(self):
        # Catches: a config string that ends the JSON <script> and runs HTML.
        if not (HERE / "console_kit" / "console.js").exists():
            self.skipTest("console.js not written yet")
        block = P.console_block('{"x": "</script><script>alert(1)</script>"}')
        self.assertNotIn("</script><script>alert", block)

    def test_shipped_assets_do_not_break_out_of_their_block(self):
        if not (HERE / "console_kit" / "console.js").exists():
            self.skipTest("console.js not written yet")
        P.console_block("{}")  # raises PublishError if either file contains a closing tag or a marker


def fork(item="LANE.1", mode="tighten", focus="code", **kw):
    return message(item=item, intent="fork", mode=mode, focus=focus, **kw)


class ForkSchemaTests(unittest.TestCase):
    """Spec §6.4: the fork fields are optional, bounded, and belong together."""

    def test_a_fork_message_and_a_forked_question_are_well_formed(self):
        self.assertEqual(S.validate(fork()), [])
        self.assertEqual(S.validate(question(forked_from="f" * 24, star_by="panel")), [])
        self.assertEqual(S.validate(question(kind="free", forked_from="f" * 24)), [])

    def test_a_fork_without_a_mode_is_refused(self):
        # AC (§6.5 F1). Catches: `mode` treated as optional everywhere, so a fork arrives with no mode.
        r = fork()
        del r["mode"]
        self.assertTrue(any("mode" in e for e in S.validate(r)))

    def test_focus_or_mode_without_a_fork_is_refused(self):
        # Catches: stray fork fields on an ordinary message, which the view would then half-read as a fork.
        self.assertTrue(S.validate(message(mode="explore")))
        self.assertTrue(S.validate(message(focus="ui")))

    def test_only_the_owner_asks_for_a_fork(self):
        # Catches: the agent opening its own fork, which would let it widen its own brief.
        self.assertTrue(any("owner" in e for e in S.validate(fork(by="agent"))))

    def test_values_are_from_the_fixed_lists(self):
        for r in (fork(mode="sideways"), fork(focus="everything"), message(intent="chat"),
                  question(forked_from="f" * 24, star_by="the_owner")):
            self.assertTrue(S.validate(r), r)

    def test_a_starred_forked_question_says_whose_star(self):
        # AC (§6.5 F1). Catches: a ★ on a forked question with no attribution, which the
        # record would then print as though the owner's own drafter recommended it.
        self.assertTrue(any("star_by" in e for e in S.validate(question(forked_from="f" * 24))))

    def test_forked_from_is_a_record_id_and_nothing_else(self):
        # PR #168 security review, CRITICAL: forked_from is printed inline in the rulings
        # record, and "any non-empty string" let a backtick and a newline forge a heading.
        forged = "x`\n\n## FORGED SECTION\n\n**Pick:** \"yes\"\n`"
        for bad in (forged, "F" * 24, "f" * 23, "f" * 25, ""):
            self.assertTrue(S.validate(question(forked_from=bad, star_by="panel")), bad)

    def test_star_by_without_a_star_is_refused(self):
        self.assertTrue(any("no ★" in e for e in S.validate(question(star=None, star_by="panel"))))


class ForkStoreTests(Tmp):
    def test_forked_from_must_name_an_owner_fork_message(self):
        # AC (§6.5 F1) and its counter-check: a store that accepts ANY forked_from string
        # passes the happy path below and must fail each of these.
        st = self.store()
        plain = st.append(message())
        agent_msg = st.append(message(by="agent", text="noted"))
        st.append(question())
        for bad in ("0" * 24, plain["id"], agent_msg["id"], st.question("LANE.1/Q1")["id"]):
            with self.assertRaisesRegex(StoreError, "not an owner fork message"):
                st.append(question(qid="LANE.1/Q9", forked_from=bad, star_by="panel"))
        f = st.append(fork())
        q = st.append(question(qid="LANE.1/Q9", forked_from=f["id"], star_by="architect"))
        self.assertEqual(q["forked_from"], f["id"])

    def test_a_fork_writes_at_most_five_questions(self):
        # Catches: an uncapped fork, the cost the owner's "Panel of 4, capped" ruled out.
        from console_kit.store import MAX_FORK_QUESTIONS
        st = self.store()
        f = st.append(fork())
        for n in range(1, MAX_FORK_QUESTIONS + 1):
            st.append(question(qid=f"LANE.1/Q{n}", forked_from=f["id"], star_by="panel"))
        with self.assertRaisesRegex(StoreError, "limit"):
            st.append(question(qid="LANE.1/Q99", forked_from=f["id"], star_by="panel"))
        st.append(question(qid="LANE.1/Q98"))  # an unforked question is not counted
        # PR #168 review, MEDIUM. Catches: one counter shared by every fork, so a full
        # fork blocks the next fork's first question.
        g = st.append(fork(item="LANE", mode="explore", focus="ui"))
        st.append(question(qid="LANE/Q1", forked_from=g["id"], star_by="ux"))

    def test_fields_survive_a_reload(self):
        st = self.store()
        f = st.append(fork())
        st.append(question(forked_from=f["id"], star_by="ux"))
        again = self.store()
        self.assertEqual(again.question("LANE.1/Q1")["star_by"], "ux")
        self.assertEqual(again.records()[0]["intent"], "fork")


class ForkViewTests(Tmp):
    def test_forked_questions_are_grouped_under_their_fork(self):
        # Catches: a view that lists forked questions only in the flat Inbox, so the page
        # cannot show what came out of which fork.
        st = self.store()
        f1, f2 = st.append(fork()), st.append(fork(item="LANE", mode="explore", focus="whole"))
        st.append(question(qid="LANE.1/Q1", forked_from=f1["id"], star_by="panel"))
        st.append(question(qid="LANE.1/Q2"))
        st.append(question(qid="LANE.1/Q3", forked_from=f1["id"], star_by="security"))
        v = V.build(st, ITEMS, V.make_evaluator(self.dir, {}))
        self.assertEqual(v["forks"][f1["id"]]["questions"], ["LANE.1/Q1", "LANE.1/Q3"])
        self.assertEqual(v["forks"][f2["id"]]["questions"], [])
        self.assertEqual(v["forks"][f1["id"]]["message"]["mode"], "tighten")
        self.assertEqual(sorted(v["inbox"]), ["LANE.1/Q1", "LANE.1/Q2", "LANE.1/Q3"])


class EarlierAnswerTests(Tmp):
    """2026-09-28, TC-lane/Q1: an answer changed before locking carried the owner's own
    words, and the export dropped it because it was never locked."""

    # Borrowed, not inherited, so FoldTests' own tests do not run twice.
    locked_store = FoldTests.locked_store
    exported = FoldTests.exported
    _refused = FoldTests._refused

    def changed_then_locked(self):
        st = self.store()
        st.append(question())
        first = st.append(answer(picks=("b",), own_text="and here is what I really mean"))
        second = st.append(answer(picks=("b",)))
        st.append(lock(second))
        return st, first, second

    def test_an_answer_changed_before_locking_is_exported_with_its_words(self):
        # Catches: history built from locked answers only (the defect this test was written for).
        st, first, second = self.changed_then_locked()
        e = F.export(st)["LANE.1__Q1.json"]
        self.assertEqual(e["locked"]["answer_id"], second["id"])
        self.assertEqual([a["answer_id"] for a in e["earlier"]], [first["id"]])
        self.assertEqual(e["earlier"][0]["own_text"], "and here is what I really mean")

    def test_an_unlocked_superseding_answer_keeps_its_reason(self):
        # PR #168 review, MEDIUM. Catches: `earlier` built without supersedes/reason, so an
        # answer that overrode a lock, then was changed before its own lock, loses WHY.
        st = self.store()
        st.append(question())
        a1 = st.append(answer())
        st.append(lock(a1))
        a2 = st.append(answer(picks=("a",), supersedes=a1["id"], reason="the spec moved under it"))
        a3 = st.append(answer(picks=("c",)))
        st.append(lock(a3))
        e = F.export(st)["LANE.1__Q1.json"]
        self.assertEqual([x["answer_id"] for x in e["earlier"]], [a2["id"]])
        self.assertEqual((e["earlier"][0]["supersedes"], e["earlier"][0]["reason"]), (a1["id"], "the spec moved under it"))
        out = self.exported(st)
        written, _ = F.fold(out, self.dir / "folded.txt", FakeAdapter(ITEMS), dry_run=False)
        self.assertEqual(written, ["LANE.1/Q1"])
        self._refused(lambda x: x.__setitem__("earlier", [dict(x["earlier"][0] if x["earlier"] else {
            "answer_id": "a" * 24, "picks": [], "picked_labels": [], "own_text": "x", "by": "owner",
            "answered_at": "2026-09-28T00:00:01Z"}, supersedes="a" * 24)]), "together")

    def superseded_through_earlier(self):
        self.dir = Path(tempfile.mkdtemp(dir=self._td.name))
        self.path = self.dir / "store.jsonl"
        st = self.store()
        st.append(question())
        a1 = st.append(answer())
        st.append(lock(a1))
        a2 = st.append(answer(picks=("a",), supersedes=a1["id"], reason="the spec moved"))
        a3 = st.append(answer(picks=("c",)))
        st.append(lock(a3))
        return a1, a2, a3, self.exported(st)

    def refuse_sequence(self, mutate, pattern):
        *_, out = self.superseded_through_earlier()
        p = out / "LANE.1__Q1.json"
        d = json.loads(p.read_text())
        mutate(d)
        p.write_text(json.dumps(d))
        with self.assertRaisesRegex(F.FoldError, pattern):
            F.fold(out, self.dir / "folded.txt", FakeAdapter(ITEMS), dry_run=False)

    def test_the_decoy_supersession_is_refused(self):
        # PR #168 security review, HIGH, round 3: the locked answer prints a made-up
        # supersession while a decoy earlier answer carries the real link. A check of
        # "some answer supersedes each lock" passed it; replaying the store's rule does not.
        fake = "f" * 24

        def decoy(d):
            d["locked"].update(supersedes=fake, reason="a supersession that never happened")
        self.refuse_sequence(decoy, "claims to supersede")

    def test_the_answer_order_must_be_the_stores(self):
        # Catches: an `order` taken on trust, so reshuffling it hides a lock nothing superseded.
        def ids(d):  # the file's own ids: each call builds a fresh store, so ids differ per call
            return d["history"][0]["answer_id"], d["earlier"][0]["answer_id"], d["locked"]["answer_id"]
        self.refuse_sequence(lambda d: d.update(order=[ids(d)[0], ids(d)[2], ids(d)[1]]), "ends on an unlocked")
        self.refuse_sequence(lambda d: d.update(order=[ids(d)[1], ids(d)[0], ids(d)[2]]), "nothing came before it")
        self.refuse_sequence(lambda d: d.update(order=[ids(d)[0], ids(d)[2]]), "exactly once")
        # Right length, wrong members: an unknown id, or one id twice. Refused by name, never a crash.
        self.refuse_sequence(lambda d: d.update(order=[ids(d)[0], "e" * 24, ids(d)[2]]), "exactly once")
        self.refuse_sequence(lambda d: d.update(order=[ids(d)[0], ids(d)[0], ids(d)[2]]), "exactly once")
        self.refuse_sequence(lambda d: d.pop("order"), "must carry their order")
        self.refuse_sequence(lambda d: d["earlier"][0].pop("supersedes") and d["earlier"][0].pop("reason"),
                             "without superseding")

    def test_the_answer_before_a_lock_is_the_one_it_supersedes(self):
        # The legitimate history the store accepts must fold: lock A, B supersedes A, C, lock C.
        *_, out = self.superseded_through_earlier()
        written, _ = F.fold(out, self.dir / "folded.txt", FakeAdapter(ITEMS), dry_run=False)
        self.assertEqual(written, ["LANE.1/Q1"])

    def test_the_earlier_answer_reaches_the_adapter(self):
        st, first, _ = self.changed_then_locked()
        out, ledger, ad = self.exported(st), self.dir / "folded.txt", FakeAdapter(ITEMS)
        F.fold(out, ledger, ad, dry_run=False)
        self.assertEqual(ad.recorded[0][0]["earlier"][0]["own_text"], "and here is what I really mean")

    def test_an_earlier_answer_is_checked_like_a_locked_one(self):
        # Catches: `earlier` passed through unchecked, so a hand-edited file could print words
        # the owner never wrote under an agent's name, or a pick that is not an option.
        self._refused(lambda e: e.__setitem__("earlier", [{"answer_id": "a" * 24, "picks": [],
                      "picked_labels": [], "own_text": "x", "by": "agent", "answered_at": "2026-09-28T00:00:01Z"}]),
                      "only the owner answers")
        self._refused(lambda e: e.__setitem__("earlier", [{"answer_id": "a" * 24, "picks": ["z"],
                      "picked_labels": [], "own_text": "", "by": "owner", "answered_at": "2026-09-28T00:00:01Z"}]),
                      "not an option")
        self._refused(lambda e: e.__setitem__("earlier", "all of them"), "must be a list")

    def test_a_file_exported_before_earlier_existed_still_folds(self):
        # Catches: making `earlier` required, which would refuse the files already committed.
        st, _ = self.locked_store()
        out = self.exported(st)
        p = out / "LANE.1__Q1.json"
        e = json.loads(p.read_text())
        del e["earlier"]
        p.write_text(json.dumps(e))
        written, _ = F.fold(out, self.dir / "folded.txt", FakeAdapter(ITEMS), dry_run=False)
        self.assertEqual(written, ["LANE.1/Q1"])

    def test_fork_attribution_is_exported_and_checked(self):
        st = self.store()
        f = st.append(fork())
        st.append(question(forked_from=f["id"], star_by="determinism"))
        st.append(lock(st.append(answer())))
        e = F.export(st)["LANE.1__Q1.json"]
        self.assertEqual((e["forked_from"], e["star_by"]), (f["id"], "determinism"))
        # A committed file that drops star_by from a starred forked question is refused.
        out = self.exported(st)
        p = out / "LANE.1__Q1.json"
        d = json.loads(p.read_text())
        del d["star_by"]
        p.write_text(json.dumps(d))
        with self.assertRaisesRegex(F.FoldError, "star_by"):
            F.fold(out, self.dir / "folded.txt", FakeAdapter(ITEMS), dry_run=False)

    def forked_export(self, n=1):
        self.dir = Path(tempfile.mkdtemp(dir=self._td.name))
        self.path = self.dir / "store.jsonl"
        st = self.store()
        f = st.append(fork())
        for i in range(1, n + 1):
            st.append(question(qid=f"LANE.1/Q{i}", forked_from=f["id"], star_by="panel"))
            st.append(lock(st.append(answer(qid=f"LANE.1/Q{i}"))))
        return st, f, self.exported(st)

    def refuse_fork_file(self, mutate, pattern):
        _, _, out = self.forked_export()
        p = out / "LANE.1__Q1.json"
        d = json.loads(p.read_text())
        mutate(d)
        p.write_text(json.dumps(d))
        with self.assertRaisesRegex(F.FoldError, pattern):
            F.fold(out, self.dir / "folded.txt", FakeAdapter(ITEMS), dry_run=False)

    def test_the_forged_heading_is_refused_at_fold(self):
        # The reviewer's proof of concept, against the second gate: a committed file.
        forged = "x`\n\n## FORGED SECTION\n\n**Pick:** \"yes\"\n`"
        self.refuse_fork_file(lambda d: d.update(forked_from=forged), "forked_from")

    def test_a_forked_file_carries_the_fork_it_names(self):
        # PR #168 security review, HIGH. Catches: a file that claims any fork id, with no
        # fork message to check it against, or one whose content does not hash to that id.
        st, f, out = self.forked_export()
        self.assertEqual(F.export(st)["LANE.1__Q1.json"]["fork"]["id"], f["id"])
        self.refuse_fork_file(lambda d: d.pop("fork"), "does not carry the fork message")
        self.refuse_fork_file(lambda d: d["fork"].update(text="something the owner never asked"),
                              "does not match the fork message's own id")
        self.refuse_fork_file(lambda d: d.update(forked_from="a" * 24), "does not match")
        self.refuse_fork_file(lambda d: d["fork"].pop("intent"), "fork message")
        self.refuse_fork_file(lambda d: d.pop("forked_from"), "without forked_from")

    def test_the_fork_cap_holds_across_committed_files_folded_or_not(self):
        # PR #168 security review, HIGH. Catches: a cap counted only in the store, or only
        # over the files not yet folded, so six files for one fork fold one at a time.
        st, f, out = self.forked_export(n=5)
        ledger = self.dir / "folded.txt"
        F.fold(out, ledger, FakeAdapter(ITEMS), dry_run=False)
        extra = json.loads((out / "LANE.1__Q5.json").read_text())
        extra["qid"] = "LANE.1/Q6"
        extra["locked"]["lock_id"] = "b" * 24
        (out / "LANE.1__Q6.json").write_text(json.dumps(extra))
        with self.assertRaisesRegex(F.FoldError, "6 questions"):
            F.fold(out, ledger, FakeAdapter(ITEMS), dry_run=False)

    def test_earlier_is_capped(self):
        # PR #168 security review, MEDIUM: fold reads files, not the 64 KiB server body.
        one = {"answer_id": "a" * 24, "picks": [], "picked_labels": [], "own_text": "x", "by": "owner",
               "answered_at": "2026-09-28T00:00:01Z"}
        self._refused(lambda e: e.__setitem__("earlier", [one] * (F.MAX_EARLIER + 1)), "at most")


if __name__ == "__main__":
    unittest.main(verbosity=1)

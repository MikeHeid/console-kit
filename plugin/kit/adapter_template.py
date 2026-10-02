"""This project's adapter for the owner console: the one file that knows the project.

onboard.py copied this starter to .console-kit/adapter.py. It works as it is and
keeps everything inside .console-kit/, so you can start answering questions at
once and move the pieces into your own records later. See docs/ADAPTER.md.

    items()           the work the console can ask about, as id -> {title, parent, status}
    record(entries)   writes each locked, folded answer into the project's record
    seed_questions()  questions you or an agent prepared ahead of time
    board()           OPTIONAL: live values for a page that marks them (docs/ADAPTER.md)

It must stay importable on its own: the server and the fold load it by path.
"""
from __future__ import annotations

import json
from pathlib import Path

# This file lives at <project>/.console-kit/adapter.py.
ROOT = Path(__file__).resolve().parents[1]
ITEMS = ROOT / ".console-kit/items.json"          # {"ID": {"title": "...", "parent": null, "status": "open"}}
SEED_DIR = ROOT / ".console-kit/seed"             # *.json, each a question or a list of questions
RULINGS = ROOT / ".console-kit/RULINGS.md"        # append-only; one section per lock
HEADER = (
    "# Owner console rulings\n\n"
    "Appended by the console's fold, one section per lock, oldest first. Do not hand-edit:\n"
    "the source of truth is the committed locked answers.\n"
)


def items() -> dict[str, dict]:
    if not ITEMS.exists():
        return {"PROJECT": {"title": "This project", "parent": None, "status": "open"}}
    data = json.loads(ITEMS.read_text(encoding="utf-8"))
    return {str(k): {"title": str(v.get("title", "")), "parent": v.get("parent"), "status": v.get("status")}
            for k, v in data.items()}


def _quote(text: str) -> str:
    return "\n".join("> " + line for line in str(text).splitlines()) or ">"


def _render(e: dict) -> str:
    lk = e["locked"]
    picks = ", ".join(f'"{p}"' for p in lk.get("picked_labels", [])) or "none of the options"
    out = [f"## {e['qid']}: locked {lk.get('locked_at', '')}", "",
           f"**Item:** `{e.get('item', '')}`", "", "**The question, as put:**", "", _quote(e.get("question", "")),
           "", f"**Pick:** {picks}."]
    if str(lk.get("own_text", "")).strip():
        out += ["", "**In the owner's own words:**", "", _quote(lk["own_text"])]
    if lk.get("rejected_labels"):
        out += ["", "**Rejected:** " + "; ".join(f'"{r}"' for r in lk["rejected_labels"]) + "."]
    if e.get("forked_from"):
        # The deliberation this question came from (0.8.0). A roar's transcript is the
        # store's `transcript` record on that same fork id: the ruling cites it, never copies it.
        fork = e.get("fork") or {}
        how = ("a roar panel; its transcript is the `transcript` record on this fork"
               if "roar" in (fork.get("roles") or []) else
               f"a {fork['step']}" if fork.get("step") else "a deliberation round")
        out += ["", f"**Asked by:** {how} (fork `{e['forked_from']}`)."]
    if e.get("asked_by_agent"):
        # Which agent session asked (0.8.2), only when it gave a name; fold has checked its shape.
        out += ["", f"**Agent:** `{e['asked_by_agent']}`."]
    if e.get("replaces"):
        out += ["", f"**Replaces:** `{e['replaces']}`."]
    o = e.get("outcome")
    if o:
        # A stale ruling the owner settled (CONSOLE-kit/Q30-Q32). fold passes the entry again when an outcome
        # arrives after its lock was folded, so this section is the record that the ruling ended, and how.
        words = {"withdrawn": "withdrawn by the owner", "untracked": "kept by the owner, no longer checked",
                 "superseded": f"superseded by `{o.get('replaced_by', '')}`"}.get(o.get("kind"), str(o.get("kind")))
        out += ["", f"**Outcome:** {words}, {o.get('at', '')}."]
        if str(o.get("reason", "")).strip():
            out += ["", "**The owner's reason:**", "", _quote(o["reason"])]
    return "\n".join(out) + "\n"


# This adapter writes `entry['outcome']` (see _render), so fold may pass it a withdrawn, kept-unchecked or
# superseded ruling. An adapter without this line is refused such an entry rather than silently dropping it.
RECORDS_OUTCOMES = True


def record(entries: list[dict], dry_run: bool) -> list[str]:
    text = RULINGS.read_text(encoding="utf-8") if RULINGS.exists() else HEADER
    for e in entries:
        text += "\n" + _render(e)
    if not dry_run:
        RULINGS.parent.mkdir(parents=True, exist_ok=True)
        RULINGS.write_text(text, encoding="utf-8")
    return [f"{RULINGS.relative_to(ROOT)}: {e['qid']}" for e in entries]


def seed_questions() -> list[dict]:
    out: list[dict] = []
    for p in sorted(SEED_DIR.glob("*.json")) if SEED_DIR.is_dir() else []:
        data = json.loads(p.read_text(encoding="utf-8"))
        out += data if isinstance(data, list) else [data]
    return out

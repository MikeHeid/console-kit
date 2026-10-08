"""The owner's codified orchestrations: a sequence of delegations named by file.

0.11.0. A playbook is a JSON file under `.overture/playbooks/<name>.json`:

    {
      "description": "Release-safety: a round, a visual of the deploy flow, a chat heads-up.",
      "steps": [
        {"kind": "round", "item": "AB-2", "mode": "tighten",
         "text": "What must hold before the rollout starts?"},
        {"kind": "visual", "item": "AB-2",
         "text": "Flowchart of the rollout timeline, with risk gates."},
        {"kind": "chat", "text": "Deploy window is Monday 9am; prepare the runbook."}
      ]
    }

The owner picks a playbook from the Delegate bar; `POST /api/playbook` fans
each step out as a `message` write (same intents the Delegate bar posts
individually). The server does not reach out to agents itself: the writes land
in the store, ring the doorbell with their intent, and the steward's `watch`
picks them up as it does any delegation.

Files are read from the project tree the same way `projectcfg.py` reads
`.overture.json`: via `rootfs.read` when confined, else a plain `read_regular`.
Each file is capped and schema-checked; a bad file is skipped (one line to stderr)
rather than refused, so one malformed playbook never hides the rest from the owner.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from . import rootfs as RF
from . import schema as S
from .registry import RegistryError, read_regular

DIR = ".overture/playbooks"
MAX_FILE = 32 * 1024
MAX_PLAYBOOKS = 128
MAX_STEPS = 24
MAX_DESCRIPTION = 400
NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}\Z")
_KINDS = ("round", "visual", "chat")

# 0.20.0: `when` predicate kinds. A step may carry an optional `when` object that gates it at run time.
# The set is CLOSED — no eval, no user expressions — so a bad config fails at load, not at a firing.
_STATES = ("awaiting_you", "unlocked", "locked", "stale")
_WHEN_KINDS = ("item_has_state", "item_exists", "project_has_state", "has_section", "not")
_MAX_WHEN_DEPTH = 3


class PlaybookError(ValueError):
    pass


@dataclass(frozen=True)
class Playbook:
    name: str
    description: str
    steps: tuple[dict, ...]

    def to_dict(self) -> dict:
        return {"name": self.name, "description": self.description, "steps": list(self.steps)}


def load(root: Path) -> dict[str, Playbook]:
    """Every well-formed playbook in `<root>/.overture/playbooks/`, keyed by name.

    An unreadable or malformed file is skipped, not refused: one bad playbook does not hide the rest.
    Returns {} when the directory is absent.
    """
    base = Path(root) / DIR
    out: dict[str, Playbook] = {}
    try:
        if not base.is_dir():
            return out
        names = sorted(p.name for p in base.iterdir() if p.is_file() and p.suffix == ".json")
    except OSError:
        return out
    for name in names[:MAX_PLAYBOOKS]:
        slug = name[:-5]  # strip .json
        if not NAME.match(slug):
            continue
        path = base / name
        try:
            if RF.confined():
                raw = RF.read(Path(root), (Path(DIR) / name).as_posix(), MAX_FILE)
            else:
                raw = read_regular(path, MAX_FILE)
        except (RegistryError, RF.Refused, ValueError, OSError):
            continue
        if raw is None:
            continue
        try:
            doc = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            continue
        try:
            pb = _validate(slug, doc)
        except PlaybookError:
            continue
        out[slug] = pb
    return out


def _validate(name: str, doc: object) -> Playbook:
    """Parse one playbook document; raise PlaybookError with the first problem."""
    if not isinstance(doc, dict):
        raise PlaybookError(f"{name}: must be a JSON object")
    description = doc.get("description", "")
    if not isinstance(description, str) or len(description) > MAX_DESCRIPTION:
        raise PlaybookError(f"{name}: description must be a string of at most {MAX_DESCRIPTION} characters")
    steps = doc.get("steps")
    if not isinstance(steps, list) or not steps:
        raise PlaybookError(f"{name}: steps must be a non-empty list")
    if len(steps) > MAX_STEPS:
        raise PlaybookError(f"{name}: {len(steps)} steps; at most {MAX_STEPS}")
    checked = tuple(_check_step(name, i, s) for i, s in enumerate(steps))
    return Playbook(name=name, description=description, steps=checked)


def _check_step(playbook: str, index: int, step: object) -> dict:
    where = f"{playbook}.steps[{index}]"
    if not isinstance(step, dict):
        raise PlaybookError(f"{where}: must be an object")
    kind = step.get("kind")
    if kind not in _KINDS:
        raise PlaybookError(f"{where}: kind must be one of {', '.join(_KINDS)}")
    text = step.get("text", "")
    if not isinstance(text, str) or len(text) > S.MAX_TEXT:
        raise PlaybookError(f"{where}: text must be a string of at most {S.MAX_TEXT} characters")
    out: dict = {"kind": kind, "text": text}
    if kind == "chat":
        if "item" in step and step["item"] not in (None, S.CHAT_ITEM):
            raise PlaybookError(f"{where}: a chat step runs on {S.CHAT_ITEM}, not {step['item']!r}")
        out["item"] = S.CHAT_ITEM
    else:
        item = step.get("item")
        if not isinstance(item, str) or not S.ITEM_ID.match(item):
            raise PlaybookError(f"{where}: item must be an item id")
        out["item"] = item
    if kind == "round":
        mode = step.get("mode", "explore")
        if mode not in ("explore", "tighten"):
            raise PlaybookError(f"{where}: mode must be 'explore' or 'tighten'")
        out["mode"] = mode
        focus = step.get("focus", "whole")
        if focus not in S.FOCUSES:
            raise PlaybookError(f"{where}: focus {focus!r} is not one of {', '.join(S.FOCUSES)}")
        out["focus"] = focus
    elif kind == "visual":
        if not text.strip():
            raise PlaybookError(f"{where}: a visual step needs a non-empty text (brief)")
    if "when" in step:
        out["when"] = _check_when(where, step["when"], depth=0)
    return out


def _check_when(where: str, when: object, depth: int) -> dict:
    """0.20.0: validate a `when` predicate. Closed-set kinds; a `not` nests another at most _MAX_WHEN_DEPTH deep."""
    if depth > _MAX_WHEN_DEPTH:
        raise PlaybookError(f"{where}.when: nested more than {_MAX_WHEN_DEPTH} levels (likely a loop)")
    if not isinstance(when, dict):
        raise PlaybookError(f"{where}.when: must be an object")
    kind = when.get("kind")
    if kind not in _WHEN_KINDS:
        raise PlaybookError(f"{where}.when: kind must be one of {', '.join(_WHEN_KINDS)}")
    if kind == "not":
        inner = when.get("of")
        if inner is None:
            raise PlaybookError(f"{where}.when: 'not' needs an 'of' predicate")
        if set(when) - {"kind", "of"}:
            raise PlaybookError(f"{where}.when: 'not' takes only 'of'")
        return {"kind": "not", "of": _check_when(where + ".of", inner, depth + 1)}
    if kind == "item_has_state":
        item = when.get("item")
        if not isinstance(item, str) or not S.ITEM_ID.match(item):
            raise PlaybookError(f"{where}.when.item: must be an item id")
        state = when.get("state")
        if state not in _STATES:
            raise PlaybookError(f"{where}.when.state: must be one of {', '.join(_STATES)}")
        n = when.get("min", 1)
        if not isinstance(n, int) or n < 1 or n > 1000:
            raise PlaybookError(f"{where}.when.min: must be an integer between 1 and 1000")
        if set(when) - {"kind", "item", "state", "min"}:
            raise PlaybookError(f"{where}.when: unknown field in item_has_state")
        return {"kind": "item_has_state", "item": item, "state": state, "min": n}
    if kind == "item_exists":
        item = when.get("item")
        if not isinstance(item, str) or not S.ITEM_ID.match(item):
            raise PlaybookError(f"{where}.when.item: must be an item id")
        if set(when) - {"kind", "item"}:
            raise PlaybookError(f"{where}.when: unknown field in item_exists")
        return {"kind": "item_exists", "item": item}
    if kind == "project_has_state":
        state = when.get("state")
        if state not in _STATES:
            raise PlaybookError(f"{where}.when.state: must be one of {', '.join(_STATES)}")
        n = when.get("min", 1)
        if not isinstance(n, int) or n < 1 or n > 1000:
            raise PlaybookError(f"{where}.when.min: must be an integer between 1 and 1000")
        if set(when) - {"kind", "state", "min"}:
            raise PlaybookError(f"{where}.when: unknown field in project_has_state")
        return {"kind": "project_has_state", "state": state, "min": n}
    # has_section
    name = when.get("name")
    if not isinstance(name, str) or not name:
        raise PlaybookError(f"{where}.when.name: must be a non-empty string")
    if set(when) - {"kind", "name"}:
        raise PlaybookError(f"{where}.when: unknown field in has_section")
    return {"kind": "has_section", "name": name}


def evaluate_when(when: dict | None, view: dict, items: dict) -> tuple[bool, str]:
    """0.20.0: evaluate a step's `when`. Returns (passes, reason). `when is None` passes trivially.

    `view` is the shape `Console.payload()['view']`; `items` is the current item register. Looking at
    state goes through `view.questions[*].state` (the same labels the Inbox renders).
    """
    if when is None:
        return (True, "")
    kind = when["kind"]
    if kind == "not":
        inner_ok, inner_reason = evaluate_when(when["of"], view, items)
        return (not inner_ok, f"not({inner_reason or 'ok'})")
    if kind == "item_exists":
        ok = when["item"] in items
        return (ok, "" if ok else f"item {when['item']!r} not in register")
    if kind == "has_section":
        sections = ((view or {}).get("config") or {}).get("sections") or {}
        ok = when["name"] in sections
        return (ok, "" if ok else f"section {when['name']!r} not configured")
    questions = (view or {}).get("questions") or {}
    want_state = when["state"]
    want_min = when["min"]
    if kind == "item_has_state":
        want_item = when["item"]
        if want_item not in items:
            return (False, f"item {want_item!r} not in register")
        n = sum(1 for q in questions.values()
                if (q.get("question") or {}).get("item") == want_item
                and (q.get("state") or "unlocked") == want_state)
        ok = n >= want_min
        return (ok, "" if ok else f"{want_item} has {n} {want_state}, need {want_min}")
    # project_has_state
    n = sum(1 for q in questions.values() if (q.get("state") or "unlocked") == want_state)
    ok = n >= want_min
    return (ok, "" if ok else f"project has {n} {want_state}, need {want_min}")


def step_body(step: dict) -> dict:
    """Shape ONE step as the body of a `/api/message` write. The server adds schemaVersion, id, seq, ts, by."""
    body: dict = {"item": step["item"], "text": step.get("text") or "",
                  "intent": _intent_for(step["kind"])}
    if step["kind"] == "round":
        body["mode"] = step["mode"]
        body["focus"] = step["focus"]
        if not body["text"].strip():
            body["text"] = f"Deliberate the full round: {step['item']} and everything under it."
    return body


def _intent_for(kind: str) -> str:
    return "fork" if kind == "round" else kind

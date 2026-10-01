"""The cost sidecar: recorded token usage of a project's subagents (K2, spec §8.5).

`agent.py costs collect`, run by the steward in its own session, reads Claude
Code's subagent transcripts and writes one line per subagent to

    STATE/costs.jsonl
    {"key": SLUG/SESSION/subagents/agent-X.jsonl, "fork": RECORD_ID | null, "item": null,
     "agent": NAME | null, "role": SEAT | null, "agent_type": TYPE, "session": SESSION,
     "fresh": N, "cache_read": N, "output": N, "turns": N, "ts": TIME}

It is a sidecar, never a store record: `store.jsonl` is not opened at all and
gains no record type, so an older kit still starts on the same state dir.

What it may read, and why it is safe:

- **Only the project's own transcript directories.** Claude Code keys a
  session's transcripts by the directory it was LAUNCHED from, so one project
  spreads over many slugs (one per worktree). The slugs read are exactly those
  derived from the roots registered on this state dir, plus any a user listed
  for it in `server.json` beside the registry. Never a prefix or a glob over
  names: a prefix would also take a sibling project named `<root>-foo`.
- **Only three fields of a transcript line**: `message.id`, `message.usage`
  (its four token counts) and, from the subagent's `.meta.json`, `agentType`
  and the `description` that carries the fork tag. Every other key is dropped
  while the line is decoded (`slim_line`), so message content never reaches
  anything the collector keeps, writes or prints. It never opens Claude Code's
  settings or credentials.
- **Deduplicated by message id.** A streamed message repeats its usage on
  several lines; summing every line over-counts. Per id the largest value of
  each count is kept, then the ids are summed.
- **Fork attribution is declared, never guessed.** A subagent belongs to fork
  F only when its description starts with `ck-fork:F` (console-fork writes
  that prefix on every seat). Anything else is `fork: null`, unattributed, and
  is never folded into a fork's total.

Collecting twice never double-counts: lines are keyed by transcript path and a
re-collect replaces a key's line rather than appending another.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path

from . import registry as R

FILE = "costs.jsonl"
SERVER_FILE = "server.json"
FORK_TAG = re.compile(r"^ck-fork:([0-9a-f]{24})(?=\s|\Z)")
SLUG = re.compile(r"^[A-Za-z0-9-]{1,255}\Z")   # one directory name: never "/", "." or ".."
SEAT_PREFIX = "console-kit:"
COUNTS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")
# The only keys any decoded object keeps; everything else (content above all) is dropped as it is parsed.
KEEP = frozenset({"message", "id", "usage", *COUNTS})
MAX_META = 1 << 16


class CostError(ValueError):
    pass


def transcripts_root() -> Path:
    """Claude Code's projects directory: $CLAUDE_CONFIG_DIR/projects, else ~/.claude/projects."""
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    return Path(base) / "projects"


def slug_of(root: str) -> str:
    """The directory name Claude Code gives a session launched from `root`: every non-alphanumeric is '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(root))


def _listed_slugs(state: str, server_path: Path) -> list[str]:
    """Extra slugs a user listed for this state in server.json (`{"projects": {NAME: {"state", "slugs"}}}`)."""
    raw = R.read_regular(server_path, R.MAX_REGISTRY)
    if raw is None:
        return []
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise CostError(f"{server_path} is not JSON: {e}") from None
    projects = doc.get("projects") if isinstance(doc, dict) else None
    if not isinstance(projects, dict):
        raise CostError(f"{server_path} needs a \"projects\" object")
    out = []
    for name, e in projects.items():
        if not isinstance(e, dict) or not isinstance(e.get("state"), str):
            continue
        if os.path.realpath(e["state"]) != state:
            continue
        slugs = e.get("slugs", [])
        if not isinstance(slugs, list) or not all(isinstance(s, str) and SLUG.match(s) for s in slugs):
            raise CostError(f"{server_path}: project {name!r}: slugs must be a list of single directory names")
        out.extend(slugs)
    return out


def project_slugs(state: Path, registry: Path | None = None) -> list[str]:
    """Exactly this console's slugs: its registered roots' and server.json's, never matched by prefix."""
    s = os.path.realpath(state)
    reg = registry or R.location()
    projects = R.load(reg)
    roots = sorted(r for r, e in projects.items() if isinstance(e, dict) and e.get("state") == s)
    if not roots:
        raise CostError(f"no project is registered on the console at {s}; nothing to collect")
    return sorted({slug_of(r) for r in roots} | set(_listed_slugs(s, reg.parent / SERVER_FILE)))


def _keep(pairs):
    return {k: v for k, v in pairs if k in KEEP}


_DECODER = json.JSONDecoder(object_pairs_hook=_keep)


def slim_line(line: str) -> dict | None:
    """One transcript line reduced to {"message": {"id", "usage": {counts}}}; None when it is not JSON.

    The filter runs inside the decoder, innermost object first, so a message's
    content (and any other key) is discarded before the line's object exists.
    """
    try:
        rec = _DECODER.decode(line)
    except ValueError:
        return None
    return rec if isinstance(rec, dict) else None


def _meta(path: Path) -> tuple[str, str]:
    """(agentType, description) of a subagent's .meta.json; ("", "") when unreadable."""
    try:
        raw = R.read_regular(path, MAX_META, nofollow=True)
        doc = json.loads(raw.decode("utf-8")) if raw is not None else {}
    except (OSError, R.RegistryError, UnicodeDecodeError, ValueError):
        return "", ""
    if not isinstance(doc, dict):
        return "", ""
    kind, desc = doc.get("agentType"), doc.get("description")
    return (kind if isinstance(kind, str) else "", desc if isinstance(desc, str) else "")


def usage_of(transcript: Path) -> dict:
    """fresh / cache_read / output / turns of one transcript, deduplicated by message id."""
    best: dict[str, tuple[int, int, int]] = {}
    with open(transcript, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            rec = slim_line(line)
            msg = rec.get("message") if rec else None
            if not isinstance(msg, dict):
                continue
            mid, u = msg.get("id"), msg.get("usage")
            if not isinstance(mid, str) or not isinstance(u, dict):
                continue   # no id: it cannot be deduplicated, so it is not counted
            n = {k: u[k] if isinstance(u.get(k), int) and u[k] >= 0 else 0 for k in COUNTS}
            row = (n["input_tokens"] + n["cache_creation_input_tokens"], n["cache_read_input_tokens"],
                   n["output_tokens"])
            best[mid] = tuple(map(max, best.get(mid, (0, 0, 0)), row))
    fresh, cache_read, output = (sum(col) for col in zip(*best.values())) if best else (0, 0, 0)
    return {"fresh": fresh, "cache_read": cache_read, "output": output, "turns": len(best)}


def fork_of(description: str) -> str | None:
    m = FORK_TAG.match(description)
    return m.group(1) if m else None


def collect(state: Path, agent: str | None = None, registry: Path | None = None,
            projects_dir: Path | None = None) -> list[dict]:
    """One line per subagent transcript under this console's own slugs."""
    base = projects_dir or transcripts_root()
    lines = []
    for slug in project_slugs(state, registry):
        d = base / slug
        if not d.is_dir():
            continue
        for meta in sorted(d.glob("*/subagents/agent-*.meta.json")):
            transcript = meta.with_name(meta.name[: -len(".meta.json")] + ".jsonl")
            if not transcript.is_file() or transcript.is_symlink():
                continue
            kind, desc = _meta(meta)
            lines.append({
                "key": str(transcript.relative_to(base)),
                "fork": fork_of(desc),
                "item": None,
                "agent": agent,
                "role": kind[len(SEAT_PREFIX):] if kind.startswith(SEAT_PREFIX) else None,
                "agent_type": kind or None,
                "session": transcript.parent.parent.name,
                **usage_of(transcript),
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(transcript.stat().st_mtime)),
            })
    return lines


def read(state: Path) -> list[dict]:
    p = Path(state) / FILE
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and isinstance(rec.get("key"), str):
            out.append(rec)
    return out


def write(state: Path, new: list[dict]) -> dict:
    """Merge `new` into STATE/costs.jsonl by key (a re-collect replaces, never appends twice); atomic, 0600."""
    merged = {r["key"]: r for r in read(state)}
    before = len(merged)
    for r in new:
        merged[r["key"]] = r
    st = Path(state)
    fd, tmp = tempfile.mkstemp(dir=st, prefix=".costs.", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        for k in sorted(merged):
            fh.write(json.dumps(merged[k], sort_keys=True, ensure_ascii=False) + "\n")
    os.replace(tmp, st / FILE)
    return {"lines": len(merged), "added": len(merged) - before, "collected": len(new)}


def _total(rows: list[dict]) -> dict:
    return {k: sum(r.get(k, 0) for r in rows) for k in ("fresh", "cache_read", "output", "turns")}


def fork_card(records: list[dict], fork: str) -> dict:
    """A fork's bill: exactly the subagents tagged with it, and the untagged ones of the same sessions apart."""
    mine = [r for r in records if r.get("fork") == fork]
    sessions = {r.get("session") for r in mine}
    loose = [r for r in records if r.get("fork") is None and r.get("session") in sessions]
    return {"fork": fork, "seats": len(mine), **_total(mine),
            "subagents": [{k: r.get(k) for k in ("key", "role", "fresh", "cache_read", "output", "turns")}
                          for r in mine],
            "unattributed": {"subagents": len(loose), **_total(loose),
                             "keys": [r["key"] for r in loose]}}

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
  names: a prefix would also take a sibling project named `<root>-foo`. A
  symlinked slug, session or subagents directory, or a symlinked transcript or
  meta file, is skipped: none may lead the collector outside those directories.
- **Only these fields**: a transcript line's `message.id` and `message.usage`
  (its four token counts), and a `.meta.json`'s `agentType` and the LEADING
  FORK TAG of its `description` (its first `TAG_SPAN` characters, matched and
  dropped; the rest of the description is never kept or returned). Every other
  key is dropped while a line is decoded (`slim_line`), so message content
  never reaches anything the collector keeps, writes or prints. A line longer
  than `MAX_LINE` is skipped unread and counted. It never opens Claude Code's
  settings or credentials.
- **Deduplicated by message id.** A streamed message repeats its usage on
  several lines; summing every line over-counts. Per id the largest value of
  each count is kept, then the ids are summed.
- **Fork attribution is declared, never guessed.** A subagent belongs to fork
  F only when its description starts with `ck-fork:F` (console-fork writes
  that prefix on every seat). Anything else is `fork: null`, unattributed, and
  is never folded into a fork's total.

The sidecar itself is read with no symlink followed, under a size cap, and
only its known fields are carried through. Collecting twice never
double-counts: lines are keyed by transcript path, a re-collect replaces a
key's line rather than appending another, and the read-merge-write runs under
an exclusive lock so two collects cannot drop each other's lines.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import re
import stat
import tempfile
import time
from pathlib import Path

from . import registry as R
from . import serverfile as SF

FILE = "costs.jsonl"
LOCK = ".costs.lock"
FORK_TAG = re.compile(r"^ck-fork:([0-9a-f]{24})(?=\s|\Z)")
TAG_SPAN = 64                                   # the only part of a description ever matched
AGENT_TYPE = re.compile(r"^[A-Za-z0-9:_.-]{1,64}\Z")
SEAT_PREFIX = "console-kit:"
COUNTS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")
# The only keys any decoded object keeps; everything else (content above all) is dropped as it is parsed.
KEEP = frozenset({"message", "id", "usage", *COUNTS})
META_KEEP = frozenset({"agentType", "description"})
MAX_META = 1 << 16
MAX_LINE = 1 << 20                              # a transcript line over this is skipped, never decoded
MAX_COSTS = 16 << 20                            # the sidecar itself
NUMBERS = ("fresh", "cache_read", "output", "turns")
TEXTS = ("key", "fork", "item", "agent", "role", "agent_type", "session", "ts")
FIELDS = frozenset(NUMBERS + TEXTS)
MAX_TEXT = 512


class CostError(ValueError):
    pass


def transcripts_root() -> Path:
    """Claude Code's projects directory: $CLAUDE_CONFIG_DIR/projects, else ~/.claude/projects."""
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    return Path(base) / "projects"


def slug_of(root: str) -> str:
    """The directory name Claude Code gives a session launched from `root`: every non-alphanumeric is '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(root))


def project_slugs(state: Path, registry: Path | None = None) -> list[str]:
    """Exactly this console's slugs: its registered roots' and server.json's, never matched by prefix."""
    s = os.path.realpath(state)
    reg = registry or R.location()
    projects = R.load(reg)
    roots = sorted(r for r, e in projects.items() if isinstance(e, dict) and e.get("state") == s)
    if not roots:
        raise CostError(f"no project is registered on the console at {s}; nothing to collect")
    try:   # server.json has one reader, shared with the console server (K3)
        listed = SF.slugs_for_state(s, reg.parent / SF.FILE)
    except SF.ServerFileError as e:
        raise CostError(str(e)) from None
    return sorted({slug_of(r) for r in roots} | set(listed))


def _keep(pairs):
    return {k: v for k, v in pairs if k in KEEP}


def _keep_meta(pairs):
    return {k: v for k, v in pairs if k in META_KEEP}


_DECODER = json.JSONDecoder(object_pairs_hook=_keep)
_META_DECODER = json.JSONDecoder(object_pairs_hook=_keep_meta)


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


def fork_of(description: str) -> str | None:
    """The fork id of a description's leading tag; only its first TAG_SPAN characters are looked at."""
    m = FORK_TAG.match(description[:TAG_SPAN])
    return m.group(1) if m else None


def _open_leaf(path: Path) -> int:
    """A file descriptor on a regular file that is not itself a symlink; OSError otherwise (never blocks)."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise OSError(errno.EINVAL, "not a regular file", str(path))
    return fd


def meta_of(path: Path) -> tuple[str | None, str | None]:
    """(agentType, fork id) of a subagent's .meta.json: nothing else of it is kept or returned."""
    try:
        fd = _open_leaf(path)
        with os.fdopen(fd, "rb") as fh:
            raw = fh.read(MAX_META + 1)
        if len(raw) > MAX_META:
            return None, None
        doc = _META_DECODER.decode(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None, None
    if not isinstance(doc, dict):
        return None, None
    kind, desc = doc.get("agentType"), doc.get("description")
    kind = kind if isinstance(kind, str) and AGENT_TYPE.match(kind) else None
    fork = fork_of(desc) if isinstance(desc, str) else None
    return kind, fork


def _lines(fh):
    """Yield (line, None) for each line within MAX_LINE, or (None, True) for one over it, skipped unread."""
    while True:
        chunk = fh.readline(MAX_LINE + 1)
        if not chunk:
            return
        if len(chunk) <= MAX_LINE or chunk.endswith(b"\n"):
            yield chunk, None
            continue
        while chunk and not chunk.endswith(b"\n"):   # drain the rest of the over-long line, a bounded read at a time
            chunk = fh.readline(MAX_LINE)
        yield None, True


def usage_of(fd: int) -> tuple[dict, int]:
    """(fresh / cache_read / output / turns, over-long lines skipped) of one open transcript, deduplicated by id."""
    best: dict[str, tuple[int, int, int]] = {}
    long_lines = 0
    with os.fdopen(fd, "rb") as fh:
        for raw, too_long in _lines(fh):
            if too_long:
                long_lines += 1
                continue
            rec = slim_line(raw.decode("utf-8", errors="replace"))
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
    return {"fresh": fresh, "cache_read": cache_read, "output": output, "turns": len(best)}, long_lines


def collect(state: Path, agent: str | None = None, registry: Path | None = None,
            projects_dir: Path | None = None) -> tuple[list[dict], dict]:
    """(one line per subagent transcript under this console's own slugs, {"long_lines": N, "skipped": N})."""
    base = Path(os.path.realpath(projects_dir or transcripts_root()))
    lines, stats = [], {"long_lines": 0, "skipped": 0}
    for slug in project_slugs(state, registry):
        d = base / slug
        if not d.is_dir() or os.path.realpath(d) != str(d):
            continue                                  # a symlinked slug dir leads elsewhere: not read
        for meta in sorted(d.glob("*/subagents/agent-*.meta.json")):
            sub = meta.parent
            if os.path.realpath(sub) != str(sub):    # a symlinked session or subagents dir: not read
                stats["skipped"] += 1
                continue
            transcript = sub / (meta.name[: -len(".meta.json")] + ".jsonl")
            try:
                fd = _open_leaf(transcript)
            except OSError:
                stats["skipped"] += 1                 # absent, a symlink, or not a regular file
                continue
            mtime = os.fstat(fd).st_mtime
            kind, fork = meta_of(meta)
            counts, long_lines = usage_of(fd)
            stats["long_lines"] += long_lines
            lines.append({
                "key": str(transcript.relative_to(base)),
                "fork": fork,
                "item": None,
                "agent": agent,
                "role": kind[len(SEAT_PREFIX):] if kind and kind.startswith(SEAT_PREFIX) else None,
                "agent_type": kind,
                "session": sub.parent.name,
                **counts,
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(mtime)),
            })
    return lines, stats


def _clean(rec: object) -> dict | None:
    """Only the sidecar's known fields, each of its own type; None for a line that is not a cost line."""
    if not isinstance(rec, dict) or not isinstance(rec.get("key"), str):
        return None
    out = {}
    for k in NUMBERS:
        v = rec.get(k, 0)
        if not isinstance(v, int) or isinstance(v, bool) or v < 0:
            return None
        out[k] = v
    for k in TEXTS:
        v = rec.get(k)
        if v is not None and not (isinstance(v, str) and len(v) <= MAX_TEXT):
            return None
        out[k] = v
    return out


def read_counted(state: Path) -> tuple[list[dict], int]:
    """(the sidecar's lines, how many malformed lines were dropped). A symlinked or oversized file is refused."""
    p = Path(state) / FILE
    try:
        raw = R.read_regular(p, MAX_COSTS, nofollow=True)
    except OSError as e:
        if e.errno == errno.ELOOP:
            raise CostError(f"{p} is a symlink; refused, nothing read") from None
        raise CostError(f"{p} cannot be read: {e}") from None
    except R.RegistryError as e:
        raise CostError(f"refused, nothing read: {e}") from None
    if raw is None:
        return [], 0
    out, bad = [], 0
    for line in raw.decode("utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            rec = _clean(json.loads(line))
        except ValueError:
            rec = None
        if rec is None:
            bad += 1
        else:
            out.append(rec)
    return out, bad


def read(state: Path) -> list[dict]:
    return read_counted(state)[0]


def write(state: Path, new: list[dict]) -> dict:
    """Merge `new` into STATE/costs.jsonl by key, under an exclusive lock; atomic, 0600, no temp file left."""
    st = Path(state)
    lock_fd = os.open(st / LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        old, bad = read_counted(st)
        merged = {r["key"]: r for r in old}
        before = len(merged)
        for r in new:
            merged[r["key"]] = _clean(r) or r
        fd, tmp = tempfile.mkstemp(dir=st, prefix=".costs.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                for k in sorted(merged):
                    fh.write(json.dumps(merged[k], sort_keys=True, ensure_ascii=False) + "\n")
            os.replace(tmp, st / FILE)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    finally:
        os.close(lock_fd)   # closing releases the flock
    return {"lines": len(merged), "added": len(merged) - before, "collected": len(new), "dropped_malformed": bad}


def _total(rows: list[dict]) -> dict:
    return {k: sum(r.get(k, 0) for r in rows) for k in NUMBERS}


def fork_card(records: list[dict], fork: str) -> dict:
    """A fork's bill: exactly the subagents tagged with it, and the untagged ones of the same sessions apart."""
    mine = [r for r in records if r.get("fork") == fork]
    sessions = {r.get("session") for r in mine}
    loose = [r for r in records if r.get("fork") is None and r.get("session") in sessions]
    return {"fork": fork, "seats": len(mine), **_total(mine),
            "subagents": [{k: r.get(k) for k in ("key", "role", *NUMBERS)} for r in mine],
            "unattributed": {"subagents": len(loose), **_total(loose),
                             "keys": [r["key"] for r in loose]}}

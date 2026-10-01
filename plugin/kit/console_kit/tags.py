"""Suggested next steps (0.8.0): rule-based tags beside each question and each round's answer set.

A tag is data, computed on the server from the store and the project's files,
never stored and never guessed on the page: `{"step": ..., "reason": ...}`.
The page shows each as a small chip whose accessible text is the reason. A tag
only SUGGESTS a step; it starts nothing.

**refine**: the question cites a spec (its `source` path, an evidence `cite`
or a `valid_if` path) under the project's `specs_dir`, its current answer is
locked, and that spec file has NOT been edited since the lock. "Edited" is:

- in a git work tree: the time of the last commit touching the file
  (`git log -1 --format=%ct -- FILE`), or the file's mtime when it has
  uncommitted changes or is untracked;
- outside git (or a repository with no commits): the file's mtime.

In the console server, git is never asked (CONSOLE-kit/Q23, `gitseam`): the
file's mtime is used, the reason says "file time ...; the last-commit time is
unavailable (no git in the server)", and `compute` returns `git` saying so.

A lock and an edit in the same second count as "not edited since". Every
git call runs with `--no-optional-locks` and `GIT_OPTIONAL_LOCKS=0` (0.8.1),
so `git status` never rewrites the index of the service checkout.

**drill**, either of:

1. the owner's own words on the current answer introduce a term nothing in
   the project names. Owner ruling, "Any backtick or Capital ★": a candidate
   term is exactly one of
   - a span in `backticks` (3 to 80 characters), or
   - a Capitalised word, or a run of them (each word a capital followed by
     lower-case letters, single-spaced, hyphenated parts allowed): "Goal",
     "Session Block". One word counts, not only runs.

   Removed before looking: every word in STOPLIST (common sentence-start and
   function words, a fixed constant below) at the start or end of a run, so
   "The Refine" is "Refine" and a lone "The", "We" or "If" at the start of a
   sentence is nothing. A candidate is **covered**, and not flagged, when,
   lower-cased with whitespace collapsed, it appears in the text of any file
   under `specs_dir`, in a spec's file name (with `-` and `_` read as
   spaces), or in any item's title from the adapter's `items()`. Matching is
   case-insensitive substring matching. At most MAX_TERMS terms per answer
   are looked at.
2. the question's item has status `proposed` and no spec's text or file name
   mentions the item's id.

Known limits of rule 1 (it suggests, never blocks). False positives: a
proper name (a person, a vendor, a product) and an ordinary word the owner
capitalised for emphasis, when no spec or title happens to contain it; a term
a spec spells differently ("column chain" vs "column-chain", a plural in the
answer and a singular nowhere); a common word missing from STOPLIST at the
start of a sentence ("Honestly, ..."). False negatives: a new idea written in
lower case without backticks is never seen; any candidate that appears
anywhere in any spec or title, even in an unrelated sense ("Goal" inside
"goalpost"), reads as covered; and a STOPLIST word ("Record", "Fix") is never
flagged even when the owner meant it as a name. With no `specs_dir`, or an
empty one, neither drill rule runs. **Changes if: the owner finds they ignore
the chip.**

**deliberate**: the question is stale (its lock's conditions no longer hold,
the same machinery as "Why stale?"), or the current answer picked options
and the question's ★ is not among them.

Cost, per page view: one directory walk of `specs_dir` (a stat per file); the
spec text is re-read only when a file's size or mtime changed, and a term is
looked up once per spec-set; one `git rev-parse` when any locked question
cites a spec, and one `git log` + `git status` per cited spec file whose
mtime, size or HEAD changed. Caps: MAX_SPEC_FILES files and MAX_SPEC_BYTES in
all; past either, the index stops there and says so in `notes`.
"""

from __future__ import annotations

import calendar
import os
import re
import threading
import time
from pathlib import Path
from typing import Mapping

from . import anchors as A
from . import gitseam as G
from . import schema as S
from . import view as V

STEPS = ("refine", "drill", "deliberate")
MAX_SPEC_FILES = 2000
MAX_SPEC_BYTES = 16 << 20
MAX_TERMS = 10
GIT_TIMEOUT = 10
# 0.8.1: every git call is READ-ONLY (`--no-optional-locks`, flag and env); both are
# set in `gitseam`, the one door every git call goes through (CONSOLE-kit/Q23).
# Pairs every backtick span (so a short `ab` cannot pair its closing tick with the next
# span's opening one); only a span of 3 to 80 characters is a term.
BACKTICK = re.compile(r"`([^`\n]{1,80})`")
_WORD = r"[A-Z][a-z]+(?:-[A-Za-z][a-z0-9]*)*"
CAPS = re.compile(rf"(?<![A-Za-z0-9`]){_WORD}(?: {_WORD})*(?![A-Za-z0-9`])")
# Common sentence-start and function words: never a term on their own, and trimmed
# from either end of a run of Capitalised words (drill rule 1).
STOPLIST = frozenset("""
The A An This That These Those It Its I We Us Our You Your He She They Them Their My Me
If And But Or Nor So Yet Also Then Than When While Where Which Who Whom Whose What Why How
In On At By For To Of From Into Onto Over Under With Without About After Before Between Through
During Since Until Unless Because Although Though However Otherwise Instead Even Still
Yes No Not Never Always Often Just Only Maybe Perhaps Please Now Here There Once Again
Is Are Was Were Be Been Being Am Do Does Did Done Have Has Had Can Could Should Would Will
Shall May Might Must Need Needs Let Lets Make Makes Keep Use Uses Go Get Put Take See Say
All Any Each Every Some Both Either Neither None Same Other Another Such Much Many More Most
First Next Last One Two Three Ok Okay Sure Fine Good Great Right Agreed Thanks
Fix Record Leave Pick Option Options Answer Question
""".split())
TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _norm(t: str) -> str:
    return " ".join(t.lower().split())


def terms(own_text: str) -> list[str]:
    """The candidate terms of drill rule 1: backticked spans, then Capitalised words and runs, at most MAX_TERMS."""
    found: list[str] = []
    for m in BACKTICK.finditer(own_text or ""):
        t = m.group(1).strip()
        if len(t) >= 3:
            found.append(t)
    rest = BACKTICK.sub(lambda m: " " * len(m.group(0)), own_text or "")  # a backticked span is one term
    for m in CAPS.finditer(rest):
        words = m.group(0).split(" ")
        while words and words[0] in STOPLIST:
            words = words[1:]
        while words and words[-1] in STOPLIST:
            words = words[:-1]
        if words:
            found.append(" ".join(words))
    out, seen = [], set()
    for t in found:
        k = _norm(t)
        if k not in seen:
            seen.add(k)
            out.append(t)
    return out[:MAX_TERMS]


class SpecIndex:
    """The spec files under `specs_dir`: names, lower-cased text, and a cache of terms looked up."""

    def __init__(self, root: Path, specs_dir: str) -> None:
        self.root = Path(root).resolve()
        self.specs_dir = specs_dir
        self.sig: tuple = ()
        self.files: dict[str, tuple[int, int]] = {}   # rel -> (mtime_ns, size)
        self.blob = ""
        self.names: list[str] = []
        self.notes: list[str] = []
        self._found: dict[str, bool] = {}

    def _walk(self) -> tuple[dict[str, tuple[int, int]], list[str]]:
        top = self.root / self.specs_dir
        out: dict[str, tuple[int, int]] = {}
        notes: list[str] = []
        if not top.is_dir() or top.is_symlink():
            return out, notes
        total = 0
        for dirpath, dirnames, filenames in os.walk(top, followlinks=False):
            dirnames.sort()
            for name in sorted(filenames):
                p = Path(dirpath) / name
                rel = p.relative_to(self.root).as_posix()
                if S.secret_path(rel):
                    continue
                try:
                    st = p.lstat()
                except OSError:
                    continue
                if not p.is_file() or p.is_symlink() or st.st_size > A.MAX_READ:
                    continue
                if len(out) >= MAX_SPEC_FILES or total + st.st_size > MAX_SPEC_BYTES:
                    notes.append(f"the spec index stopped at {len(out)} files ({total} bytes): "
                                 f"the limits are {MAX_SPEC_FILES} files and {MAX_SPEC_BYTES} bytes")
                    return out, notes
                total += st.st_size
                out[rel] = (st.st_mtime_ns, st.st_size)
        return out, notes

    def refresh(self) -> None:
        files, notes = self._walk()
        sig = tuple(sorted(files.items()))
        if sig == self.sig:
            return
        texts, names = [], []
        for rel in files:
            try:
                texts.append(_norm((self.root / rel).read_bytes().decode("utf-8", errors="replace")))
            except OSError:
                continue
            stem = rel.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower()
            names += [stem, _norm(stem.replace("-", " ").replace("_", " "))]
        self.sig, self.files, self.notes = sig, files, notes
        self.blob, self.names, self._found = "\n".join(texts), names, {}

    def mentions(self, term: str) -> bool:
        k = _norm(term)
        if k not in self._found:
            dashed = k.replace(" ", "-")
            self._found[k] = (k in self.blob or any(k in n or dashed in n for n in self.names))
        return self._found[k]

    def has(self, rel: str) -> bool:
        return rel in self.files


class _Git:
    """When each spec file was last edited, cached by (file, mtime, size, HEAD)."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        self._cache: dict[tuple, float] = {}

    def _run(self, args: list[str]) -> str | None:
        # Through gitseam (Q23): in the server (seam closed) no git is started, and edits read as file time.
        out = G.run(args, self.root, timeout=GIT_TIMEOUT, text=True)
        return None if isinstance(out, G.Unavailable) else out

    def head(self) -> str | None:
        out = self._run(["rev-parse", "--verify", "-q", "HEAD"])
        return out.strip() if out else None

    def edited_at(self, rel: str, stat: tuple[int, int], head: str | None) -> tuple[float, str]:
        """(epoch seconds, basis): basis is 'git' (last commit) or 'mtime' (dirty, untracked or no git)."""
        key = (rel, stat, head)
        if key in self._cache:
            return self._cache[key]
        mtime = stat[0] / 1e9
        got = (mtime, "mtime")
        if head is not None:
            dirty = self._run(["status", "--porcelain", "--", rel])
            ct = self._run(["log", "-1", "--format=%ct", "--", rel])
            if dirty is not None and not dirty.strip() and ct and ct.strip().isdigit():
                got = (float(ct.strip()), "git")
        self._cache[key] = got
        return got


_LOCK = threading.Lock()
_INDEXES: dict[tuple[str, str], SpecIndex] = {}
_GITS: dict[str, _Git] = {}


def _index(root: Path, specs_dir: str) -> SpecIndex:
    key = (str(Path(root).resolve()), specs_dir)
    with _LOCK:
        idx = _INDEXES.get(key)
        if idx is None:
            idx = _INDEXES[key] = SpecIndex(root, specs_dir)
        idx.refresh()
        return idx


def _git(root: Path) -> _Git:
    key = str(Path(root).resolve())
    with _LOCK:
        return _GITS.setdefault(key, _Git(root))


def _epoch(ts: str) -> float | None:
    try:
        return float(calendar.timegm(time.strptime(ts, TS_FORMAT)))
    except (TypeError, ValueError):
        return None


def cited_paths(q: dict) -> list[str]:
    """Every file path a question cites: its source, its evidence rows, its valid_if paths; in that order."""
    out = [q["source"].split(":", 1)[0]]
    for row in q.get("evidence") or ():
        parts = S.cite_parts(row.get("cite")) if isinstance(row, dict) else None
        if parts:
            out.append(parts[0])
    out += [c["path"] for c in q["valid_if"] if isinstance(c, dict) and isinstance(c.get("path"), str)]
    seen: list[str] = []
    for p in out:
        p = p.removeprefix("./")
        if p not in seen:
            seen.append(p)
    return seen


def _under(path: str, d: str) -> bool:
    return path == d or path.startswith(d + "/")


def compute(store, view: dict, items: Mapping[str, dict], root: Path, specs_dir: str | None) -> dict:
    """Tags for every question and every round in `view` (see the module doc)."""
    idx = _index(root, specs_dir) if specs_dir else None
    git = _git(root)
    head: list = []           # HEAD, read at most once per call, and only when a spec is cited by a lock
    basis: set[str] = set()
    # Item titles, lower-cased: a name the register already uses is not new (drill rule 1).
    titles = "\n".join(_norm(str(d.get("title") or "")) for d in items.values() if isinstance(d, Mapping))
    out_q: dict[str, list[dict]] = {}
    for qid, q in view["questions"].items():
        rec = q["question"]
        tags: list[dict] = []
        answers = q["answers"]
        cur = answers[-1] if answers else None
        # refine: a locked answer that cites a spec nobody has edited since
        if idx is not None and cur is not None and cur.get("locked"):
            lk = store.lock_of(cur["id"])
            locked_at = _epoch(lk["ts"]) if lk else None
            for path in cited_paths(rec):
                if locked_at is None or not _under(path, specs_dir) or not idx.has(path):
                    continue
                if not head:
                    head.append(git.head())
                edited, how = git.edited_at(path, idx.files[path], head[0])
                basis.add(how)
                if edited <= locked_at:
                    when = "last commit" if how == "git" else "file time"
                    shown = f"{when} {time.strftime(TS_FORMAT, time.gmtime(edited))}"
                    if not G.is_open():  # Q23: say which time is shown, and why not the commit's
                        shown += f"; the last-commit time is {G.UNAVAILABLE}"
                    tags.append({"step": "refine", "reason": f"cites {path}, not edited since you locked this "
                                                             f"({shown}, lock {lk['ts']})"})
                    break
        # drill: new words with no spec, or a proposed item no spec names
        if idx is not None and idx.files:
            new = [t for t in terms(cur["own_text"])] if cur is not None else []
            new = [t for t in new if not idx.mentions(t) and _norm(t) not in titles]
            if new:
                shown = ", ".join(f"`{t}`" if " " not in t else f"\"{t}\"" for t in new[:3])
                more = f" and {len(new) - 3} more" if len(new) > 3 else ""
                tags.append({"step": "drill", "reason": f"your words name {shown}{more}, which no spec under "
                                                        f"{specs_dir}/ and no item title mentions"})
            else:
                it = items.get(rec["item"]) or {}
                if str(it.get("status") or "").lower() == "proposed" and not idx.mentions(rec["item"]):
                    tags.append({"step": "drill", "reason": f"item {rec['item']} is proposed, and no spec under "
                                                            f"{specs_dir}/ names it"})
        # deliberate: stale, or a pick against the ★
        if q["state"] == "stale":
            first = V.condition_words(q["failing"][0]) if q["failing"] else "a condition no longer holds"
            more = f" (and {len(q['failing']) - 1} more)" if len(q["failing"]) > 1 else ""
            tags.append({"step": "deliberate", "reason": f"stale: this no longer holds: {first}{more}"})
        elif cur is not None and rec["star"] is not None and cur["picks"] and rec["star"] not in cur["picks"]:
            labels = {o["id"]: o["label"] for o in rec["options"]}
            tags.append({"step": "deliberate", "reason": f"your pick went against the ★ "
                                                         f"({labels.get(rec['star'], rec['star'])})"})
        if tags:
            out_q[qid] = tags
    out_f: dict[str, list[dict]] = {}
    for fid, f in view["forks"].items():
        tags = []
        for step in STEPS:
            hit = [(qid, t) for qid in f["questions"] for t in out_q.get(qid, []) if t["step"] == step]
            if hit:
                qids = ", ".join(qid for qid, _ in hit)
                tags.append({"step": step, "reason": f"{qids}: {hit[0][1]['reason']}", "qids": [q for q, _ in hit]})
        if tags:
            out_f[fid] = tags
    out = {"questions": out_q, "forks": out_f, "specs_dir": specs_dir,
           "basis": sorted(basis), "notes": list(idx.notes) if idx is not None else []}
    if not G.is_open():
        out["git"] = G.UNAVAILABLE   # Q23: named even when no tag needed git this time
    return out

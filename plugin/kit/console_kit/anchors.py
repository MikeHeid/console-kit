"""What decides whether a locked answer still holds, and why it does not (0.5.0).

A question carries `valid_if` conditions. Until 0.5.0 nothing re-anchored them,
so a whole-file `file_sha256` of a big living file went stale after almost any
unrelated change, and the owner had no way to clear it. Three things change:

- **An `excerpt` condition** holds while its cited text is found anywhere in
  the file. Moving the text keeps it; changing or deleting it does not.
  Whitespace and line endings are normalised before matching, so re-wrapping a
  paragraph is not a change.
- **Anchors on the lock.** The conditions that decide a locked answer are, in
  order: the latest `anchor` record written for that lock (by `reanchor`), the
  lock's own `anchors`, and otherwise the question's `valid_if`. A lock with
  neither evaluates exactly as it did in 0.4.0.
- **Re-locking re-anchors.** A lock on a question that was locked before gets
  fresh anchors from the tree as it is now (`fresh_anchors`), computed by the
  server, never taken from the page.

`explain` says, in plain words, why each failing condition fails, and
`plan_reanchor` works out which stale locks can be re-anchored with evidence
from git history, and which cannot.
"""

from __future__ import annotations

import difflib
import hashlib
import re
import subprocess
from pathlib import Path
from typing import Callable, Mapping

from . import schema as S

HISTORY_COMMITS = 300   # how far back `git log` looks for the version a lock was taken on
GIT_TIMEOUT = 20        # seconds for one git call
DIFF_LINES = 40         # a "why stale" diff is cut to this many lines...
DIFF_CHARS = 4000       # ...and this many characters
SIMILAR = 0.5           # below this, a region is "not similar" and no diff is shown
SOURCE_RANGE = re.compile(r"^(?P<path>[^:]+):(?P<a>[0-9]+)(?:-(?P<b>[0-9]+))?$")


def normalise(text: str) -> str:
    """Collapse every run of whitespace (line endings included) to one space, and trim."""
    return " ".join(text.split())


class _File:
    __slots__ = ("status", "text", "sha")

    def __init__(self, status: str, data: bytes | None) -> None:
        self.status = status
        self.text = None if data is None else data.decode("utf-8", errors="replace")
        self.sha = None if data is None else hashlib.sha256(data).hexdigest()


class Tree:
    """The project as it is now.

    A snapshot Tree (the default) reads each file once for its lifetime, so one
    request that checks many conditions on one big file reads and hashes it
    once; build one per request. A live Tree (`snapshot=False`) re-reads the
    file on every check, as the 0.4.0 evaluator did, and keeps only the
    normalised text, keyed by the file's hash.
    """

    def __init__(self, root: Path, item_status: Mapping[str, str | None], *, snapshot: bool = True) -> None:
        self.root = Path(root).resolve()
        self.item_status = item_status
        self.snapshot = snapshot
        self._files: dict[str, _File] = {}
        self._norms: dict[str, str] = {}

    def _file(self, rel: str) -> _File:
        p = (self.root / rel).resolve()
        if self.root not in p.parents:
            return _File("outside", None)
        if not p.is_file():
            return _File("missing", None)
        f = self._files.get(rel) if self.snapshot else None
        if f is None:
            try:
                f = _File("ok", p.read_bytes())
            except OSError:
                return _File("missing", None)
            if self.snapshot:
                self._files[rel] = f
        return f

    def text(self, rel: str) -> tuple[str, str | None]:
        """(status, text): status is 'ok', 'missing' or 'outside'."""
        f = self._file(rel)
        return f.status, f.text

    def sha(self, rel: str) -> str | None:
        return self._file(rel).sha

    def norm(self, rel: str) -> str | None:
        f = self._file(rel)
        if f.status != "ok":
            return None
        if f.sha not in self._norms:
            self._norms[f.sha] = normalise(f.text)
        return self._norms[f.sha]

    def holds(self, c: dict) -> bool:
        if c["kind"] == "item_status":
            return self.item_status.get(c["item"]) == c["status"]
        if c["kind"] == "file_sha256":
            return self.sha(c["path"]) == c["sha256"]
        n = self.norm(c["path"])
        return n is not None and normalise(c["text"]) in n


def conditions_for(store, q: dict) -> tuple[list[dict], str]:
    """The conditions that decide `q`'s current lock, and where they came from.

    'reanchor' (an `anchor` record), 'lock' (the lock's own anchors) or
    'question' (its `valid_if`, as in 0.4.0). An unlocked question is decided by
    its `valid_if`.
    """
    head = store.head(q["qid"])
    lk = store.lock_of(head["id"]) if head is not None else None
    if lk is None:
        return q["valid_if"], "question"
    conds, origin, _ = lock_conditions(store, q, lk)
    return conds, origin


def lock_conditions(store, q: dict, lk: dict) -> tuple[list[dict], str, dict | None]:
    """The conditions that decide one lock, where they came from, and the `anchor` record if one decides.

    The same order everywhere, the view and the export alike: the lock's latest
    `anchor` record, then the lock's own `anchors`, then the question's `valid_if`.
    """
    a = store.anchor_of(lk["id"])
    if a is not None:
        return a["anchors"], "reanchor", a
    if "anchors" in lk:
        return lk["anchors"], "lock", None
    return q["valid_if"], "question", None


def fresh_anchors(base: list[dict], tree: Tree) -> list[dict]:
    """The anchors a re-lock takes: each condition re-read against the tree as it is now.

    An `excerpt` that holds stays as it is. An `excerpt` whose text is gone
    cannot be re-derived from anything the owner saw, so it falls back to the
    whole file's hash as it is now: strict, but it pins what the owner re-locked
    against. A `file_sha256` is re-hashed. An `item_status` is kept as it is.
    A condition on a file that is gone stays as it is, and keeps the answer stale.
    """
    out = []
    for c in base:
        if c["kind"] == "item_status" or tree.holds(c):
            out.append(c)
            continue
        sha = tree.sha(c["path"])
        out.append(c if sha is None else {"kind": "file_sha256", "path": c["path"], "sha256": sha})
    return out


# -- git history ---------------------------------------------------------------

def _safe_path(rel: str) -> bool:
    return (isinstance(rel, str) and rel and not rel.startswith(("/", "-"))
            and ".." not in rel.split("/") and not any(ord(ch) < 32 for ch in rel))


class History:
    """Past versions of files from the project's git history, looked up by sha256.

    One `git log` and one `git cat-file --batch` per path, cached, so a whole
    check costs a few git calls however many answers cite the same file.
    """

    def __init__(self, root: Path, max_commits: int = HISTORY_COMMITS) -> None:
        self.root = Path(root).resolve()
        self.max_commits = max_commits
        self._versions: dict[str, list[tuple[str, bytes]] | None] = {}

    def _git(self, args: list[str], data: bytes | None = None) -> bytes | None:
        try:
            r = subprocess.run(["git", *args], cwd=self.root, input=data, capture_output=True,
                               timeout=GIT_TIMEOUT, check=False)
        except (OSError, subprocess.SubprocessError):
            return None
        return r.stdout if r.returncode == 0 else None

    def versions(self, rel: str) -> list[tuple[str, bytes]] | None:
        """(commit, content) for each commit that touched `rel`, newest first; None without git."""
        if rel in self._versions:
            return self._versions[rel]
        out = None
        if _safe_path(rel):
            log = self._git(["log", f"-n{self.max_commits}", "--format=%H", "--", rel])
            if log is not None:
                commits = log.decode().split()
                blobs = self._git(["cat-file", "--batch"],
                                  "".join(f"{c}:./{rel}\n" for c in commits).encode()) if commits else b""
                out = _parse_batch(commits, blobs or b"")
        self._versions[rel] = out
        return out

    def find(self, rel: str, sha256: str) -> tuple[str, str] | None:
        """(commit, text) of the newest version of `rel` whose sha256 is `sha256`, or None."""
        for commit, blob in self.versions(rel) or []:
            if hashlib.sha256(blob).hexdigest() == sha256:
                return commit, blob.decode("utf-8", errors="replace")
        return None


def _parse_batch(commits: list[str], raw: bytes) -> list[tuple[str, bytes]]:
    out, pos = [], 0
    for c in commits:
        nl = raw.find(b"\n", pos)
        if nl < 0:
            break
        head = raw[pos:nl].split()
        pos = nl + 1
        if len(head) == 3 and head[1] == b"blob":
            size = int(head[2])
            out.append((c, raw[pos:pos + size]))
            pos += size + 1  # the content, then its newline
    return out


def cited_range(source: str, path: str) -> tuple[int, int] | None:
    """The 1-based inclusive line range `source` cites in `path`, or None when it cites none there."""
    m = SOURCE_RANGE.match(source or "")
    if not m or m.group("path") != path:
        return None
    a = int(m.group("a"))
    b = int(m.group("b") or a)
    return (a, b) if 1 <= a <= b else None


def _lines(text: str, rng: tuple[int, int]) -> str | None:
    lines = text.replace("\r\n", "\n").split("\n")
    a, b = rng
    if b > len(lines):
        return None
    return "\n".join(lines[a - 1:b])


# -- why stale -------------------------------------------------------------------

def _cap(lines: list[str]) -> str:
    out = "\n".join(lines[:DIFF_LINES])
    if len(lines) > DIFF_LINES:
        out += f"\n… {len(lines) - DIFF_LINES} more line(s) not shown"
    return out[:DIFF_CHARS]


def nearest(cited: str, current: str) -> dict | None:
    """The region of `current` most like `cited`, and a small diff from one to the other.

    Cheap on purpose: the most distinctive (longest) cited line is matched
    against every line of the file, and the region is laid around that match.
    """
    want = [ln for ln in cited.replace("\r\n", "\n").split("\n")]
    keys = sorted((ln.strip() for ln in want if ln.strip()), key=len, reverse=True)
    if not keys:
        return None
    key = keys[0]
    have = current.replace("\r\n", "\n").split("\n")
    best, best_i = 0.0, -1
    sm = difflib.SequenceMatcher(autojunk=False)
    sm.set_seq2(key)
    for i, ln in enumerate(have):
        sm.set_seq1(ln.strip())
        if sm.real_quick_ratio() <= best or sm.quick_ratio() <= best:
            continue
        r = sm.ratio()
        if r > best:
            best, best_i = r, i
    if best < SIMILAR:
        return None
    offset = next(n for n, ln in enumerate(want) if ln.strip() == key)
    start = max(0, best_i - offset)
    region = have[start:start + len(want)]
    raw = list(difflib.unified_diff(want, region, "cited", "now", lineterm="", n=2))[2:]  # no file headers
    diff = [ln for ln in raw if not ln.startswith("@@")]  # nor hunk headers: they mean nothing to a reader
    return {"line": start + 1, "diff": _cap(diff)}


def explain(c: dict, tree: Tree, history: History, source: str) -> dict:
    """Why condition `c` holds or not, in plain words, with a small diff where it is cheap."""
    out = {"condition": c, "holds": tree.holds(c)}
    if out["holds"]:
        return {**out, "reason": "holds", "words": "This still holds."}
    if c["kind"] == "item_status":
        now = tree.item_status.get(c["item"])
        words = (f"Item {c['item']} is no longer in the register; the answer assumed it was {c['status']}."
                 if c["item"] not in tree.item_status else
                 f"Item {c['item']} was expected to be {c['status']}; it is now {now}.")
        return {**out, "reason": "status_changed", "expected": c["status"], "actual": now, "words": words}
    status, text = tree.text(c["path"])
    if status == "outside":
        return {**out, "reason": "outside_project", "words": f"{c['path']} resolves outside the project."}
    if status == "missing":
        return {**out, "reason": "file_missing",
                "words": f"{c['path']} is gone: it was moved, renamed or deleted."}
    if c["kind"] == "excerpt":
        near = nearest(c["text"], text)
        words = f"The text this answer cites is no longer in {c['path']}: it was changed or deleted."
        if near:
            words += f" The most similar text now starts at line {near['line']}."
            return {**out, "reason": "text_changed", "words": words, **near}
        return {**out, "reason": "text_changed", "words": words + " Nothing similar is left in the file."}
    return {**out, "reason": "file_changed", **_file_changed(c, text, history, source)}


def _file_changed(c: dict, text: str, history: History, source: str) -> dict:
    words = f"{c['path']} changed since this answer was locked."
    found = history.find(c["path"], c["sha256"])
    if found is None:
        return {"words": words + " The version it was locked against is not in the recent git history, "
                                 "so what changed cannot be shown."}
    commit, old = found
    out = {"locked_version": commit[:12]}
    rng = cited_range(source, c["path"])
    cited = _lines(old, rng) if rng else None
    if cited is None or not normalise(cited):
        return {**out, "words": words + f" The question cites no line range in this file, so the whole file "
                                        f"counts (it was locked against commit {commit[:12]})."}
    if normalise(cited) in normalise(text):
        return {**out, "cited_text": "unchanged",
                "words": words + f" The lines the question cites ({rng[0]}-{rng[1]} when it was locked) are "
                                 f"unchanged; only other parts of the file changed. Re-locking, or "
                                 f"`agent.py reanchor`, clears this."}
    near = nearest(cited, text) or {}
    return {**out, "cited_text": "changed", **near,
            "words": words + f" The lines the question cites ({rng[0]}-{rng[1]} when it was locked) changed."}


def check(store, root: Path, item_status: Mapping[str, str | None],
          history: History | None = None) -> dict:
    """Every stale answer, and for each of its conditions whether it holds and why not."""
    tree = Tree(root, item_status)
    history = history or History(root)
    out = {}
    for q in (r for r in store.records() if r["type"] == "question"):
        conds, origin = conditions_for(store, q)
        head = store.head(q["qid"])
        if head is None or store.lock_of(head["id"]) is None or all(tree.holds(c) for c in conds):
            continue
        out[q["qid"]] = {"anchored_by": origin,
                         "conditions": [explain(c, tree, history, q["source"]) for c in conds]}
    return out


# -- one-time re-anchor --------------------------------------------------------------

def plan_reanchor(store, root: Path, item_status: Mapping[str, str | None],
                  history: History | None = None) -> list[dict]:
    """For every stale lock, which conditions can be re-anchored with evidence, and which cannot.

    Only a failing `file_sha256` is ever replaced, and only when all of this is
    true: the question's `source` names a line range in that same file; git
    history holds the version whose sha256 the condition names (the version the
    answer was locked against); and the lines the question cited in that
    version are still in the file now. The replacement is an `excerpt` of those
    lines. Anything else stays as it is, and is reported with the reason.
    """
    tree = Tree(root, item_status)
    history = history or History(root)
    plan = []
    for q in (r for r in store.records() if r["type"] == "question"):
        head = store.head(q["qid"])
        lk = store.lock_of(head["id"]) if head is not None else None
        if lk is None:
            continue
        conds, origin = conditions_for(store, q)
        if all(tree.holds(c) for c in conds):
            continue
        new, changes, kept = [], [], []
        for c in conds:
            if tree.holds(c):
                new.append(c)
                continue
            repl, why = _reanchor_one(c, tree, history, q["source"])
            new.append(repl or c)
            (changes if repl else kept).append({"from": c, **({"to": repl} if repl else {}), "why": why})
        plan.append({"qid": q["qid"], "lock": lk["id"], "anchored_by": origin, "base": conds, "anchors": new,
                     "changes": changes, "unresolved": kept,
                     "fresh": all(tree.holds(c) for c in new)})
    return plan


def still_supported(entry: dict, tree: Tree) -> str | None:
    """None when every replacement in a planned re-anchor still holds, exactly once, in `tree`; else why not."""
    for ch in entry["changes"]:
        to = ch["to"]
        n = tree.norm(to["path"])
        seen = 0 if n is None else n.count(normalise(to["text"]))
        if seen != 1:
            return (f"{to['path']} changed while this ran: the cited text is now there {seen} time(s); "
                    f"nothing was written, run it again")
    return None


def _reanchor_one(c: dict, tree: Tree, history: History, source: str) -> tuple[dict | None, str]:
    if c["kind"] == "item_status":
        return None, f"item {c['item']} is not {c['status']}; a status change is real, so it stays stale"
    if c["kind"] == "excerpt":
        return None, f"the cited text is no longer in {c['path']}"
    if tree.text(c["path"])[0] != "ok":
        return None, f"{c['path']} is gone"
    rng = cited_range(source, c["path"])
    if rng is None:
        return None, f"the question's source ({source}) names no line range in {c['path']}"
    found = history.find(c["path"], c["sha256"])
    if found is None:
        return None, (f"the version of {c['path']} it was locked against is not in the last "
                      f"{history.max_commits} commits that touched it")
    commit, old = found
    cited = _lines(old, rng)
    if cited is None or not normalise(cited):
        return None, f"lines {rng[0]}-{rng[1]} are not in {c['path']} at {commit[:12]}"
    if len(normalise(cited)) < S.MIN_EXCERPT:
        return None, f"lines {rng[0]}-{rng[1]} are too short to anchor on ({len(normalise(cited))} characters)"
    if len(cited) > S.MAX_EXCERPT:
        return None, (f"lines {rng[0]}-{rng[1]} are {len(cited)} characters, over the "
                      f"{S.MAX_EXCERPT} an excerpt may hold")
    seen = tree.norm(c["path"]).count(normalise(cited))
    if seen == 0:
        return None, f"lines {rng[0]}-{rng[1]} as locked (commit {commit[:12]}) changed; the answer is really stale"
    if seen > 1:
        # An excerpt found in several places would hold while the one the question
        # meant was changed or deleted: too weak to justify calling the answer fresh.
        return None, (f"lines {rng[0]}-{rng[1]} as locked appear {seen} times in {c['path']} now, "
                      f"so they cannot pin the one the question meant")
    return ({"kind": "excerpt", "path": c["path"], "text": cited},
            f"lines {rng[0]}-{rng[1]} as locked (commit {commit[:12]}) are unchanged in the file now")


def evaluator(root: Path, item_status: Mapping[str, str | None], *,
              snapshot: bool = False) -> Callable[[dict], bool]:
    """A `holds(condition)` function; live by default, or over one reading of the tree (see `Tree`)."""
    return Tree(root, item_status, snapshot=snapshot).holds

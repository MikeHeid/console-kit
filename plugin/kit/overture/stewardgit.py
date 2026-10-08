"""What git would have told the server, computed by the steward and pushed as data (CONSOLE-kit/Q23, part 2).

The console server starts no git (`gitseam`, 0.8.10): a project's repository
config can run a program, so git runs only in the steward's own process, inside
its jail. `agent.py history-push` runs it there and sends the server two kinds
of DATA, both untrusted, neither ever executed nor used to choose a path:

- **Blobs**: the content of the version of a file a lock was taken against.
  Content-addressed: a blob is stored only when its sha256 is exactly the hash
  a `file_sha256` condition of a CURRENT lock names, and the server re-hashes
  every byte, on receipt and again on every read. So a blob proves itself: the
  steward can withhold one (the panel says history is unavailable) but cannot
  make the server claim that cited lines are unchanged. From the blob, the
  server computes the cited lines, the diff and any re-anchor ITSELF, against
  its own current tree, exactly as it did when it ran git. The commit id is the
  one field it cannot check: it is kept only in a 40/64-hex shape, and shown
  "(from the steward)".

  Only `file_sha256` conditions are restorable this way: they are the only kind
  that names a whole-file hash, and the only kind whose explanation needs a past
  version. An `excerpt` is explained from the current file alone, and an
  `item_status` from the register; neither ever needed git.

- **Spec times**: when each cited spec file was last committed, for the refine
  tag. Keyed by what the server can observe itself, (path, mtime_ns, size), and
  used only while that key still matches the file AND the path is one the
  server itself asked for. HEAD is recorded as provenance; the server cannot
  read it (a worktree's `.git` points outside the root), so a commit that
  changes no file's stat is not seen until the next push.

Storage, per project under STATE/steward-git/: `blobs/<sha256 hex>` (written
to an O_EXCL temporary and renamed over, opened with O_NOFOLLOW, never named by
anything the client sent; every operation relative to a held folder descriptor,
`_Folders`) and `index.json` (commit ids and spec times). Every push prunes
the blobs no current lock names.
"""

from __future__ import annotations

import base64
import binascii
import errno
import hashlib
import json
import os
import re
import time
from pathlib import Path

from . import anchors as A
from . import atfile as AF
from . import gitseam as G

DIR = "steward-git"
BLOBS = "blobs"
INDEX = "index.json"
SHA256 = re.compile(r"^[0-9a-f]{64}\Z")
COMMIT = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
MAX_BLOB = A.MAX_READ                      # a larger file is never read by the server either
MAX_BLOB_BODY = MAX_BLOB * 4 // 3 + 4096   # its base64, plus the JSON around it
MAX_SPECS = 2000                           # tags.MAX_SPEC_FILES
MAX_STORED = 64 << 20                      # all of one project's blobs together
FROM = "from the steward"


class PushError(ValueError):
    pass


def named_shas(store) -> dict[str, str]:
    """sha256 -> path, for every `file_sha256` condition of every CURRENT lock: the only blobs ever kept."""
    out: dict[str, str] = {}
    for q in (r for r in store.records() if r["type"] == "question"):
        head = store.head(q["qid"])
        if head is None or store.lock_of(head["id"]) is None:
            continue
        for c in A.conditions_for(store, q)[0]:
            if _file_condition(c):
                out.setdefault(c["sha256"], c["path"])
    return out


def _file_condition(c: object) -> bool:
    """A `file_sha256` condition in the shape the code below indexes; any other is skipped, never raised on."""
    return (isinstance(c, dict) and c.get("kind") == "file_sha256" and isinstance(c.get("sha256"), str)
            and isinstance(c.get("path"), str))


def wants(store, root: Path, item_status, spec_stats: dict[str, tuple[int, int]]) -> dict:
    """What the steward should compute: the blobs a FAILING lock condition needs, and the cited spec files.

    Every path here comes from the server's own store and its own walk of the
    tree; the steward reads git for them and nothing else.
    """
    tree = A.Tree(root, item_status)
    blobs = []
    seen = set()
    for q in (r for r in store.records() if r["type"] == "question"):
        head = store.head(q["qid"])
        if head is None or store.lock_of(head["id"]) is None:
            continue
        for c in A.conditions_for(store, q)[0]:
            if _file_condition(c) and c["sha256"] not in seen and not tree.holds(c):
                seen.add(c["sha256"])
                blobs.append({"path": c["path"], "sha256": c["sha256"]})
    specs = [{"path": p, "mtime_ns": s[0], "size": s[1]} for p, s in sorted(spec_stats.items())]
    return {"blobs": blobs, "specs": specs[:MAX_SPECS]}


def collect(root: Path, want: dict) -> tuple[list[dict], list[dict]]:
    """THE STEWARD'S SIDE, run in its own process where git is allowed: the blobs and spec times `want` names.

    Raw bytes, never decoded text, so each blob hashes to exactly the sha256 the
    lock names. A version not in the recent history, and a spec whose stat here
    differs from what the server saw, are left out: the server then shows its label.
    """
    from . import tags as T   # here, not at the top: the server never runs this half
    root = Path(root)
    hist = A.History(root)
    blobs = []
    for b in want.get("blobs") or []:
        sha = b.get("sha256") if isinstance(b, dict) else None
        if not (isinstance(sha, str) and SHA256.match(sha)):
            continue
        for commit, data in hist.versions(str(b.get("path"))) or []:
            if hashlib.sha256(data).hexdigest() == sha and len(data) <= MAX_BLOB:
                blobs.append({"sha256": sha, "commit": commit,
                              "content_b64": base64.b64encode(data).decode("ascii")})
                break
    git = T._Git(root)
    head = git.head()
    specs = []
    for s in want.get("specs") or []:
        if not isinstance(s, dict) or not A._safe_path(s.get("path")):
            continue
        try:
            st = os.stat(root / s["path"], follow_symlinks=False)
        except OSError:
            continue
        now = (st.st_mtime_ns, st.st_size)
        if now != (s.get("mtime_ns"), s.get("size")):
            continue   # the file differs from what the server saw: its time would not match there
        edited, how = git.edited_at(s["path"], now, head)
        if how == "git":
            specs.append({"path": s["path"], "mtime_ns": now[0], "size": now[1], "head": head,
                          "edited": int(edited)})
    return blobs, specs


# -- the folders: every operation relative to a held descriptor ---------------------------------

DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class _Folders:
    """STATE/steward-git and its blobs/, each held open as a descriptor.

    Every open, rename, stat, list and unlink below names ONE component relative
    to one of these descriptors, never a path: a symlink planted where either
    folder goes is refused (O_NOFOLLOW on the folder itself), and one swapped in
    after the open changes nothing, because nothing is looked up by path again.
    `create` makes the folders (0700) for a write; a reader passes False and
    gets None when they are not there.
    """

    def __init__(self, top: int, blobs: int) -> None:
        self.top, self.blobs = top, blobs

    @classmethod
    def open(cls, state: Path, create: bool) -> "_Folders | None":
        try:
            st = os.open(state, DIR_FLAGS & ~os.O_NOFOLLOW)   # the configured state folder: the server's own
        except OSError:
            if create:
                raise PushError("the state folder cannot be opened; nothing was stored") from None
            return None
        fds: list[int] = []
        try:
            parent = st
            for name in (DIR, BLOBS):
                if create:
                    try:
                        os.mkdir(name, 0o700, dir_fd=parent)
                    except FileExistsError:
                        pass
                try:
                    fd = os.open(name, DIR_FLAGS, dir_fd=parent)
                except OSError as e:
                    if e.errno not in (errno.ENOENT, errno.ELOOP, errno.ENOTDIR):
                        raise
                    if create:   # a symlink or a file planted where the folder goes
                        raise PushError(f"{name} in the state folder is not a plain folder; nothing was stored") \
                            from None
                    return None
                fds.append(fd)
                parent = fd
            return cls(fds[0], fds[1])
        finally:
            os.close(st)
            if len(fds) < 2:
                for fd in fds:
                    os.close(fd)

    def close(self) -> None:
        os.close(self.blobs)
        os.close(self.top)

    def __enter__(self) -> "_Folders":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def names(self) -> list[str]:
        return sorted(os.listdir(self.blobs))   # on a descriptor: fdopendir of a dup, no lookup by path

    def size(self, name: str) -> int:
        return os.stat(name, dir_fd=self.blobs, follow_symlinks=False).st_size

    def unlink(self, name: str) -> None:
        os.unlink(name, dir_fd=self.blobs)   # unlinkat never follows: a planted link is removed, not its target

    def read(self, folder: int, name: str, limit: int) -> bytes | None:
        """At most `limit` + 1 bytes of the REGULAR file `name` in `folder`; None when absent or not a plain file."""
        return AF.read_at(folder, name, limit)

    def write(self, folder: int, name: str, data: bytes) -> None:
        """`name` in `folder`, whole or not at all (`atfile.write_at`, the one writer for STATE's own files)."""
        AF.write_at(folder, name, data)


# -- writing ----------------------------------------------------------------------------------

def _empty() -> dict:
    return {"commits": {}, "specs": [], "pushed_at": None}


def _read_index(state: Path) -> dict:
    f = _Folders.open(state, create=False)
    if f is None:
        return _empty()
    with f:
        return _load_index(f)


def _load_index(f: _Folders) -> dict:
    raw = f.read(f.top, INDEX, 4 << 20)
    if raw is None:
        return _empty()
    try:
        doc = json.loads(raw)
    except (ValueError, RecursionError):   # a planted index nested past the decoder's depth reads as none
        return _empty()
    if not isinstance(doc, dict):
        return _empty()
    commits = doc.get("commits") if isinstance(doc.get("commits"), dict) else {}
    specs = doc.get("specs") if isinstance(doc.get("specs"), list) else []
    return {"commits": {k: v for k, v in commits.items()
                        if isinstance(k, str) and SHA256.match(k) and isinstance(v, str) and COMMIT.match(v)},
            "specs": [s for s in specs if _spec_problem(s) is None],
            "pushed_at": doc.get("pushed_at") if isinstance(doc.get("pushed_at"), str) else None}


def pushed_at(state: Path) -> str | None:
    """When the steward last pushed (UTC), or None when it never has."""
    return _read_index(Path(state))["pushed_at"]


def _write_index(f: _Folders, doc: dict) -> None:
    doc["pushed_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    f.write(f.top, INDEX, json.dumps(doc, sort_keys=True).encode())


def _prune(f: _Folders, named) -> int:
    """Remove every blob no current lock names; the number removed."""
    pruned = 0
    for name in f.names():
        if name not in named:
            try:
                f.unlink(name)
                pruned += 1
            except OSError:
                pass
    return pruned


def push_blob(state: Path, store, body: object) -> dict:
    """One blob: {"sha256": hex, "commit": hex | null, "content_b64": str}. Refused unless it proves itself."""
    if not isinstance(body, dict) or set(body) - {"sha256", "commit", "content_b64"} or \
            not {"sha256", "content_b64"} <= set(body):
        raise PushError('history-blob sends {"sha256": "<hex>", "commit": "<hex>" | null, "content_b64": "..."}')
    sha, commit, b64 = body["sha256"], body.get("commit"), body["content_b64"]
    if not (isinstance(sha, str) and SHA256.match(sha)):
        raise PushError("sha256 must be 64 lower-case hex digits")
    if not isinstance(b64, str) or len(b64) > MAX_BLOB_BODY:
        raise PushError(f"content_b64 must be base64 of at most {MAX_BLOB} bytes")
    try:
        data = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError):
        raise PushError("content_b64 is not base64") from None
    if len(data) > MAX_BLOB:
        raise PushError(f"the blob is over {MAX_BLOB} bytes")
    if hashlib.sha256(data).hexdigest() != sha:
        raise PushError(f"the content does not hash to {sha}; nothing was stored")
    named = named_shas(store)
    if sha not in named:
        raise PushError(f"no current lock names {sha}; nothing was stored")
    with _Folders.open(state, create=True) as f:
        _prune(f, named)   # first: blobs whose locks have moved on never count against the cap
        held = sum(f.size(n) for n in f.names() if n != sha)
        if held + len(data) > MAX_STORED:
            raise PushError(f"this project's stored versions would pass {MAX_STORED} bytes; nothing was stored")
        f.write(f.blobs, sha, data)   # the file's name is the verified hash, never a string the client chose
        idx = _load_index(f)
        kept = isinstance(commit, str) and bool(COMMIT.match(commit))
        if kept:
            idx["commits"][sha] = commit
        else:
            idx["commits"].pop(sha, None)   # a commit id in any other shape is dropped
        _write_index(f, idx)
    return {"stored": sha, "commit": commit if kept else None}


def _spec_problem(s: object) -> str | None:
    if not isinstance(s, dict) or set(s) != {"path", "mtime_ns", "size", "head", "edited"}:
        return "each spec is {path, mtime_ns, size, head, edited}"
    if not isinstance(s["path"], str) or not A._safe_path(s["path"]) or len(s["path"]) > 1024:
        return "a spec path is not a relative project path"
    if not all(isinstance(s[k], int) and not isinstance(s[k], bool) and s[k] >= 0 for k in ("mtime_ns", "size")):
        return "mtime_ns and size are non-negative integers"
    if s["head"] is not None and not (isinstance(s["head"], str) and COMMIT.match(s["head"])):
        return "head is a 40/64-hex commit or null"
    if not (isinstance(s["edited"], int) and not isinstance(s["edited"], bool) and 0 <= s["edited"] < 1 << 40):
        return "edited is whole epoch seconds"
    return None


def push_specs(state: Path, store, body: object, wanted: dict[str, tuple[int, int]]) -> dict:
    """The spec times, replacing the last set; and a prune of every blob no current lock names.

    A record whose path the server did not itself ask for is ignored, and so is
    one whose (mtime_ns, size) is not what the server sees now.
    """
    if not isinstance(body, dict) or set(body) != {"specs"} or not isinstance(body["specs"], list) \
            or len(body["specs"]) > MAX_SPECS:
        raise PushError(f'history-specs sends {{"specs": [...]}}, at most {MAX_SPECS}')
    keep, ignored = [], []
    for s in body["specs"]:
        why = _spec_problem(s)
        if why:
            raise PushError(why)
        if wanted.get(s["path"]) != (s["mtime_ns"], s["size"]):
            ignored.append(s["path"])   # not asked for, or the file changed since: never stored
            continue
        keep.append(s)
    named = named_shas(store)
    with _Folders.open(state, create=True) as f:
        pruned = _prune(f, named)
        idx = _load_index(f)
        idx["commits"] = {k: v for k, v in idx["commits"].items() if k in named}
        idx["specs"] = keep
        _write_index(f, idx)
    return {"specs": len(keep), "ignored": ignored, "pruned": pruned}


# -- reading ----------------------------------------------------------------------------------

class PushedHistory:
    """`anchors.History` for the server: past versions only from verified blobs; else `G.NO_GIT`."""

    source = FROM
    max_commits = A.HISTORY_COMMITS

    def __init__(self, state: Path) -> None:
        self.state = Path(state)
        self._commits = _read_index(self.state)["commits"]

    def find(self, rel: str, sha256: str):
        if not (isinstance(sha256, str) and SHA256.match(sha256)):
            return G.NO_GIT
        f = _Folders.open(self.state, create=False)
        if f is None:
            return G.NO_GIT
        with f:
            data = f.read(f.blobs, sha256, MAX_BLOB)
        if data is None:
            return G.NO_GIT   # absent, not a plain file, or a symlink planted in its place: never followed
        if len(data) > MAX_BLOB or hashlib.sha256(data).hexdigest() != sha256:
            return G.NO_GIT   # re-checked on every read: a changed file proves nothing
        return self._commits.get(sha256), data.decode("utf-8", errors="replace")


class PushedTimes:
    """Spec last-commit times from the steward, each usable only while its key still matches the file."""

    def __init__(self, state: Path) -> None:
        self._by_path = {s["path"]: s for s in _read_index(Path(state))["specs"]}

    def edited_at(self, rel: str, stat_now: tuple[int, int]) -> tuple[float, str | None] | None:
        s = self._by_path.get(rel)
        if s is None or (s["mtime_ns"], s["size"]) != tuple(stat_now):
            return None   # missing, or the file changed since: the label, never this time
        return float(s["edited"]), s["head"]

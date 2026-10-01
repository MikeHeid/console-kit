"""Reads beneath a project's root that no symlink and no swap can lead out of (K3, spec §3.6, AC3.7, AC3.8).

The repository is agent-written, so a symlink in it (or a `..` in a path it
names) could point at another project's state dir, the tokens directory or
`server.json`, all of which the one console server can read. A check-then-open
(resolve with `realpath`, compare, then open the resolved path) is not enough:
an agent can swap a parent directory for a symlink between the check and the
open. So a confined read never opens by path string:

- the server holds an `O_DIRECTORY` descriptor on each project's root, opened
  once when the project is opened (`hold`);
- each read opens beneath that descriptor one component at a time, every
  component with `O_NOFOLLOW` (and `O_DIRECTORY` for all but the last), so the
  check and the open are one step and a swapped parent is met as the symlink it
  now is, and refused;
- `..`, `.`, an absolute path, an empty component and a symlink at ANY depth
  are refused, including a symlink that points inside the root. That forbids
  in-repo symlinks for the host page, `valid_if` paths, anchors and specs: it is
  the price of making the check and the open one step.

Every refusal is `Refused`, whose message names the path as given and never a
target. The one console server switches confinement on for its whole process
(`confine()`); the single-project server leaves it off, so its behaviour is
unchanged.
"""

from __future__ import annotations

import errno
import os
import stat
import threading
from pathlib import Path

_confined = False
_roots: dict[str, int] = {}
_lock = threading.Lock()
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


class Refused(OSError):
    """A path that is not a plain file (or folder) beneath the root, by the path as given only."""

    def __init__(self, rel: str) -> None:
        super().__init__(errno.EACCES, f"{rel}: refused, not a plain file under the project root")
        self.rel = rel

    def __str__(self) -> str:   # the message alone, as served: no "[Errno 13]" prefix
        return self.strerror


def confine() -> None:
    """From now on, every root read in this process goes through a held root descriptor. There is no undo."""
    global _confined
    _confined = True


def confined() -> bool:
    return _confined


def _key(root: Path) -> str:
    """The configured root as TEXT, never resolved: a read must not consult the filesystem to find its root.

    Resolving per read (the first cut) let a root path swapped for a symlink to
    ANOTHER held root find that project's descriptor. Keyed by text, a swapped
    path still names the descriptor its own project opened at start.
    """
    return os.path.abspath(os.fspath(root))


def hold(root: Path) -> None:
    """Open and keep the descriptor of `root`, resolved once, here. Raises OSError."""
    key = _key(root)
    with _lock:
        if key not in _roots:
            _roots[key] = os.open(os.path.realpath(key), DIR_FLAGS)


def _root_fd(root: Path) -> int:
    key = _key(root)
    with _lock:
        fd = _roots.get(key)
    if fd is None:
        raise Refused("(root)")   # a root this process never held is never read
    return fd


def _parts(rel: str) -> list[str]:
    if not isinstance(rel, str) or not rel or rel.startswith("/") or "\0" in rel:
        raise Refused(str(rel))
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise Refused(rel)
    return parts


def _walk_dirs(root: Path, parts: list[str], rel: str) -> int:
    """A new descriptor on the folder `parts` names beneath the root; the caller closes it."""
    cur = os.dup(_root_fd(root))
    try:
        for p in parts:
            nxt = os.open(p, DIR_FLAGS, dir_fd=cur)
            os.close(cur)
            cur = nxt
    except OSError as e:
        os.close(cur)
        if e.errno == errno.ENOENT:
            raise FileNotFoundError(errno.ENOENT, f"{rel}: not in the project") from None
        raise Refused(rel) from None   # ELOOP (a symlink), ENOTDIR (a file where a folder was), …
    return cur


def open_file(root: Path, rel: str) -> int:
    """A descriptor on the regular file `rel` beneath `root`; Refused, or FileNotFoundError when absent."""
    parts = _parts(rel)
    d = _walk_dirs(root, parts[:-1], rel)
    try:
        fd = os.open(parts[-1], FILE_FLAGS, dir_fd=d)
    except OSError as e:
        if e.errno == errno.ENOENT:
            raise FileNotFoundError(errno.ENOENT, f"{rel}: not in the project") from None
        raise Refused(rel) from None
    finally:
        os.close(d)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise Refused(rel)
    return fd


def read(root: Path, rel: str, cap: int) -> bytes:
    """The bytes of `rel` beneath `root`, at most `cap`; Refused, FileNotFoundError, or ValueError when larger."""
    fd = open_file(root, rel)
    with os.fdopen(fd, "rb") as fh:
        if os.fstat(fh.fileno()).st_size > cap:
            raise ValueError(f"{rel} is over {cap} bytes")
        data = fh.read(cap + 1)
    if len(data) > cap:
        raise ValueError(f"{rel} is over {cap} bytes")
    return data


def files_under(root: Path, rel_dir: str, max_files: int) -> list[tuple[str, int, int]]:
    """(rel, mtime_ns, size) of each regular file under the folder `rel_dir`, symlinks skipped, never followed."""
    d = _walk_dirs(root, _parts(rel_dir), rel_dir)
    out: list[tuple[str, int, int]] = []
    try:
        for dirpath, dirnames, filenames, dfd in os.fwalk(".", dir_fd=d, follow_symlinks=False):
            dirnames.sort()
            for name in sorted(filenames):
                try:
                    st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
                except OSError:
                    continue
                if not stat.S_ISREG(st.st_mode):
                    continue
                sub = os.path.normpath(os.path.join(dirpath, name))
                out.append((f"{rel_dir}/{sub}", st.st_mtime_ns, st.st_size))
                if len(out) >= max_files:
                    return out
    finally:
        os.close(d)
    return out

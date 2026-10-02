"""Read and write one file relative to a held folder descriptor: never a path looked up again.

The server's own files under STATE (steward-git's blobs and index, the pushed
items) are read and written through these, so a symlink planted in place of
the file is refused (O_NOFOLLOW), and nothing is resolved by path after the
folder was opened.
"""

from __future__ import annotations

import errno
import os
import stat

# O_NONBLOCK: opening a FIFO planted in the file's place returns at once instead of waiting for a writer
# (an open with a lock held would stall every later request); the fstat below then refuses it. A regular
# file never blocks, so the flag changes nothing for what is read.
FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK


def read_at(folder: int, name: str, limit: int) -> bytes | None:
    """At most `limit` + 1 bytes of the REGULAR file `name` in `folder`; None when absent or not a plain file.

    Anything else in its place (a symlink, a FIFO, a directory, a device) is refused without being read.
    """
    try:
        fd = os.open(name, FILE_FLAGS, dir_fd=folder)
    except OSError as e:
        if e.errno not in (errno.ENOENT, errno.ELOOP, errno.ENOTDIR):
            raise
        return None   # absent, or a symlink planted in its place: never followed
    try:
        regular = stat.S_ISREG(os.fstat(fd).st_mode)   # on the raw fd: fdopen itself raises for a directory
    except BaseException:
        os.close(fd)
        raise
    if not regular:
        os.close(fd)
        return None
    try:
        fh = os.fdopen(fd, "rb")
    except BaseException:   # fdopen failed: nothing owns the descriptor yet, so close it here
        os.close(fd)
        raise
    with fh:
        return fh.read(limit + 1)


def write_at(folder: int, name: str, data: bytes) -> None:
    """`name` in `folder`, whole or not at all: a 0600 temporary created O_EXCL, then renamed over it.

    On any failure the temporary is removed, relative to the same descriptor.
    """
    for _ in range(16):
        tmp = ".tmp." + os.urandom(8).hex()
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
                         dir_fd=folder)
            break
        except FileExistsError:
            continue
    else:
        raise FileExistsError(errno.EEXIST, "no temporary name was free")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())   # the bytes reach the disk before the name does: a crash leaves old or new, never empty
        os.rename(tmp, name, src_dir_fd=folder, dst_dir_fd=folder)
    except BaseException:
        try:
            os.unlink(tmp, dir_fd=folder)
        except OSError:
            pass
        raise

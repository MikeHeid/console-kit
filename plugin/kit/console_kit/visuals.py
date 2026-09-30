"""Visuals (0.8.0): the files an agent draws for an owner's visual request, under the project's `visuals_dir`.

An owner asks for a visual on an item (a message with intent 'visual'). An
agent answers with `agent.py visual`: a Mermaid block (`.mmd`) or a
self-contained HTML mock (`.html`), plus a short doc. The SERVER writes:

    <visuals_dir>/<item>/<request id[:8]>-<sha256[:12]>.mmd|.html   the visual, byte for byte
    <visuals_dir>/<item>/<request id[:8]>-<sha256[:12]>.md          its doc, linking to it
    <visuals_dir>/INDEX.md                                            regenerated from the store

and appends one `visual` record holding the file's path, size and sha256. The
store stays the record of what was drawn; the files are what a PR lands.

Every read and write is jailed like evidence: a relative path of plain
characters under `visuals_dir`, no `..`, never a secrets file, resolving
inside the project (no symlink out), never following a symlink at the file
itself, and at most `schema.MAX_VISUAL` bytes. A read also checks the file
still hashes to what the record says: a file edited after it was stored is
refused by name, never shown as if it were what the agent posted.

INDEX.md is only ever overwritten when it carries the kit's generated marker,
so a hand-written file of that name is refused, never clobbered.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from typing import Iterable

from . import schema as S

INDEX = "INDEX.md"
MARK = "<!-- console-kit: generated from the console's store. Do not edit: the next visual rewrites it. -->"


class VisualError(Exception):
    """A visual the kit refuses to write or to serve; the message says why and holds no file content."""


def paths(visuals_dir: str, item: str, request: str, fmt: str, sha: str) -> tuple[str, str]:
    stem = f"{visuals_dir}/{item}/{request[:8]}-{sha[:12]}"
    return stem + S.VISUAL_FORMATS[fmt], stem + ".md"


def _jail(root: Path, visuals_dir: str, rel: str, field: str = "path") -> Path:
    why = S.visual_path_problem(rel, field)
    if why:
        raise VisualError(why)
    if not rel.startswith(visuals_dir + "/"):
        raise VisualError(f"{field} {rel!r} is not under visuals_dir {visuals_dir!r}")
    top = Path(root).resolve()
    base = (top / visuals_dir).resolve()
    if top not in base.parents:
        raise VisualError(f"visuals_dir {visuals_dir!r} resolves outside the project")
    p = top / rel
    parent = p.parent.resolve()
    if parent != base and base not in parent.parents:
        raise VisualError(f"{field} {rel!r} resolves outside visuals_dir (a symlink out?)")
    return parent / p.name


def _read_nofollow(p: Path, cap: int) -> bytes:
    try:
        fd = os.open(p, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK)
    except FileNotFoundError:
        raise VisualError("the file is not there any more") from None
    except OSError as e:
        raise VisualError(f"the file cannot be opened ({type(e).__name__}); a symlink is never followed") from None
    try:
        import stat as _stat
        st = os.fstat(fd)
        if not _stat.S_ISREG(st.st_mode):
            raise VisualError("the path is not a regular file")
        if st.st_size > cap:
            raise VisualError(f"the file is {st.st_size} bytes, over the {cap}-byte limit")
        return os.read(fd, cap + 1)[:cap]
    finally:
        os.close(fd)


def _write_once(p: Path, data: bytes) -> None:
    """Write `data` at `p` atomically; an existing file must already hold exactly these bytes."""
    if p.is_symlink():
        raise VisualError(f"{p.name} is a symlink; the kit never writes through one")
    if p.exists():
        if _read_nofollow(p, max(len(data), S.MAX_VISUAL) + 1) != data:
            raise VisualError(f"{p.name} is already there with other content; nothing was overwritten")
        return
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".ck-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, p)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def doc_markdown(title: str, item: str, fmt: str, visual_name: str, text: str, request_text: str) -> str:
    quoted = "\n".join("> " + ln for ln in request_text.splitlines()) or ">"
    return (f"# {title}\n\n"
            f"A {'Mermaid diagram' if fmt == 'mermaid' else 'static HTML mock'} for item `{item}`: "
            f"[{visual_name}]({visual_name}).\n\n"
            f"The owner asked:\n\n{quoted}\n\n{text.rstrip()}\n")


def write(root: Path, visuals_dir: str, rel: str, doc_rel: str, content: bytes, doc: str) -> None:
    """Write a visual and its doc under `visuals_dir`, jailed; refuse rather than overwrite."""
    p, d = _jail(root, visuals_dir, rel), _jail(root, visuals_dir, doc_rel, "doc_path")
    top = Path(root).resolve()
    (top / rel).parent.mkdir(parents=True, exist_ok=True)
    p, d = _jail(root, visuals_dir, rel), _jail(root, visuals_dir, doc_rel, "doc_path")  # again, now it exists
    _write_once(p, content)
    _write_once(d, doc.encode("utf-8"))


def index_markdown(visuals_dir: str, records: Iterable[dict]) -> str:
    rows: dict[str, list[dict]] = {}
    for r in records:
        rows.setdefault(r["item"], []).append(r)
    out = [MARK, "", "# Visuals", "",
           f"Drawn by agents for the owner console's visual requests. Each row links the visual and its doc; "
           f"the console's store holds each file's sha256. This file is regenerated under `{visuals_dir}/`.", ""]
    if not rows:
        out.append("No visuals yet.")
    for item in sorted(rows):
        out += [f"## `{item}`", "", "| When | Title | Format | Files |", "|---|---|---|---|"]
        for r in sorted(rows[item], key=lambda r: r["seq"]):
            f = r["path"][len(visuals_dir) + 1:]
            d = r["doc_path"][len(visuals_dir) + 1:]
            title = r["title"].replace("|", "\\|")
            out.append(f"| {r['ts']} | {title} | {r['format']} | [visual]({f}) · [doc]({d}) |")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def write_index(root: Path, visuals_dir: str, records: Iterable[dict]) -> None:
    top = Path(root).resolve()
    base = (top / visuals_dir)
    base.mkdir(parents=True, exist_ok=True)
    p = _jail(root, visuals_dir, f"{visuals_dir}/{INDEX}", "index")
    if p.is_symlink():
        raise VisualError(f"{INDEX} is a symlink; the kit never writes through one")
    if p.exists():
        head = _read_nofollow(p, 4 << 20).decode("utf-8", errors="replace").split("\n", 1)[0]
        if head.strip() != MARK:
            raise VisualError(f"{visuals_dir}/{INDEX} was not written by the kit; move it aside, and the next "
                              f"visual regenerates it")
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".ck-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(index_markdown(visuals_dir, records))
        os.chmod(tmp, 0o644)
        os.replace(tmp, p)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def read(root: Path, visuals_dir: str | None, rec: dict) -> bytes:
    """The visual's bytes, only when it is still exactly what its record says."""
    if not visuals_dir:
        raise VisualError("this project sets no visuals_dir in .console-kit.json")
    p = _jail(root, visuals_dir, rec["path"])
    data = _read_nofollow(p, S.MAX_VISUAL)
    if hashlib.sha256(data).hexdigest() != rec["sha256"]:
        raise VisualError(f"{rec['path']} changed since the agent stored it (its sha256 no longer matches); "
                          f"it is not shown")
    return data

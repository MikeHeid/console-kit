"""Visuals: the files an agent draws for an owner's visual request.

An owner asks for a visual on an item (a message with intent 'visual'). An
agent answers with `agent.py visual`: a Mermaid block (`.mmd`) or a
self-contained HTML mock (`.html`), plus a short doc.

**Where they live (0.8.1).** The server stores them in its STATE directory,
the same trust root as `store.jsonl`, and NEVER in the project's working tree:

    STATE/visuals/<item>/<request id[:8]>-<sha256[:12]>.mmd|.html   the visual, byte for byte
    STATE/visuals/<item>/<request id[:8]>-<sha256[:12]>.md          its doc, linking to it

and appends one `visual` record holding the file's state-relative path, size
and sha256 (the hash lives in the record, in `store.jsonl`). The console
serves the visual from there.

0.8.0 wrote these files, and a regenerated `INDEX.md`, into the project's
checkout: the live service checkout the console runs from. An agent then
landed the same paths by PR, and a deploy that follows main with
`git merge --ff-only` was refused, because git will not overwrite untracked
files. The server must never write into the project's working tree.

**How they land.** `visuals_dir` in `.console-kit.json` is now only a
DESTINATION: the repo-relative folder a PR puts them in. `agent.py
visual-export --project DIR` copies stored visuals into DIR/<visuals_dir>/
(DIR being the agent's OWN worktree, never the server's directory) and
regenerates DIR/<visuals_dir>/INDEX.md there (`export_plan`, `export_write`).

A stored file is found by its record's item and file name (`stored_names`),
never by the prefix of the record's path, so a visual 0.8.0 wrote into the
checkout is served again once the operator moves it into STATE/visuals/<item>/
(RUNBOOK, "Upgrading to 0.8.1"). The server never reads, imports or deletes
anything under `visuals_dir` in the checkout.

Every read and write is jailed like evidence: a relative path of plain
characters, no `..`, never a secrets file, every directory on the way a real
directory (never a symlink), the file itself opened with O_NOFOLLOW and
checked to be a regular file, and at most `schema.MAX_VISUAL` bytes. A write
never replaces a file holding other bytes. A read also checks the file still
hashes to what the record says: a file edited after it was stored is refused
by name, never shown as if it were what the agent posted.

INDEX.md is only ever overwritten when it carries the kit's generated marker,
so a hand-written file of that name is refused, never clobbered.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat as _stat
import tempfile
from pathlib import Path
from typing import Iterable

from . import schema as S

STORE_DIR = "visuals"          # under STATE
INDEX = "INDEX.md"
MARK = "<!-- console-kit: generated from the console's store. Do not edit: the next visual rewrites it. -->"
MAX_DOC_BYTES = 256 * 1024     # a doc is a title, at most MAX_VISUAL_DOC characters and the owner's request
MAX_INDEX_BYTES = 4 << 20
NAME = re.compile(r"^([0-9a-f]{8})-([0-9a-f]{12})(\.mmd|\.html)$")


class VisualError(Exception):
    """A visual the kit refuses to write or to serve; the message says why and holds no file content."""


# -- names ------------------------------------------------------------------------------

def file_names(request: str, fmt: str, sha: str) -> tuple[str, str]:
    """The visual's file name and its doc's, from what the record holds."""
    stem = f"{request[:8]}-{sha[:12]}"
    return stem + S.VISUAL_FORMATS[fmt], stem + ".md"


def paths(item: str, request: str, fmt: str, sha: str) -> tuple[str, str]:
    """The STATE-relative paths a new record holds: visuals/<item>/<name>."""
    name, doc = file_names(request, fmt, sha)
    return f"{STORE_DIR}/{item}/{name}", f"{STORE_DIR}/{item}/{doc}"


def stored_names(rec: dict) -> tuple[str, str]:
    """Where a record's files are, as names under visuals/<item>/, checked against the record itself.

    A 0.8.0 record's path starts with the checkout's visuals_dir and a 0.8.1
    record's with `visuals/`; either way its last part must be the name its
    own request and sha256 give, or the record is refused rather than trusted.
    """
    fmt, sha = rec.get("format"), rec.get("sha256")
    if fmt not in S.VISUAL_FORMATS or not isinstance(sha, str) or not isinstance(rec.get("request"), str):
        raise VisualError("the record is not a visual record")
    name, doc = file_names(rec["request"], fmt, sha)
    for field, want in (("path", name), ("doc_path", doc)):
        why = S.visual_path_problem(rec.get(field), field)
        if why:
            raise VisualError(why)
        if rec[field].rsplit("/", 1)[-1] != want:
            raise VisualError(f"{field} {rec[field]!r} does not end in {want!r}, the name its request and "
                              f"sha256 give")
    return name, doc


# -- jailed file primitives ------------------------------------------------------------

def _dirs_nofollow(top: Path, parts: list[str], create: bool) -> Path:
    """top/parts..., each part a plain name and a REAL directory (never a symlink); made when `create`."""
    cur = top
    for part in parts:
        if part in ("", ".", "..") or "/" in part:
            raise VisualError(f"{part!r} is not a plain folder name")
        cur = cur / part
        try:
            st = os.lstat(cur)
        except FileNotFoundError:
            if not create:
                raise VisualError(f"{cur.relative_to(top).as_posix()} is not there") from None
            try:
                os.mkdir(cur, 0o755)
            except FileExistsError:
                pass
            st = os.lstat(cur)
        if _stat.S_ISLNK(st.st_mode):
            raise VisualError(f"{cur.relative_to(top).as_posix()} is a symlink; the kit never follows one")
        if not _stat.S_ISDIR(st.st_mode):
            raise VisualError(f"{cur.relative_to(top).as_posix()} is not a folder")
    return cur


def _jail(top: Path, rel: str, field: str, create: bool) -> Path:
    """`rel` under `top`, checked like evidence: plain, no `..`, no secrets file, no symlinked folder."""
    why = S.visual_path_problem(rel, field)
    if why:
        raise VisualError(why)
    top = Path(top).resolve()
    parts = rel.split("/")
    parent = _dirs_nofollow(top, parts[:-1], create)
    real = parent.resolve()
    if real != top and top not in real.parents:  # belt and braces: nothing above resolved out
        raise VisualError(f"{field} {rel!r} resolves outside {top}")
    return parent / parts[-1]


def _read_nofollow(p: Path, cap: int) -> bytes | None:
    """The bytes of a regular file at `p`, opened without following a symlink; None when absent."""
    try:
        fd = os.open(p, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as e:
        raise VisualError(f"{p.name} cannot be opened ({type(e).__name__}); a symlink is never followed") from None
    try:
        st = os.fstat(fd)
        if not _stat.S_ISREG(st.st_mode):
            raise VisualError(f"{p.name} is not a regular file")
        if st.st_size > cap:
            raise VisualError(f"{p.name} is {st.st_size} bytes, over the {cap}-byte limit")
        chunks, left = [], cap + 1
        while left > 0:
            b = os.read(fd, left)
            if not b:
                break
            chunks.append(b)
            left -= len(b)
        data = b"".join(chunks)
        if len(data) > cap:
            raise VisualError(f"{p.name} grew past the {cap}-byte limit while it was read")
        return data
    finally:
        os.close(fd)


def _atomic_write(p: Path, data: bytes) -> None:
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


def _same_or_absent(p: Path, data: bytes, shown: str) -> bool:
    """True when `p` already holds exactly `data`, False when absent; refuse other content or a non-file."""
    if p.is_symlink():
        raise VisualError(f"{shown} is a symlink; the kit never writes through one")
    have = _read_nofollow(p, max(len(data), S.MAX_VISUAL, MAX_DOC_BYTES) + 1)
    if have is None:
        return False
    if have != data:
        raise VisualError(f"{shown} is already there with other content; nothing was overwritten")
    return True


def _write_once(p: Path, data: bytes, shown: str) -> bool:
    """Write `data` at `p` atomically unless it already holds exactly these bytes; True when written."""
    if _same_or_absent(p, data, shown):
        return False
    _atomic_write(p, data)
    return True


# -- docs and the index ----------------------------------------------------------------

MD_META = "\\`*_[]()#<>!|"


def md_escape(text: str) -> str:
    """A one-line title as inert Markdown text: every metacharacter backslash-escaped.

    The agent writes the title, and the generated files are read as Markdown
    (in a PR, on a docs site), so a title must never become a link, an image,
    a heading, an HTML tag or a table cell break.
    """
    return "".join("\\" + ch if ch in MD_META else ch for ch in text)


def doc_markdown(title: str, item: str, fmt: str, visual_name: str, text: str, request_text: str) -> str:
    quoted = "\n".join("> " + ln for ln in request_text.splitlines()) or ">"
    return (f"# {md_escape(title)}\n\n"
            f"A {'Mermaid diagram' if fmt == 'mermaid' else 'static HTML mock'} for item `{item}`: "
            f"[{visual_name}]({visual_name}).\n\n"
            f"The owner asked:\n\n{quoted}\n\n{text.rstrip()}\n")


def index_markdown(visuals_dir: str, records: Iterable[dict], unknown: Iterable[str] = ()) -> str:
    """INDEX.md for `records` (each listed by its item and file name) and files no record names."""
    rows: dict[str, list[dict]] = {}
    for r in records:
        rows.setdefault(r["item"], []).append(r)
    out = [MARK, "", "# Visuals", "",
           f"Drawn by agents for the owner console's visual requests. Each row links the visual and its doc; "
           f"the console's store holds each file's sha256. This file is regenerated under `{visuals_dir}/` "
           f"by agent.py visual-export.", ""]
    unknown = sorted(unknown)
    if not rows and not unknown:
        out.append("No visuals yet.")
    for item in sorted(rows):
        out += [f"## `{item}`", "", "| When | Title | Format | Files |", "|---|---|---|---|"]
        for r in sorted(rows[item], key=lambda r: r["seq"]):
            f, d = stored_names(r)
            title = md_escape(r["title"])
            out.append(f"| {r['ts']} | {title} | {r['format']} | [visual]({item}/{f}) · [doc]({item}/{d}) |")
        out.append("")
    if unknown:
        out += ["## Not in this console's store", "",
                "Files here that no visual record of this console names (another console, or a hand edit):", ""]
        out += [f"- [{md_escape(u)}]({u})" for u in unknown]
        out.append("")
    return "\n".join(out).rstrip() + "\n"


# -- the server's store, under STATE ---------------------------------------------------

def store(state: Path, rel: str, doc_rel: str, content: bytes, doc: str) -> None:
    """Write a visual and its doc under STATE/visuals/, jailed; refuse rather than overwrite."""
    for r in (rel, doc_rel):
        if not r.startswith(STORE_DIR + "/"):
            raise VisualError(f"{r!r} is not under {STORE_DIR}/")
    p = _jail(state, rel, "path", create=True)
    d = _jail(state, doc_rel, "doc_path", create=True)
    _write_once(p, content, rel)
    _write_once(d, doc.encode("utf-8"), doc_rel)


def _locate(state: Path, rec: dict) -> tuple[Path, Path, str]:
    name, doc = stored_names(rec)
    item = rec.get("item")
    if not isinstance(item, str) or not S.ITEM_ID.match(item):
        raise VisualError("the record's item is not an item id")
    rel = f"{STORE_DIR}/{item}/{name}"
    try:
        p = _jail(state, rel, "path", create=False)
        d = _jail(state, f"{STORE_DIR}/{item}/{doc}", "doc_path", create=False)
    except VisualError as e:
        if "is not there" not in str(e):
            raise
        p = d = Path(state) / rel  # the folder is missing: the file is too, named below
    return p, d, rel


def _missing(rec: dict, rel: str) -> VisualError:
    old = "" if rec["path"].startswith(STORE_DIR + "/") else (
        f" This record was stored by 0.8.0 at {rec['path']} in the project's checkout; 0.8.1 reads only the "
        f"console's state, so move that file into STATE/{STORE_DIR}/{rec['item']}/ (RUNBOOK, 'Upgrading to 0.8.1').")
    return VisualError(f"the visual is not in the console's state at {rel}.{old}")


def read(state: Path, rec: dict) -> bytes:
    """The visual's bytes from STATE, only when it is still exactly what its record says."""
    p, _, rel = _locate(state, rec)
    data = _read_nofollow(p, S.MAX_VISUAL)
    if data is None:
        raise _missing(rec, rel)
    if hashlib.sha256(data).hexdigest() != rec["sha256"]:
        raise VisualError(f"{rel} changed since the agent stored it (its sha256 no longer matches); it is not shown")
    return data


def read_doc(state: Path, rec: dict, request_text: str) -> str:
    """The visual's doc from STATE, only when it is exactly the doc its record and request make."""
    _, d, rel = _locate(state, rec)
    name, _ = stored_names(rec)
    want = doc_markdown(rec["title"], rec["item"], rec["format"], name, rec["text"], request_text)
    data = _read_nofollow(d, MAX_DOC_BYTES)
    if data is None:
        raise _missing(rec, rel.rsplit(".", 1)[0] + ".md")
    if data != want.encode("utf-8"):
        raise VisualError(f"the doc of {rel} changed since the agent stored it; it is not exported")
    return want


# -- export into an agent's worktree ---------------------------------------------------

def _dest_names(item: str, name: str, doc: str, visuals_dir: str) -> tuple[str, str]:
    return f"{visuals_dir}/{item}/{name}", f"{visuals_dir}/{item}/{doc}"


def scan_destination(top: Path, visuals_dir: str) -> set[tuple[str, str]]:
    """(item, file name) of every visual-shaped file already under top/visuals_dir, never through a symlink."""
    try:
        base = _dirs_nofollow(Path(top).resolve(), visuals_dir.split("/"), create=False)
    except VisualError as e:
        if "is not there" in str(e):
            return set()
        raise
    found: set[tuple[str, str]] = set()
    for item_dir in sorted(os.listdir(base)):
        p = base / item_dir
        if not S.ITEM_ID.match(item_dir) or not _stat.S_ISDIR(os.lstat(p).st_mode):
            continue
        for name in sorted(os.listdir(p)):
            if NAME.match(name) and _stat.S_ISREG(os.lstat(p / name).st_mode):
                found.add((item_dir, name))
    return found


def export_plan(top: Path, visuals_dir: str, entries: list[dict]) -> list[tuple[str, bytes, bool]]:
    """(dest rel, bytes, already there) for each file of each entry; raises VisualError naming EVERY conflict.

    Each entry is a visual record plus `content` and `doc` (str). Nothing is
    written here, so a refusal leaves the destination as it was.
    """
    top = Path(top).resolve()
    plan, problems = [], []
    for e in entries:
        name, doc = stored_names(e)
        for rel, data in zip(_dest_names(e["item"], name, doc, visuals_dir),
                             (e["content"].encode("utf-8"), e["doc"].encode("utf-8"))):
            try:
                p = _jail(top, rel, "destination", create=False)
                there = _same_or_absent(p, data, rel)
            except VisualError as err:
                if "is not there" not in str(err):
                    problems.append(str(err))
                    continue
                there = False
            plan.append((rel, data, there))
    if problems:
        raise VisualError("; ".join(problems))
    return plan


def check_index(top: Path, visuals_dir: str) -> None:
    """Refuse an INDEX.md the kit did not write, or one that is a symlink or not a file."""
    rel = f"{visuals_dir}/{INDEX}"
    try:
        p = _jail(Path(top).resolve(), rel, "index", create=False)
    except VisualError as e:
        if "is not there" in str(e):
            return
        raise
    if p.is_symlink():
        raise VisualError(f"{rel} is a symlink; the kit never writes through one")
    have = _read_nofollow(p, MAX_INDEX_BYTES)
    if have is not None and have.decode("utf-8", errors="replace").split("\n", 1)[0].strip() != MARK:
        raise VisualError(f"{rel} was not written by the kit; move it aside, and the next export regenerates it")


def export_write(top: Path, visuals_dir: str, plan: list[tuple[str, bytes, bool]], records: list[dict]) -> dict:
    """Write the plan's new files, then regenerate INDEX.md from what is in top/visuals_dir now.

    `records` is every visual record the console holds: a file in the
    destination that one names gets its row; any other visual-shaped file is
    listed as not in this console's store. Returns what was written and skipped.
    """
    top = Path(top).resolve()
    check_index(top, visuals_dir)
    written, same = [], []
    for rel, data, there in plan:
        p = _jail(top, rel, "destination", create=True)
        (written if _write_once(p, data, rel) else same).append(rel)
    present = scan_destination(top, visuals_dir)
    by_name, rows = {}, []
    for r in records:
        try:
            by_name[(r["item"], stored_names(r)[0])] = r
        except VisualError:
            continue
    for key in sorted(present):
        if key in by_name:
            rows.append(by_name[key])
    unknown = [f"{i}/{n}" for (i, n) in sorted(present) if (i, n) not in by_name]
    index = index_markdown(visuals_dir, rows, unknown).encode("utf-8")
    base = _dirs_nofollow(top, visuals_dir.split("/"), create=True)
    p = base / INDEX
    check_index(top, visuals_dir)           # again, just before the write
    old = _read_nofollow(p, MAX_INDEX_BYTES)
    if old != index:
        _atomic_write(p, index)
        written.append(f"{visuals_dir}/{INDEX}")
    else:
        same.append(f"{visuals_dir}/{INDEX}")
    return {"written": written, "unchanged": same}

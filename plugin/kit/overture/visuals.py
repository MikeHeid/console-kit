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

**How they land.** `visuals_dir` in `.overture.json` is now only a
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
directory (never a symlink), opened as a directory fd and used through that
fd (0.8.2), the file itself opened with O_NOFOLLOW and
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
import secrets
import stat as _stat
from pathlib import Path
from typing import Iterable

from . import schema as S

STORE_DIR = "visuals"          # under STATE
INDEX = "INDEX.md"
MARK = "<!-- overture: generated from the console's store. Do not edit: the next visual rewrites it. -->"
MAX_DOC_BYTES = 256 * 1024     # a doc is a title, at most MAX_VISUAL_DOC characters and the owner's request
MAX_INDEX_BYTES = 4 << 20
NAME = re.compile(r"^([0-9a-f]{8})-([0-9a-f]{12})(\.mmd|\.html)\Z")

# HTML sanitization. The sandboxed iframe already runs under `default-src 'none'` with no
# `allow-scripts`, so inline `<script>` and `onerror=` can't fire in the browser — but defense-in-
# breadth is cheap and the adversarial reviewer specifically asked for a server-side strip.
# Patterns are case-insensitive. Order matters: strip whole dangerous elements first (with their
# content), then dangerous attributes, then dangerous URL schemes.
_HTML_STRIP_ELEMENTS = re.compile(
    # <style> is intentionally NOT stripped: CSS is inert under the sandbox's `default-src 'none';
    # style-src 'unsafe-inline'`, and prototypes without CSS are useless. Script-equivalent
    # elements only.
    r"<\s*(?P<tag>script|iframe|frame|frameset|object|embed|form|applet)\b[^>]*>.*?<\s*/\s*(?P=tag)\s*>",
    re.IGNORECASE | re.DOTALL,
)
# Self-closing or stray openers for the same dangerous elements, plus <meta http-equiv=refresh> and <base>.
_HTML_STRIP_SELF = re.compile(
    r"<\s*(?:script|iframe|frame|frameset|object|embed|form|applet|base)\b[^>]*/?>",
    re.IGNORECASE,
)
_HTML_STRIP_META_REFRESH = re.compile(
    r"<\s*meta\b[^>]*\bhttp-equiv\s*=\s*['\"]?\s*refresh\b[^>]*>",
    re.IGNORECASE,
)
# Event-handler attributes: on* names, in any whitespace- or tag-boundary context.
_HTML_STRIP_ON = re.compile(
    r"\s(on[a-z]+)\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+)",
    re.IGNORECASE,
)
# javascript: and vbscript: URLs on href/src/action/formaction attributes.
_HTML_STRIP_JS_URL = re.compile(
    r"\s(href|src|action|formaction|xlink:href)\s*=\s*(?:\"\s*(?:javascript|vbscript|data)\s*:[^\"]*\""
    r"|'\s*(?:javascript|vbscript|data)\s*:[^']*')",
    re.IGNORECASE,
)


def sanitize_html(html: str) -> tuple[str, int]:
    """strip script/style/iframe/form/meta-refresh/on* /javascript: from HTML before storage.

    Returns (cleaned, strip_count). Never raises; the sandbox is still the first line of defense,
    this is layered on top. The output is encoded utf-8 by the caller and stored verbatim.
    """
    count = 0
    out, n = _HTML_STRIP_ELEMENTS.subn("", html);               count += n
    out, n = _HTML_STRIP_SELF.subn("", out);                     count += n
    out, n = _HTML_STRIP_META_REFRESH.subn("", out);             count += n
    out, n = _HTML_STRIP_ON.subn("", out);                       count += n
    out, n = _HTML_STRIP_JS_URL.subn("", out);                   count += n
    return out, count


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
#
# 0.8.2 (the 0.8.1 review's follow-up (b)): every folder on the way is opened as
# a directory file descriptor, O_DIRECTORY|O_NOFOLLOW, each one relative to the
# one before it (`dir_fd=`), and every file operation after the walk goes
# through the last one. So the folder that was checked is the folder used: a
# folder swapped for a symlink after the walk changes nothing here (the kit
# still holds the real one), and one swapped in during the walk is refused
# when its turn comes, because O_NOFOLLOW will not open it. 0.8.1 checked each
# folder with lstat and then used its PATH again, which a swap in between
# redirected. The refusals are the same words as before.

_O_DIR = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_O_FILE = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK


class _At:
    """An open folder (a directory fd reached without following a symlink) and one name in it.

    A context manager: the fd is closed on exit. `shown` is the path as the
    refusals say it.
    """

    def __init__(self, fd: int, name: str, shown: str) -> None:
        self.fd, self.name, self.shown = fd, name, shown

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def __enter__(self) -> "_At":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def lstat(self) -> os.stat_result | None:
        """The name's own stat, never through a symlink; None when absent."""
        return _lstat_at(self.fd, self.name)


def _open_child(fd: int, part: str, shown: str, create: bool) -> int:
    """Open folder `part` inside the open folder `fd`, never through a symlink; make it first when `create`."""
    try:
        return os.open(part, _O_DIR, dir_fd=fd)
    except FileNotFoundError:
        if not create:
            raise VisualError(f"{shown} is not there") from None
    except OSError:
        st = _lstat_at(fd, part)
        if st is not None and _stat.S_ISLNK(st.st_mode):
            raise VisualError(f"{shown} is a symlink; the kit never follows one") from None
        if st is not None and not _stat.S_ISDIR(st.st_mode):
            raise VisualError(f"{shown} is not a folder") from None
        raise VisualError(f"{shown} cannot be opened as a folder") from None
    try:
        os.mkdir(part, 0o755, dir_fd=fd)
    except FileExistsError:
        pass                                     # made by someone else meanwhile: opened and checked below
    return _open_child(fd, part, shown, create=False)


def _lstat_at(fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _open_dirs(top: Path, parts: list[str], create: bool) -> int:
    """A directory fd for top/parts..., each part a plain name and a REAL folder (never a symlink); made when `create`.

    `top` itself is trusted (the caller resolved it). The caller closes the fd.
    """
    try:
        fd = os.open(top, _O_DIR)
    except FileNotFoundError:
        raise VisualError(f"{top} is not there") from None
    except OSError as e:
        raise VisualError(f"{top} cannot be opened as a folder ({type(e).__name__})") from None
    walked: list[str] = []
    try:
        for part in parts:
            if part in ("", ".", "..") or "/" in part:
                raise VisualError(f"{part!r} is not a plain folder name")
            walked.append(part)
            nxt = _open_child(fd, part, "/".join(walked), create)
            os.close(fd)
            fd = nxt
        return fd
    except BaseException:
        os.close(fd)
        raise


def _jail(top: Path, rel: str, field: str, create: bool) -> _At:
    """`rel` under `top`, checked like evidence: plain, no `..`, no secrets file, no symlinked folder.

    Returns the open parent folder and the file's name: every use goes through it.
    """
    why = S.visual_path_problem(rel, field)
    if why:
        raise VisualError(why)
    parts = rel.split("/")
    return _At(_open_dirs(Path(top).resolve(), parts[:-1], create), parts[-1], rel)


def _read_nofollow(at: _At, cap: int) -> bytes | None:
    """The bytes of the regular file `at` names, opened without following a symlink; None when absent."""
    try:
        fd = os.open(at.name, _O_FILE, dir_fd=at.fd)
    except FileNotFoundError:
        return None
    except OSError as e:
        raise VisualError(f"{at.name} cannot be opened ({type(e).__name__}); a symlink is never followed") from None
    try:
        st = os.fstat(fd)
        if not _stat.S_ISREG(st.st_mode):
            raise VisualError(f"{at.name} is not a regular file")
        if st.st_size > cap:
            raise VisualError(f"{at.name} is {st.st_size} bytes, over the {cap}-byte limit")
        chunks, left = [], cap + 1
        while left > 0:
            b = os.read(fd, left)
            if not b:
                break
            chunks.append(b)
            left -= len(b)
        data = b"".join(chunks)
        if len(data) > cap:
            raise VisualError(f"{at.name} grew past the {cap}-byte limit while it was read")
        return data
    finally:
        os.close(fd)


def _atomic_write(at: _At, data: bytes) -> None:
    """Write `data` as `at`'s name: a new temporary file in the same open folder, then a rename over it."""
    for _ in range(16):
        tmp = f".ck-{secrets.token_hex(8)}.tmp"
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600,
                         dir_fd=at.fd)
            break
        except FileExistsError:
            continue
    else:
        raise VisualError(f"no free temporary name beside {at.shown}")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fchmod(fh.fileno(), 0o644)
            os.fsync(fh.fileno())
        os.replace(tmp, at.name, src_dir_fd=at.fd, dst_dir_fd=at.fd)
    except BaseException:
        try:
            os.unlink(tmp, dir_fd=at.fd)
        except FileNotFoundError:
            pass
        raise


def _same_or_absent(at: _At, data: bytes, shown: str) -> bool:
    """True when `at` already holds exactly `data`, False when absent; refuse other content or a non-file."""
    st = at.lstat()
    if st is not None and _stat.S_ISLNK(st.st_mode):
        raise VisualError(f"{shown} is a symlink; the kit never writes through one")
    have = _read_nofollow(at, max(len(data), S.MAX_VISUAL, MAX_DOC_BYTES) + 1)
    if have is None:
        return False
    if have != data:
        raise VisualError(f"{shown} is already there with other content; nothing was overwritten")
    return True


def _write_once(at: _At, data: bytes, shown: str) -> bool:
    """Write `data` at `at` atomically unless it already holds exactly these bytes; True when written."""
    if _same_or_absent(at, data, shown):
        return False
    _atomic_write(at, data)
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
    with _jail(state, rel, "path", create=True) as p, _jail(state, doc_rel, "doc_path", create=True) as d:
        _write_once(p, content, rel)
        _write_once(d, doc.encode("utf-8"), doc_rel)


def _locate(state: Path, rec: dict, field: str) -> tuple[_At | None, str]:
    """The open folder holding a record's visual (`path`) or doc (`doc_path`), or None when its folder is missing."""
    name, doc = stored_names(rec)
    item = rec.get("item")
    if not isinstance(item, str) or not S.ITEM_ID.match(item):
        raise VisualError("the record's item is not an item id")
    rel = f"{STORE_DIR}/{item}/{name if field == 'path' else doc}"
    try:
        return _jail(state, rel, field, create=False), rel
    except VisualError as e:
        if "is not there" not in str(e):
            raise
        return None, rel  # the folder is missing: the file is too, named by the caller


def _missing(rec: dict, rel: str) -> VisualError:
    old = "" if rec["path"].startswith(STORE_DIR + "/") else (
        f" This record was stored by 0.8.0 at {rec['path']} in the project's checkout; 0.8.1 reads only the "
        f"console's state, so move that file into STATE/{STORE_DIR}/{rec['item']}/ (RUNBOOK, 'Upgrading to 0.8.1').")
    return VisualError(f"the visual is not in the console's state at {rel}.{old}")


def read(state: Path, rec: dict) -> bytes:
    """The visual's bytes from STATE, only when it is still exactly what its record says."""
    p, rel = _locate(state, rec, "path")
    if p is None:
        raise _missing(rec, rel)
    with p:
        data = _read_nofollow(p, S.MAX_VISUAL)
    if data is None:
        raise _missing(rec, rel)
    if hashlib.sha256(data).hexdigest() != rec["sha256"]:
        raise VisualError(f"{rel} changed since the agent stored it (its sha256 no longer matches); it is not shown")
    return data


def read_doc(state: Path, rec: dict, request_text: str) -> str:
    """The visual's doc from STATE, only when it is exactly the doc its record and request make."""
    d, doc_rel = _locate(state, rec, "doc_path")
    name, _ = stored_names(rec)
    rel = doc_rel.rsplit("/", 1)[0] + "/" + name
    want = doc_markdown(rec["title"], rec["item"], rec["format"], name, rec["text"], request_text)
    if d is None:
        raise _missing(rec, doc_rel)
    with d:
        data = _read_nofollow(d, MAX_DOC_BYTES)
    if data is None:
        raise _missing(rec, doc_rel)
    if data != want.encode("utf-8"):
        raise VisualError(f"the doc of {rel} changed since the agent stored it; it is not exported")
    return want


# -- export into an agent's worktree ---------------------------------------------------

def _dest_names(item: str, name: str, doc: str, visuals_dir: str) -> tuple[str, str]:
    return f"{visuals_dir}/{item}/{name}", f"{visuals_dir}/{item}/{doc}"


def scan_destination(top: Path, visuals_dir: str) -> set[tuple[str, str]]:
    """(item, file name) of every visual-shaped file already under top/visuals_dir, never through a symlink."""
    try:
        base = _open_dirs(Path(top).resolve(), visuals_dir.split("/"), create=False)
    except VisualError as e:
        if "is not there" in str(e):
            return set()
        raise
    found: set[tuple[str, str]] = set()
    try:
        for item_dir in sorted(os.listdir(base)):
            st = _lstat_at(base, item_dir)
            if not S.ITEM_ID.match(item_dir) or st is None or not _stat.S_ISDIR(st.st_mode):
                continue
            try:
                sub = os.open(item_dir, _O_DIR, dir_fd=base)
            except OSError:
                continue                      # swapped for something else since the listing: not ours
            try:
                for name in sorted(os.listdir(sub)):
                    fst = _lstat_at(sub, name)
                    if NAME.match(name) and fst is not None and _stat.S_ISREG(fst.st_mode):
                        found.add((item_dir, name))
            finally:
                os.close(sub)
    finally:
        os.close(base)
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
                with _jail(top, rel, "destination", create=False) as p:
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
    with p:
        _index_now(p, rel)


def _index_now(at: _At, rel: str) -> bytes | None:
    """The kit's INDEX.md as it is in the open folder, or None; refuse a symlink or one the kit did not write."""
    st = at.lstat()
    if st is not None and _stat.S_ISLNK(st.st_mode):
        raise VisualError(f"{rel} is a symlink; the kit never writes through one")
    have = _read_nofollow(at, MAX_INDEX_BYTES)
    if have is not None and have.decode("utf-8", errors="replace").split("\n", 1)[0].strip() != MARK:
        raise VisualError(f"{rel} was not written by the kit; move it aside, and the next export regenerates it")
    return have


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
        with _jail(top, rel, "destination", create=True) as p:
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
    rel = f"{visuals_dir}/{INDEX}"
    with _At(_open_dirs(top, visuals_dir.split("/"), create=True), INDEX, rel) as p:
        old = _index_now(p, rel)            # again, just before the write, in the folder written to
        if old != index:
            _atomic_write(p, index)
            written.append(rel)
        else:
            same.append(rel)
    return {"written": written, "unchanged": same}

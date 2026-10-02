"""The console page as the steward last snapshotted it from a reviewed ref (owner ruling CONSOLE-kit/Q28).

Before Q28 the server read its page from the project's checkout on every
request and injected the console into it. Any agent that could edit that file
could put a script in the owner's browser, where it could answer and lock as
the owner. Now no server reads a page from a project: not `--page`, not the
one server's `page` entry. The page served is the snapshot kept here, in the
console's own STATE dir, and only `agent.py page-snapshot` writes it. That
command reads the page from a commit with `git cat-file` in the steward's
process, never from the working tree. Unless told `--unreviewed`, it refuses a
commit that is not an ancestor of origin/main. The server cannot check any of
that; it is a guard against mistakes and jailed agents (Q19), and the footer
says which kind of ref the page came from.

STATE/page-snapshot.json is read and written relative to a held descriptor of
STATE (`atfile`): opened O_NOFOLLOW|O_NONBLOCK, refused unless a regular
file, and replaced whole by an O_EXCL temporary, an fsync and a rename.
"""

from __future__ import annotations

import base64
import binascii
import html as H
import json
import os
import re
import sys
from pathlib import Path

from . import atfile as AF
from . import publish as P
from . import serverfile as SF

SNAPSHOT = "page-snapshot.json"
MAX_PAGE = 4 << 20                         # the page's own bytes, as the one server always capped them
MAX_BODY = MAX_PAGE * 4 // 3 + 4096        # its base64, plus the JSON around it (as /history-blob)
MAX_FILE = MAX_BODY                        # what is stored is what was sent, so the same bound
FIELDS = {"content", "ref", "commit", "path", "reviewed"}
REF = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]{0,199}\Z")   # shown in the footer: plain characters only
COMMIT = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
STATE_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC

# What the page says when there is no snapshot to serve: never blank, never a page nobody reviewed (F63).
NO_SNAPSHOT = "No dashboard snapshot yet: run agent.py page-snapshot in the project's checkout."
UNREADABLE = ("The dashboard snapshot cannot be read, so it is not shown: run agent.py page-snapshot "
              "in the project's checkout again.")


def snapshot_problem(doc: object) -> str | None:
    """Why a pushed (or stored) snapshot cannot be served, naming the first bad field; None when it can."""
    if not isinstance(doc, dict) or set(doc) != FIELDS:
        return ('page-snapshot sends {"content": <base64 of the page>, "ref": str, "commit": <hex id>, '
                '"path": str, "reviewed": bool}')
    if not isinstance(doc["ref"], str) or not REF.match(doc["ref"]):
        return "ref is a git ref of plain characters (letters, digits, '_', '.', '/', '-'), at most 200"
    if not isinstance(doc["commit"], str) or not COMMIT.match(doc["commit"]):
        return "commit is a full commit id in lowercase hex"
    why = SF._page_problem(doc["path"])
    if why:
        return why
    if not isinstance(doc["reviewed"], bool):
        return "reviewed is true or false"
    page = page_text(doc["content"])
    if isinstance(page, ValueError):
        return str(page)
    try:
        P.inject(page, "")   # the injection's own check: exactly one </body> and no console block
    except P.PublishError as e:
        return f"the page cannot carry the console: {e}"
    return None


def page_text(content: object) -> str | ValueError:
    """The page from its base64, or a ValueError naming why not (returned, so the check above reads flat)."""
    if not isinstance(content, str) or len(content) > MAX_BODY:
        return ValueError(f"content is the page's base64, at most {MAX_PAGE} bytes of page")
    try:
        raw = base64.b64decode(content, validate=True)
    except (binascii.Error, ValueError):
        return ValueError("content is not base64")
    if len(raw) > MAX_PAGE:
        return ValueError(f"the page is over {MAX_PAGE} bytes")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return ValueError("the page is not UTF-8")


def store(state: Path, body: dict) -> None:
    """Write a CHECKED snapshot to STATE/page-snapshot.json, whole or not at all."""
    data = json.dumps({k: body[k] for k in sorted(FIELDS)}, sort_keys=True).encode("ascii")
    sfd = os.open(state, STATE_FLAGS)
    try:
        aside = AF.set_aside_dir(sfd, SNAPSHOT)   # the rename replaces a file, link or FIFO; not a directory
        if aside:
            sys.stderr.write(f"console page: a directory at {Path(state, SNAPSHOT)} was set aside as {aside}\n")
        AF.write_at(sfd, SNAPSHOT, data)
    finally:
        os.close(sfd)


def load(state: Path) -> tuple[dict | None, str | None]:
    """(the stored snapshot, None), or (None, the note to show instead). Never raises for the file itself.

    Absent: the "no snapshot yet" note. Anything else that cannot be served
    (not a plain file, not readable, too large, not JSON, failing the check)
    is logged with its reason and shows the "cannot be read" note: the console
    still works.
    """
    sfd = os.open(state, STATE_FLAGS)
    try:
        doc, why = _read(sfd)
    finally:
        os.close(sfd)
    if why is None:
        return doc, None
    if why is NO_SNAPSHOT:
        return None, NO_SNAPSHOT
    sys.stderr.write(f"console page: {Path(state, SNAPSHOT)} is {why}\n")
    return None, UNREADABLE


def _read(sfd: int) -> tuple[dict | None, str | None]:
    """(the checked snapshot, None), (None, NO_SNAPSHOT) when absent, or (None, why it cannot be served)."""
    try:
        raw = AF.read_at(sfd, SNAPSHOT, MAX_FILE)
        if raw is None:
            os.stat(SNAPSHOT, dir_fd=sfd, follow_symlinks=False)   # FileNotFoundError: there is none yet
            return None, "not a plain file"
    except FileNotFoundError:
        return None, NO_SNAPSHOT
    except OSError as e:   # EACCES, EIO...: as unreadable as a bad file, never an error out of the page
        return None, f"not readable: {e.strerror or type(e).__name__}"
    if len(raw) > MAX_FILE:
        return None, f"over {MAX_FILE} bytes"
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError) as e:   # UnicodeDecodeError, JSONDecodeError, deep nesting
        return None, f"not JSON: {e}"
    why = snapshot_problem(doc)
    return (None, why) if why else (doc, None)


def source_line(doc: dict) -> str:
    """The footer's provenance: which ref and commit the page came from, and whether it was a reviewed one."""
    kind = "from the steward" if doc["reviewed"] else "unreviewed ref"
    return f"Dashboard page from {doc['ref']} @ {doc['commit'][:12]} ({kind})"


def kit_page(note: str) -> str:
    """The page served with no snapshot: the console alone, with a labelled note saying why."""
    return ('<!doctype html>\n<html lang="en"><head><meta charset="utf-8"><title>Console</title></head>\n'
            f'<body>\n<p class="ck-page-note" role="status">{H.escape(note)}</p>\n</body></html>\n')


def render(state: Path, block: str) -> str:
    """The page to serve: the stored snapshot (or the kit-only page) with its provenance line and the console."""
    doc, note = load(state)
    if doc is None:
        return P.inject(kit_page(note), block)
    line = f'<p class="ck-page-source" role="note">{H.escape(source_line(doc))}</p>\n'
    return P.inject(page_text(doc["content"]), line + block)

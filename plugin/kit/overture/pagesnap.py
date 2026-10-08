"""The console page: staged by the steward from a commit, published by the owner (CONSOLE-kit/Q28, Q29).

Before Q28 the server read its page from the project's checkout on every
request and injected the console into it. Any agent that could edit that file
could put a script in the owner's browser, where it could answer and lock as
the owner. Now no server reads a page from a project: not `--page`, not the
one server's `page` entry.

`agent.py page-snapshot` reads the page from a commit with `git cat-file` in
the steward's process, never from the working tree, and refuses a commit that
is not an ancestor of origin/main unless told `--unreviewed`. What it sends is
only STAGED (Q29, "Agents stage, I publish"): kept in STATE/page-staged.json
and never served. The page shows the proposal (ref, commit, size, reviewed or
not), a sandboxed preview, and "Use this page"; only that owner route, behind
the owner's Access login, moves it to STATE/page-snapshot.json, which is the
one page served. No agent route can publish. The server cannot check the
agent-side guards (it runs no git); it re-checks everything it can (the closed
schema, the caps, the injection) at stage time, at publish time, and on every
read.

Both files are read and written relative to a held descriptor of STATE
(`atfile`): opened O_NOFOLLOW|O_NONBLOCK, refused unless a regular file, and
replaced whole by an O_EXCL temporary, an fsync and a rename.
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

SNAPSHOT = "page-snapshot.json"            # the published page: the only one ever served
STAGED = "page-staged.json"                # the proposed page: shown as a proposal and a preview, never served
MAX_PAGE = 4 << 20                         # the page's own bytes, as the one server always capped them
MAX_BODY = MAX_PAGE * 4 // 3 + 4096        # its base64, plus the JSON around it (as /history-blob)
MAX_FILE = MAX_BODY                        # what is stored is what was sent, so the same bound
FIELDS = {"content", "ref", "commit", "path", "reviewed"}
REF = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]{0,199}\Z")   # shown on the page: plain characters only
COMMIT = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
STATE_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC

# What the page says when no page is published: never blank, never a page the owner did not choose (F63).
NO_SNAPSHOT = ("No dashboard page is published yet: run agent.py page-snapshot in the project's checkout, "
               "then press \"Use this page\" here.")
UNREADABLE = ("The published dashboard page cannot be read, so it is not shown: stage it again with "
              "agent.py page-snapshot, then press \"Use this page\".")
STAGED_UNREADABLE = "A proposed dashboard page cannot be read: ask the steward to run agent.py page-snapshot again."
USE = "Use this page"
WAITING = "New dashboard page waiting · Review"
# "reviewed" is what the agent's command said (its ancestor check); this server runs no git and cannot verify it.
REVIEWED_CLAIM = "reviewed (agent's claim, not checked by the server)"


class PublishError(Exception):
    """Why the owner's "Use this page" did nothing, with the HTTP status it answers with."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def snapshot_problem(doc: object) -> str | None:
    """Why a pushed (or stored) page cannot be served, naming the first bad field; None when it can."""
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


def _store(sfd: int, state: Path, name: str, doc: dict) -> None:
    """Write a CHECKED page to STATE/`name`, whole or not at all."""
    data = json.dumps({k: doc[k] for k in sorted(FIELDS)}, sort_keys=True).encode("ascii")
    aside = AF.set_aside_dir(sfd, name)   # the rename replaces a file, link or FIFO; not a directory
    if aside:
        sys.stderr.write(f"console page: a directory at {Path(state, name)} was set aside as {aside}\n")
    AF.write_at(sfd, name, data)


def stage(state: Path, body: dict) -> None:
    """Keep a CHECKED page as the proposal (STATE/page-staged.json). It replaces any earlier one; nothing served moves."""
    sfd = os.open(state, STATE_FLAGS)
    try:
        _store(sfd, state, STAGED, body)
    finally:
        os.close(sfd)


def publish(state: Path, commit: object) -> dict:
    """The owner's "Use this page": the staged page becomes the published one, if it is the one they saw.

    `commit` is the staged commit the owner's page showed. A different one (a
    newer page was staged since) is refused by name, so a race between two
    stagings never publishes a page the owner did not see. The staged file is
    read and checked again here, then written as the published page and
    removed. Raises PublishError.
    """
    if not isinstance(commit, str) or not COMMIT.match(commit):
        raise PublishError(400, 'page-publish takes {"commit": <the staged commit id, full lowercase hex>}')
    sfd = os.open(state, STATE_FLAGS)
    try:
        doc, why = _read(sfd, STAGED)
        if why is NO_SNAPSHOT:
            raise PublishError(404, "no dashboard page is staged; ask the steward to run agent.py page-snapshot")
        if why is not None:
            sys.stderr.write(f"console page: {Path(state, STAGED)} is {why}\n")
            raise PublishError(409, STAGED_UNREADABLE)
        if doc["commit"] != commit:
            raise PublishError(409, f"the staged page is now {doc['commit'][:12]}, not {commit[:12]}: a newer one "
                                    f"was staged since this page loaded; reload, check it, and press "
                                    f"\"{USE}\" again")
        _store(sfd, state, SNAPSHOT, doc)
        try:
            os.unlink(STAGED, dir_fd=sfd)
        except FileNotFoundError:
            pass
    finally:
        os.close(sfd)
    return {"published": doc["commit"], "ref": doc["ref"], "reviewed": doc["reviewed"]}


def load(state: Path, name: str = SNAPSHOT) -> tuple[dict | None, str | None]:
    """(the stored page, None), or (None, the note to show instead). Never raises for the file itself.

    Absent: NO_SNAPSHOT. Anything else that cannot be served (not a plain
    file, not readable, too large, not JSON, failing the check) is logged with
    its reason and gives the "cannot be read" note: the console still works.
    """
    sfd = os.open(state, STATE_FLAGS)
    try:
        doc, why = _read(sfd, name)
    finally:
        os.close(sfd)
    if why is None:
        return doc, None
    if why is NO_SNAPSHOT:
        return None, NO_SNAPSHOT
    sys.stderr.write(f"console page: {Path(state, name)} is {why}\n")
    return None, (UNREADABLE if name == SNAPSHOT else STAGED_UNREADABLE)


def _read(sfd: int, name: str) -> tuple[dict | None, str | None]:
    """(the checked page, None), (None, NO_SNAPSHOT) when absent, or (None, why it cannot be served)."""
    try:
        raw = AF.read_at(sfd, name, MAX_FILE)
        if raw is None:
            os.stat(name, dir_fd=sfd, follow_symlinks=False)   # FileNotFoundError: there is none
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
    """The footer's provenance: which ref and commit the page came from, and that the owner chose it."""
    kind = "staged by the steward" if doc["reviewed"] else "staged from an unreviewed ref"
    return f"Dashboard page from {doc['ref']} @ {doc['commit'][:12]} ({kind}, published by you)"


def _size(n: int) -> str:
    return f"{n} bytes" if n < 1024 else f"{n / 1024:.1f} KiB" if n < 1 << 20 else f"{n / (1 << 20):.1f} MiB"


def proposal_strip(doc: dict | None, note: str | None) -> str:
    """The staged page as a proposal: its ref, commit, size and kind, a sandboxed preview, and "Use this page".

    The preview is `<iframe sandbox="">` on GET /api/page-staged, which itself
    answers under VISUAL_HTML_CSP (sandbox, default-src 'none'): two layers,
    each alone enough to stop the staged page's script, and the second stops
    it fetching anything. The kit shows agent-written HTML mocks the same way.

    The proposal is a `<dialog>` (owner, 2026-10-02: "can you create a modal rather than place on bottom
    (button hides under header)"), rendered WITHOUT `open`, so nothing opens on load. console.js upgrades it:
    it marks the region `data-ck-ready`, shows the fixed "New dashboard page waiting" button and opens the
    dialog with showModal() only when that button (or the stale-board bar's) is pressed. Until it does, the
    CSS lays the dialog out inline at the end of the page, as the strip was, so a page whose console script
    fails still shows the proposal.
    """
    if doc is None:
        if note is NO_SNAPSHOT or note is None:
            return ""
        return f'<div class="ck-page-proposed" role="status"><p>{H.escape(note)}</p></div>\n'
    size = len(base64.b64decode(doc["content"]))
    kind = REVIEWED_CLAIM if doc["reviewed"] else "unreviewed"
    line = f"Proposed dashboard: {doc['ref']} @ {doc['commit'][:12]} · {_size(size)} · {kind}"
    return ('<div class="ck-page-proposed" role="region" aria-label="Proposed dashboard page">\n'
            f'<button type="button" class="ck-page-waiting" aria-haspopup="dialog" hidden>{WAITING}</button>\n'
            '<dialog class="ck-page-dialog" aria-labelledby="ck-page-dialog-title" tabindex="-1">\n'
            '<h2 class="ck-page-dialog-title" id="ck-page-dialog-title">New dashboard page</h2>\n'
            f'<p class="ck-page-proposed-line">{H.escape(line)}</p>\n'
            '<div class="ck-page-preview"><p class="ck-page-preview-note">Preview (its scripts do not run here)</p>\n'
            '<iframe sandbox="" src="/api/page-staged" title="Proposed dashboard page" '
            'referrerpolicy="no-referrer"></iframe>\n'
            '</div>\n'
            '<p class="ck-page-use-error" role="alert" hidden></p>\n'
            '<div class="ck-page-actions">\n'
            f'<button type="button" class="ck-page-use" data-commit="{H.escape(doc["commit"])}">{USE}</button>\n'
            '<button type="button" class="ck-page-not-now">Not now</button>\n'
            '</div>\n</dialog>\n</div>\n')


def staged_page(state: Path) -> bytes | None:
    """The staged page's bytes for the sandboxed preview, or None when there is none to show."""
    doc, _ = load(state, STAGED)
    return None if doc is None else base64.b64decode(doc["content"])


def render(state: Path, block: str) -> str:
    """The page to serve: the PUBLISHED page (or the kit-only page) with its provenance, any proposal, the console."""
    doc, note = load(state, SNAPSHOT)
    staged, staged_note = load(state, STAGED)
    strip = proposal_strip(staged, staged_note)
    if doc is None:
        return P.inject(kit_page(note), strip + block)
    line = f'<p class="ck-page-source" role="note">{H.escape(source_line(doc))}</p>\n'
    return P.inject(page_text(doc["content"]), line + strip + block)


def kit_page(note: str) -> str:
    """The page served with nothing published: the console alone, with a labelled note saying why."""
    return ('<!doctype html>\n<html lang="en"><head><meta charset="utf-8"><title>Console</title></head>\n'
            f'<body>\n<p class="ck-page-note" role="status">{H.escape(note)}</p>\n</body></html>\n')

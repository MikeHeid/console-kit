"""The project's pull requests, as its steward pushes them (owner ask: "have PR requests and history show up").

No server talks to GitHub or starts `gh`: the server starts no process at all
(`gitseam`), and `gh` reads the repository's own git config to find its remote,
which an agent can write. So `agent.py prs-push` runs `gh pr list` in the
steward's own process, inside its jail, turns the output into the closed shape
below with `from_gh`, and sends it as DATA over the agent socket. The server
checks it with `snapshot_problem` (unknown keys refused, every count and string
capped and refused by name), stamps when it arrived and which agent sent it,
and keeps the last push in STATE/prs.json, read and written relative to a held
descriptor of STATE (`atfile`), never resolved by path again.

Everything in a push is untrusted external text: a PR's title and branch are
whatever someone typed on GitHub. The page renders them as text, never HTML,
and links only to the PR's own `https://github.com/<repo>/pull/<number>` URL,
which the check here pins exactly.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

from . import atfile as AF
from . import names as N

PRS = "prs.json"
MAX_FILE = 3 << 20              # a full push under every cap below, in UTF-8, fits with room to spare
MAX_PRS = 200                   # what the server keeps; the steward asks for at most 2 x MAX_LIMIT
MAX_TITLE = 1024                # GitHub caps a title at 256 characters
MAX_REF = 255                   # a branch name, as git and GitHub cap one
MAX_LOGIN = 100                 # GitHub logins are at most 39; an app's is "app/<slug>"
MAX_DAYS = 365
DEFAULT_DAYS = 30               # the steward's window for merged and closed PRs
DEFAULT_LIMIT = 50              # per list: open, and merged-or-closed in the window
MAX_LIMIT = 100
STATES = ("open", "merged", "closed")
CHECKS = ("success", "failure", "pending", "none")
PUSH_FIELDS = {"repo", "window_days", "prs"}
STORED_FIELDS = PUSH_FIELDS | {"pushed_at", "by"}
PR_FIELDS = {"number", "title", "state", "draft", "head", "base", "author", "created_at", "updated_at",
             "merged_at", "closed_at", "url", "merge_commit", "checks"}
REPO = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}\Z")
TS = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")
COMMIT = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
STATE_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC

# What the PRs view says before the steward's first push: never a blank list, never invented PRs (F63).
NOT_PUSHED = ("No pull requests yet: the console server never talks to GitHub, so they appear when the steward "
              "pushes them (agent.py prs-push).")
UNREADABLE = ("The stored pull requests cannot be read, so none are shown: the steward should push them again "
              "(agent.py prs-push).")

# What `agent.py prs-push` asks gh for: the fields `from_gh` reads, and nothing else.
GH_FIELDS = ("number,title,state,isDraft,headRefName,baseRefName,author,createdAt,updatedAt,mergedAt,closedAt,"
             "url,mergeCommit,statusCheckRollup")


class PushError(ValueError):
    """gh's output was not the shape `from_gh` reads: named, and nothing is sent."""


def _ts_problem(v: object, name: str, nullable: bool) -> str | None:
    if v is None and nullable:
        return None
    if not isinstance(v, str) or not TS.match(v):
        return f"{name} is {'null or ' if nullable else ''}a UTC time like 2026-10-01T12:00:00Z"
    return None


def _text_problem(v: object, name: str, cap: int, refs: bool = False) -> str | None:
    if not isinstance(v, str):
        return f"{name} must be str, not {type(v).__name__}"
    if len(v) > cap:
        return f"{name} is over {cap} characters"
    if refs and (not v or CONTROL.search(v)):
        return f"{name} is a branch name: not empty, no control characters"
    return None


def pr_problem(pr: object, n: int, repo: str) -> str | None:
    """Why one PR cannot be kept, naming the field; None when it can."""
    where = f"prs[{n}]"
    if not isinstance(pr, dict):
        return f"{where} is not an object"
    if set(pr) != PR_FIELDS:
        extra, missing = sorted(set(pr) - PR_FIELDS), sorted(PR_FIELDS - set(pr))
        return (f"{where} has unknown fields {extra[:5]}" if extra else f"{where} is missing {missing[:5]}") + \
            f"; known: {sorted(PR_FIELDS)}"
    num = pr["number"]
    if not isinstance(num, int) or isinstance(num, bool) or not 1 <= num <= 10**9:
        return f"{where}.number is a whole number from 1"
    where = f"prs[{n}] (#{num})"
    for f, cap, refs in (("title", MAX_TITLE, False), ("head", MAX_REF, True), ("base", MAX_REF, True)):
        why = _text_problem(pr[f], f"{where}.{f}", cap, refs)
        if why:
            return why
    if pr["state"] not in STATES:
        return f"{where}.state is one of {', '.join(STATES)}"
    if not isinstance(pr["draft"], bool):
        return f"{where}.draft is true or false"
    a = pr["author"]
    if a is not None and (not isinstance(a, str) or not a or len(a) > MAX_LOGIN or CONTROL.search(a)):
        return f"{where}.author is null or a login of at most {MAX_LOGIN} characters"
    for f, nullable in (("created_at", False), ("updated_at", False), ("merged_at", True), ("closed_at", True)):
        why = _ts_problem(pr[f], f"{where}.{f}", nullable)
        if why:
            return why
    if pr["url"] != f"https://github.com/{repo}/pull/{num}":
        return f"{where}.url must be https://github.com/{repo}/pull/{num}"
    mc = pr["merge_commit"]
    if mc is not None and (not isinstance(mc, str) or not COMMIT.match(mc)):
        return f"{where}.merge_commit is null or a full commit id in lowercase hex"
    if pr["checks"] not in CHECKS:
        return f"{where}.checks is one of {', '.join(CHECKS)}"
    if pr["state"] == "merged" and pr["merged_at"] is None:
        return f"{where} is merged, so merged_at is a time"
    if pr["state"] == "open" and (pr["merged_at"] is not None or pr["closed_at"] is not None):
        return f"{where} is open, so merged_at and closed_at are null"
    if pr["state"] == "closed" and pr["merged_at"] is not None:
        return f"{where} is closed unmerged, so merged_at is null"
    return None


def snapshot_problem(doc: object, stored: bool = False) -> str | None:
    """Why a pushed (or, with `stored`, a kept) snapshot cannot be served, naming the first bad field; None when it can.

    One check for both doors in: the server refuses a push with it, `agent.py
    prs-push` runs it before sending, and a stored STATE/prs.json that fails
    it is shown as unreadable, never half-served.
    """
    fields = STORED_FIELDS if stored else PUSH_FIELDS
    if not isinstance(doc, dict) or set(doc) != fields:
        return 'prs-push sends {"repo": "<owner>/<name>", "window_days": <days>, "prs": [...]}' if not stored \
            else f"a stored snapshot has exactly {sorted(fields)}"
    if not isinstance(doc["repo"], str) or not REPO.match(doc["repo"]):
        return "repo is <owner>/<name> on GitHub"
    w = doc["window_days"]
    if not isinstance(w, int) or isinstance(w, bool) or not 1 <= w <= MAX_DAYS:
        return f"window_days is a whole number from 1 to {MAX_DAYS}"
    prs = doc["prs"]
    if not isinstance(prs, list):
        return "prs is a list"
    if len(prs) > MAX_PRS:
        return f"prs is over {MAX_PRS} pull requests"
    seen: set[int] = set()
    for n, pr in enumerate(prs):
        why = pr_problem(pr, n, doc["repo"])
        if why:
            return why
        if pr["number"] in seen:
            return f"prs[{n}]: #{pr['number']} is listed twice"
        seen.add(pr["number"])
    if stored:
        why = _ts_problem(doc["pushed_at"], "pushed_at", False)
        if why:
            return why
        if doc["by"] is not None and (not isinstance(doc["by"], str) or N.problem(doc["by"])):
            return "by is null or an agent name"
    return None


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def store_snapshot(state: Path, body: dict, by: str | None, at: str | None = None) -> dict:
    """Keep a CHECKED push as STATE/prs.json, whole or not at all, stamped with when it arrived and who sent it.

    Raises ValueError, naming the cap, when it is over MAX_FILE; nothing is written then. A file, link or FIFO
    at prs.json is replaced without being followed; a directory is first set aside, unread and kept.
    """
    doc = {"repo": body["repo"], "window_days": body["window_days"], "prs": body["prs"],
           "pushed_at": at or now(), "by": by}
    data = json.dumps(doc, sort_keys=True, ensure_ascii=False).encode("utf-8")
    if len(data) > MAX_FILE:
        raise ValueError(f"the pushed pull requests are over {MAX_FILE} bytes")
    sfd = os.open(state, STATE_FLAGS)
    try:
        aside = AF.set_aside_dir(sfd, PRS)
        if aside:
            sys.stderr.write(f"console prs: a directory at {Path(state, PRS)} was set aside as {aside}\n")
        AF.write_at(sfd, PRS, data)
    finally:
        os.close(sfd)
    return doc


def load(state: Path) -> dict:
    """What the owner's PRs view shows: the kept push with `pushed` true, or `pushed` false and a note.

    Never raises for the file itself: absent gives NOT_PUSHED; anything that cannot be served (not a plain
    file, unreadable, too large, not JSON, failing the check) is logged with its reason and gives UNREADABLE.
    """
    sfd = os.open(state, STATE_FLAGS)
    try:
        doc, why = _read(sfd)
    finally:
        os.close(sfd)
    if why is None:
        return {"pushed": True, "note": None, **doc}
    if why is NOT_PUSHED:
        return {"pushed": False, "note": NOT_PUSHED}
    sys.stderr.write(f"console prs: {Path(state, PRS)} is {why}\n")
    return {"pushed": False, "note": UNREADABLE}


def _read(sfd: int) -> tuple[dict | None, str | None]:
    """(the checked snapshot, None), (None, NOT_PUSHED) when absent, or (None, why it cannot be served)."""
    try:
        raw = AF.read_at(sfd, PRS, MAX_FILE)
        if raw is None:
            os.stat(PRS, dir_fd=sfd, follow_symlinks=False)   # FileNotFoundError: none was pushed
            return None, "not a plain file"
    except FileNotFoundError:
        return None, NOT_PUSHED
    except OSError as e:   # EACCES, EIO...: as unreadable as a bad file, never an error out of the view
        return None, f"not readable: {e.strerror or type(e).__name__}"
    if len(raw) > MAX_FILE:
        return None, f"over {MAX_FILE} bytes"
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError) as e:
        return None, f"not JSON: {e}"
    why = snapshot_problem(doc, stored=True)
    return (None, why) if why else (doc, None)


# -- the steward's side: gh's JSON into the closed shape (agent.py prs-push) -------------------------------

FAIL = {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE"}
PASS = {"SUCCESS", "NEUTRAL", "SKIPPED"}


def rollup(checks: object) -> str:
    """One word for a PR's checks: failure if any failed, pending if any has not finished, success, or none."""
    if checks is None:
        return "none"
    if not isinstance(checks, list):
        raise PushError("statusCheckRollup is not a list")
    if not checks:
        return "none"
    pending = False
    for c in checks:
        if not isinstance(c, dict):
            raise PushError("a statusCheckRollup entry is not an object")
        if "state" in c and "conclusion" not in c:     # a commit status (StatusContext)
            word = str(c.get("state") or "").upper()
            if word in FAIL:
                return "failure"
            pending = pending or word not in PASS
        else:                                           # a check run (CheckRun)
            if str(c.get("status") or "").upper() != "COMPLETED":
                pending = True
                continue
            word = str(c.get("conclusion") or "").upper()
            if word in FAIL:
                return "failure"
            pending = pending or word not in PASS
    return "pending" if pending else "success"


def _gh_time(v: object, name: str) -> str | None:
    """gh's time, as TS, or None when unset (gh gives null, "", or Go's zero time for an unset one)."""
    if v in (None, "") or (isinstance(v, str) and v.startswith("0001-01-01")):
        return None
    if not isinstance(v, str):
        raise PushError(f"{name} is not a time")
    try:
        t = dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        raise PushError(f"{name} {v[:40]!r} is not a time") from None
    if t.tzinfo is None:
        raise PushError(f"{name} {v[:40]!r} has no time zone")
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _one(row: object, repo: str) -> dict:
    if not isinstance(row, dict):
        raise PushError("a row of gh's output is not an object")
    missing = [f for f in GH_FIELDS.split(",") if f not in row]
    if missing:
        raise PushError(f"gh's output has no {', '.join(missing)}: is gh too old?")
    num = row["number"]
    state = str(row["state"]).lower()
    author = row["author"]
    login = author.get("login") if isinstance(author, dict) else None
    mc = row["mergeCommit"]
    pr = {"number": num, "title": row["title"], "state": state, "draft": row["isDraft"],
          "head": row["headRefName"], "base": row["baseRefName"], "author": login or None,
          "created_at": _gh_time(row["createdAt"], f"#{num} createdAt"),
          "updated_at": _gh_time(row["updatedAt"], f"#{num} updatedAt"),
          "merged_at": _gh_time(row["mergedAt"], f"#{num} mergedAt"),
          "closed_at": _gh_time(row["closedAt"], f"#{num} closedAt"),
          "url": row["url"], "merge_commit": mc.get("oid") if isinstance(mc, dict) else None,
          "checks": rollup(row["statusCheckRollup"])}
    if pr["state"] != "merged":
        pr["merge_commit"] = None   # GitHub can name a test-merge commit on an open PR: it was never merged
    return pr


def from_gh(repo: object, open_rows: object, recent_rows: object, window_days: int) -> dict:
    """The push body from gh's JSON: `gh repo view --json nameWithOwner`, then the open and the recent lists.

    Open PRs first, then merged or closed ones, newest first; a PR in both lists is kept once, as its open
    row. Raises PushError naming what is wrong with gh's output; the result is checked by `snapshot_problem`
    (by the caller and again by the server), so a cap is refused there by name, never truncated here.
    """
    name = repo.get("nameWithOwner") if isinstance(repo, dict) else None
    if not isinstance(name, str) or not REPO.match(name):
        raise PushError("gh repo view gave no nameWithOwner: is this checkout's remote on GitHub?")
    for rows, what in ((open_rows, "open"), (recent_rows, "merged or closed")):
        if not isinstance(rows, list):
            raise PushError(f"gh's list of {what} pull requests is not a list")
    prs, seen = [], set()
    opened = [_one(r, name) for r in open_rows]
    recent = [_one(r, name) for r in recent_rows]
    opened.sort(key=lambda p: p["number"] if isinstance(p["number"], int) else 0, reverse=True)
    recent.sort(key=lambda p: p["merged_at"] or p["closed_at"] or "", reverse=True)
    for pr in opened + recent:
        if pr["number"] in seen:
            continue
        seen.add(pr["number"])
        prs.append(pr)
    return {"repo": name, "window_days": window_days, "prs": prs}


def gh_lists(window_days: int, limit: int, today: dt.date | None = None) -> tuple[list[str], list[str], list[str]]:
    """The three gh command lines `prs-push` runs (without the leading `gh`): the repo, open, and recent PRs."""
    since = (today or dt.datetime.now(dt.timezone.utc).date()) - dt.timedelta(days=window_days)
    return (["repo", "view", "--json", "nameWithOwner"],
            ["pr", "list", "--state", "open", "--limit", str(limit), "--json", GH_FIELDS],
            ["pr", "list", "--state", "closed", "--search", f"closed:>={since.isoformat()}", "--limit", str(limit),
             "--json", GH_FIELDS])

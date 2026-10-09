"""The project's GitHub Issues, as its steward pushes them (owner ask: "close the audit loop at the
issue boundary — rulings should link to the issue that caused the discussion").

Mirrors `prs.py` byte-for-byte in intent and discipline: no server talks to GitHub or starts `gh`;
the steward's `agent.py issues-push` runs `gh issue list` inside its jail, turns the output into the
closed shape below with `from_gh`, and sends it as DATA over the agent socket. The server checks it
with `snapshot_problem`, stamps when it arrived and which agent sent it, and keeps the last push in
`STATE/issues.json`, read and written relative to a held descriptor of STATE (`atfile`), never
resolved by path again.

The browser side extends the PR→ruling title-scan: when a stored issue's title or body mentions a
`qid` literal (`A/Q1`, `A.1.2/Q7`), the owner page threads a chip under the matching locked ruling
labelled "Discussed in" (vs. the existing "Shipped as" for PRs). Nothing in a push reaches a peer.
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

ISSUES = "issues.json"
MAX_FILE = 3 << 20              # a full push under every cap below, in UTF-8, fits with room to spare
MAX_ISSUES = 200                # what the server keeps; the steward asks for at most 2 x MAX_LIMIT
MAX_TITLE = 1024                # GitHub caps a title at 256 characters
MAX_BODY = 65_536               # issue bodies run long (templates, repros); 64 KiB is plenty
MAX_LOGIN = 100
MAX_LABEL = 100
MAX_LABELS = 50                 # per issue
MAX_DAYS = 365
DEFAULT_DAYS = 30
DEFAULT_LIMIT = 50
MAX_LIMIT = 100
STATES = ("open", "closed")
PUSH_FIELDS = {"repo", "window_days", "issues"}
STORED_FIELDS = PUSH_FIELDS | {"pushed_at", "by"}
ISSUE_FIELDS = {"number", "title", "body", "state", "author", "labels",
                "created_at", "updated_at", "closed_at", "url"}
REPO = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}\Z")
TS = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
STATE_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC

NOT_PUSHED = ("No issues yet: the console server never talks to GitHub, so they appear when the steward "
              "pushes them (agent.py issues-push).")
UNREADABLE = ("The stored issues cannot be read, so none are shown: the steward should push them again "
              "(agent.py issues-push).")

GH_FIELDS = "number,title,body,state,author,labels,createdAt,updatedAt,closedAt,url"


class PushError(ValueError):
    """gh's output was not the shape `from_gh` reads: named, and nothing is sent."""


def _ts_problem(v: object, name: str, nullable: bool) -> str | None:
    if v is None and nullable:
        return None
    if not isinstance(v, str) or not TS.match(v):
        return f"{name} is {'null or ' if nullable else ''}a UTC time like 2026-10-01T12:00:00Z"
    return None


def _text_problem(v: object, name: str, cap: int) -> str | None:
    if not isinstance(v, str):
        return f"{name} must be str, not {type(v).__name__}"
    if len(v) > cap:
        return f"{name} is over {cap} characters"
    return None


def _labels_problem(labels: object, where: str) -> str | None:
    if not isinstance(labels, list):
        return f"{where}.labels is a list"
    if len(labels) > MAX_LABELS:
        return f"{where}.labels is over {MAX_LABELS}"
    for i, lab in enumerate(labels):
        if not isinstance(lab, str) or not lab or len(lab) > MAX_LABEL or CONTROL.search(lab):
            return f"{where}.labels[{i}] is a non-empty string up to {MAX_LABEL} chars, no control characters"
    return None


def issue_problem(issue: object, n: int, repo: str) -> str | None:
    where = f"issues[{n}]"
    if not isinstance(issue, dict):
        return f"{where} is not an object"
    if set(issue) != ISSUE_FIELDS:
        extra, missing = sorted(set(issue) - ISSUE_FIELDS), sorted(ISSUE_FIELDS - set(issue))
        return (f"{where} has unknown fields {extra[:5]}" if extra else f"{where} is missing {missing[:5]}") + \
            f"; known: {sorted(ISSUE_FIELDS)}"
    num = issue["number"]
    if not isinstance(num, int) or isinstance(num, bool) or not 1 <= num <= 10**9:
        return f"{where}.number is a whole number from 1"
    where = f"issues[{n}] (#{num})"
    for f, cap in (("title", MAX_TITLE), ("body", MAX_BODY)):
        why = _text_problem(issue[f], f"{where}.{f}", cap)
        if why:
            return why
    if issue["state"] not in STATES:
        return f"{where}.state is one of {', '.join(STATES)}"
    a = issue["author"]
    if a is not None and (not isinstance(a, str) or not a or len(a) > MAX_LOGIN or CONTROL.search(a)):
        return f"{where}.author is null or a login of at most {MAX_LOGIN} characters"
    why = _labels_problem(issue["labels"], where)
    if why:
        return why
    for f, nullable in (("created_at", False), ("updated_at", False), ("closed_at", True)):
        why = _ts_problem(issue[f], f"{where}.{f}", nullable)
        if why:
            return why
    if issue["url"] != f"https://github.com/{repo}/issues/{num}":
        return f"{where}.url must be https://github.com/{repo}/issues/{num}"
    if issue["state"] == "open" and issue["closed_at"] is not None:
        return f"{where} is open, so closed_at is null"
    if issue["state"] == "closed" and issue["closed_at"] is None:
        return f"{where} is closed, so closed_at is a time"
    return None


def snapshot_problem(doc: object, stored: bool = False) -> str | None:
    fields = STORED_FIELDS if stored else PUSH_FIELDS
    if not isinstance(doc, dict) or set(doc) != fields:
        return 'issues-push sends {"repo": "<owner>/<name>", "window_days": <days>, "issues": [...]}' if not stored \
            else f"a stored snapshot has exactly {sorted(fields)}"
    if not isinstance(doc["repo"], str) or not REPO.match(doc["repo"]):
        return "repo is <owner>/<name> on GitHub"
    w = doc["window_days"]
    if not isinstance(w, int) or isinstance(w, bool) or not 1 <= w <= MAX_DAYS:
        return f"window_days is a whole number from 1 to {MAX_DAYS}"
    issues = doc["issues"]
    if not isinstance(issues, list):
        return "issues is a list"
    if len(issues) > MAX_ISSUES:
        return f"issues is over {MAX_ISSUES} issues"
    seen: set[int] = set()
    for n, issue in enumerate(issues):
        why = issue_problem(issue, n, doc["repo"])
        if why:
            return why
        if issue["number"] in seen:
            return f"issues[{n}]: #{issue['number']} is listed twice"
        seen.add(issue["number"])
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
    doc = {"repo": body["repo"], "window_days": body["window_days"], "issues": body["issues"],
           "pushed_at": at or now(), "by": by}
    data = json.dumps(doc, sort_keys=True, ensure_ascii=False).encode("utf-8")
    if len(data) > MAX_FILE:
        raise ValueError(f"the pushed issues are over {MAX_FILE} bytes")
    sfd = os.open(state, STATE_FLAGS)
    try:
        aside = AF.set_aside_dir(sfd, ISSUES)
        if aside:
            sys.stderr.write(f"console issues: a directory at {Path(state, ISSUES)} was set aside as {aside}\n")
        AF.write_at(sfd, ISSUES, data)
    finally:
        os.close(sfd)
    return doc


def load(state: Path) -> dict:
    sfd = os.open(state, STATE_FLAGS)
    try:
        doc, why = _read(sfd)
    finally:
        os.close(sfd)
    if why is None:
        return {"pushed": True, "note": None, **doc}
    if why is NOT_PUSHED:
        return {"pushed": False, "note": NOT_PUSHED}
    sys.stderr.write(f"console issues: {Path(state, ISSUES)} is {why}\n")
    return {"pushed": False, "note": UNREADABLE}


def _read(sfd: int) -> tuple[dict | None, str | None]:
    try:
        raw = AF.read_at(sfd, ISSUES, MAX_FILE)
        if raw is None:
            os.stat(ISSUES, dir_fd=sfd, follow_symlinks=False)
            return None, "not a plain file"
    except FileNotFoundError:
        return None, NOT_PUSHED
    except OSError as e:
        return None, f"not readable: {e.strerror or type(e).__name__}"
    if len(raw) > MAX_FILE:
        return None, f"over {MAX_FILE} bytes"
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError) as e:
        return None, f"not JSON: {e}"
    why = snapshot_problem(doc, stored=True)
    return (None, why) if why else (doc, None)


# -- the steward's side: gh's JSON into the closed shape (agent.py issues-push) --------------------

def _gh_time(v: object, name: str) -> str | None:
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


def _labels_from_gh(labels: object) -> list[str]:
    if labels is None:
        return []
    if not isinstance(labels, list):
        raise PushError("labels is not a list")
    out: list[str] = []
    for lab in labels:
        if isinstance(lab, dict) and isinstance(lab.get("name"), str):
            out.append(lab["name"])
        elif isinstance(lab, str):
            out.append(lab)
        else:
            raise PushError("a label is not an object with a name")
    return out


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
    body = row["body"] if isinstance(row["body"], str) else ""
    # Cap body here (the push cap is a byte cap, but character length matters for the title-scan too).
    if len(body) > MAX_BODY:
        body = body[:MAX_BODY]
    return {
        "number": num,
        "title": row["title"],
        "body": body,
        "state": state,
        "author": login or None,
        "labels": _labels_from_gh(row.get("labels")),
        "created_at": _gh_time(row["createdAt"], f"#{num} createdAt"),
        "updated_at": _gh_time(row["updatedAt"], f"#{num} updatedAt"),
        "closed_at": _gh_time(row["closedAt"], f"#{num} closedAt"),
        "url": row["url"],
    }


def gh_lists(window_days: int, limit: int, today: dt.date | None = None) -> tuple[list[str], list[str], list[str]]:
    """The three gh command lines `issues-push` runs (without the leading `gh`): repo, open, recent closed."""
    since = (today or dt.datetime.now(dt.timezone.utc).date()) - dt.timedelta(days=window_days)
    return (["repo", "view", "--json", "nameWithOwner"],
            ["issue", "list", "--state", "open", "--limit", str(limit), "--json", GH_FIELDS],
            ["issue", "list", "--state", "closed", "--search", f"closed:>={since.isoformat()}",
             "--limit", str(limit), "--json", GH_FIELDS])


def from_gh(repo: object, open_rows: object, recent_rows: object, window_days: int) -> dict:
    name = repo.get("nameWithOwner") if isinstance(repo, dict) else None
    if not isinstance(name, str) or not REPO.match(name):
        raise PushError("gh repo view gave no nameWithOwner: is this checkout's remote on GitHub?")
    for rows, what in ((open_rows, "open"), (recent_rows, "recent closed")):
        if not isinstance(rows, list):
            raise PushError(f"gh's list of {what} issues is not a list")
    seen: dict[int, dict] = {}
    for row in open_rows:
        one = _one(row, name)
        seen[one["number"]] = one
    for row in recent_rows:
        one = _one(row, name)
        seen.setdefault(one["number"], one)
    issues = sorted(seen.values(), key=lambda i: (0 if i["state"] == "open" else 1, -i["number"]))
    return {"repo": name, "window_days": int(window_days), "issues": issues}

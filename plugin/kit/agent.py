#!/usr/bin/env python3
"""The agent's side of the owner console: talk to the server's agent door (a user-only Unix socket).

    agent.py --state DIR inbox [--since SEQ | --all]
                                                the owner's writes from the doorbell, after the agent's cursor
    agent.py --state DIR watch [--since SEQ] [--timeout SECONDS]
                                                block until the owner sends 'process' or 'fork', print it, exit (§7.3)
    agent.py --state DIR view                   print the current view as JSON
    agent.py --state DIR answers [--item ID] [--fork RECORD_ID] [--json]
                                                every question in a scope with every answer it got (§7.6)
    agent.py --state DIR fork-context FORK_ID   the round's bundle for its committee (§6.3, D14)
    agent.py --state DIR reply ITEM TEXT [--reply-to RECORD_ID]
    agent.py --state DIR ask QUESTION.json [...]
                                                append questions (each the record without type or by), in
                                                order; stops at the first refusal and names what was not sent
    agent.py --state DIR working ITEM [ITEM ...]
                                                show the owner "agent active" on these items; the next
                                                `synced` clears it, and it lapses after an hour
    agent.py --state DIR synced [--through SEQ] [--error MSG]
                                                record that the agent has processed the doorbell up to SEQ
    agent.py --state DIR register --project DIR
                                                switch the plugin on for a project (you run this, never a session)

`register` records, in your own registry (~/.config/console-kit/projects.json),
the project, this state dir, and the kit this agent.py lives in. The plugin
acts only in registered projects and takes the paths it runs from there, never
from the repository (§7.7).

`watch` exits 0 with the waiting lines, or 3 when --timeout runs out. The
agent's cursor moves only through `synced --through`, and never backwards, so
a signal that arrives while the agent works is not marked processed.

The server stamps every write from this door `by: agent`.
"""

import argparse
import datetime as dt
import json
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from console_kit import bundle as B  # noqa: E402
from console_kit import doorbell as D  # noqa: E402
from console_kit import registry as R  # noqa: E402
from console_kit import view as V  # noqa: E402
from console_kit.server import agent_request  # noqa: E402


def _get_view(state: Path):
    """The view and items from the agent door, or an exit code after saying why not."""
    try:
        code, out = agent_request(state / "agent.sock", "GET", "/view", None)
    except OSError as e:
        print(f"cannot reach the console server at {state / 'agent.sock'}: {e}", file=sys.stderr)
        return None, 2
    if code != 200:
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return None, 1
    return out, 0


def _answers(state: Path, item: str | None, fork: str | None, as_json: bool) -> int:
    """Print the answers sheet the page shows, from the same view (§7.6)."""
    out, rc = _get_view(state)
    if out is None:
        return rc
    if item is not None and item not in out["items"]:
        print(f"no item {item!r} in the project's item list", file=sys.stderr)
        return 1
    if fork is not None and fork not in out["view"]["forks"]:
        print(f"no fork {fork!r}", file=sys.stderr)
        return 1
    sheet = V.answers_sheet(out["view"], out["items"], item=item, fork=fork)
    print(json.dumps(sheet, indent=2, ensure_ascii=False) if as_json else V.sheet_markdown(sheet), end="")
    return 0


def _fork_context(state: Path, fork: str) -> int:
    out, rc = _get_view(state)
    if out is None:
        return rc
    try:
        print(B.fork_context(out["view"], out["items"], fork), end="")
    except KeyError as e:
        print(e.args[0], file=sys.stderr)
        return 1
    except B.BundleTooLarge as e:
        print(e, file=sys.stderr)
        return 1
    return 0


def _call(state: Path, method: str, path: str, body=None) -> int:
    try:
        code, out = agent_request(state / "agent.sock", method, path, body)
    except OSError as e:
        print(f"cannot reach the console server at {state / 'agent.sock'}: {e}", file=sys.stderr)
        return 2
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0 if code == 200 else 1


def _refused_note(files: list, n: int, why: str | None, rc: int = 1) -> int:
    """A batch ask stopped at files[n]: say what was posted and what was not, so a re-run sends only the rest."""
    if why:
        print(why, file=sys.stderr)
    if len(files) > 1:
        print(f"posted {n} of {len(files)}; not sent: {' '.join(str(f) for f in files[n:])}", file=sys.stderr)
    return rc


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state", type=Path, required=True)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("inbox")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--since", type=int)
    g.add_argument("--all", action="store_true")
    s = sub.add_parser("watch")
    s.add_argument("--since", type=int)
    s.add_argument("--timeout", type=float)
    s.add_argument("--poll", type=float, default=2.0)
    sub.add_parser("view")
    s = sub.add_parser("answers")
    s.add_argument("--item")
    s.add_argument("--fork")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("fork-context")
    s.add_argument("fork")
    s = sub.add_parser("reply")
    s.add_argument("item")
    s.add_argument("text")
    s.add_argument("--reply-to")
    s = sub.add_parser("ask")
    s.add_argument("files", type=Path, nargs="+", metavar="file")
    s = sub.add_parser("working")
    s.add_argument("items", nargs="+", help="the item ids this session is now working on")
    s = sub.add_parser("synced")
    s.add_argument("--through", type=int)
    s.add_argument("--error")
    s = sub.add_parser("register")
    s.add_argument("--project", type=Path, required=True)
    a = ap.parse_args(argv)
    bell = a.state / "inbox.jsonl"
    try:
        return _run(a, bell)
    except R.RegistryError as e:  # an unsafe or unreadable console file, named
        print(str(e), file=sys.stderr)
        return 1


def _run(a, bell: Path) -> int:
    if a.cmd == "register":
        e = R.register(a.project, a.state, Path(__file__).resolve().parent)
        print(f"registered {Path(a.project).resolve()}: state {e['state']}, kit {e['kit']} in {R.location()}")
        return 0
    if a.cmd == "inbox":
        since = 0 if a.all else (a.since if a.since is not None else D.read_cursor(a.state))
        for line in D.pending(bell, since, intents=None):
            print(json.dumps(line, sort_keys=True))
        return 0
    if a.cmd == "watch":
        since = a.since if a.since is not None else D.read_cursor(a.state)
        found = D.watch(bell, since, poll=a.poll, timeout=a.timeout)
        if not found:
            print(f"no 'process' or 'fork' signal after seq {since} within {a.timeout:g}s", file=sys.stderr)
            return 3
        for line in found:
            print(json.dumps(line, sort_keys=True))
        return 0
    if a.cmd == "view":
        return _call(a.state, "GET", "/view")
    if a.cmd == "answers":
        return _answers(a.state, a.item, a.fork, a.json)
    if a.cmd == "fork-context":
        return _fork_context(a.state, a.fork)
    if a.cmd == "reply":
        body = {"item": a.item, "text": a.text, "nonce": secrets.token_urlsafe(12)}
        if a.reply_to:
            body["reply_to"] = a.reply_to
        return _call(a.state, "POST", "/message", body)
    if a.cmd == "working":
        return _call(a.state, "POST", "/working", {"items": a.items})
    if a.cmd == "ask":
        for n, f in enumerate(a.files):
            try:
                body = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                return _refused_note(a.files, n, f"{f} is not a readable JSON question: {e}")
            if not isinstance(body, dict):
                return _refused_note(a.files, n, f"{f} is not a JSON object")
            body.setdefault("nonce", secrets.token_urlsafe(12))
            rc = _call(a.state, "POST", "/question", body)
            if rc:
                return _refused_note(a.files, n, None, rc)
        if len(a.files) > 1:
            print(f"posted {len(a.files)} questions", file=sys.stderr)
        return 0
    if a.through is not None:
        if a.through < 0:
            print("--through takes a doorbell seq, 0 or more", file=sys.stderr)
            return 1
        held = D.write_cursor(a.state, a.through)
        print(f"agent cursor: processed through seq {held}", file=sys.stderr)
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return _call(a.state, "POST", "/cursor", {"last_synced_at": now, "last_error": a.error})


if __name__ == "__main__":
    sys.exit(main())

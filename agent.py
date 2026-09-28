#!/usr/bin/env python3
"""The agent's side of the owner console: talk to the server's agent door (a user-only Unix socket).

    agent.py --state DIR inbox [--since SEQ]    print the owner's writes, from the doorbell file
    agent.py --state DIR view                   print the current view as JSON
    agent.py --state DIR reply ITEM TEXT [--reply-to RECORD_ID]
    agent.py --state DIR ask QUESTION.json      append a question (the record without type or by)
    agent.py --state DIR synced [--error MSG]   record that the agent has read up to now

The server stamps every write from this door `by: agent`.
"""

import argparse
import datetime as dt
import json
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from console_kit.server import agent_request  # noqa: E402


def _call(state: Path, method: str, path: str, body=None) -> int:
    try:
        code, out = agent_request(state / "agent.sock", method, path, body)
    except OSError as e:
        print(f"cannot reach the console server at {state / 'agent.sock'}: {e}", file=sys.stderr)
        return 2
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0 if code == 200 else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state", type=Path, required=True)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("inbox")
    s.add_argument("--since", type=int, default=0)
    sub.add_parser("view")
    s = sub.add_parser("reply")
    s.add_argument("item")
    s.add_argument("text")
    s.add_argument("--reply-to")
    s = sub.add_parser("ask")
    s.add_argument("file", type=Path)
    s = sub.add_parser("synced")
    s.add_argument("--error")
    a = ap.parse_args(argv)

    if a.cmd == "inbox":
        p = a.state / "inbox.jsonl"
        for line in p.read_text(encoding="utf-8").splitlines() if p.exists() else []:
            if json.loads(line)["seq"] > a.since:
                print(line)
        return 0
    if a.cmd == "view":
        return _call(a.state, "GET", "/view")
    if a.cmd == "reply":
        body = {"item": a.item, "text": a.text, "nonce": secrets.token_urlsafe(12)}
        if a.reply_to:
            body["reply_to"] = a.reply_to
        return _call(a.state, "POST", "/message", body)
    if a.cmd == "ask":
        body = json.loads(a.file.read_text(encoding="utf-8"))
        body.setdefault("nonce", secrets.token_urlsafe(12))
        return _call(a.state, "POST", "/question", body)
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return _call(a.state, "POST", "/cursor", {"last_synced_at": now, "last_error": a.error})


if __name__ == "__main__":
    sys.exit(main())

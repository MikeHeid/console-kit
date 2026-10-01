#!/usr/bin/env python3
"""The agent's side of the owner console: talk to the server's agent door (a user-only Unix socket).

    agent.py --state DIR inbox [--since SEQ | --all]
                                                the owner's writes from the doorbell, after the agent's cursor
    agent.py --state DIR watch [--since SEQ] [--timeout SECONDS]
                                                block until the owner sends 'process', 'fork', 'chat' or
                                                'visual', print it, exit
    agent.py --state DIR todo [--full]          what is waiting on the agent, as JSON: forks not done (id,
                                                item, kind, mode, roles), awaiting_agent, chat, visual
                                                requests, inbox qids, seq; filtered from `view`, never
                                                worked out a second way
    agent.py --state DIR view [--item ID] [--since SEQ] [--full]
                                                the current view as JSON; --item narrows to that item and
                                                everything under it (@chat: the chat), --since to what
                                                changed after a store seq
    agent.py --state DIR health                 the server's health as JSON; exit 0 healthy, 1 not, 2 unreachable
    agent.py --state DIR answers [--item ID] [--fork RECORD_ID] [--since SEQ] [--json] [--full]
                                                every question in a scope with every answer it got (§7.6)
    agent.py --state DIR fork-context FORK_ID   the round's bundle for its committee (§6.3, D14)
    agent.py --state DIR check [--json]         why each stale answer is stale, condition by condition (0.5.0)
    agent.py --state DIR reanchor [--dry-run] [--json]
                                                re-anchor stale locks that git history shows were cited
                                                text still in the file; the server computes every anchor
    agent.py --state DIR reply ITEM TEXT [--reply-to RECORD_ID]
    agent.py --state DIR ask QUESTION.json [...]
                                                append questions (each the record without type or by), in
                                                order; stops at the first refusal and names what was not sent
    agent.py --state DIR transcript FORK_ID FILE
                                                store a roar panel's transcript (Markdown, at most 48 KiB)
                                                on the roar fork it ran; one per fork, refused if larger
    agent.py --state DIR visual REQUEST_ID --format mermaid|html --file FILE --doc DOC.md --title TITLE
                                                answer an owner's visual request: the server stores FILE
                                                and its doc in its STATE dir (never in the project) and
                                                shows it on the item
    agent.py --state DIR visual-export --project WORKTREE [--visual ID ...]
                                                copy stored visuals (default: all; identical files are
                                                skipped) into WORKTREE/<visuals_dir>/ and regenerate its
                                                INDEX.md there, to land by PR. WORKTREE is the TOP of
                                                your own git work tree, on a branch (never a folder
                                                inside it, never the server's checkout); a different
                                                file already at a target path is refused. Exits 0 when
                                                every chosen visual was exported, 3 when files were
                                                written but at least one visual was refused (each is
                                                named), 1 when nothing was written, 2 when the server
                                                cannot be reached
    agent.py --state DIR next-step refine|drill --project DIR
                                                print the installed user skill .console-kit.json's
                                                `next_step` names for that kind; exit 1 naming why when
                                                it is unset, not installed, or resolves into the project
    agent.py --state DIR working ITEM [ITEM ...]
                                                show the owner "agent active" on these items; this
                                                session's next `synced` clears it (0.8.2: only its own
                                                marks, by --as name), and it lapses after an hour
    agent.py --state DIR synced [--through SEQ] [--error MSG]
                                                record that the agent has processed the doorbell up to SEQ
    agent.py --state DIR costs collect | show --fork RECORD_ID
                                                collect: sum this project's subagent transcripts (usage, id
                                                and agentType only, deduplicated by message id) into
                                                STATE/costs.jsonl, a sidecar the store never holds;
                                                show: one fork's bill, unattributed subagents apart (K2)
    agent.py --state DIR server add NAME --hostname HOST --aud AUD --port PORT --team-domain TEAM
                       [--root DIR] [--page PATH] [--slug SLUG ...]
                                                host this console on the one console server (K3): writes
                                                server.json beside your registry, never the registry (you
                                                run this, never a session)
    agent.py --state DIR register --project DIR [--steward NAME]
                                                switch the plugin on for a project (you run this, never a session)
    agent.py --state DIR steward [NAME | --clear]
                                                show, set or clear the console's steward in your registry (0.8.3;
                                                you run this, never a session)

**Slim reads (K1).** JSON goes out compact when stdout is not a terminal.
`todo`, `view` and `answers` refuse a read over 64 KiB unless given --full:
they print nothing on stdout, name the narrower command on stderr and exit 4,
since a cut read would look whole.

`register` records, in your own registry (~/.config/console-kit/projects.json),
the project, this state dir, and the kit this agent.py lives in. The plugin
acts only in registered projects and takes the paths it runs from there, never
from the repository (§7.7).

While `watch` waits it records so in watch.json beside the doorbell, and the
owner's console shows "agent listening" until it exits or stops renewing it.

`watch` exits 0 with the waiting lines, or 3 when --timeout runs out. The
agent's cursor moves only through `synced --through`, and never backwards, so
a signal that arrives while the agent works is not marked processed.

The server stamps every write from this door `by: agent`.

**Agent names (0.8.2).** When several sessions share one console, each may
say who it is: `agent.py --as agent-6 ...` (before the subcommand), or
CONSOLE_KIT_AGENT=agent-6 in its environment; `--as` wins. The name is 1 to 32
lowercase letters, digits and single hyphens, starting with a letter, and
neither "agent" nor "owner"; anything else is refused by name (exit 2) and
nothing is sent. The owner's console shows it on the questions, replies,
visuals and transcripts this session writes, and `working`/`synced` then keep
and clear this session's own "agent active" marks, never another's. With no
name, everything is exactly as in 0.8.1, and the unnamed sessions share one
set of marks.

**The steward (0.8.3).** The doorbell has one cursor, so when several
sessions share a console, the user may name one of them its steward, in their
own registry: `agent.py --state DIR steward agent-5` (or `register ...
--steward agent-5`). Then `watch` and `synced` (and `fold.py`) refuse, exit 1,
unless this session's name (`--as`, or CONSOLE_KIT_AGENT) is the steward's; the
refusal names the steward and points at `ask`, `reply` and `working`, which
stay open to every session, named or not. The repository's `.console-kit.json`
cannot set it. With no steward, everything is as in 0.8.2. It is a guardrail
between cooperating sessions of one user, not a security boundary: any process
running as that user can edit the registry or the cursor.

**Session names (0.8.4).** The user may name a Claude Code session by typing
`/console-kit:as agent-5` in it; the plugin records that session's id against
the name in DIR/sessions.jsonl and puts the id in the session's Bash
environment as CONSOLE_KIT_SESSION. This session's name is then, in order:
`--as`, the name recorded for CONSOLE_KIT_SESSION, CONSOLE_KIT_AGENT.
"""

import argparse
import datetime as dt
import json
import os
import secrets
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from console_kit import bundle as B  # noqa: E402
from console_kit import doorbell as D  # noqa: E402
from console_kit import gitseam as G  # noqa: E402
from console_kit import names as N  # noqa: E402
from console_kit import registry as R  # noqa: E402
from console_kit import sessions as SN  # noqa: E402
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
        sys.stdout.write(_json(out))
        return None, 1
    return out, 0


MAX_READ = 64 * 1024  # bytes of UTF-8 a read prints without --full (K1, E4)
TOO_LARGE = 4         # the exit code of a read refused for its size: nothing was printed


def _json(obj) -> str:
    """JSON for stdout: indented for a person at a terminal, compact for anything else (K1, E3)."""
    if sys.stdout.isatty():
        return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False) + "\n"


def _emit(text: str, full: bool, what: str, narrower: str) -> int:
    """Print `text` whole, or refuse it whole when it is over MAX_READ and --full was not given (K1, E4).

    A cut read would look complete when it is not, so nothing is printed on a
    refusal; the message names the size and the narrower command that answers it.
    """
    size = len(text.encode("utf-8"))
    if size > MAX_READ and not full:
        print(f"refused: `{what}` would print {size} bytes, over the {MAX_READ}-byte (64 KiB) cap; nothing was "
              f"printed. Read less with {narrower}, or pass --full to print all of it.", file=sys.stderr)
        return TOO_LARGE
    sys.stdout.write(text)
    return 0


def _slim_view(state: Path, since: bool = False):
    """The view for a slim read, or an exit code after saying why not.

    The client upgrades with the plugin and the server only when restarted, so
    the view can come from an older kit. What the slim reads need is checked
    first; a view lacking it is refused in one line naming the server's version.
    """
    out, rc = _get_view(state)
    if out is None:
        return None, rc
    try:
        V.check_view(out.get("view") if isinstance(out, dict) else None)
        if not isinstance(out.get("items"), dict):
            raise V.ViewTooOld("the payload has no items")
        if since:
            V.check_since(out["view"])
    except V.ViewTooOld as e:
        try:
            _code, health = agent_request(state / "agent.sock", "GET", "/health", None)
            ver = health.get("version", "unknown") if isinstance(health, dict) else "unknown"
        except OSError:
            ver = "unknown"
        print(f"refused: the console server runs kit {ver} and {e}, which this client (kit {_kit_version()}) "
              f"needs; restart the console server so it runs this kit", file=sys.stderr)
        return None, 1
    return out, 0


def _kit_version() -> str:
    from console_kit import __version__
    return __version__


def _view(state: Path, item: str | None, since: int | None, full: bool) -> int:
    """The view, narrowed to an item (E2) and to what changed after a seq (E5), capped (E4)."""
    out, rc = _slim_view(state, since=since is not None)
    if out is None:
        return rc
    if item is not None:
        try:
            out = V.item_view(out, item)
        except KeyError as e:
            print(e.args[0], file=sys.stderr)
            return 1
    if since is not None:
        out = {**out, "view": V.since(out["view"], since)}
    what = "view" + (f" --item {item}" if item is not None else "") + (f" --since {since}" if since is not None else "")
    return _emit(_json(out), full, what, "`todo` (what is waiting), `view --item ID` (one item and everything "
                                          "under it) or `view --since SEQ` (only what changed after a store seq)")


def _todo(state: Path, full: bool) -> int:
    """What is waiting on the agent, filtered from the same view the page shows (E1)."""
    out, rc = _slim_view(state)
    if out is None:
        return rc
    return _emit(_json(V.todo(out["view"])), full, "todo", "`view --item ID` for one item")


def _answers(state: Path, item: str | None, fork: str | None, as_json: bool, since: int | None = None,
             full: bool = False) -> int:
    """Print the answers sheet the page shows, from the same view (§7.6)."""
    out, rc = _slim_view(state, since=since is not None)
    if out is None:
        return rc
    if item is not None and item not in out["items"]:
        print(f"no item {item!r} in the project's item list", file=sys.stderr)
        return 1
    if fork is not None and fork not in out["view"]["forks"]:
        print(f"no fork {fork!r}", file=sys.stderr)
        return 1
    view = out["view"]
    if since is not None:  # only the questions changed after SEQ (E5); the forks stay for --fork
        view = {**view, "questions": V.since(view, since)["questions"]}
    sheet = V.answers_sheet(view, out["items"], item=item, fork=fork)
    return _emit(_json(sheet) if as_json else V.sheet_markdown(sheet), full, "answers",
                 "`answers --item ID`, `answers --fork RECORD_ID` or `answers --since SEQ`")


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


def _check(state: Path, as_json: bool) -> int:
    try:
        code, out = agent_request(state / "agent.sock", "GET", "/check", None)
    except OSError as e:
        print(f"cannot reach the console server at {state / 'agent.sock'}: {e}", file=sys.stderr)
        return 2
    if code != 200 or as_json:
        sys.stdout.write(_json(out))
        return 0 if code == 200 else 1
    stale = out["stale"]
    if out.get("history"):
        print(f"git history: {out['history']}", file=sys.stderr)
    if not stale:
        print("No answer is stale.")
    for qid, s in stale.items():
        print(f"{qid} (anchored by {s['anchored_by']}):")
        for c in s["conditions"]:
            print(f"  {'holds' if c['holds'] else 'FAILS'}: {c['words']}")
            if c.get("diff"):
                print("\n".join("      " + ln for ln in c["diff"].splitlines()))
    return 0


def _reanchor(state: Path, dry_run: bool, as_json: bool) -> int:
    """Ask the server to re-anchor; print what changed (or would), and what stays stale and why."""
    try:
        code, out = agent_request(state / "agent.sock", "POST", "/reanchor", {"dry_run": dry_run})
    except OSError as e:
        print(f"cannot reach the console server at {state / 'agent.sock'}: {e}", file=sys.stderr)
        return 2
    if code != 200 or as_json:
        sys.stdout.write(_json(out))
        return 0 if code == 200 else 1
    if out.get("history"):
        print(f"git history: {out['history']}", file=sys.stderr)
    verb = "would re-anchor" if dry_run else "re-anchored"
    not_done = {p["lock"] for p in out["plan"] if p.get("skipped") or p.get("error")}
    done = [p for p in out["plan"] if p["changes"] and p["lock"] not in not_done]
    for p in out["plan"]:
        tail = "" if p["fresh"] else " (still stale)"
        if p["lock"] in not_done:
            print(f"{p['qid']}: not re-anchored: {p.get('skipped') or p.get('error')}")
            continue
        for c in p["changes"]:
            print(f"{p['qid']}: {verb} {c['from']['path']}: {c['why']}{tail}")
        for c in p["unresolved"]:
            where = c["from"].get("path") or c["from"].get("item")
            print(f"{p['qid']}: left stale, {where}: {c['why']}")
    print(f"{len(done)} of {len(out['plan'])} stale answer(s) {verb}"
          + ("; nothing was written (dry run)" if dry_run else ""), file=sys.stderr)
    return 0


def _call(state: Path, method: str, path: str, body=None, agent: str | None = None) -> int:
    try:
        code, out = agent_request(state / "agent.sock", method, path, body, agent=agent)
    except OSError as e:
        print(f"cannot reach the console server at {state / 'agent.sock'}: {e}", file=sys.stderr)
        return 2
    sys.stdout.write(_json(out))
    return 0 if code == 200 else 1


READ_CAP = 1 << 20  # a transcript or visual file larger than this is refused before it is sent


def _read_text(path: Path, what: str) -> str | None:
    """A local UTF-8 file's text, or None after saying why not. The server applies the real limits."""
    try:
        data = R.read_regular(path, READ_CAP + 1)
    except R.RegistryError as e:
        print(f"{what} {path}: {e}", file=sys.stderr)
        return None
    if data is None:
        print(f"{what} {path} does not exist", file=sys.stderr)
        return None
    if len(data) > READ_CAP:
        print(f"{what} {path} is over {READ_CAP} bytes; refused, not cut", file=sys.stderr)
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        print(f"{what} {path} is not UTF-8 text", file=sys.stderr)
        return None


def _git_top(d: Path) -> Path | None:
    """The top of the git work tree holding `d`, or None when `d` is in none. Read-only: no optional locks.

    Agent side: this runs in `agent.py`, inside the agent's own jail, so it may
    use git (the server may not, CONSOLE-kit/Q23); it still goes through gitseam.
    """
    out = G.run(["rev-parse", "--is-inside-work-tree", "--show-toplevel"], d, timeout=30, text=True)
    if not isinstance(out, str):
        return None
    lines = out.splitlines()
    if len(lines) != 2 or lines[0].strip() != "true":
        return None
    return Path(lines[1]).resolve()


def _visual_export(state: Path, project: Path, ids: list[str]) -> int:
    """Copy stored visuals into PROJECT/<visuals_dir>/ and regenerate its INDEX.md (0.8.1).

    PROJECT is the agent's OWN worktree, on a branch, to be landed by PR. It
    must be the top of a git work tree, and it is refused when it is the
    directory the console server runs from (or that directory's work tree):
    the server's checkout follows main and must never gain untracked files.

    Exit codes (0.8.2): 0 every chosen visual exported (or already there,
    identical); 3 some files written, at least one visual refused and named;
    1 nothing written (refused, or every chosen visual refused); 2 the server
    cannot be reached.
    """
    from console_kit import projectcfg as PC
    from console_kit import visuals as VIS
    dest = Path(project).resolve()
    top = _git_top(dest)
    if top is None:
        print(f"refused: {project} is not a git work tree; export into your own worktree on a branch",
              file=sys.stderr)
        return 1
    if top != dest:
        print(f"refused: {project} is inside the work tree {top}; pass the top of your worktree, since "
              f"visuals_dir is a path from the repository's top", file=sys.stderr)
        return 1
    try:
        code, out = agent_request(state / "agent.sock", "POST", "/visual-export", {"ids": ids})
    except OSError as e:
        print(f"cannot reach the console server at {state / 'agent.sock'}: {e}", file=sys.stderr)
        return 2
    if code != 200:
        print(f"refused: {out.get('error', out)}", file=sys.stderr)
        return 1
    server_root = Path(out["root"]).resolve()
    server_top = _git_top(server_root)
    if dest in (server_root, server_top):
        print(f"refused: {project} is the directory the console server runs from ({server_root}). Its checkout "
              f"follows main, and a file written there blocks the next fast-forward. Export into your own "
              f"worktree on a branch (git worktree add ../visuals-branch -b visuals) and land it by PR",
              file=sys.stderr)
        return 1
    for r in out["refused"]:
        print(f"not exported: visual {r['id']}: {r['why']}", file=sys.stderr)
    if out["refused"] and not out["visuals"]:
        # Every chosen visual was refused: write nothing at all, not even a regenerated INDEX.md.
        print(f"refused: none of the {len(out['refused'])} chosen visual(s) could be exported; nothing was "
              f"written", file=sys.stderr)
        return 1
    try:
        vdir = PC._dir(dest, out["visuals_dir"], "visuals_dir")   # the same jail, now against YOUR tree
        plan = VIS.export_plan(dest, vdir, out["visuals"])
        VIS.check_index(dest, vdir)
        done = VIS.export_write(dest, vdir, plan, out["records"])
    except (PC.ConfigError, VIS.VisualError, OSError) as e:
        print(f"refused, nothing overwritten: {e}", file=sys.stderr)
        return 1
    sys.stdout.write(_json({"project": str(dest), "visuals_dir": vdir, **done, "refused": out["refused"]}))
    if not out["refused"]:
        return 0
    # 3: a partial export, told apart from 1 ("nothing was written") so a caller knows files landed.
    return 3 if done["written"] else 1


def _costs(a) -> int:
    """`costs collect` / `costs show --fork ID` (K2). Reads transcripts here, in the session; the server never does."""
    from console_kit import costs as C
    if a.action == "show" and not a.fork:
        print("costs show needs --fork RECORD_ID", file=sys.stderr)
        return 1
    if a.action == "collect":
        why = steward_refusal(a.state, a.agent, "`costs collect`")   # §8.5: the steward runs it
        if why:
            print(why, file=sys.stderr)
            return 1
    try:
        if a.action == "show":
            rows, bad = C.read_counted(a.state)
            out = C.fork_card(rows, a.fork)
        else:
            lines, stats = C.collect(a.state, agent=a.agent)
            out = {**C.write(a.state, lines), **stats}
            bad = out["dropped_malformed"]
    except (C.CostError, R.RegistryError, OSError) as e:
        print(f"refused: {e}", file=sys.stderr)
        return 1
    if bad:
        print(f"note: {bad} malformed line(s) in {a.state / C.FILE} were dropped", file=sys.stderr)
    sys.stdout.write(_json(out))
    return 0


def _items_push(a) -> int:
    """`items-push` (K3): the adapter runs in THIS process; the one server receives its three results as data."""
    from console_kit import fold as FO
    from console_kit import serverfile as SF
    from console_kit.multiserver import socket_path
    state = os.path.realpath(a.state)
    try:
        found = R.enclosing(a.project)
        if found is None or found[1].get("state") != state:
            raise FO.FoldError(f"{Path(a.project).resolve()} is not a project registered on the console at {state}")
        root = Path(found[0])
        names = [n for n, e in SF.load()["projects"].items() if isinstance(e, dict) and e.get("state") == state]
        if len(names) != 1:
            raise FO.FoldError(f"server.json hosts {len(names)} projects on the console at {state}; "
                               f"run `agent.py --state {state} server add NAME …` first")
        adapter = FO.load_adapter(FO.inside(root, a.adapter, "--adapter"))
        board = getattr(adapter, "board", None)
        body = {"items": adapter.items(), "seed_questions": adapter.seed_questions(),
                "board": board() if callable(board) else None}
    except (FO.FoldError, SF.ServerFileError) as e:
        print(f"refused, nothing sent: {e}", file=sys.stderr)
        return 1
    sock = socket_path(SF.location())
    try:
        code, out = agent_request(sock, "POST", f"/p/{names[0]}/items", body, agent=a.agent)
    except OSError as e:
        print(f"the console server is not answering on {sock}: {e}; nothing was sent", file=sys.stderr)
        return 2
    if code != 200:
        print(f"refused ({code}): {out.get('error')}", file=sys.stderr)
        return 1
    sys.stdout.write(_json(out))
    return 0


def _history_push(a) -> int:
    """`history-push` (Q23 part 2): git runs HERE; the server gets past versions and spec times as data."""
    from console_kit import serverfile as SF
    from console_kit import stewardgit as SG
    from console_kit.multiserver import socket_path
    state = os.path.realpath(a.state)
    found = R.enclosing(a.project)
    if found is None or found[1].get("state") != state:
        print(f"refused, nothing sent: {Path(a.project).resolve()} is not a project registered on the console at "
              f"{state}", file=sys.stderr)
        return 1
    root = Path(found[0])
    # The server that holds this console: its own socket, else the one server (K3) that hosts it.
    sock, prefix = Path(state) / "agent.sock", ""
    if not sock.is_socket():
        try:
            names = [n for n, e in SF.load()["projects"].items() if isinstance(e, dict) and e.get("state") == state]
        except SF.ServerFileError as e:
            print(f"no console server answers for {state}: no {sock}, and {e}", file=sys.stderr)
            return 2
        if len(names) != 1:
            print(f"no console server answers for {state}: no {sock}, and server.json hosts {len(names)} projects "
                  f"there", file=sys.stderr)
            return 2
        sock, prefix = socket_path(SF.location()), f"/p/{names[0]}"
    try:
        code, want = agent_request(sock, "GET", f"{prefix}/history-wants", None, agent=a.agent)
        if code != 200:
            print(f"refused ({code}): {want.get('error')}", file=sys.stderr)
            return 1
        blobs, specs = SG.collect(root, want)
        refused = []
        for b in blobs:
            code, out = agent_request(sock, "POST", f"{prefix}/history-blob", b, agent=a.agent)
            if code != 200:
                refused.append(f"{b['sha256'][:12]}: {out.get('error')}")
        code, out = agent_request(sock, "POST", f"{prefix}/history-specs", {"specs": specs}, agent=a.agent)
        if code != 200:
            refused.append(f"spec times: {out.get('error')}")
    except OSError as e:
        print(f"the console server is not answering on {sock}: {e}", file=sys.stderr)
        return 2
    asked = len(want.get("blobs") or [])
    sys.stdout.write(_json({"blobs": {"asked": asked, "sent": len(blobs) - len(refused), "not_in_history":
                                      asked - len(blobs)},
                            "specs": out if code == 200 else None, "refused": refused}))
    for r in refused:
        print(f"refused: {r}", file=sys.stderr)
    return 1 if refused else 0


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
    ap.add_argument("--as", dest="agent", metavar="NAME",
                    help=f"this session's agent name, e.g. agent-6 (default: this session's /console-kit:as, "
                         f"then ${N.ENV}; none: unnamed, as in 0.8.1)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("inbox")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--since", type=int)
    g.add_argument("--all", action="store_true")
    s = sub.add_parser("watch")
    s.add_argument("--since", type=int)
    s.add_argument("--timeout", type=float)
    s.add_argument("--poll", type=float, default=2.0)
    s = sub.add_parser("todo", description="What is waiting on the agent: forks not done, threads and the chat "
                       "awaiting it, visual requests, the owner's inbox and the store seq (K1).")
    s.add_argument("--full", action="store_true", help=f"print it even when over {MAX_READ} bytes")
    s = sub.add_parser("view")
    s.add_argument("--item", metavar="ID", help="only this item and everything under it (@chat: the chat)")
    s.add_argument("--since", type=int, metavar="SEQ",
                   help="only what changed after this store seq: a question when any record on it (asked, "
                        "answered, locked, re-anchored) is newer, and every stale one, since staleness has no "
                        "seq; a question that became valid again with no new record is not shown")
    s.add_argument("--full", action="store_true", help=f"print it even when over {MAX_READ} bytes")
    sub.add_parser("health")
    s = sub.add_parser("answers")
    s.add_argument("--item")
    s.add_argument("--fork")
    s.add_argument("--json", action="store_true")
    s.add_argument("--since", type=int, metavar="SEQ",
                   help="only questions with a record (asked, answered, locked, re-anchored) after this store "
                        "seq, and every stale one; one that became valid again with no new record is not shown")
    s.add_argument("--full", action="store_true", help=f"print it even when over {MAX_READ} bytes")
    s = sub.add_parser("fork-context")
    s.add_argument("fork")
    s = sub.add_parser("check")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("reanchor")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("reply")
    s.add_argument("item")
    s.add_argument("text")
    s.add_argument("--reply-to")
    s = sub.add_parser("ask")
    s.add_argument("files", type=Path, nargs="+", metavar="file")
    s = sub.add_parser("transcript")
    s.add_argument("fork")
    s.add_argument("file", type=Path)
    s = sub.add_parser("visual")
    s.add_argument("request")
    s.add_argument("--format", required=True, choices=("mermaid", "html"))
    s.add_argument("--file", type=Path, required=True)
    s.add_argument("--doc", type=Path, required=True)
    s.add_argument("--title", required=True)
    s = sub.add_parser("visual-export", description=(
        "Copy stored visuals into PROJECT/<visuals_dir>/ and regenerate its INDEX.md, to land by PR. PROJECT "
        "must be the top of your own git work tree (visuals_dir is a path from the repository's top), never a "
        "folder inside it and never the server's checkout. It exits 0 when every chosen visual was exported, "
        "exits 3 when files were written but at least one visual was refused (each refusal is named), exits 1 "
        "when nothing was written, and exits 2 when the server cannot be reached."))
    s.add_argument("--project", type=Path, required=True,
                   help="YOUR worktree: the TOP of a git work tree, never a folder inside it or the server's checkout")
    s.add_argument("--visual", action="append", default=[], metavar="ID",
                   help="a visual record id; repeat for more (default: every stored visual)")
    s = sub.add_parser("next-step")
    s.add_argument("kind", choices=("refine", "drill"))
    s.add_argument("--project", type=Path, required=True)
    s = sub.add_parser("working")
    s.add_argument("items", nargs="+", help="the item ids this session is now working on")
    s = sub.add_parser("synced")
    s.add_argument("--through", type=int)
    s.add_argument("--error")
    s = sub.add_parser("register")
    s.add_argument("--project", type=Path, required=True)
    s.add_argument("--steward", metavar="NAME",
                   help="the one session that may watch, sync and fold this console (0.8.3; default: keep it)")
    s = sub.add_parser("steward", description="Show, set or clear this console's steward in your own registry "
                       "(0.8.3). You run this, never a session.")
    g = s.add_mutually_exclusive_group()
    g.add_argument("name", nargs="?", help="the steward's agent name, e.g. agent-5")
    g.add_argument("--clear", action="store_true", help="no steward: every session may watch, sync and fold")
    s = sub.add_parser("costs", description="The cost sidecar (K2): `collect` sums this project's subagent "
                       "transcripts into STATE/costs.jsonl; `show --fork ID` prints that fork's bill.")
    s.add_argument("action", choices=("collect", "show"))
    s.add_argument("--fork", help="show: the fork record id whose tagged subagents to total")
    s = sub.add_parser("items-push", description="Run this project's adapter HERE, in your own process, and send "
                       "its items, seed questions and board to the one console server as data (K3, §3.6). The "
                       "server never runs project code.")
    s.add_argument("--adapter", required=True, help="the adapter, a path inside the project")
    s.add_argument("--project", type=Path, default=Path.cwd(), help="the project root (default: here)")
    s = sub.add_parser("history-push", description="Read git HERE, in your own process, for what the console "
                       "server asks (the version each stale lock was taken against, the cited specs' last-commit "
                       "times) and send it as data. The server starts no git (CONSOLE-kit/Q23).")
    s.add_argument("--project", type=Path, default=Path.cwd(), help="the project root (default: here)")
    s = sub.add_parser("server", description="Host this console on the one console server (K3): `add NAME` "
                       "writes the project to server.json beside your registry. You run this, never a session.")
    ss = s.add_subparsers(dest="server_cmd", required=True)
    s = ss.add_parser("add")
    s.add_argument("project_name", metavar="NAME", help="the project's name, e.g. myproject (the agent-name shape)")
    s.add_argument("--hostname", required=True, help="the project console's public hostname, a bare DNS name")
    s.add_argument("--aud", required=True, help="the project's Access application AUD tag")
    s.add_argument("--port", type=int, required=True, help="the project's owner-door loopback port")
    s.add_argument("--team-domain", required=True, help="e.g. yourteam.cloudflareaccess.com")
    s.add_argument("--root", type=Path, help="the main root, when several are registered on --state")
    s.add_argument("--page", default="index.html", help="the host page, a path under the root")
    s.add_argument("--slug", action="append", dest="slugs", metavar="SLUG",
                   help="an extra transcript slug for the cost collector; repeat for more")
    a = ap.parse_args(argv)
    # The agent name: --as, then this session's /console-kit:as (0.8.4), then CONSOLE_KIT_AGENT (0.8.2);
    # an empty variable counts as unset.
    a.agent, where = SN.resolve(a.agent, a.state)
    if a.agent is not None:
        why = N.problem(a.agent)
        if why:
            print(f"agent.py: {where}: {why}; nothing was sent", file=sys.stderr)
            return 2
    for given in (getattr(a, "steward", None), getattr(a, "name", None)):
        if given is not None and N.problem(given):
            print(f"agent.py: steward: {N.problem(given)}; nothing was written", file=sys.stderr)
            return 2
    bell = a.state / "inbox.jsonl"
    try:
        return _run(a, bell)
    except R.RegistryError as e:  # an unsafe or unreadable console file, named
        print(str(e), file=sys.stderr)
        return 1


def steward_refusal(state: Path, agent: str | None, what: str) -> str | None:
    """Why this session may not `what` (watch, synced, the fold), or None when it may (0.8.3).

    The check lives HERE, in the client, and not on the agent socket: `watch`
    reads the doorbell file and writes its heartbeat without ever calling the
    server, `synced` moves the cursor file before its one POST, and the fold
    reads the store file directly. A server check would see only that POST.
    Raises R.RegistryError when the registry cannot be read or names two
    stewards: a lock the user set does not quietly switch itself off.
    """
    name = R.steward_for_state(state)
    if name is None or agent == name:
        return None
    who = f"this session ({agent})" if agent else "this session (unnamed)"
    return (f"refused: {what} belongs to this console's steward, {name}, and {who} is not it. Only the steward "
            f"watches the doorbell, marks it synced and folds answers. Use `ask`, `reply` and `working` instead, "
            f"signed with --as YOUR-NAME: the steward handles the owner's requests. (The steward is set in your "
            f"own registry, {R.location()}, with `agent.py steward`; a session named {name} runs as it: "
            f"--as {name}, /console-kit:as {name} typed in the session, or {N.ENV}={name}.)")


def _run(a, bell: Path) -> int:
    if a.cmd == "register":
        e = R.register(a.project, a.state, Path(__file__).resolve().parent, steward=a.steward)
        print(f"registered {Path(a.project).resolve()}: state {e['state']}, kit {e['kit']}"
              + (f", steward {e[R.STEWARD]}" if R.STEWARD in e else "") + f" in {R.location()}")
        return 0
    if a.cmd == "steward":
        if a.name is None and not a.clear:
            name = R.steward_for_state(a.state)
            print(f"steward: {name}" if name else "no steward: every session may watch, sync and fold")
            return 0
        roots = R.set_steward(a.state, None if a.clear else a.name)
        print((f"steward {a.name}" if a.name else "no steward") + f" for the console at "
              f"{Path(a.state).resolve()}: {', '.join(roots)} in {R.location()}")
        return 0
    if a.cmd == "costs":
        return _costs(a)
    if a.cmd == "items-push":
        return _items_push(a)
    if a.cmd == "history-push":
        return _history_push(a)
    if a.cmd == "server":
        from console_kit import serverfile as SF
        try:   # the name is the user's word on this command line, never a key of the repository's config
            e = SF.add(a.project_name, a.state, a.hostname, a.aud, a.port, a.team_domain, root=a.root,
                       page=a.page, slugs=a.slugs)
        except SF.ServerFileError as err:
            print(f"refused, nothing written: {err}", file=sys.stderr)
            return 1
        print(f"server: project {a.project_name} on port {e['port']} ({e['hostname']}), state {e['state']}, "
              f"root {e['root']} in {SF.location()}")
        return 0
    if a.cmd in ("watch", "synced"):
        why = steward_refusal(a.state, a.agent, f"`{a.cmd}`")
        if why:
            print(why, file=sys.stderr)
            return 1
    if a.cmd == "inbox":
        since = 0 if a.all else (a.since if a.since is not None else D.read_cursor(a.state))
        for line in D.pending(bell, since, intents=None):
            print(json.dumps(line, sort_keys=True))
        return 0
    if a.cmd == "watch":
        since = a.since if a.since is not None else D.read_cursor(a.state)
        beat = D.Heartbeat(a.state, a.poll)  # the owner sees "agent listening" while this waits (0.6.0)
        # A session that ends kills its watch with SIGTERM or SIGHUP; turn those into an
        # ordinary exit so `finally` says "stopped" now, rather than the owner seeing
        # "listening" until the promise lapses. SIGKILL cannot be caught: that is the lapse's job.
        old = {sig: signal.signal(sig, lambda n, _f: sys.exit(128 + n)) for sig in (signal.SIGTERM, signal.SIGHUP)}
        try:
            found = D.watch(bell, since, poll=a.poll, timeout=a.timeout, heartbeat=beat)
        finally:  # woken, timed out, or interrupted: the owner is told at once, not when the promise lapses
            beat.stop()
            for sig, h in old.items():
                signal.signal(sig, h)
        if not found:
            print(f"no 'process', 'fork', 'chat' or 'visual' signal after seq {since} within {a.timeout:g}s",
                  file=sys.stderr)
            return 3
        for line in found:
            print(json.dumps(line, sort_keys=True))
        return 0
    if a.cmd == "todo":
        return _todo(a.state, a.full)
    if a.cmd == "view":
        return _view(a.state, a.item, a.since, a.full)
    if a.cmd == "health":
        return _call(a.state, "GET", "/health")
    if a.cmd == "answers":
        return _answers(a.state, a.item, a.fork, a.json, a.since, a.full)
    if a.cmd == "fork-context":
        return _fork_context(a.state, a.fork)
    if a.cmd == "check":
        return _check(a.state, a.json)
    if a.cmd == "reanchor":
        return _reanchor(a.state, a.dry_run, a.json)
    if a.cmd == "reply":
        body = {"item": a.item, "text": a.text, "nonce": secrets.token_urlsafe(12)}
        if a.reply_to:
            body["reply_to"] = a.reply_to
        return _call(a.state, "POST", "/message", body, agent=a.agent)
    if a.cmd == "working":
        return _call(a.state, "POST", "/working", {"items": a.items}, agent=a.agent)
    if a.cmd == "next-step":
        from console_kit import projectcfg as PC
        try:
            cfg = PC.load(a.project)
            name = cfg.next_step.get(a.kind)
            if not name:
                raise PC.ConfigError(f".console-kit.json names no next_step skill for {a.kind!r}")
            path = PC.resolve_skill(name, a.project)
        except PC.ConfigError as e:
            print(f"refused: {e}", file=sys.stderr)
            return 1
        print(json.dumps({"kind": a.kind, "skill": name, "skill_md": str(path)}))
        return 0
    if a.cmd == "transcript":
        text = _read_text(a.file, "transcript")
        if text is None:
            return 1
        return _call(a.state, "POST", "/transcript", {"fork": a.fork, "text": text,
                                                       "nonce": secrets.token_urlsafe(12)}, agent=a.agent)
    if a.cmd == "visual":
        content, doc = _read_text(a.file, "visual"), _read_text(a.doc, "doc")
        if content is None or doc is None:
            return 1
        return _call(a.state, "POST", "/visual", {"request": a.request, "format": a.format, "title": a.title,
                                                  "content": content, "text": doc,
                                                  "nonce": secrets.token_urlsafe(12)}, agent=a.agent)
    if a.cmd == "visual-export":
        return _visual_export(a.state, a.project, a.visual)
    if a.cmd == "ask":
        for n, f in enumerate(a.files):
            try:
                body = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                return _refused_note(a.files, n, f"{f} is not a readable JSON question: {e}")
            if not isinstance(body, dict):
                return _refused_note(a.files, n, f"{f} is not a JSON object")
            body.setdefault("nonce", secrets.token_urlsafe(12))
            rc = _call(a.state, "POST", "/question", body, agent=a.agent)
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
    return _call(a.state, "POST", "/cursor", {"last_synced_at": now, "last_error": a.error}, agent=a.agent)


if __name__ == "__main__":
    sys.exit(main())

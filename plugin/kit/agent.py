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
    agent.py --state DIR synced [--through SEQ] [--rx-through RX] [--error MSG]
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
                                                run this, never a session); it also writes the project's
                                                agent token to tokens/NAME beside server.json (K4)
    agent.py --state DIR server token rotate NAME
                                                a new token for the project; the old one is refused at once
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

**The one server and project tokens (K4).** When the console is hosted on the
one console server (`server add`), agent.py takes its project from the
directory it runs in: the registered root holding it names the console, and
server.json names that console's project and its token file,
tokens/NAME beside server.json. --state must name that same console, or
nothing is sent; there is no --token and no --project flag. The token file is
read on every call, so `server token rotate` needs no session restart, and the
token is never printed. A console server.json does not host keeps its own
STATE/agent.sock, as before.

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


class DoorRefused(Exception):
    """agent.py will not send anything: the project it runs in and the one it was told do not agree (K4)."""


class _Door:
    """Where this call goes: a per-project server's STATE/agent.sock, or the one server's `/p/<project>`.

    On the one server the token is read from its file on EVERY call (K4, AC4.3),
    so a rotation needs no session restart; it is sent as a bearer header and
    never printed, logged or put in a message.
    """

    def __init__(self, sock: Path, project: str | None = None) -> None:
        self.sock, self.project = sock, project

    def request(self, method: str, path: str, body=None, agent: str | None = None):
        if self.project is None:
            return agent_request(self.sock, method, path, body, agent=agent)
        from console_kit import serverfile as SF
        try:
            tok = SF.read_token(self.project)
        except SF.ServerFileError as e:
            raise DoorRefused(f"refused, nothing sent: {e}") from None
        return agent_request(self.sock, method, f"/p/{self.project}{path}", body, agent=agent, token=tok)


_DOOR: _Door | None = None   # set by main() once the working directory has chosen the project


def _door(state: Path) -> _Door:
    return _DOOR if _DOOR is not None else _Door(Path(state) / "agent.sock")


def _req(state: Path, method: str, path: str, body=None, agent: str | None = None):
    return _door(state).request(method, path, body, agent=agent)


def _where(state: Path) -> str:
    d = _door(state)
    return f"{d.sock} (project {d.project})" if d.project else str(d.sock)


def resolve_door(state: Path) -> _Door:
    """Pick the door from WHERE this process runs, never from what it was asked (K4, spec §3.5, AC4.5).

    The registered root holding the working directory names a console (the
    registry lookup the plugin already does); server.json maps that console to
    a project, and the project to its token file. When either the working
    directory's console or --state's is hosted on the one server: run outside
    every registered root, or with --state naming another console, this
    refuses, naming both, before anything is sent. There is no --token and no
    --project flag. A console no server.json entry hosts keeps its own
    per-project socket, STATE/agent.sock, exactly as before the one server.
    """
    from console_kit import serverfile as SF
    from console_kit.multiserver import socket_path
    s = os.path.realpath(state)
    try:
        hosted = {os.path.realpath(e["state"]): n for n, e in SF.load()["projects"].items()
                  if isinstance(e, dict) and isinstance(e.get("state"), str)}
    except SF.ServerFileError as e:
        raise DoorRefused(f"refused, nothing sent: {e}") from None
    cwd = Path.cwd()
    here = R.enclosing(cwd)
    here_state = os.path.realpath(here[1]["state"]) if here and isinstance(here[1].get("state"), str) else None
    if s not in hosted and here_state not in hosted:
        return _Door(Path(state) / "agent.sock")
    if here is None:
        raise DoorRefused(f"refused, nothing sent: {cwd} is inside no registered project root. agent.py takes its "
                          f"project from the directory it runs in; run it from inside your project's checkout")
    if here_state != s:
        raise DoorRefused(f"refused, nothing sent: --state {s} is not this project's console. {cwd} is in "
                          f"{here[0]}, registered on the console at {here_state}; agent.py takes its project from "
                          f"the directory it runs in, so pass that console's --state or run it from the other "
                          f"project's checkout")
    return _Door(socket_path(SF.location()), hosted[s])


def _get_view(state: Path):
    """The view and items from the agent door, or an exit code after saying why not."""
    try:
        code, out = _req(state, "GET", "/view", None)
    except OSError as e:
        print(f"cannot reach the console server at {_where(state)}: {e}", file=sys.stderr)
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
            _code, health = _req(state, "GET", "/health", None)
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
    else:   # a proposal's text can be 40 KB: a whole read names it by digest, `--item` prints it
        out = {**out, "view": V.slim_refactor(out["view"])}
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
    view = out["view"] if item is not None else V.slim_refactor(out["view"])   # as `view`: --item keeps the text
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
        code, out = _req(state, "GET", "/check", None)
    except OSError as e:
        print(f"cannot reach the console server at {_where(state)}: {e}", file=sys.stderr)
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
        code, out = _req(state, "POST", "/reanchor", {"dry_run": dry_run})
    except OSError as e:
        print(f"cannot reach the console server at {_where(state)}: {e}", file=sys.stderr)
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
        code, out = _req(state, method, path, body, agent=agent)
    except OSError as e:
        print(f"cannot reach the console server at {_where(state)}: {e}", file=sys.stderr)
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
        code, out = _req(state, "POST", "/visual-export", {"ids": ids})
    except OSError as e:
        print(f"cannot reach the console server at {_where(state)}: {e}", file=sys.stderr)
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
    """`items-push` (K3, Q24): the adapter runs in THIS process; the server receives its three results as data.

    Through the door the working directory chose (K4): the one server, with
    this project's token, or a console's own single server (STATE/agent.sock).
    Neither server ever runs the adapter itself.
    """
    from console_kit import fold as FO
    state = os.path.realpath(a.state)
    door = _door(a.state)
    found = R.enclosing(a.project)
    if found is None or found[1].get("state") != state:
        print(f"refused, nothing sent: {Path(a.project).resolve()} is not a project registered on the console at "
              f"{state}", file=sys.stderr)
        return 1
    root = Path(found[0])
    if door.project is None and not door.sock.is_socket():   # before the adapter runs: nothing for no server
        print(f"no console server answers for {state}: no {door.sock}, and server.json hosts no project there; "
              f"nothing was sent", file=sys.stderr)
        return 2
    try:
        adapter = FO.load_adapter(FO.inside(root, a.adapter, "--adapter"))
        board = getattr(adapter, "board", None)
        body = {"items": adapter.items(), "seed_questions": adapter.seed_questions(),
                "board": board() if callable(board) else None}
    except FO.FoldError as e:
        print(f"refused, nothing sent: {e}", file=sys.stderr)
        return 1
    try:
        code, out = door.request("POST", "/items", body, agent=a.agent)
    except OSError as e:
        print(f"the console server is not answering on {door.sock}: {e}; nothing was sent", file=sys.stderr)
        return 2
    if code != 200:
        print(f"refused ({code}): {out.get('error')}", file=sys.stderr)
        return 1
    sys.stdout.write(_json(out))
    # 0.9.13: --sync-dashboard DIR inserts a <details id="item-X" data-ck-item="X"> stub into the given
    # dashboard page for every item not already present, nested under its parent. Idempotent.
    if getattr(a, "sync_dashboard", None):
        _sync_dashboard_write(root, a.sync_dashboard, body["items"])
    return 0


def _sync_dashboard(a) -> int:
    """0.9.13: insert a stub section into the dashboard page for every item not already there."""
    from console_kit import fold as FO
    state = os.path.realpath(a.state)
    found = R.enclosing(a.project)
    if found is None or found[1].get("state") != state:
        print(f"refused: {Path(a.project).resolve()} is not a project registered on the console at {state}",
              file=sys.stderr)
        return 1
    root = Path(found[0])
    try:
        adapter = FO.load_adapter(FO.inside(root, a.adapter, "--adapter"))
        items = adapter.items()
    except FO.FoldError as e:
        print(f"refused, nothing written: {e}", file=sys.stderr)
        return 1
    return _sync_dashboard_write(root, a.page, items)


def _sync_dashboard_write(root: Path, page_rel: str, items: dict) -> int:
    """Shared write-step for `sync-dashboard` and `items-push --sync-dashboard`."""
    from console_kit import scaffold as SC
    page = (Path(root) / page_rel).resolve()
    if Path(root).resolve() not in page.parents:
        print(f"refused: {page} is not inside {root}", file=sys.stderr)
        return 1
    try:
        before = page.read_text(encoding="utf-8") if page.exists() else ""
    except OSError as e:
        print(f"refused: cannot read {page}: {e}", file=sys.stderr)
        return 1
    after, added = SC.sync_items_block(before or "<!doctype html>\n<html><body>\n</body></html>\n", items)
    if not added and before:
        print("sync-dashboard: every item already on the page; nothing changed.")
        return 0
    try:
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(after, encoding="utf-8")
    except OSError as e:
        print(f"refused: cannot write {page}: {e}", file=sys.stderr)
        return 1
    print(f"sync-dashboard: {len(added)} item{'' if len(added) == 1 else 's'} inserted into "
          f"{page.relative_to(Path(root)).as_posix()}:")
    for i in added:
        print(f"  + {i}")
    print()
    print("Next: review the diff, `git commit`, then `agent.py page-snapshot --path "
          f"{Path(page_rel).as_posix()}` and press 'Use this page' in the console.")
    return 0


REVIEWED_FALLBACK = "origin/main"   # Q28: used when origin/HEAD does not resolve
GIT_SECONDS = 60


def _scaffold_dashboard(a) -> int:
    """0.9.3: write a dashboard page and inject board() into the project's adapter.

    Idempotent. The page is wrapped in `scaffold:<name> start/end` markers so re-running adds missing
    sections without clobbering hand edits; `board()` is only added when it is absent.
    """
    from console_kit import scaffold as SC
    try:
        plan = SC.prepare(a.project, a.page, a.adapter, a.name or Path(a.project).resolve().name)
        touched = SC.apply(plan)
    except SC.ScaffoldError as e:
        print(f"refused, nothing written: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"refused: {e}", file=sys.stderr)
        return 1
    if not touched:
        print("scaffold: already in place; nothing to write.")
        return 0
    for line in touched:
        print(line)
    print()
    print("Next:")
    print("  1. Review the page and the adapter; both are yours to edit.")
    print(f"  2. Commit so page-snapshot can read it: `git add -A && git commit -m 'scaffold dashboard'`.")
    print(f"  3. python3 agent.py --state <STATE> page-snapshot --path {Path(a.page).as_posix()}")
    print("     then press 'Use this page' in the console.")
    print(f"  4. python3 agent.py --state <STATE> items-push --adapter {Path(a.adapter).as_posix()}")
    print("  5. (Optional) keep the values fresh:")
    print(f"     python3 agent.py --state <STATE> items-watch --adapter {Path(a.adapter).as_posix()}")
    return 0


def _items_watch(a) -> int:
    """0.9.3: poll the project's items + adapter and re-run items-push whenever anything changes.

    Watches `--adapter`, `.console-kit/items.json`, `.console-kit.json`, and any extra paths named by
    `--watch` (comma-separated, project-relative). An initial push runs at startup; subsequent pushes
    fire when any file's (mtime, size) fingerprint changes. Ctrl+C exits cleanly. No file-watcher
    dependency: a plain stat loop at `--interval` seconds (default 5).
    """
    import time
    import argparse as _ap
    root = Path(a.project).resolve()
    adapter_rel = Path(a.adapter).as_posix()
    adapter_abs = (root / a.adapter).resolve() if not Path(a.adapter).is_absolute() else Path(a.adapter)

    paths: list[Path] = [adapter_abs]
    for rel in (".console-kit/items.json", ".console-kit.json"):
        paths.append((root / rel).resolve())
    for extra in (a.watch or "").split(","):
        extra = extra.strip()
        if extra:
            paths.append((root / extra).resolve() if not Path(extra).is_absolute() else Path(extra))

    def fingerprint() -> dict[str, tuple[float, int] | None]:
        out: dict[str, tuple[float, int] | None] = {}
        for p in paths:
            try:
                st = p.stat()
                out[str(p)] = (st.st_mtime, st.st_size)
            except FileNotFoundError:
                out[str(p)] = None
        return out

    print(f"items-watch: {len(paths)} path(s) under {root}, every {a.interval:g}s")
    for p in paths:
        print(f"  {p}")
    print()
    seen: dict[str, tuple[float, int] | None] = {}
    interval = max(1.0, min(300.0, float(a.interval)))
    try:
        while True:
            fp = fingerprint()
            if fp != seen:
                # Build a Namespace with the same fields _items_push reads; keep agent name through.
                ns = _ap.Namespace(state=a.state, project=root, adapter=adapter_rel, agent=a.agent)
                rc = _items_push(ns)
                if rc != 0:
                    print(f"items-watch: push failed (rc={rc}); will retry on the next change",
                          file=sys.stderr)
                seen = fp
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nitems-watch: stopped")
        return 0


def _resolve_reviewed(root: Path) -> str:
    """0.9.1: the remote's default branch, e.g. `origin/main` or `origin/dev`.

    Reads `git symbolic-ref --short refs/remotes/origin/HEAD`, so a project whose default branch is not
    `main` (`claude/foo-bar`, `dev`, `trunk`, ...) still has a working `page-snapshot` default. Falls back
    to `origin/main` when the symbolic ref is absent — the owner then needs to pass `--from-ref` or run
    `git remote set-head origin --auto` once.
    """
    out = G.run(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], root, timeout=GIT_SECONDS, text=True)
    if out:
        s = out.strip()
        if s:
            return s
    return REVIEWED_FALLBACK


def _page_snapshot(a) -> int:
    """`page-snapshot` (Q28): the page read from a COMMIT here, never from the working tree; sent as data.

    The server stores what it is given and never runs git. So the guard is
    here: the commit must be an ancestor of origin's default branch (reviewed
    and merged) unless `--unreviewed` says otherwise, and then the footer says
    so. The default is read from `origin/HEAD` (0.9.1), so a project whose
    default branch is `claude/foo-bar` or `dev` works without `--from-ref`.
    The page is the blob at `--path` in that commit, a plain file (never a
    link), read with `git cat-file`; an edit in the working tree never
    reaches it.
    """
    import base64
    from console_kit import pagesnap as PS
    from console_kit import serverfile as SF

    def refuse(why: str, rc: int = 1) -> int:
        print(f"refused, nothing sent: {why}", file=sys.stderr)
        return rc

    state = os.path.realpath(a.state)
    found = R.enclosing(a.project)
    if found is None or found[1].get("state") != state:
        return refuse(f"{Path(a.project).resolve()} is not a project registered on the console at {state}")
    root = Path(found[0])
    door = _door(a.state)
    if door.project is None and not door.sock.is_socket():
        return refuse(f"no console server answers for {state}: no {door.sock}, and server.json hosts no project "
                      f"there", 2)
    path = a.path
    if path is None and door.project is not None:   # the one server: server.json's page is the default
        path = SF.load()["projects"].get(door.project, {}).get("page")
    if path is None:
        return refuse("pass --path, the page's path in the repository (e.g. docs/dashboard/index.html)")
    why = SF._page_problem(path)
    if why:
        return refuse(why)
    reviewed = _resolve_reviewed(root)   # 0.9.1: origin's default branch, not hard-coded
    from_ref = a.from_ref or reviewed
    if not PS.REF.match(from_ref):
        return refuse(f"--from-ref {from_ref!r}: a ref of plain characters, such as origin/main or a tag")
    out = G.run(["rev-parse", "--verify", "--quiet", f"{from_ref}^{{commit}}"], root, timeout=GIT_SECONDS,
                text=True)
    if not out:
        return refuse(f"{from_ref} names no commit in {root}; for {reviewed}, run `git fetch` first")
    commit = out.strip()
    if not a.unreviewed:
        if not G.run(["rev-parse", "--verify", "--quiet", f"{reviewed}^{{commit}}"], root, timeout=GIT_SECONDS):
            return refuse(f"{reviewed} is not in {root}: run `git fetch`, or pass --unreviewed to send a page "
                          f"nobody reviewed (the footer will say so)")
        if G.run(["merge-base", "--is-ancestor", commit, reviewed], root, timeout=GIT_SECONDS) is None:
            return refuse(f"{from_ref} @ {commit[:12]} is not in {reviewed}, so it was not reviewed and merged. "
                          f"Merge it first, or pass --unreviewed (the footer will say \"unreviewed ref\")")
    a.from_ref = from_ref   # the body echoes it below; keep it consistent after the default resolved
    entry = G.run(["ls-tree", "-z", commit, "--", path], root, timeout=GIT_SECONDS)
    head, _, name = (entry or b"").rstrip(b"\0").partition(b"\t")
    fields = head.split()
    if name.decode("utf-8", "replace") != path or len(fields) != 3 or fields[1] != b"blob" \
            or fields[0] not in (b"100644", b"100755"):
        return refuse(f"{path} is not a plain file at {a.from_ref} @ {commit[:12]} (absent, a link or a folder)")
    raw = G.run(["cat-file", "blob", fields[2].decode("ascii")], root, timeout=GIT_SECONDS)
    if raw is None:
        return refuse(f"git could not read {path} at {commit[:12]}")
    body = {"content": base64.b64encode(raw).decode("ascii"), "ref": a.from_ref, "commit": commit, "path": path,
            "reviewed": not a.unreviewed}
    why = PS.snapshot_problem(body)   # the server's own check, here first, so a bad page is named before sending
    if why:
        return refuse(why)
    try:
        code, out = door.request("POST", "/page-snapshot", body, agent=a.agent)
    except OSError as e:
        return refuse(f"the console server is not answering on {door.sock}: {e}", 2)
    if code != 200:
        print(f"refused ({code}): {out.get('error')}", file=sys.stderr)
        return 1
    sys.stdout.write(_json(out))
    # Q29: staged only. Nothing served changes until the owner presses the button; no agent can.
    print(f"staged {commit[:12]}: the console shows it as a proposal; it is served once the owner presses "
          f"\"{PS.USE}\"", file=sys.stderr)
    return 0


def _history_push(a) -> int:
    """`history-push` (Q23 part 2): git runs HERE; the server gets past versions and spec times as data."""
    from console_kit import stewardgit as SG
    state = os.path.realpath(a.state)
    found = R.enclosing(a.project)
    if found is None or found[1].get("state") != state:
        print(f"refused, nothing sent: {Path(a.project).resolve()} is not a project registered on the console at "
              f"{state}", file=sys.stderr)
        return 1
    root = Path(found[0])
    # The door the working directory chose (K4): this console's own socket, or the one server with its token.
    door = _door(a.state)
    try:
        code, want = door.request("GET", "/history-wants", None, agent=a.agent)
        if code != 200:
            print(f"refused ({code}): {want.get('error')}", file=sys.stderr)
            return 1
        blobs, specs = SG.collect(root, want)
        refused = []
        for b in blobs:
            code, out = door.request("POST", "/history-blob", b, agent=a.agent)
            if code != 200:
                refused.append(f"{b['sha256'][:12]}: {out.get('error')}")
        code, out = door.request("POST", "/history-specs", {"specs": specs}, agent=a.agent)
        if code != 200:
            refused.append(f"spec times: {out.get('error')}")
    except OSError as e:
        print(f"the console server is not answering on {door.sock}: {e}", file=sys.stderr)
        return 2
    asked = len(want.get("blobs") or [])
    sys.stdout.write(_json({"blobs": {"asked": asked, "sent": len(blobs) - len(refused), "not_in_history":
                                      asked - len(blobs)},
                            "specs": out if code == 200 else None, "refused": refused}))
    for r in refused:
        print(f"refused: {r}", file=sys.stderr)
    return 1 if refused else 0


def _prs_push(a) -> int:
    """`prs-push`: gh runs HERE, in the steward's process; the server gets the pull requests as data.

    Open pull requests, plus those merged or closed in the last `--days`, at
    most `--limit` of each, for the GitHub repository of the checkout this
    runs in. The server checks the same closed schema again; it never talks to
    GitHub and starts no process.
    """
    from console_kit import prs as PR

    def refuse(why: str, rc: int = 1) -> int:
        print(f"refused, nothing sent: {why}", file=sys.stderr)
        return rc

    state = os.path.realpath(a.state)
    found = R.enclosing(a.project)
    if found is None or found[1].get("state") != state:
        return refuse(f"{Path(a.project).resolve()} is not a project registered on the console at {state}")
    root = Path(found[0])
    if not 1 <= a.days <= PR.MAX_DAYS:
        return refuse(f"--days is a whole number from 1 to {PR.MAX_DAYS}")
    if not 1 <= a.limit <= PR.MAX_LIMIT:
        return refuse(f"--limit is a whole number from 1 to {PR.MAX_LIMIT}")
    door = _door(a.state)
    if door.project is None and not door.sock.is_socket():
        return refuse(f"no console server answers for {state}: no {door.sock}, and server.json hosts no project "
                      f"there", 2)
    got = []
    for args in PR.gh_lists(a.days, a.limit):
        out = G.gh(args, root)
        if isinstance(out, G.Unavailable):   # never in this process: the seam closes only in the server
            return refuse(f"gh is {out.reason}")
        rc, stdout, stderr = out
        if rc != 0:
            return refuse(f"`gh {' '.join(args[:3])}` failed ({rc}): {stderr.strip()[:500] or 'no message'}")
        try:
            got.append(json.loads(stdout))
        except ValueError as e:
            return refuse(f"`gh {' '.join(args[:3])}` did not print JSON: {e}")
    try:
        body = PR.from_gh(got[0], got[1], got[2], a.days)
    except PR.PushError as e:
        return refuse(str(e))
    why = PR.snapshot_problem(body)   # the server's own check, here first, so a bad push is named before sending
    if why:
        return refuse(why)
    try:
        code, out = door.request("POST", "/prs", body, agent=a.agent)
    except OSError as e:
        return refuse(f"the console server is not answering on {door.sock}: {e}", 2)
    if code != 200:
        print(f"refused ({code}): {out.get('error')}", file=sys.stderr)
        return 1
    sys.stdout.write(_json(out))
    return 0


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
    s = sub.add_parser("propose-anchor", description=(
        "PROPOSE a new anchor for a stale answer (CONSOLE-kit/Q31). Name the lines the ruling rests on; the "
        "server reads them now, each must be in its file exactly once, and nothing changes until the owner "
        "confirms the proposal on the console. To replace a stale ruling with a new question instead, `ask` it "
        "with \"replaces\": \"<qid>\" in the question file."))
    s.add_argument("qid")
    s.add_argument("--cite", action="append", required=True, metavar="PATH:FIRST-LAST",
                   help="lines the ruling rests on, read by the server; repeat for up to 4")
    s.add_argument("--basis", required=True, help="why these lines carry the ruling now, shown to the owner")
    s = sub.add_parser("advise", description=(
        "Answer a scan for a resolve (CONSOLE-kit/Q40) with a star recommendation for one stale ruling: withdraw "
        "it or keep it, with the evidence. It changes nothing; the owner's own Withdraw or Keep does. Prefer "
        "`propose-anchor` when the ruling's premise moved to lines that still carry it, and `ask` with "
        "\"replaces\" when the question itself must be asked again."))
    s.add_argument("qid")
    s.add_argument("--star", required=True, choices=("withdraw", "keep"))
    s.add_argument("--evidence", required=True,
                   help="where the cited text went, whether the premise holds, and what replaced it")
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
    s.add_argument("--rx-through", type=int, help="the highest `rx` among the scan lines processed (Q40)")
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
                       "its items, seed questions and board to the console server as data: the single server or "
                       "the one server that hosts this console (K3 §3.6, Q24). Neither server ever runs project "
                       "code. Run it after an upgrade, and whenever the register changes.")
    s.add_argument("--adapter", required=True, help="the adapter, a path inside the project")
    s.add_argument("--project", type=Path, default=Path.cwd(), help="the project root (default: here)")
    s.add_argument("--sync-dashboard", default=None, metavar="PAGE",
                   help="0.9.13: after a successful push, insert a <details data-ck-item=...> stub into PAGE "
                        "for every item not already there (idempotent, nested by parent). You still commit "
                        "and run page-snapshot yourself.")
    s = sub.add_parser("sync-dashboard", description="0.9.13: insert a <details id=item-X data-ck-item=X> stub "
                       "into the dashboard page for every item not already there. Nested by parent via per-item "
                       "markers so re-runs preserve hand-edits inside each node.")
    s.add_argument("--adapter", default=".console-kit/adapter.py", help="the adapter, a path inside the project")
    s.add_argument("--page", default="docs/console/page.html",
                   help="the dashboard page to update (project-relative)")
    s.add_argument("--project", type=Path, default=Path.cwd(), help="the project root (default: here)")
    s = sub.add_parser("items-watch", description="0.9.3: watch the project's adapter and items files and re-run "
                       "items-push whenever any of them changes. A plain stat loop (no file-watcher dependency); "
                       "Ctrl+C exits cleanly. Pair with the scaffolded board() to keep the dashboard fresh.")
    s.add_argument("--adapter", required=True, help="the adapter, a path inside the project")
    s.add_argument("--project", type=Path, default=Path.cwd(), help="the project root (default: here)")
    s.add_argument("--interval", type=float, default=5.0, help="poll seconds (default: 5; clamped to 1..300)")
    s.add_argument("--watch", default="", help="comma-separated project-relative paths to watch in addition to the "
                   "adapter, .console-kit/items.json and .console-kit.json")
    s = sub.add_parser("scaffold-dashboard", description="0.9.3: write a dashboard page with Items / Rollout / "
                       "Engine / Spec / Footer sections and inject a matching board() into the project's adapter. "
                       "Idempotent: re-run adds missing sections without clobbering hand edits.")
    s.add_argument("--adapter", default=".console-kit/adapter.py",
                   help="the adapter, a path inside the project (default: .console-kit/adapter.py)")
    s.add_argument("--page", default="docs/console/page.html",
                   help="where to write the dashboard page (default: docs/console/page.html)")
    s.add_argument("--project", type=Path, default=Path.cwd(), help="the project root (default: here)")
    s.add_argument("--name", default=None, help="the project name shown in the header (default: the directory name)")
    s = sub.add_parser("page-snapshot", description="Read the console's page from a COMMIT here (git cat-file, "
                       "never the working tree) and send it to the console server, which serves only that "
                       "snapshot (CONSOLE-kit/Q28). Refuses a commit that is not in origin's default branch "
                       "unless --unreviewed. Run it after an upgrade, and whenever the page's merged "
                       "version changes.")
    s.add_argument("--from-ref", default=None, help="the ref to read the page from (default: origin/HEAD, "
                   "the remote's default branch after a `git fetch` you run; origin/main is the fallback)")
    s.add_argument("--path", default=None, help="the page's path in the repository (default on the one server: "
                   "the project's page in server.json)")
    s.add_argument("--unreviewed", action="store_true", help="send a commit that is not in origin's default "
                   "branch; the footer says \"unreviewed ref\" instead of \"from the steward\"")
    s.add_argument("--project", type=Path, default=Path.cwd(), help="the project root (default: here)")
    s = sub.add_parser("history-push", description="Read git HERE, in your own process, for what the console "
                       "server asks (the version each stale lock was taken against, the cited specs' last-commit "
                       "times) and send it as data. The server starts no git (CONSOLE-kit/Q23).")
    s.add_argument("--project", type=Path, default=Path.cwd(), help="the project root (default: here)")
    s = sub.add_parser("prs-push", description="Run `gh pr list` HERE, in your own process, for the GitHub "
                       "repository of this checkout, and send the console server its open pull requests and those "
                       "merged or closed in the last --days, as data. The server never talks to GitHub and starts "
                       "no process. Run it after an upgrade, and whenever you want the PRs view fresh.")
    s.add_argument("--project", type=Path, default=Path.cwd(), help="the project root (default: here)")
    s.add_argument("--days", type=int, default=30, help="merged and closed PRs from this many days back "
                   "(default: 30, at most 365)")
    s.add_argument("--limit", type=int, default=50, help="at most this many open PRs, and as many merged or "
                   "closed ones (default: 50, at most 100)")
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
    s = ss.add_parser("token", description="The project's agent token (K4): `rotate NAME` writes a new token "
                      "file and hash; the old token is refused from the next request. The token itself is never "
                      "printed.")
    s.add_argument("token_cmd", choices=("rotate",))
    s.add_argument("project_name", metavar="NAME")
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
    global _DOOR
    try:
        # The user's own commands write the user's files; every other command reaches (or, for watch, inbox and
        # synced, reads) ONE project's console, and that project is the one the working directory is in (K4).
        if a.cmd not in USER_COMMANDS:
            _DOOR = resolve_door(a.state)
        return _run(a, bell)
    except R.RegistryError as e:  # an unsafe or unreadable console file, named
        print(str(e), file=sys.stderr)
        return 1
    except DoorRefused as e:
        print(str(e), file=sys.stderr)
        return 1
    finally:
        _DOOR = None


USER_COMMANDS = ("register", "steward", "server", "costs", "next-step")


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
    if a.cmd == "items-watch":
        return _items_watch(a)
    if a.cmd == "scaffold-dashboard":
        return _scaffold_dashboard(a)
    if a.cmd == "sync-dashboard":
        return _sync_dashboard(a)
    if a.cmd == "history-push":
        return _history_push(a)
    if a.cmd == "prs-push":
        return _prs_push(a)
    if a.cmd == "page-snapshot":
        return _page_snapshot(a)
    if a.cmd == "server" and a.server_cmd == "token":
        from console_kit import serverfile as SF
        try:
            f = SF.rotate(a.project_name)
        except SF.ServerFileError as err:
            print(f"refused, nothing written: {err}", file=sys.stderr)
            return 1
        print(f"server: project {a.project_name} has a new token in {f} (mode 0600); the old one is refused from "
              f"the next request, and agent.py reads the new one on its next call")
        return 0
    if a.cmd == "server":
        from console_kit import serverfile as SF
        try:   # the name is the user's word on this command line, never a key of the repository's config
            e = SF.add(a.project_name, a.state, a.hostname, a.aud, a.port, a.team_domain, root=a.root,
                       page=a.page, slugs=a.slugs)
        except SF.ServerFileError as err:
            print(f"refused, nothing written: {err}", file=sys.stderr)
            return 1
        print(f"server: project {a.project_name} on port {e['port']} ({e['hostname']}), state {e['state']}, "
              f"root {e['root']} in {SF.location()}; its agents' token is in {SF.token_file(a.project_name)} "
              f"(mode 0600)")
        return 0
    if a.cmd in ("watch", "synced"):
        why = steward_refusal(a.state, a.agent, f"`{a.cmd}`")
        if why:
            print(why, file=sys.stderr)
            return 1
    if a.cmd == "inbox":
        since = 0 if a.all else (a.since if a.since is not None else D.read_cursor(a.state))
        rx_since = 0 if a.all else D.read_rx_cursor(a.state)
        for line in D.pending(bell, since, intents=None, rx_since=rx_since):
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
            found = D.watch(bell, since, poll=a.poll, timeout=a.timeout, heartbeat=beat,
                            rx_since=D.read_rx_cursor(a.state))
        finally:  # woken, timed out, or interrupted: the owner is told at once, not when the promise lapses
            beat.stop()
            for sig, h in old.items():
                signal.signal(sig, h)
        if not found:
            print(f"no 'process', 'fork', 'chat', 'visual' or 'scan' signal after seq {since} within {a.timeout:g}s",
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
    if a.cmd == "propose-anchor":
        return _call(a.state, "POST", "/anchor-proposal", {"qid": a.qid, "cites": a.cite, "basis": a.basis,
                                                           "nonce": secrets.token_urlsafe(12)}, agent=a.agent)
    if a.cmd == "advise":
        return _call(a.state, "POST", "/refactor-advice", {"qid": a.qid, "star": a.star, "evidence": a.evidence,
                                                           "nonce": secrets.token_urlsafe(12)}, agent=a.agent)
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
    if a.through is not None or a.rx_through is not None:
        if (a.through or 0) < 0 or (a.rx_through or 0) < 0:
            print("--through and --rx-through take a doorbell seq, 0 or more", file=sys.stderr)
            return 1
        held = D.write_cursor(a.state, a.through or 0, a.rx_through)
        print(f"agent cursor: processed through seq {held}"
              + (f", refactor seq {D.read_rx_cursor(a.state)}" if a.rx_through is not None else ""), file=sys.stderr)
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return _call(a.state, "POST", "/cursor", {"last_synced_at": now, "last_error": a.error}, agent=a.agent)


if __name__ == "__main__":
    sys.exit(main())

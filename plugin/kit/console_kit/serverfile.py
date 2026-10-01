"""The one console server's file: which projects it hosts, and where (K3, spec §3.4, §3.5).

    ${XDG_CONFIG_HOME:-~/.config}/console-kit/server.json
    {"team_domain": "team.example.cloudflareaccess.com",
     "projects": {"<name>": {"state": "<abs dir>", "root": "<abs dir>", "page": "<path under root>",
                             "hostname": "<bare DNS name>", "aud": "<Access AUD>", "port": N,
                             "slugs": ["<one directory name>", ...],
                             "token_sha256": "<64 hex: SHA-256 of the project's agent token>"}}}

It sits beside the user's registry and, like it, is written only by a command
the user runs (`agent.py server add`), never by a session and never from a
repository. It is a SEPARATE file on purpose (§1's trap): the registry's
entries keep their exact shape and bytes, so every installed plugin copy and
SessionStart hook still reads them. This module reads `projects.json` and
never writes it.

A project's name is the user's word, given on the command line: it is never
read from the repository's `.console-kit.json`, whatever keys that file holds.

The cost collector (K2) reads `slugs` from here too, through `slugs_for_state`:
this is the one reader of the file. Keys it does not know are ignored, so an
older reader still reads a file carrying K4's `token_sha256`.

**Project tokens (K4, spec §3.5).** `add` (for a project with no token yet)
and `rotate` mint 256 random bits as `ck1_<base64url>`. The PLAINTEXT is
written once, to `tokens/<NAME>` beside this file, mode 0600 in a 0700
directory, by temporary name and rename; only its SHA-256 goes into this file.
The plaintext is never returned, printed, logged or put in an error message:
callers learn the file's path, never its bytes. Both refuse a tokens directory
that resolves inside any registered root, so a token never lands in a
repository. SHA-256 with no salt is enough here: the input is 256 random bits,
so there is nothing to guess and no table to precompute.
"""

from __future__ import annotations

import base64
import contextlib
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import tempfile
from pathlib import Path

from . import names as N
from . import registry as R

FILE = "server.json"
MAX_FILE = 1 << 20
SLUG = re.compile(r"^[A-Za-z0-9-]{1,255}\Z")   # one directory name: never "/", "." or ".."
# A bare DNS name: lowercase labels of letters, digits and inner hyphens, dot-separated. No scheme, port,
# path, "@", whitespace or trailing dot; it is put into an Origin check and a deep link, never parsed.
HOSTNAME = re.compile(r"^(?=.{1,253}\Z)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+\Z")
AUD = re.compile(r"^[A-Za-z0-9_-]{1,256}\Z")
TEAM = HOSTNAME
PAGE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]{0,511}\Z")
TOKENS = "tokens"
TOKEN_KEY = "token_sha256"
TOKEN = re.compile(r"^ck1_[A-Za-z0-9_-]{43}\Z")      # 32 bytes, unpadded base64url
TOKEN_HASH = re.compile(r"^[0-9a-f]{64}\Z")
MAX_TOKEN_FILE = 256
WRITE_LOCK = "server.json.lock"   # beside server.json: every writer of it, and of tokens/, holds this
TOKEN_TMP = re.compile(r"^\.token\..*\.tmp\Z")


class ServerFileError(ValueError):
    pass


def location() -> Path:
    return R.location().parent / FILE


def tokens_dir(path: Path | None = None) -> Path:
    """Where the plaintext tokens live: `tokens/` beside server.json, outside every repository."""
    return Path(path or location()).parent / TOKENS


def token_file(name: str, path: Path | None = None) -> Path:
    if N.problem(name):   # the name is a path component: only the name shape, never "..", "/" or ""
        raise ServerFileError(f"project name: {N.problem(name)}")
    return tokens_dir(path) / name


def token_hash(token: str) -> str:
    """The hex SHA-256 the server compares against; the one way a token becomes what server.json holds."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def read_token(name: str, path: Path | None = None) -> str:
    """This project's token, read afresh from its file (so a rotation needs no restart). Never echoes it.

    Raises ServerFileError naming the FILE when it is absent, not a regular
    file, a symlink, readable by others, or not a token; the bytes are never
    put in the message.
    """
    f = token_file(name, path)
    try:
        fd = os.open(f, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        raise ServerFileError(f"no token file for project {name} at {f}; the user runs "
                              f"`agent.py --state DIR server token rotate {name}`") from None
    except OSError as e:
        raise ServerFileError(f"the token file {f} cannot be opened: {e.strerror}") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise ServerFileError(f"the token file {f} must be a regular file you own with mode 0600")
        raw = os.read(fd, MAX_TOKEN_FILE + 1)
    finally:
        os.close(fd)
    tok = raw.decode("ascii", "replace").strip()
    if len(raw) > MAX_TOKEN_FILE or not TOKEN.match(tok):
        raise ServerFileError(f"the token file {f} does not hold a token; the user runs "
                              f"`agent.py --state DIR server token rotate {name}`")
    return tok


def _inside_a_root(d: Path, registry: Path | None) -> str | None:
    """The registered root that the real path of `d` lies in (or is), else None."""
    real = os.path.realpath(d)
    for root in R.load(registry):
        r = os.path.realpath(root)
        if real == r or real.startswith(r.rstrip("/") + "/"):
            return r
    return None


def _check_tokens_dir(path: Path | None, registry: Path | None) -> None:
    """Refuse a tokens directory inside a registered root, or one that is a link or not the user's own.

    Runs before anything touches tokens/, the stale-temp sweep included, so a
    tokens/ linked into a repository is never read, swept or written.
    """
    d = tokens_dir(path)
    inside = _inside_a_root(d, registry)
    if inside is not None:
        raise ServerFileError(f"the tokens directory {d} lies inside the registered project root {inside}; a token "
                              f"must never be stored in a repository. Move XDG_CONFIG_HOME (or that link) outside "
                              f"every registered root")
    try:
        st = d.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise ServerFileError(f"the tokens directory {d} must be a directory you own")


def _write_token(name: str, path: Path | None, registry: Path | None) -> str:
    """Mint a token, write it to its file (0600, 0700 dir, temp + rename) and return its HASH only."""
    d = tokens_dir(path)
    _check_tokens_dir(path, registry)
    d.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    d.mkdir(exist_ok=True, mode=0o700)
    st = d.lstat()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise ServerFileError(f"the tokens directory {d} must be a directory you own")
    os.chmod(d, 0o700)
    tok = "ck1_" + base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".token.", suffix=".tmp")   # 0600 from creation
    try:
        with os.fdopen(fd, "w", encoding="ascii") as fh:
            fh.write(tok + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, token_file(name, path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return token_hash(tok)


@contextlib.contextmanager
def _writing(p: Path):
    """Serialise every load-modify-write of server.json (and of tokens/) with an exclusive flock beside it.

    Without it, a `rotate` racing an `add` could have the add, which loaded the
    file before the rotation, write the revoked hash back. The lock also makes
    the stale-temp sweep safe: no other writer can be mid-write while it runs.
    """
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(p.parent / WRITE_LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)   # releases the lock


def _sweep(p: Path) -> None:
    """Remove `tokens/.token.*.tmp` left by a writer that died between mkstemp and rename (plaintext tokens).

    Called under `_writing`, so no live writer's temp file can be among them.
    """
    d = tokens_dir(p)
    try:
        names = os.listdir(d)
    except OSError:
        return
    for n in names:
        if TOKEN_TMP.match(n):
            try:
                os.unlink(d / n)
            except OSError:
                pass


def rotate(name: str, path: Path | None = None, registry: Path | None = None) -> Path:
    """`server token rotate NAME`: a new token file and hash; the old token is refused from the next request.

    The file is written first and the hash second, so there is never a moment
    in which both tokens work: in between, the server still holds the OLD hash
    while the file holds the NEW token, and a call made then is refused (and
    succeeds when retried). Returns the token file's path; never the token.
    """
    p = Path(path or location())
    with _writing(p):
        _check_tokens_dir(p, registry)
        _sweep(p)
        return _rotate(name, p, registry)


def _rotate(name: str, p: Path, registry: Path | None) -> Path:
    doc = load(p)
    e = doc["projects"].get(name)
    if not isinstance(e, dict):
        raise ServerFileError(f"server.json hosts no project {name!r}; run `agent.py --state DIR server add` first")
    e = {**e, TOKEN_KEY: _write_token(name, p, registry)}
    doc["projects"][name] = e
    _write(p, {"team_domain": doc["team_domain"], "projects": doc["projects"]})
    return token_file(name, p)


def _page_problem(page: object) -> str | None:
    if not isinstance(page, str) or not PAGE.match(page):
        return f"page {page!r} is not a relative path of plain characters"
    if any(part in ("", ".", "..") for part in page.split("/")):
        return f"page {page!r} must name a file under the root: no '.', '..' or empty component"
    return None


def entry_problems(name: object, e: object) -> list[str]:
    """Why a hosted project's entry is unusable; [] when it is well formed. Unknown keys are ignored."""
    if N.problem(name):
        return [f"project name: {N.problem(name)}"]
    if not isinstance(e, dict):
        return [f"{name}: an entry is an object"]
    out = []
    for k in ("state", "root"):
        if not (isinstance(e.get(k), str) and R.PLAIN_PATH.match(e[k])):
            out.append(f"{name}: {k} {e.get(k)!r} is not an absolute path of plain characters")
    why = _page_problem(e.get("page"))
    if why:
        out.append(f"{name}: {why}")
    if not (isinstance(e.get("hostname"), str) and HOSTNAME.match(e["hostname"])):
        out.append(f"{name}: hostname {e.get('hostname')!r} is not a bare DNS name")
    if not (isinstance(e.get("aud"), str) and AUD.match(e["aud"])):
        out.append(f"{name}: aud is not an Access AUD tag")
    port = e.get("port")
    if not (isinstance(port, int) and not isinstance(port, bool) and 1 <= port <= 65535):
        out.append(f"{name}: port {port!r} is not 1-65535")
    slugs = e.get("slugs", [])
    if not isinstance(slugs, list) or not all(isinstance(s, str) and SLUG.match(s) for s in slugs):
        out.append(f"{name}: slugs must be a list of single directory names")
    if TOKEN_KEY in e and not (isinstance(e[TOKEN_KEY], str) and TOKEN_HASH.match(e[TOKEN_KEY])):
        out.append(f"{name}: {TOKEN_KEY} is not 64 lowercase hex digits")
    return out


def load(path: Path | None = None) -> dict:
    """The whole file as {"team_domain": str | None, "projects": {...}}; empty when absent. Raises on bad JSON."""
    p = path or location()
    raw = R.read_regular(p, MAX_FILE)
    if raw is None:
        return {"team_domain": None, "projects": {}}
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise ServerFileError(f"{p} is not JSON: {e}") from None
    except RecursionError:   # nesting deeper than the parser's stack: unreadable like any other bad file
        raise ServerFileError(f"{p} is not JSON: nested too deeply") from None
    projects = doc.get("projects") if isinstance(doc, dict) else None
    if not isinstance(projects, dict):
        raise ServerFileError(f"{p} needs a \"projects\" object")
    team = doc.get("team_domain")
    return {"team_domain": team if isinstance(team, str) else None, "projects": projects}


def slugs_for_state(state: str, path: Path | None = None) -> list[str]:
    """The extra transcript slugs listed for the console at `state` (an absolute real path); K2 reads these."""
    p = path or location()
    out = []
    for name, e in load(p)["projects"].items():
        if not isinstance(e, dict) or not isinstance(e.get("state"), str):
            continue
        if os.path.realpath(e["state"]) != state:
            continue
        slugs = e.get("slugs", [])
        if not isinstance(slugs, list) or not all(isinstance(s, str) and SLUG.match(s) for s in slugs):
            raise ServerFileError(f"{p}: project {name!r}: slugs must be a list of single directory names")
        out.extend(slugs)
    return out


def _write(p: Path, doc: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".server.", suffix=".tmp")   # 0600 from creation
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def registered_roots(state: str | Path, projects: dict) -> list[str]:
    """The roots the registry (`R.load`'s result) registers on the console at `state`, real paths, sorted."""
    s = os.path.realpath(state)
    return sorted(r for r, e in projects.items() if isinstance(e, dict) and e.get("state") == s)


def add(name: str, state: Path, hostname: str, aud: str, port: int, team_domain: str,
        root: Path | None = None, page: str = "index.html", slugs: list[str] | None = None,
        registry: Path | None = None, path: Path | None = None) -> dict:
    """Host `name` on the one server. Only the user runs this. Writes server.json; never the registry.

    Refuses: a name not in the name shape; a state no registry entry names; a
    root that is not registered on that state (and, with no --root, more than
    one root to choose from); a hostname that is not a bare DNS name; another
    project already holding this state, port or hostname. Re-adding a name
    replaces its entry, keeping its slugs and its token unless given.

    K4: a project with no token hash, or whose token file is gone, gets a new
    token (see `rotate`); one that has both keeps them, so re-adding never
    cuts off a running agent. The tokens directory inside a registered root
    is refused before anything is written.
    """
    p = Path(path or location())
    with _writing(p):
        _check_tokens_dir(p, registry)
        _sweep(p)
        return _add(name, state, hostname, aud, port, team_domain, root, page, slugs, registry, p)


def _add(name, state, hostname, aud, port, team_domain, root, page, slugs, registry, p: Path) -> dict:
    problems = []
    if N.problem(name):
        problems.append(f"project name: {N.problem(name)}")
    s = os.path.realpath(state)
    roots = registered_roots(s, R.load(registry))
    r = ""
    if not roots:
        problems.append(f"no project is registered on the console at {s}; run `agent.py --state {s} register "
                        f"--project DIR` first")
    elif root is not None:
        r = os.path.realpath(root)
        if r not in roots:
            problems.append(f"root {r} is not registered on the console at {s} (registered: {', '.join(roots)})")
    elif len(roots) > 1:
        problems.append(f"several roots are registered on the console at {s}; name the main one with --root "
                        f"({', '.join(roots)})")
    else:
        r = roots[0]
    if not (isinstance(team_domain, str) and TEAM.match(team_domain)):
        problems.append(f"team domain {team_domain!r} is not a bare DNS name")
    doc = load(p)
    old = doc["projects"].get(name) if isinstance(doc["projects"].get(name), dict) else {}
    e = {**old, "state": s, "root": r, "page": page, "hostname": hostname, "aud": aud, "port": port,
         "slugs": list(slugs) if slugs is not None else list(old.get("slugs", []))}
    if not (isinstance(e.get(TOKEN_KEY), str) and TOKEN_HASH.match(e[TOKEN_KEY])):
        e.pop(TOKEN_KEY, None)   # a hand-broken hash is replaced by a fresh token below, never kept
    problems += [x for x in entry_problems(name, e)
                 if not x.startswith("project name:") and not (not r and " root " in x)]
    if doc["team_domain"] not in (None, team_domain):
        problems.append(f"the server's team domain is {doc['team_domain']}; one server serves one Access team")
    for other, oe in doc["projects"].items():
        if other == name or not isinstance(oe, dict):
            continue
        if oe.get("state") == s:
            problems.append(f"project {other} already holds the console at {s}")
        if oe.get("port") == port:
            problems.append(f"project {other} already holds port {port}")
        if oe.get("hostname") == hostname:
            problems.append(f"project {other} already holds hostname {hostname}")
    if not problems:
        inside = _inside_a_root(tokens_dir(p), registry)
        if inside is not None:
            problems.append(f"the tokens directory {tokens_dir(p)} lies inside the registered project root "
                            f"{inside}; a token must never be stored in a repository")
    if problems:
        raise ServerFileError("; ".join(problems))
    if not (isinstance(e.get(TOKEN_KEY), str) and TOKEN_HASH.match(e[TOKEN_KEY])
            and os.path.lexists(token_file(name, p))):
        e[TOKEN_KEY] = _write_token(name, p, registry)
    doc["projects"][name] = e
    _write(p, {"team_domain": team_domain, "projects": doc["projects"]})
    return e

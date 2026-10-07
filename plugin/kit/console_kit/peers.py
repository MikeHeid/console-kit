"""Portfolio peers: the home console reads other consoles' slim JSON via Cloudflare Access service tokens (0.19.0).

Configuration lives in two places:

- `.console-kit/portfolio.json` — a committed list of peers, each with the URL, the service-token
  `client_id`, and the **sha256** of the service-token secret. The sha256 is a drift check; the plaintext
  is NEVER in the committed config.
- `STATE/portfolio-secrets/<peer>.json` — 0600, out-of-repo, holds the plaintext secret:
  `{"client_secret": "..."}`. The home console reads it, hashes it, checks against the configured
  `token_sha256`, then uses the pair to sign outbound calls to the peer's Cloudflare Access.

Peer URLs must be https:// of plain characters (no path), under one of the operator's own hostnames or any
public hostname they trust. The home server never fetches a peer whose stored secret does not match its
configured sha, so a secret file lost or swapped drifts loudly rather than silently.

The home server exposes `GET /api/portfolio` (owner-gated), which fans out to each peer's
`GET /api/portfolio-slim` with the Access service-token headers. Results are cached in-process with a short
TTL so a slow peer does not hang the aggregator. The slim JSON is small on purpose (counts + timestamps),
never the owner's view content.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import rootfs as RF
from .registry import RegistryError, read_regular

FILE = ".console-kit/portfolio.json"
MAX_FILE = 32 * 1024
MAX_PEERS = 32
PEER_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}\Z")
URL = re.compile(r"^https://[a-z0-9][a-z0-9.\-]{0,255}(:[0-9]{1,5})?/?\Z")
CLIENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}(\.access)?\Z")
SHA256_HEX = re.compile(r"^[0-9a-f]{64}\Z")

SECRETS_DIR = "portfolio-secrets"
MAX_SECRET_FILE = 4096
TIMEOUT_S = 4.0
CACHE_TTL_S = 5.0


class PeersError(ValueError):
    pass


@dataclass(frozen=True)
class Peer:
    name: str
    url: str                # trailing slash normalised off
    client_id: str
    token_sha256: str

    def to_public(self) -> dict:
        """Shape for /api/portfolio: never includes the token secret, which stays in STATE."""
        return {"name": self.name, "url": self.url}


def load(root: Path) -> dict[str, Peer]:
    """Parse `.console-kit/portfolio.json`; return {name: Peer}. Raises PeersError on any bad entry."""
    try:
        if RF.confined():
            raw = RF.read(Path(root), FILE, MAX_FILE)
        else:
            raw = read_regular(Path(root) / FILE, MAX_FILE)
    except (RegistryError, RF.Refused, ValueError, OSError) as e:
        raise PeersError(str(e)) from None
    if raw is None:
        return {}
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise PeersError(f"{FILE} is not JSON: {e}") from None
    if not isinstance(doc, dict):
        raise PeersError(f"{FILE} must be a JSON object")
    peers = doc.get("peers")
    if peers is None:
        return {}
    if not isinstance(peers, dict):
        raise PeersError(f"{FILE}: peers must be an object mapping name → definition")
    if len(peers) > MAX_PEERS:
        raise PeersError(f"{FILE}: {len(peers)} peers; at most {MAX_PEERS}")
    out: dict[str, Peer] = {}
    for name, cfg in peers.items():
        if not isinstance(name, str) or not PEER_NAME.match(name):
            raise PeersError(f"{FILE}: peer name {name!r} must be slug-shaped")
        out[name] = _validate(name, cfg)
    return out


def _validate(name: str, cfg: object) -> Peer:
    if not isinstance(cfg, dict):
        raise PeersError(f"{FILE}: peers[{name!r}] must be an object")
    url = cfg.get("url")
    if not isinstance(url, str) or not URL.match(url):
        raise PeersError(f"{FILE}: peers[{name!r}].url must be https:// of a plain hostname")
    client_id = cfg.get("client_id")
    if not isinstance(client_id, str) or not CLIENT_ID.match(client_id):
        raise PeersError(f"{FILE}: peers[{name!r}].client_id must be a Cloudflare Access client ID")
    token_sha = cfg.get("token_sha256")
    if not isinstance(token_sha, str) or not SHA256_HEX.match(token_sha):
        raise PeersError(f"{FILE}: peers[{name!r}].token_sha256 is 64 lowercase hex chars")
    return Peer(name=name, url=url.rstrip("/"), client_id=client_id, token_sha256=token_sha)


def load_secret(state: Path, peer: Peer) -> str:
    """Read the plaintext secret from STATE/portfolio-secrets/<peer>.json; verify its sha256 matches config."""
    path = Path(state) / SECRETS_DIR / f"{peer.name}.json"
    try:
        st = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        raise PeersError(f"the secret file {path} is missing; `agent.py portfolio-token` prints the layout") from None
    except OSError as e:
        raise PeersError(f"{path} cannot be read: {e}") from None
    if not stat.S_ISREG(st.st_mode):
        raise PeersError(f"{path} is not a regular file")
    if st.st_size > MAX_SECRET_FILE:
        raise PeersError(f"{path} is over {MAX_SECRET_FILE} bytes")
    with open(path, "rb") as f:
        raw = f.read(MAX_SECRET_FILE + 1)
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise PeersError(f"{path} is not JSON") from None
    secret = doc.get("client_secret") if isinstance(doc, dict) else None
    if not isinstance(secret, str) or not secret:
        raise PeersError(f'{path} must hold {{"client_secret": "..."}}')
    got = hashlib.sha256(secret.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(got, peer.token_sha256):
        raise PeersError(f"the secret in {path} does not match .console-kit/portfolio.json's token_sha256 "
                         f"for {peer.name!r}; rotate the pair or correct the config")
    return secret


class Aggregator:
    """In-process cache for peer slim responses. One shared instance per Console."""

    def __init__(self, state: Path) -> None:
        self.state = Path(state)
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, dict]] = {}

    def fetch_slim(self, peer: Peer) -> dict:
        """Fetch a peer's /api/portfolio-slim. Returns the slim dict or an error row shaped the same way.

        Caches positive AND negative results for CACHE_TTL_S so a slow peer does not hang the aggregator.
        """
        now = time.time()
        with self._lock:
            cached = self._cache.get(peer.name)
            if cached and now - cached[0] < CACHE_TTL_S:
                return cached[1]
        try:
            secret = load_secret(self.state, peer)
        except PeersError as e:
            row = _error_row(peer, str(e))
            with self._lock:
                self._cache[peer.name] = (now, row)
            return row
        row = _fetch_slim_http(peer, secret)
        with self._lock:
            self._cache[peer.name] = (now, row)
        return row


def _fetch_slim_http(peer: Peer, secret: str) -> dict:
    """One HTTPS call to the peer's /api/portfolio-slim with Access service-token headers."""
    import urllib.request
    import urllib.error
    req = urllib.request.Request(peer.url + "/api/portfolio-slim")
    req.add_header("CF-Access-Client-Id", peer.client_id)
    req.add_header("CF-Access-Client-Secret", secret)
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            if resp.status != 200:
                return _error_row(peer, f"{peer.name}: HTTP {resp.status}")
            raw = resp.read(64 * 1024)
    except urllib.error.HTTPError as e:
        return _error_row(peer, f"{peer.name}: HTTP {e.code}")
    except urllib.error.URLError as e:
        return _error_row(peer, f"{peer.name}: {e.reason}")
    except TimeoutError:
        return _error_row(peer, f"{peer.name}: timed out after {TIMEOUT_S:g}s")
    except OSError as e:
        return _error_row(peer, f"{peer.name}: {e}")
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return _error_row(peer, f"{peer.name}: response not JSON")
    if not isinstance(doc, dict):
        return _error_row(peer, f"{peer.name}: response not an object")
    # Normalise and keep only known keys so no peer content leaks into the home console.
    row: dict = {"name": peer.name, "url": peer.url, "ok": True}
    for k in ("project", "items", "awaiting_you", "unlocked", "locked", "stale",
              "visuals_waiting", "last_activity_at"):
        if k in doc:
            row[k] = doc[k]
    return row


def _error_row(peer: Peer, msg: str) -> dict:
    return {"name": peer.name, "url": peer.url, "ok": False, "error": msg}

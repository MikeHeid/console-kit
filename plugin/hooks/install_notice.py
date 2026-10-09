"""One-shot post-install / post-upgrade reminder (1.0.0).

Prints an Overture banner and the agent-registration commands exactly once per
installed version — then writes a flag so it stays silent on later sessions.

The flag lives at `${XDG_STATE_HOME:-~/.local/state}/overture/install-notice-seen-<VERSION>`.
When the plugin upgrades (zip replaces this hook file), the version embedded here
changes and the flag path with it, so the banner fires again on the first session
after the upgrade. On the version it was written for, it never fires twice.

The banner names the registration path for Linux / macOS, WSL on Windows, and
PowerShell on Windows that drives WSL for the server side (the server runs on
POSIX; Windows users host it in WSL).

Trust: this hook does nothing more than stdout + one flag file write. It never
reads project state and never fails the session.
"""

from __future__ import annotations

import os
import platform
import sys
from pathlib import Path

def _read_version() -> str:
    """Read the plugin's version from `kit/overture/__init__.py` without importing it.

    The hook runs with its own PWD and no sys.path tweaks; a plain import would miss, and we would
    rather name the real version in the banner than fall through to a placeholder.
    """
    here = Path(__file__).resolve().parent
    for candidate in (here.parent / "kit" / "overture" / "__init__.py",):
        try:
            txt = candidate.read_text(encoding="utf-8")
            for line in txt.splitlines():
                if line.startswith("__version__"):
                    rhs = line.split("=", 1)[1].strip()
                    if rhs and rhs[0] in "\"'":
                        quote = rhs[0]
                        end = rhs.find(quote, 1)
                        if end > 0:
                            return rhs[1:end]
                    return rhs
        except OSError:
            continue
    return "1.0.0"


VERSION = _read_version()

BANNER = r"""
    ___                __
   / _ \_  _____ ____/ /___  _______
  / // / |/ / -_) __/ __/ // / __/ -_)
 /____/|___/\__/_/  \__/\_,_/_/  \__/     v{ver}
                                           the AI-operations cockpit
""".lstrip("\n")


def _state_root() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return Path(base) / "overture"


def _flag_path() -> Path:
    return _state_root() / f"install-notice-seen-{VERSION}"


def _already_shown() -> bool:
    try:
        return _flag_path().exists()
    except OSError:
        return True   # cannot read the flag → err on the side of silence


def _prior_versions() -> list[str]:
    """Every version this hook has fired on before, oldest first.

    Enables install-vs-upgrade branching: an empty list means first install, any entry
    means an upgrade (or downgrade). A rollback to an older version whose flag was
    deleted re-fires naturally through `_already_shown`.
    """
    try:
        return sorted(
            p.name.removeprefix("install-notice-seen-")
            for p in _state_root().glob("install-notice-seen-*")
            if p.is_file() and p.name != f"install-notice-seen-{VERSION}"
        )
    except OSError:
        return []


def _mark_shown() -> None:
    try:
        _state_root().mkdir(parents=True, exist_ok=True)
        _flag_path().write_text(f"shown-at={os.getpid()}\n", encoding="utf-8")
    except OSError:
        pass   # cannot write → best effort; next session will see it again


def _is_windows_native() -> bool:
    """True on Windows Python (not WSL). WSL identifies as Linux."""
    return platform.system() == "Windows"


def _install_lines() -> list[str]:
    """First-install banner body: register + start the user service."""
    lines: list[str] = [
        "Welcome to Overture " + VERSION + ".",
        "",
        "Register this project's server agent before Overture can serve its console:",
        "",
    ]
    if _is_windows_native():
        lines += [
            "  # On Windows, the server runs inside WSL (POSIX only).",
            "  # In WSL (Ubuntu-24.04 recommended):",
            "",
            "    wsl -d Ubuntu-24.04 -- python3 ~/.local/share/overture/kit/plugin/kit/agent.py register",
            "",
            "  # Then in PowerShell, start the user service:",
            "",
            "    wsl -d Ubuntu-24.04 -- systemctl --user start overture.service",
        ]
    else:
        lines += [
            "  # Linux / macOS / WSL:",
            "",
            "    python3 ~/.local/share/overture/kit/plugin/kit/agent.py register",
            "",
            "  # Then start the user service:",
            "",
            "    systemctl --user start overture.service",
        ]
    lines += [
        "",
        "  Full install guide: https://github.com/MikeHeid/overture#install",
        "",
        "  Tip: this reminder shows once per version; it will fire again the next time",
        "  you upgrade. Nothing to clean up.",
    ]
    return lines


def _upgrade_lines(prior: list[str]) -> list[str]:
    """Upgrade banner body: restart the user service to pick up the new binary.

    The register step from the first install is skipped — the agent is already registered
    and the socket path has not moved. On a running service the correct action is a
    restart, not a start.
    """
    from_ver = prior[-1] if prior else "an earlier version"
    lines: list[str] = [
        f"Upgraded to Overture {VERSION} (from {from_ver}).",
        "",
        "Restart the server to pick up the new binary:",
        "",
    ]
    if _is_windows_native():
        lines += [
            "  # In PowerShell:",
            "",
            "    wsl -d Ubuntu-24.04 -- systemctl --user restart overture.service",
        ]
    else:
        lines += [
            "  # Linux / macOS / WSL:",
            "",
            "    systemctl --user restart overture.service",
        ]
    lines += [
        "",
        "  Changelog: https://github.com/MikeHeid/overture/blob/main/CHANGELOG.md",
        "",
        "  Tip: this reminder shows once per version; nothing to clean up.",
    ]
    return lines


def _instructions() -> str:
    prior = _prior_versions()
    lines = _upgrade_lines(prior) if prior else _install_lines()
    return "\n".join(lines)


def main() -> int:
    if _already_shown():
        return 0
    # SessionStart hooks on Claude Code print context the session reads; the user sees it too.
    sys.stdout.write("\n")
    sys.stdout.write(BANNER.format(ver=VERSION))
    sys.stdout.write("\n")
    sys.stdout.write(_instructions())
    sys.stdout.write("\n")
    _mark_shown()
    return 0


if __name__ == "__main__":
    sys.exit(main())

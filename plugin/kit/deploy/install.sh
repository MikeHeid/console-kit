#!/usr/bin/env bash
# Install one project's owner console as systemd USER units.
#
#   deploy/install.sh --project DIR            copy the kit, build the venv, render the units; start nothing
#   deploy/install.sh --project DIR --start    also enable and start the SERVER (loopback only)
#
# The project must have been onboarded first (onboard.py write), which leaves
# DIR/.console-kit/console.env. Nothing of any one project is baked in here.
#
# It never creates a tunnel, never writes DNS, never starts the tunnel unit and
# never registers the project: those make the console reachable from the
# internet, or make your Claude sessions trust it, and you approve them yourself.
set -euo pipefail

PROJECT=""; START=0
while [ $# -gt 0 ]; do
  case "$1" in
    --project) PROJECT=${2:?--project needs a directory}; shift 2 ;;
    --start) START=1; shift ;;
    *) echo "install.sh: unknown argument $1" >&2; exit 2 ;;
  esac
done
[ -n "$PROJECT" ] || { echo "install.sh: --project DIR is required" >&2; exit 2; }
PROJECT=$(cd "$PROJECT" && pwd)
ENV_FILE="$PROJECT/.console-kit/console.env"
[ -f "$ENV_FILE" ] || { echo "install.sh: $ENV_FILE is missing; run onboard.py write first" >&2; exit 2; }

SRC=$(cd "$(dirname "$0")/.." && pwd)
# Re-validate the env file with the same rules onboarding used, and read it
# without sourcing it: a shell `source` would run whatever the file holds.
VALUES=$(python3 "$SRC/onboard.py" show --project "$PROJECT") || exit 2
CONSOLE_NAME=$(printf '%s\n' "$VALUES" | sed -n 's/^CONSOLE_NAME=//p')
CONSOLE_TUNNEL=$(printf '%s\n' "$VALUES" | sed -n 's/^CONSOLE_TUNNEL=//p')
CONSOLE_PORT=$(printf '%s\n' "$VALUES" | sed -n 's/^CONSOLE_PORT=//p')

SHARE="${XDG_DATA_HOME:-$HOME/.local/share}/console-kit"
KIT="$SHARE/kit"
UNITS="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
STATE="${XDG_STATE_HOME:-$HOME/.local/state}/console-kit/$CONSOLE_NAME"
CLOUDFLARED=$(command -v cloudflared || echo /usr/local/bin/cloudflared)

# Every value rendered into a unit must be a plain path or name. The names are
# validated by onboard.py; the PATHS come from this machine ($HOME, the project
# directory, where cloudflared lives) and are checked here. A `|`, `&` or `\`
# would corrupt the sed below, a newline could add a directive to the unit, and
# a space or `%` means something to systemd itself. Refused by name, never escaped.
for pair in "project directory=$PROJECT" "kit=$KIT" "python=$SHARE/venv/bin/python" "cloudflared=$CLOUDFLARED" \
            "state=$STATE" "units=$UNITS"; do
  value=${pair#*=}
  if ! printf '%s' "$value" | LC_ALL=C grep -qxE '/[A-Za-z0-9_./-]*' || [ "$(printf '%s' "$value" | wc -l)" != 0 ]; then
    echo "install.sh: the ${pair%%=*} path '$value' holds a character a systemd unit cannot take safely;" >&2
    echo "            only letters, digits and _ . / - are allowed. Move it, or link it from a plain path." >&2
    exit 2
  fi
done

# The kit is COPIED to a fixed place, so the services never run from a plugin
# cache or a checkout that an update or a branch switch can change underneath
# them. Re-running this script is how the installed kit is updated.
if [ "$SRC" != "$KIT" ]; then
  mkdir -p "$SHARE"
  rm -rf "$KIT.new"
  mkdir "$KIT.new"
  for part in console_kit deploy demo docs agent.py fold.py onboard.py publish.py server.py \
              adapter_template.py requirements.txt README.md INSTALL.md VERSION; do
    if [ -e "$SRC/$part" ]; then cp -R "$SRC/$part" "$KIT.new/"; fi
  done
  find "$KIT.new" -name __pycache__ -prune -exec rm -rf {} +
  rm -rf "$KIT"
  mv "$KIT.new" "$KIT"
  echo "kit:   $KIT"
fi

# A venv of its own, so the pinned PyJWT and cryptography never change the
# host's shared Python (and PEP 668 hosts refuse `pip --user` anyway).
VENV="$SHARE/venv"
[ -x "$VENV/bin/python" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" install -q -r "$KIT/requirements.txt"
PYTHON="$VENV/bin/python"
"$PYTHON" -c 'import jwt, cryptography; print("venv:  PyJWT", jwt.__version__, "cryptography", cryptography.__version__)'

mkdir -p "$UNITS"
install -d -m 700 "$STATE"
for unit in console console-tunnel; do
  out="$UNITS/$CONSOLE_NAME-$unit.service"
  sed -e "s|@PROJECT@|$PROJECT|g" -e "s|@KIT@|$KIT|g" -e "s|@PYTHON@|$PYTHON|g" \
      -e "s|@CLOUDFLARED@|$CLOUDFLARED|g" -e "s|@NAME@|$CONSOLE_NAME|g" -e "s|@TUNNEL@|$CONSOLE_TUNNEL|g" \
      "$KIT/deploy/$unit.service.in" > "$out"
  echo "unit:  $out"
done
systemctl --user daemon-reload

if [ "$START" = 1 ]; then
  # enable, then RESTART: `enable --now` leaves an already-running server on the
  # old kit, so re-running this after an update would change nothing.
  systemctl --user enable "$CONSOLE_NAME-console.service"
  systemctl --user restart "$CONSOLE_NAME-console.service"
  sleep 1
  systemctl --user --no-pager status "$CONSOLE_NAME-console.service" | head -5 || true
  printf 'check: loopback without a token answers %s (want 403)\n' \
    "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$CONSOLE_PORT/api/view" || echo none)"
fi

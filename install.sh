#!/usr/bin/env bash
# AI Bridge installer. Run from the project folder:
#   bash install.sh
# In a terminal, existing installations update with progress. --stage is unattended staging only.
# --update | --setup-authenticator | --restart | --stage | --activate RELEASE | --rollback [RELEASE]
set -euo pipefail

APP_USER="$(id -un)"
APP_HOME="$HOME"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Dispatch removal before installation, staging, or privilege bootstrap.
if [ "${1:-}" = --uninstall ]; then
  shift
  exec bash "$SRC_DIR/uninstall.sh" "$@"
fi
DATA_DIR="$APP_HOME/.local/share/ai-bridge"
ETC_DIR="/etc/ai-bridge"
ENV_FILE="$ETC_DIR/ai-bridge.env"
PASS_FILE="$ETC_DIR/sudo.pass"
UNIT_FILE="/etc/systemd/system/ai-bridge.service"
PORT="${AI_BRIDGE_PORT:-8765}"

say() { printf '\n==> %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# Root can manage its own install without a sudo binary.
if [ "$(id -u)" -eq 0 ]; then
  sudo() { if [ "${1:-}" = -v ]; then return 0; fi; "$@"; }
fi

# Staging needs no sudo and never changes the active application or service.
case "${1:-}" in
  --help|-h)
    cat <<'HELP'
Usage: bash install.sh [OPTION]

  (no option)             Install; in a terminal, update an existing installation
  --update                Build, test, activate, restart and verify with progress
  --setup-authenticator   Set up phone codes, restart and verify
  --restart               Restart with service progress and a health check
  --stage                 Only build and test; no service changes
  --activate RELEASE      Select a staged release; no restart
  --rollback [RELEASE]    Select an older release; no restart
  --uninstall             Removal options (see docs/operations.md)
HELP
    exit 0
    ;;
  --update|--setup-authenticator|--restart)
    [ "$#" -eq 1 ] || die "Unexpected arguments."
    exec python3 -u "$SRC_DIR/installer.py" "${1#--}"
    ;;
esac
if [ "${1:-}" = "--stage" ]; then
  exec python3 "$SRC_DIR/deploy.py" stage --root "$DATA_DIR" --source "$SRC_DIR"
fi
if [ "${1:-}" = "--activate" ] || [ "${1:-}" = "--rollback" ]; then
  ACTION="${1#--}"
  RELEASE="${2:-}"
  if [ "$ACTION" = activate ]; then
    [ -n "$RELEASE" ] || die "--activate requires a verified release path."
  fi
  if [ -n "$RELEASE" ]; then
    SELECTED="$(python3 "$SRC_DIR/deploy.py" "$ACTION" --root "$DATA_DIR" "$RELEASE")"
  else
    SELECTED="$(python3 "$SRC_DIR/deploy.py" "$ACTION" --root "$DATA_DIR")"
  fi
  # Preserve the old unit for first-migration rollback. No config/credential edits.
  say "Updating the service definition (your Linux password may be requested)"
  sudo python3 - "$UNIT_FILE" "$SELECTED" <<'UNITPY'
import os,sys,tempfile
from pathlib import Path
path,root=Path(sys.argv[1]),Path(sys.argv[2])
original=path.read_text()
backup=path.with_name('ai-bridge.service.before-versioned')
try:
    fd=os.open(backup,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as stream:stream.write(original)
except FileExistsError:
    pass
lines=[]
for line in original.splitlines():
    if line.startswith('ExecStart='):
        line='ExecStart='+str(root/'venv/bin/python')+' -m ai_bridge'
    elif line.startswith('Environment=PYTHONPATH='):
        line='Environment=PYTHONPATH='+str(root/'app')
    lines.append(line)
fd,name=tempfile.mkstemp(prefix='.ai-bridge-unit-',dir=path.parent)
with os.fdopen(fd,'w') as stream:
    os.fchmod(stream.fileno(),0o644)
    stream.write('\n'.join(lines)+'\n')
os.replace(name,path)
UNITPY
  sudo systemctl daemon-reload
  echo "Release selected. Running service is unchanged. From a host terminal, restart and verify."
  echo "For restart through MCP, use a detached systemd job after the request returns."
  exit 0
fi
MODE="${1:-}"
case "$MODE" in
  ""|--run-as-root) [ "$#" -le 1 ] || die "Unexpected arguments." ;;
  --dedicated-user) [ "$#" -le 2 ] || die "Use --dedicated-user [NAME]." ;;
  *) die "Usage: bash install.sh [--uninstall [OPTIONS] | --dedicated-user [NAME] | --run-as-root | --stage | --activate RELEASE | --rollback [RELEASE]]" ;;
esac
if [ "$(id -u)" -ne 0 ] && [ -n "$MODE" ]; then
  die "Root mode flags require root. Run without flags for the existing non-root workflow."
fi
if [ "$MODE" = --dedicated-user ]; then
  exec python3 "$SRC_DIR/bootstrap.py" --source "$SRC_DIR" --dedicated-user "${2:-ai-bridge}"
fi
if [ -d "$DATA_DIR/app" ] || [ -L "$DATA_DIR/current" ]; then
  if [ -t 0 ] && [ -t 1 ] && [ -z "${AI_BRIDGE_CHILD:-}" ]; then
    exec python3 -u "$SRC_DIR/installer.py" update
  fi
  say "Existing installation: staging only. Its active listener is left running." >&2
  RELEASE="$(python3 "$SRC_DIR/deploy.py" stage --root "$DATA_DIR" --source "$SRC_DIR")"
  echo "Staged and tested: $RELEASE"
  echo "Activation is separate: bash install.sh --activate '$RELEASE'"
  exit 0
fi

if [ "$(id -u)" -eq 0 ] && [ "$MODE" != --run-as-root ]; then
  [ -t 0 ] && [ -t 1 ] || die "Root installation requires a terminal choice, or explicit --run-as-root. No installation was started."
  echo "1) Dedicated unprivileged ai-bridge account (recommended); authenticated sudo for root tasks."
  echo "2) Run directly as root (HIGH RISK): every command and agent already has root access."
  read -r -p "Install mode [1]: " CHOICE
  case "${CHOICE:-1}" in
    1) exec python3 "$SRC_DIR/bootstrap.py" --source "$SRC_DIR" ;;
    2) MODE=--run-as-root ;;
    *) die "Invalid choice. No installation was started." ;;
  esac
fi
if [ "$(id -u)" -ne 0 ]; then
  command -v sudo >/dev/null || die "sudo is required for unprivileged installs."
else
  say "HIGH RISK: service, ordinary commands and agents will run directly as root."
fi
# Fresh bootstrap must never overwrite another identity's existing installation.
if [ -e "$UNIT_FILE" ] || [ -e "$ENV_FILE" ]; then
  die "Existing service/config found without this user's installation. Use its owner and the staged deployment workflow; refusing to overwrite."
fi
command -v systemctl >/dev/null || die "systemd is required."
command -v python3 >/dev/null || die "python3 is required (3.10+)."
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' || die "Python 3.10+ required."

# Warn before expensive preparation; a busy port does not prevent installation.
PORT_STATE="$(python3 "$SRC_DIR/deploy.py" check-port --port "$PORT" --warn-only)"

say "Checking installation privileges"
sudo -v

say "Installing Python venv support if missing"
if ! python3 -m venv --help >/dev/null 2>&1 || ! python3 -c 'import ensurepip' >/dev/null 2>&1; then
  if command -v apt-get >/dev/null; then
    PYVER="$(python3 -c 'import sys;print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
    sudo apt-get update -y && sudo apt-get install -y "python${PYVER}-venv" || sudo apt-get install -y python3-venv
  elif command -v dnf >/dev/null; then sudo dnf install -y python3-virtualenv || true
  fi
fi

say "Staging and testing a versioned release"
RELEASE="$(python3 "$SRC_DIR/deploy.py" stage --root "$DATA_DIR" --source "$SRC_DIR")"
python3 "$SRC_DIR/deploy.py" activate --root "$DATA_DIR" "$RELEASE"

say "Writing config to $ENV_FILE"
sudo mkdir -p "$ETC_DIR"
sudo chmod 755 "$ETC_DIR"
TOKEN=""
if sudo test -f "$ENV_FILE"; then
  TOKEN="$(sudo sed -n 's/^AI_BRIDGE_TOKEN=//p' "$ENV_FILE" | head -1)"
fi
[ -n "$TOKEN" ] || TOKEN="$(python3 -c 'import secrets;print(secrets.token_urlsafe(40).replace("-","x").replace("_","y"))')"

# Capture your interactive PATH so the service finds claude / codex / cursor-agent
# installed via npm, nvm, ~/.local/bin, etc.
USER_PATH="$(bash -ic 'printf %s "$PATH"' 2>/dev/null || printf %s "$PATH")"
USER_PATH="$USER_PATH:$APP_HOME/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

TMP_ENV="$(mktemp)"
cat > "$TMP_ENV" <<EOF
# AI Bridge config. Restart after edits: sudo systemctl restart ai-bridge
AI_BRIDGE_TOKEN=$TOKEN
AI_BRIDGE_HOST=127.0.0.1
AI_BRIDGE_PORT=$PORT
AI_BRIDGE_DEFAULT_CWD=$APP_HOME
# full | edit | readonly  (what local agents may do when the assistant does not say)
AI_BRIDGE_AGENT_DEFAULT_PERMISSION=edit
# Optional: your public hostname, enables Host header checks. Example: myserver.example.com
AI_BRIDGE_ALLOWED_HOSTS=
# Optional additional allowed Origin URLs (comma separated; checked when hosts are configured)
AI_BRIDGE_ALLOWED_ORIGINS=
# Optional comma-separated adapters to disable without affecting core tools
AI_BRIDGE_DISABLED_AGENTS=
AI_BRIDGE_MAX_OUTPUT_CHARS=30000
AI_BRIDGE_CAPTURE_BYTES=262144
AI_BRIDGE_JOB_LOG_BYTES=4194304
AI_BRIDGE_RETENTION_DAYS=0
AI_BRIDGE_AUDIT_BYTES=2097152
# Privileged calls require host approval; enroll phone codes with approvals.py totp-setup
AI_BRIDGE_SUDO_APPROVAL=1
AI_BRIDGE_SUDO_TOTP_SECRET=
# Optional host configuration root (default: ~/.config/ai-bridge)
# AI_BRIDGE_CONFIG_DIR=/path/to/ai-bridge-config
# Optional skill discovery roots, separated by colons (defaults: ~/.codex/skills,
# ~/.claude/skills, ~/.cursor/skills, ~/.agents/skills)
# AI_BRIDGE_SKILL_DIRS=/path/to/skills:/another/skills
# Optional CLI overrides
# AI_BRIDGE_CLAUDE_BIN=/path/to/claude
# AI_BRIDGE_CODEX_BIN=/path/to/codex
# AI_BRIDGE_CURSOR_BIN=/path/to/cursor-agent
PATH=$USER_PATH
HOME=$APP_HOME
EOF
if sudo test -f "$ENV_FILE"; then
  # Keep the existing file verbatim; append defaults only for absent keys.
  TMP_EXISTING="$(mktemp)"
  sudo cat "$ENV_FILE" > "$TMP_EXISTING"
  python3 "$SRC_DIR/merge_env.py" "$TMP_EXISTING" "$TMP_ENV"
  rm -f "$TMP_EXISTING"
fi
sudo install -o root -g "$(id -gn)" -m 640 "$TMP_ENV" "$ENV_FILE"
rm -f "$TMP_ENV"
PORT="$(sudo sed -n 's/^AI_BRIDGE_PORT=//p' "$ENV_FILE")"

say "Creating editable MCP host configuration (preserving existing files)"
PYTHONPATH="$RELEASE/app" "$RELEASE/venv/bin/python" - "$ENV_FILE" "$SRC_DIR/docs/instructions-guide.md" <<'PY'
import os
import sys
from pathlib import Path
from ai_bridge.host_config import HostConfig, initialize

for line in Path(sys.argv[1]).read_text().splitlines():
    key, separator, value = line.partition('=')
    if separator and key.strip() in ('AI_BRIDGE_CONFIG_DIR', 'XDG_CONFIG_HOME', 'AI_BRIDGE_STATE_DIR'):
        os.environ[key.strip()] = value.strip().strip('"').strip("'")
initialize()
state = Path(os.environ.get('AI_BRIDGE_STATE_DIR') or Path.home()/'.local/state/ai-bridge')
state.mkdir(mode=0o700, parents=True, exist_ok=True)
(state/'jobs').mkdir(mode=0o700, exist_ok=True)
host = HostConfig()
guide = host.root / 'instructions/README.md'
try:
    with guide.open('x', encoding='utf-8') as output:
        output.write(Path(sys.argv[2]).read_text(encoding='utf-8'))
except FileExistsError:
    pass
print(f"Host configuration ready: {host.root}")
PY

say "Recording private uninstall ownership metadata"
if ! sudo python3 "$SRC_DIR/uninstall.py" --record-install "$APP_USER"; then
  echo "WARNING: ownership registration failed. Installation will continue; unverified custom paths will require manual uninstall review." >&2
fi

say "Sudo password for AI commands"
CRED_LINE="# LoadCredential=sudo_pass:$PASS_FILE"
if [ "$(id -u)" -eq 0 ]; then
  echo "Root service: sudo is unnecessary; no sudo password will be stored or loaded."
elif sudo -k -n -- bash -c true 2>/dev/null && ! sudo test -f "$PASS_FILE"; then
  echo "Your user already has passwordless sudo. Nothing to store."
else
  if sudo test -f "$PASS_FILE"; then
    read -r -p "A sudo password is already stored. Replace it? [y/N] " ans
  else
    read -r -p "Store your sudo password so the AI can run sudo commands? [Y/n] " ans
    ans="${ans:-y}"
  fi
  if [[ "$ans" =~ ^[Yy] ]]; then
    read -r -s -p "Sudo password for $APP_USER: " PW; echo
    if printf '%s\n' "$PW" | sudo -k -S -p '' -v 2>/dev/null; then
      # Authenticate separately, then create the credential privately from outset.
      printf '%s' "$PW" | sudo -n python3 -c '
import os,sys,tempfile
from pathlib import Path
path=Path(sys.argv[1])
fd,name=tempfile.mkstemp(prefix=".sudo-pass-",dir=path.parent)
try:
    with os.fdopen(fd,"wb") as stream:
        os.fchmod(stream.fileno(),0o600)
        stream.write(sys.stdin.buffer.read())
        stream.flush();os.fsync(stream.fileno())
    os.replace(name,path)
finally:
    if os.path.exists(name):os.unlink(name)
' "$PASS_FILE"
      echo "Password verified and stored in $PASS_FILE (root only, 600)."
    else
      echo "That password did not work. Skipping. Re-run the installer to try again."
    fi
    unset PW
  fi
  if sudo test -f "$PASS_FILE"; then CRED_LINE="LoadCredential=sudo_pass:$PASS_FILE"; fi
fi

say "Installing systemd service"
TMP_UNIT="$(mktemp)"
cat > "$TMP_UNIT" <<EOF
[Unit]
Description=AI Bridge MCP server (shell and optional coding agents)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$APP_USER
Group=$(id -gn)
WorkingDirectory=$APP_HOME
EnvironmentFile=$ENV_FILE
Environment=PYTHONPATH=$RELEASE/app
Environment=PYTHONUNBUFFERED=1
$CRED_LINE
ExecStart=$RELEASE/venv/bin/python -m ai_bridge
# Let background jobs and agent runs survive a server restart.
KillMode=process
Restart=on-failure
RestartSec=3
# Do not set NoNewPrivileges: sudo needs it off.

[Install]
WantedBy=multi-user.target
EOF
sudo install -o root -g root -m 644 "$TMP_UNIT" "$UNIT_FILE"
rm -f "$TMP_UNIT"
# Use the effective port from the saved configuration for the final startup decision.
PORT_STATE="$(python3 "$SRC_DIR/deploy.py" check-port --port "$PORT" --warn-only)"
sudo systemctl daemon-reload
sudo systemctl enable ai-bridge >/dev/null
# Never restart synchronously from an MCP call served by this unit.
if [ "$PORT_STATE" = available ]; then
  if [ -t 0 ] && [ -t 1 ] && [ -z "${AI_BRIDGE_CHILD:-}" ]; then
    python3 -u "$SRC_DIR/installer.py" finish-install
  else
    sudo systemd-run --collect --on-active=3s --unit="ai-bridge-install-$(date +%s)" /usr/bin/systemctl restart ai-bridge
    say "Installation complete. Service startup scheduled; verify health after startup."
  fi
else
  say "Installation complete. Startup pending: 127.0.0.1:$PORT is unavailable." >&2
  cat >&2 <<PORT_HELP
To start the bridge:
  1. Edit $ENV_FILE (for example: sudoedit $ENV_FILE).
  2. Set AI_BRIDGE_PORT to an unused port, or free port $PORT.
  3. Run: sudo systemctl restart ai-bridge
  4. Update the tunnel backend if you changed the port, then verify /healthz.
Startup was not scheduled, to avoid repeatedly failing on the occupied port.
PORT_HELP
fi

cat <<EOF

Next steps
----------
1) Expose it over HTTPS (pick one, see README):
     Tailscale Funnel:   sudo tailscale funnel --bg $PORT
     Cloudflare Tunnel:  cloudflared tunnel --url http://127.0.0.1:$PORT   (quick test)

2) Your connector URL is:
     https://<your-public-host>/<TOKEN>/mcp

   Show the token any time with:
     sudo grep AI_BRIDGE_TOKEN $ENV_FILE

   Treat that URL like a root password.

3) Add it in Claude: Settings > Connectors > Add custom connector (no OAuth).
EOF

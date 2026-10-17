#!/usr/bin/env bash
# Full removal requires a trusted host terminal. Always preview before confirming.
set -euo pipefail
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ "$(id -u)" -eq 0 ] || [ "${1:-}" = --help ] || [ "${1:-}" = -h ]; then
  exec python3 -I "$SRC_DIR/uninstall.py" "$@"
fi
command -v sudo >/dev/null || { echo "ERROR: sudo is required; ask root to run this script from a host terminal." >&2; exit 1; }
if [ -t 0 ] && [ -t 1 ]; then
  exec sudo -- python3 -I "$SRC_DIR/uninstall.py" "$@"
else
  exec sudo -n -- python3 -I "$SRC_DIR/uninstall.py" "$@"
fi

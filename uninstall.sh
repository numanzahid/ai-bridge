#!/usr/bin/env bash
# Removes the AI Bridge service, config and stored sudo password.
# Keeps job logs in ~/.local/state/ai-bridge unless you pass --purge.
set -euo pipefail
sudo systemctl disable --now ai-bridge 2>/dev/null || true
sudo rm -f /etc/systemd/system/ai-bridge.service
sudo systemctl daemon-reload
sudo rm -rf /etc/ai-bridge
rm -rf "$HOME/.local/share/ai-bridge"
if [ "${1:-}" = "--purge" ]; then rm -rf "$HOME/.local/state/ai-bridge"; fi
echo "AI Bridge removed. If you exposed it with Tailscale Funnel or a Cloudflare tunnel, turn that off too:"
echo "  sudo tailscale funnel reset      or      stop/delete your cloudflared tunnel"

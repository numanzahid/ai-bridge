# Operations

Run these from a trusted host terminal as the service account. In direct root
mode, drop `sudo`. Never uninstall through the bridge you're removing.

## Updates and rollback

Bump the version first ([releasing](releasing.md)), then run this in a host terminal:

```bash
bash install.sh --update
```

The updater shows numbered steps, the current test (`32/300`), pass/fail/error/skip
counts, and a final summary. Quiet build steps print elapsed time every five seconds.
Tests that fail print their tracebacks and the log location; that release is not activated.
An existing authenticator is preserved without another code prompt. Otherwise the updater
offers enrollment. Codes are visible as you type; the Linux sudo password stays hidden.

Restart progress shows `deactivating`, `activating`, or `running` with elapsed time. Completion
requires a new server process, the expected version, `/healthz`, and authenticated MCP
`system_info` to succeed. It prints the running version and authenticator state, then explicitly
says no more input is needed. A timeout or failed service is reported as failure, not success.
An update from an older version can still take up to the old service's 90-second shutdown
timeout; progress remains visible. New servers limit graceful request shutdown to 10 seconds.

For setup or a restart without building another release:

```bash
bash install.sh --setup-authenticator
bash install.sh --restart
```

Running `bash install.sh` without flags also updates an existing installation when used in a
terminal. Without a terminal it retains staging-only behavior. MCP child processes cannot
use the interactive update/setup/restart flow.

For manual staging and activation:

```bash
bash install.sh --stage                      # build + test a release; prints its path
bash install.sh --activate '<RELEASE>'       # select it (needs sudo)
sudo systemctl restart ai-bridge             # the old code runs until this
curl --fail http://127.0.0.1:8765/healthz
```

- **Stage** builds a separate app and venv under `~/.local/share/ai-bridge/releases/`,
  runs counted tests and records checksums. Only passing releases are marked `READY`.
  No sudo, no restart.
- **Activate** re-checks the checksums, points `current` at the release (old one becomes
  `previous`) and updates the systemd unit. Settings and credentials are untouched.
- **Restarting from an MCP request?** Let the request return first:
  `sudo systemd-run --collect --on-active=3s /usr/bin/systemctl restart ai-bridge`
- **Check from the client:** login works, 18 tools listed, a harmless call works, a wrong
  token is refused. `/healthz` alone doesn't prove the client works.

**Rollback:** `bash install.sh --rollback` (or pass a release path), then restart.
It switches code only: not config, credentials, state, or work already done.

Detached jobs survive restarts (`KillMode=process`) and keep using their release.
Don't delete a release while its jobs run.

## Backups

Before updates, back up privately (preferably encrypted):

- `/etc/ai-bridge/`, the service unit and drop-ins, any bridge sudo policy
- Host config and state (`~/.config/ai-bridge`, `~/.local/state/ai-bridge`),
  `/var/lib/ai-bridge/installation.json`
- Releases still used by running jobs

## Tests

```bash
~/.local/share/ai-bridge/current/venv/bin/python -m unittest discover -s tests
bash -n install.sh uninstall.sh
```

Tests use fake commands. Check real sudo, systemd, restart, rollback and uninstall on a VM.

## Rotate the token

1. Generate a long random token; store it in a password manager.
2. `sudoedit /etc/ai-bridge/ai-bridge.env` and replace `AI_BRIDGE_TOKEN`.
3. Restart, update the client connector, and confirm the old token fails.

Never put tokens in commands, chat or logs. Sudo password: see [setup](setup.md#sudo-credentials).

## Troubleshooting

| Symptom | Check |
|---|---|
| Port warning at install | Change `AI_BRIDGE_PORT` or free the port; restart; update the tunnel |
| Health fails | `systemctl status ai-bridge`, `sudo journalctl -u ai-bridge -n 50` |
| MCP returns 404 | Token, header, `/mcp` path (`/healthz` needs no token) |
| Host/origin rejected (421/403) | `AI_BRIDGE_ALLOWED_HOSTS` / `AI_BRIDGE_ALLOWED_ORIGINS` |
| Health OK, client fails | HTTPS route, client login, refresh client metadata |
| Agent CLI missing or logged out | Service PATH/HOME; log in to the CLI as the service user |
| Sudo fails | Sudo policy, stored password, restart after changes |
| Resume fails | Exact session ID, project still exists, chat not open elsewhere |
| Old job "uncertain" | Inspect locally; never kill by a saved PID alone |
| SSH fails | Run `ssh_hosts`; check the alias and host key in a terminal as the service user |
| Host config error | `python -m ai_bridge.host_config --check` ([host guide](../ai_bridge/host_defaults/README.md)) |

## Full uninstall

Permanently removes bridge files, not the server. Back up what you want first.
From a separate host terminal:

```bash
bash install.sh --uninstall --dry-run    # review targets, blockers, manifest SHA256
bash install.sh --uninstall              # type the confirmation phrase shown
```

| Removed | Kept |
|---|---|
| Service unit, links, bridge drop-ins | Accounts, groups, homes |
| `/etc/ai-bridge` (env + sudo credential), bridge sudo policy | Agent CLIs, their logins and chat history |
| Installed app/releases, bridge config, state, jobs, logs | Source checkouts, task workspaces |
| Bootstrap copies, ownership record | Tunnels, network, global logs, backups |

- Running or uncertain jobs, unknown units, symlinks or mounts **block** removal. Nothing is force-killed.
- Identity comes from the unit, not your HOME. Without a unit or record, add `--user <account>`.
- Unattended removal needs all three, matching a fresh dry-run:
  ```bash
  bash uninstall.sh --yes --confirm 'UNINSTALL AI-BRIDGE for <user>' --manifest-sha256 '<SHA256>'
  ```
- Not a transaction: on failure, read the output, fix, and dry-run again.
- Remove only the bridge's tunnel route; **never reset a shared tunnel**.

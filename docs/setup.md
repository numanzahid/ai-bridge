# Setup

Read [SECURITY.md](../SECURITY.md) first: the token grants admin access as the service user.

## Install

Needs Linux, systemd, Python 3.10+ and internet access for dependencies. Coding CLIs
are optional; install and log in to them as the service user.

```bash
bash install.sh
systemctl status ai-bridge --no-pager
curl --fail http://127.0.0.1:8765/healthz
```

The installer:

- builds and tests a release under `~/.local/share/ai-bridge/releases/`
- writes `/etc/ai-bridge/ai-bridge.env` (random token, mode 0640) and enables the service
- offers to store a sudo password
- starts the service after a short delay

If the port is busy, install still finishes but doesn't start the service. Set
`AI_BRIDGE_PORT`, restart, and point your tunnel at it. On an existing install,
`install.sh` only stages; see [operations](operations.md#updates-and-rollback).

## Root bootstrap choices

| Command | Result |
|---|---|
| `bash install.sh` (normal user) | Runs as that user; sudo for install steps |
| `bash install.sh` (root, interactive) | Choose dedicated `ai-bridge` account (default) or direct root |
| `bash install.sh --dedicated-user NAME` (root) | Dedicated account with your chosen name |
| `bash install.sh --run-as-root` (root) | Direct root; works without a terminal |

- **Dedicated account:** asks before granting `ALL=(root) PASSWD: ALL` (sudo with a
  password, never NOPASSWD). Sets a password only for new accounts. Copies the source
  to `/var/lib/ai-bridge-bootstrap/<user>-<id>/`; update from that copy later.
- **Direct root:** every command and agent already runs as root, even with `sudo=false`.
- Fresh root install without a terminal is refused unless you pass `--run-as-root`.

## Connect a client

Keep `AI_BRIDGE_HOST=127.0.0.1`. Point an HTTPS tunnel or reverse proxy you control at
`http://127.0.0.1:8765`, and set `AI_BRIDGE_ALLOWED_HOSTS` to your public hostname.

| Auth form | Connector setting |
|---|---|
| Bearer header (preferred) | URL `https://bridge.example.com/mcp` + header `Authorization: Bearer <TOKEN>` |
| Token in path | `https://bridge.example.com/<TOKEN>/mcp` ("No authentication" in the client) |

- Enter the token only in the host and connector settings, never in chat or commands.
- `/healthz` only shows the server is up. Confirm the client lists 17 tools.
- Keep the client's action approvals on.
- Name each connector after its host (for example "Workspace"). The bridge also sends its title
  (`AI Bridge (<hostname>)`), version, description, website and icon, but ChatGPT and Claude
  mostly ignore these for custom connectors. Where the client asks for an icon, upload
  `ai_bridge/assets/icon-128.png`.

## Sudo credentials

Optional. The password lives in `/etc/ai-bridge/sudo.pass` (root, 0600) and reaches
the service through systemd `LoadCredential`.

- **With a password:** sudo authenticates over a private channel; it never reaches command input.
- **Without one:** only passwordless (`NOPASSWD`) rules work.
- **Root service:** sudo isn't used.

After changing the password or unit, restart. Staging never updates it.

## Configuration

Edit `/etc/ai-bridge/ai-bridge.env` with `sudoedit` (literal values, no shell syntax), then restart.

| Setting | Default |
|---|---|
| `AI_BRIDGE_TOKEN` | Required, 32+ random characters |
| `AI_BRIDGE_HOST` / `AI_BRIDGE_PORT` | `127.0.0.1` / `8765` |
| `AI_BRIDGE_ALLOWED_HOSTS` | Your public hostname (with port if used). Empty disables host checks |
| `AI_BRIDGE_ALLOWED_ORIGINS` | Extra allowed origins, comma-separated |
| `AI_BRIDGE_DEFAULT_CWD` | Service user's home |
| `AI_BRIDGE_TASKS_DIR` | `~/.ai-bridge-tasks` (never auto-deleted) |
| `AI_BRIDGE_TRUST_TASKS` | `0`. `1` trusts new task workspaces for Claude, Codex and Cursor; cleanup removes it |
| `AI_BRIDGE_AGENT_DEFAULT_PERMISSION` | `edit` |
| `AI_BRIDGE_CONFIG_DIR` / `AI_BRIDGE_STATE_DIR` | `~/.config/ai-bridge` / `~/.local/state/ai-bridge` |
| `AI_BRIDGE_SKILL_DIRS` | Skill folders, colon-separated |
| `AI_BRIDGE_MAX_OUTPUT_CHARS` | `30000` per response |
| `AI_BRIDGE_CAPTURE_BYTES` / `AI_BRIDGE_JOB_LOG_BYTES` / `AI_BRIDGE_AUDIT_BYTES` | 256 KiB / 4 MiB / 2 MiB |
| `AI_BRIDGE_RETENTION_DAYS` | `0` (keep jobs forever) |
| `AI_BRIDGE_SUDO_PASSWORD_FILE` | Overrides the systemd credential |
| `AI_BRIDGE_SUDO_APPROVAL` | `1`: privileged calls wait for approval on the host ([sudo approval](sudo-approval.md)). `0` turns it off |
| `AI_BRIDGE_CLAUDE_BIN`, `_CODEX_BIN`, `_CURSOR_BIN` | Agent CLI paths |
| `AI_BRIDGE_CLAUDE_EXTRA_ARGS`, `_CODEX_EXTRA_ARGS`, `_CURSOR_EXTRA_ARGS` | Extra CLI flags (check permission effects) |
| `AI_BRIDGE_CURSOR_HOME` | Cursor CLI storage |
| `AI_BRIDGE_DISABLED_AGENTS` | Agents to hide, comma-separated |

Agents also honor `CODEX_HOME` and `CLAUDE_CONFIG_DIR`. Client guidance, resources
and prompts: see the [host guide](../ai_bridge/host_defaults/README.md).

## Remote SSH preparation

As the service user, in a terminal:

1. Add named aliases to `~/.ssh/config` (static settings; `ProxyJump` is fine, `ProxyCommand`/`Match` aren't).
2. Verify each host's fingerprint and connect once to pin the key.
3. Make sure key-based login works without prompts.
4. For remote sudo, the remote host needs passwordless `sudo -n`. Uploads need GNU `ln`.

The bridge never accepts new host keys or forwards passwords. Details: [SSH tools](reference.md#ssh).

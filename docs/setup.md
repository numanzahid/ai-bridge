# Setup

Read [SECURITY.md](../SECURITY.md) first: a bridge token grants administrative
access as the service user, including any available sudo capabilities.

## First installation

Requirements: Linux, systemd, Python 3.10+, and outbound access for dependencies.
Unprivileged installation also needs sudo. Coding CLIs are optional; install and
authenticate them as the service user. No local model or bridge AI API key is needed.

Review the source and run from a trusted host terminal in its folder:

```bash
bash install.sh
systemctl status ai-bridge --no-pager
curl --fail http://127.0.0.1:8765/healthz
```

Fresh setup builds a versioned app/venv under the service user's
`~/.local/share/ai-bridge/releases/`, installs pinned dependencies, runs tests,
and records app/venv checksums before selecting the release. It can attempt to
install missing venv support through the host package manager. Test progress and
failures are shown; incomplete candidates remain for diagnosis, not activation.

It creates editable host guidance/state, offers sudo credential storage, and
writes `/etc/ai-bridge/ai-bridge.env` as root:service-primary-group, mode 0640.
The environment contains a random token and captured PATH/HOME. The systemd unit
is enabled for boot, with startup scheduled through a detached job after a short
delay. If the loopback port is unavailable, **installation still completes** but
startup is not scheduled. Change `AI_BRIDGE_PORT` in the environment file, restart,
and point the tunnel at the same port. The unit remains enabled: fix this before
reboot. Port availability can change between checking and startup.

An existing installation stages only; it does not refresh credentials/config or
restart. Use [operations](operations.md#updates-and-rollback) for deployment.

## Root bootstrap choices

| Invocation | Behavior |
|---|---|
| `bash install.sh` as a normal user | Uses that account; sudo authenticates installation actions. |
| `bash install.sh` as root, interactively | Choice of dedicated `ai-bridge` account (default) or direct root (high risk). |
| `bash install.sh --dedicated-user bridge-admin` as root | Interactive bootstrap with a chosen normal account. |
| `bash install.sh --run-as-root` as root | Explicit fresh root installation; allowed without a terminal. |

Root without a terminal refuses fresh setup unless the root-mode flag is given.
Dedicated bootstrap always needs a terminal and requires `runuser`, `useradd`,
`chpasswd`, sudo and `visudo`. Missing sudo can be installed after explicit approval
on supported package-manager hosts. No fallback root installation occurs on failure.

Dedicated bootstrap validates the account/home and asks before granting
`ALL=(root) PASSWD: ALL`: unrestricted root administration after authentication.
It sets a new account's password privately, never resets an existing password,
and adds neither NOPASSWD nor group membership for other software. Existing native
sudo policies may already grant passwordless access; review them separately.

The original checkout is not recursively re-owned. Public source is copied to a
new private `/var/lib/ai-bridge-bootstrap/<user>-<unique>/` directory and handed to
the account with a clean environment and attached terminal. Root's CLI credentials
are not copied. Unsafe paths, conflicting policies and another identity's existing
installation are refused. A failed bootstrap can leave an account, policy or copy
for diagnosis; review those before retrying. Direct root mode creates no account
or sudo credential: **ordinary commands and agents are already root**, even with
`sudo=false`. Changing an installed service identity is a separate migration.

## HTTPS and client connection

Keep `AI_BRIDGE_HOST=127.0.0.1`. Configure a tunnel/reverse proxy you administer
from a public HTTPS endpoint to `http://127.0.0.1:8765` (or your chosen port).
HTTPS protects transport; the tunnel does not replace bridge authentication.
Keep tunnel credentials outside the checkout. TLS terminators can inspect traffic
and path tokens can enter logs. A browser-only login challenge may block MCP.

Use either authentication form in your client's remote/custom MCP connector:

| Form | Private connector setting |
|---|---|
| Token path | `https://bridge.example.com/<TOKEN>/mcp` |
| Bearer header (preferred when supported) | URL `https://bridge.example.com/mcp`, header `Authorization: Bearer <TOKEN>` |

The bridge uses a shared token, not OAuth. For the path form, a client choice
called “No authentication” means no *additional* client-managed authentication.
Client account policies and support for remote MCP, headers, resources and prompts
vary. Keep client action approvals enabled and connect only chats needing access.

Enter credentials privately on the host and in connector settings, not chat,
screenshots, issue reports or command-line arguments. `/healthz` is unauthenticated
liveness only. Verify authenticated MCP initialization and `tools/list` through
the intended client, and confirm unauthorized `/mcp` requests fail. Connectivity
testing needs no privileged command.

## Sudo credentials

The optional `/etc/ai-bridge/sudo.pass` is root-owned, mode 0600. systemd delivers
it with `LoadCredential=sudo_pass:...`; the service holds it in memory.
`AI_BRIDGE_SUDO_PASSWORD_FILE` is checked first; the first readable, nonempty
credential file wins, with systemd delivery as the fallback.

With a password, execution uses `sudo -k -A -p '' --` and a temporary askpass helper
backed by a private Unix socket for the service UID/root. Authentication bytes
never go to target stdin. Jobs receive the credential privately in their runner's
stdin and keep it in memory. Without a password, non-root escalation uses
`sudo -n --` and needs an applicable passwordless policy. A root service bypasses
sudo. Policies must cover the bridge's shells/helpers, not an executable allowlist
assumed from the user's command. Same-user code may recover loaded credentials;
redaction is best effort. Manage password/unit changes privately, then restart
separately. Existing-install staging does not update them.

## Configuration

Edit `/etc/ai-bridge/ai-bridge.env` privately, preserving ownership/mode. This is a
systemd EnvironmentFile, not a shell script: use literal values. Restart to apply.

| Setting | Default / meaning |
|---|---|
| `AI_BRIDGE_TOKEN` | Required, at least 32 characters; use a long random token. |
| `AI_BRIDGE_HOST`, `AI_BRIDGE_PORT` | `127.0.0.1`, `8765` |
| `AI_BRIDGE_ALLOWED_HOSTS` | Empty; set your public hostname, including explicit port forms when used. |
| `AI_BRIDGE_ALLOWED_ORIGINS` | Optional comma-separated extra origins; applies with host validation. |
| `AI_BRIDGE_DEFAULT_CWD` | Service user's home for shell/file jobs; new agent work uses managed tasks. |
| `AI_BRIDGE_TASKS_DIR` | `~/.ai-bridge-tasks`; private managed workspaces, no automatic deletion. |
| `AI_BRIDGE_AGENT_DEFAULT_PERMISSION` | `edit`; controls agent flags, not shell/file access. |
| `AI_BRIDGE_CONFIG_DIR` | Otherwise `$XDG_CONFIG_HOME/ai-bridge` or `~/.config/ai-bridge` |
| `AI_BRIDGE_STATE_DIR` | `~/.local/state/ai-bridge` |
| `AI_BRIDGE_SKILL_DIRS` | Optional colon-separated discovery roots |
| `AI_BRIDGE_MAX_OUTPUT_CHARS` | `30000`; some control metadata has a minimum response budget. |
| `AI_BRIDGE_CAPTURE_BYTES` | `262144`, minimum 4096, per captured stream |
| `AI_BRIDGE_JOB_LOG_BYTES` | `4194304`, minimum 8192 |
| `AI_BRIDGE_AUDIT_BYTES` | `2097152`, minimum 4096 |
| `AI_BRIDGE_RETENTION_DAYS` | `0` disables automatic job cleanup. |
| `AI_BRIDGE_SUDO_PASSWORD_FILE` | Optional credential file overriding systemd delivery |
| `AI_BRIDGE_{CLAUDE,CODEX,CURSOR}_BIN` | Optional CLI executable overrides |
| `AI_BRIDGE_{CLAUDE,CODEX,CURSOR}_EXTRA_ARGS` | Operator-supplied CLI arguments; review permission effects. |
| `AI_BRIDGE_CURSOR_HOME` | Optional local CLI storage root |
| `AI_BRIDGE_DISABLED_AGENTS` | Comma-separated adapter names; does not restrict shell execution of CLIs. |

The brace forms above abbreviate separate variables, not literal key names.
With allowed hosts configured, loopback forms and derived HTTPS origins are also
allowed, along with built-in web-client origins. Without allowed hosts, MCP
DNS-rebinding checks are disabled. These checks supplement token authentication.
Native stores also honor `CODEX_HOME` and `CLAUDE_CONFIG_DIR`.

For editable MCP instructions/resources/prompts, see the
[host guide](../ai_bridge/host_defaults/README.md). For removal, use the
[full uninstall procedure](operations.md#full-uninstall): it includes bridge
instructions/state but retains accounts/homes, source clones, native history and
shared tunnels. Always preview from a separate host terminal.

## Remote SSH preparation

Install the SSH client and desired transfer clients locally; authenticate as the
bridge service account. Prepare named static aliases in that account's SSH config
and pin host keys through a separate trusted terminal after verifying fingerprints.
The adapter never accepts new keys automatically or modifies known_hosts. Batch
key/agent authentication must already work; agent forwarding is disabled. Prefer
static settings/ProxyJump over executable or conditional configuration. Confirm
remote shell utilities and the selected transfer subsystem are available; uploads
need GNU `ln -T`. Remote sudo must already work noninteractively with `sudo -n`;
the bridge never stores or forwards remote passwords. Keep existing local sudo
credentials separate. See [SSH tools](reference.md#ssh-administration) for limits.

Host tooling is moving to [ai-bridge-cli](https://github.com/numanzahid/ai-bridge-cli).
The server installs and starts independently of that project.

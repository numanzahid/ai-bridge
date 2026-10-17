# Setup

Read [SECURITY.md](../SECURITY.md) first. A connector token grants shell and file
access as the service user and potential root access through sudo. Use a host
whose data and credentials you are willing to expose to the connected client.

## Install from a host terminal

Requirements: Linux, systemd, Python 3.10+, and outbound access to install
Python dependencies. Unprivileged installation additionally requires sudo. Optional agent CLIs must be installed and authenticated as
the service user. No local model or bridge-specific AI API key is required.

Review the source, then run from a host terminal in the project folder:

```bash
bash install.sh
systemctl status ai-bridge --no-pager
curl --fail http://127.0.0.1:8765/healthz
```

On first installation the script:

1. Checks sudo and Python; attempts package-manager installation of missing venv support.
2. Builds a versioned app/venv under `~/.local/share/ai-bridge/releases/`, installs pinned dependencies from `requirements.lock`, runs tests, records checksums, and selects it.
3. Writes `/etc/ai-bridge/ai-bridge.env`, owned by root and the user's primary group, mode 0640, with a random access token and the captured interactive PATH/HOME.
4. Offers to verify and store a sudo password, if needed.
5. Initializes editable host configuration and an instruction guide without overwriting existing files.
6. Installs and enables `ai-bridge.service` as the normal user and schedules its startup through a detached systemd job after the installer returns.

Existing installations only stage a tested release. Re-running does not change
credentials, configuration, or the running service. See [operations](operations.md)
for activation, restart, and rollback. The installer is an automated bootstrap;
review its package-manager and privileged actions before running it.

## Root bootstrap choices

Interactive `bash install.sh` as root asks:

1. **Dedicated account (default/recommended):** create `ai-bridge`, or reuse an
   explicitly named normal account. Commands and agents run as that account.
   Bootstrap requires `sudo`, `visudo`, `runuser`, `useradd`, and `chpasswd`.
   If sudo is absent, it offers an explicit package install on apt/dnf hosts;
   otherwise install missing host packages first. It refuses system/root accounts, unsafe homes, conflicting
   sudo policy, and installations belonging to another identity. Existing account
   passwords are never reset. New accounts get a password entered privately at
   the terminal. Confirm the grant of `ALL=(root) PASSWD: ALL` explicitly. This
   grants full root administration through authenticated sudo, not a restricted
   command set. No NOPASSWD rule or Docker membership is added. Review other
   native sudo policies separately; they can already grant passwordless access.
2. **Direct root (explicit high risk):** the service and all commands/agents
   already run as root. No non-root account, sudo binary, or sudo credential is
   needed. `sudo=false` does not remove these privileges.

To pick a different dedicated account interactively:

```bash
bash install.sh --dedicated-user bridge-admin
```

For deliberate automated **fresh root** installation only:

```bash
bash install.sh --run-as-root
```

Root without a terminal and without an explicit mode refuses a fresh install
before staging or account/config changes. Dedicated bootstrap always requires a
terminal, including with `--dedicated-user`; password collection is never from
command-line arguments or an environment variable. Non-root users run without
these mode flags and keep the original workflow. `--stage`, `--activate`, and
`--rollback` remain separate operations, without a new bootstrap prompt.

A root-owned checkout is never recursively re-owned. Bootstrap copies only public
source/test/docs/dependency inputs into a new private directory under
`/var/lib/ai-bridge-bootstrap/<user>-<unique>/`, then hands that copy to the service
user and invokes the installer with a clean environment and attached terminal.
Original source ownership is preserved. Existing copy directories are not
replaced. Symlinked source/home paths and writable copy parents are refused.
The service user's HOME contains its releases, state, host instructions and
optional native CLI configuration; root's agent credentials are not copied.
The user authenticates sudo during setup and may store the verified password in
root-owned `/etc/ai-bridge/sudo.pass` (0600), delivered by systemd LoadCredential.
Keep the password private and use a trusted terminal.

An existing installation stages only; bootstrap will not replace another user's
service/config. Run updates as the installed service identity. On partial failure,
no fallback root installation is attempted. A newly created account, validated
sudo policy, or private source copy can remain for diagnosis; inspect these
before retrying. Bootstrap does not automatically delete accounts or data.
Full Linux account/systemd/password provisioning still needs clean-VM validation;
unit tests simulate these operations and do not change host accounts.

## Public HTTPS with bridge authentication

Keep `AI_BRIDGE_HOST=127.0.0.1`. Route a public HTTPS endpoint to
`http://127.0.0.1:8765` using a tunnel you administer. HTTPS protects transport;
the bridge token authenticates MCP requests. A public tunnel alone does not
restrict who can reach the endpoint. Do not expose the raw HTTP port publicly.

For Tailscale Funnel, install and authenticate Tailscale using its documentation,
then run `sudo tailscale funnel --bg 8765` from the host terminal. Use the HTTPS
hostname reported by `tailscale funnel status` privately in your connector.

For Cloudflare Tunnel, a temporary test route can use:

```bash
cloudflared tunnel --url http://127.0.0.1:8765
```

Temporary URLs can change. For a persistent route, create a named tunnel and DNS
route using your provider's setup procedure. A generic ingress fragment is:

```yaml
ingress:
  - hostname: bridge.example.com
    service: http://127.0.0.1:8765
  - service: http_status:404
```

Keep tunnel credential files outside the source checkout. The provider terminates
TLS and can see requests, including tokens in URL paths. Review proxy logging and
retention. Additional proxy authentication must be supported by your MCP client;
a browser login challenge can prevent a connector from working.

## Connect an MCP client

Use either of these authentication forms, substituting the token privately:

- Token path: `https://bridge.example.com/<TOKEN>/mcp`.
- Bearer header: URL `https://bridge.example.com/mcp` with `Authorization: Bearer <TOKEN>`.

Prefer a bearer header when the client supports it to reduce URL-token exposure.
The bridge implements shared-token authentication, not OAuth. In a client that
requires the path form, a setting called “No authentication” means no additional
client-managed authentication; the path token still authenticates the request.

In Claude or ChatGPT, use the remote/custom MCP connector controls available for
your account or workspace, supply the endpoint, and enable it only for chats that
need host access. Keep action approvals enabled. Check the provider's connection
instructions for client-specific availability and controls; no UI path is assumed
here. Other HTTP MCP clients can use the bearer form if they support headers.

Retrieve and enter credentials only through a trusted host terminal and private
client settings. Do not paste tokens into chat, screenshots, issue reports, shell
command arguments, or public documentation. `/healthz` returns `ok` without a
token; it proves liveness only. Validate an authenticated MCP initialization and
`tools/list` through the intended client, and confirm unauthenticated `/mcp`
requests fail. Do not use privileged commands merely to test connectivity.

## Sudo credentials

The optional password is stored at `/etc/ai-bridge/sudo.pass` with root ownership
and mode 0600. systemd supplies it using `LoadCredential=sudo_pass:...`; the
service loads it into memory. `AI_BRIDGE_SUDO_PASSWORD_FILE` is an alternative
credential source and takes precedence over the systemd credential.

Current execution uses `sudo -k -A -p '' --` and a temporary askpass helper backed
by a private Unix socket, accessible to the service UID and root. The helper
contains a socket path, not a password. Authentication bytes are kept separate
from the target command's stdin. For sudo jobs the password is passed privately
to the detached runner; it remains sensitive in process memory. Without a stored
password, execution uses `sudo -n --` and requires an applicable NOPASSWD policy.

The service user and root must be trusted: same-user shell execution may recover
service credentials. Redaction is not a vault. Avoid blanket passwordless sudo;
review native sudo policy locally. Policies must cover the invoked shells and
helpers, not just an imagined executable allowlist. Existing installs do not
update stored credentials when re-run; manage them privately on the host and
restart separately after a credential or unit change.

## Secure configuration

Edit `/etc/ai-bridge/ai-bridge.env` privately on the host. It is a systemd
EnvironmentFile, not a shell script; use literal values, not shell expansion.
Keep secrets out of the checkout. Preserve ownership and permissions when editing.

| Setting | Default / purpose |
|---|---|
| `AI_BRIDGE_TOKEN` | Required; at least 32 characters. Use a long cryptographically random value. |
| `AI_BRIDGE_HOST`, `AI_BRIDGE_PORT` | `127.0.0.1`, `8765` |
| `AI_BRIDGE_ALLOWED_HOSTS` | Empty by default; set comma-separated public hostnames, e.g. `bridge.example.com`, to enable MCP host/origin validation. Include explicit port forms for alternate HTTPS ports. |
| `AI_BRIDGE_ALLOWED_ORIGINS` | Additional comma-separated origins; takes effect with host validation enabled. |
| `AI_BRIDGE_DEFAULT_CWD` | Service user's home |
| `AI_BRIDGE_AGENT_DEFAULT_PERMISSION` | `edit`; consider `readonly` for agent work. This does not restrict shell/file tools. |
| `AI_BRIDGE_CONFIG_DIR` | Host config root; otherwise `$XDG_CONFIG_HOME/ai-bridge` or `~/.config/ai-bridge` |
| `AI_BRIDGE_SKILL_DIRS` | Optional colon-separated skill roots |
| `AI_BRIDGE_STATE_DIR` | `~/.local/state/ai-bridge` |
| `AI_BRIDGE_MAX_OUTPUT_CHARS` | `30000`, response budget |
| `AI_BRIDGE_CAPTURE_BYTES` | `262144`, minimum 4096 |
| `AI_BRIDGE_JOB_LOG_BYTES` | `4194304`, minimum 8192 |
| `AI_BRIDGE_AUDIT_BYTES` | `2097152`, minimum 4096 |
| `AI_BRIDGE_RETENTION_DAYS` | `0` disables automatic job retention cleanup |
| `AI_BRIDGE_CLAUDE_BIN`, `AI_BRIDGE_CODEX_BIN`, `AI_BRIDGE_CURSOR_BIN` | Optional absolute CLI executable overrides |
| `AI_BRIDGE_CLAUDE_EXTRA_ARGS`, `AI_BRIDGE_CODEX_EXTRA_ARGS`, `AI_BRIDGE_CURSOR_EXTRA_ARGS` | Operator-supplied CLI arguments; review their permission implications |
| `AI_BRIDGE_CURSOR_HOME` | Optional Cursor storage root instead of `~/.cursor` |
| `AI_BRIDGE_DISABLED_AGENTS` | Comma-separated adapter names to disable; does not restrict direct shell access to their CLIs |

When allowed hosts are configured, the server also allows loopback host/port
forms, derives HTTPS origins from allowed hosts, and includes Claude and ChatGPT
web origins. With no allowed hosts, MCP DNS-rebinding protection is disabled.
Host/origin validation supplements token authentication; it does not replace it.

Host manifests, tool descriptions, instructions, resources and prompts are covered
in the [reference](reference.md) and [host configuration guide](../ai_bridge/host_defaults/README.md).
Restart for environment or metadata changes; instruction/resource/prompt body edits
are read on demand. Agent storage honors `CLAUDE_CONFIG_DIR` and `CODEX_HOME`.

## Removing the installation

Use `bash install.sh --uninstall --dry-run` from a separate trusted host terminal
before full removal. `bash uninstall.sh` uses the same planner. The service
identity comes from its unit and passwd entry, so running as root or a different
sudo user does not target that caller's HOME. Fresh installs record directory
ownership privately for later verification of dedicated alternate config/state
paths. No account/home or native CLI history is automatically deleted. Read
[full uninstall](operations.md#full-uninstall) for typed confirmation, job safety,
the changed `--purge` meaning, retained artifacts and manual tunnel cleanup.

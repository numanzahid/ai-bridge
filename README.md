# AI Bridge

We built AI Bridge to manage a Linux machine from a chat on a phone or laptop,
without switching to SSH for every task. We also wanted to continue conversations
with the coding agents already installed on that machine.

The bridge is a small Python MCP server connecting your client to shell commands,
files, background jobs, and optional coding agents. Ask what is using disk space,
start a build and check it later, read a configuration file, or resume a project
chat with its existing context. New agent work without a project uses a private
`~/.ai-bridge-tasks/` workspace. A [host CLI](docs/agent-management.md) manages tasks,
saved sessions and agent jobs without an MCP token. Register `ai-bridge` with
`bash install.sh --install-cli` as the service user after release activation.

No local model or bridge-specific AI API key is needed. The connected client
supplies the AI. Native coding CLIs are optional and keep their own installation,
authentication, settings, and conversation stores. Host administration works
without them. Shared instructions and skills are discovered on demand; optional
MCP resources and prompt templates are editable on the host. Three SSH tools add
remote commands and file transfers through configured aliases with existing host-key trust.

```text
MCP client → HTTPS + bridge token → tunnel → 127.0.0.1:8765
                                              | shell, files, jobs
                                              | optional native agents
```

This is remote administrative access. The token grants the service user's access,
including potential root access through sudo. In root mode, commands and agents
already run as root. Read [SECURITY.md](SECURITY.md) before exposing an endpoint.

## Get started

Use Linux with systemd, Python 3.10+, and outbound dependency access. Review the
source, then run these commands from its folder in a trusted host terminal:

```bash
python3 --version
bash install.sh
systemctl status ai-bridge --no-pager
curl --fail http://127.0.0.1:8765/healthz
```

Follow [setup](docs/setup.md) to configure HTTPS, authentication, and the client.
Startup is delayed briefly; if the port is occupied, installation completes but
startup is left pending with instructions for changing the configured port.

## Choose the service identity

- As a normal user, installation uses that account and asks for sudo as needed.
- As root, interactive setup recommends a dedicated `ai-bridge` account with
  explicitly approved password-authenticated sudo. Direct root execution is the
  high-risk alternative; it needs no sudo binary or non-root account.
- Unattended fresh root setup requires `--run-as-root`. Dedicated bootstrap
  requires a terminal. See [bootstrap choices](docs/setup.md#root-bootstrap-choices).

The optional stored sudo password uses systemd credentials and a private askpass
channel, separate from command stdin. Privileged results distinguish a sudo
request, an attempt, and confirmed target launch; task success is separate.
Clients are instructed to announce privileged work. Agent `full` permission
changes CLI flags, not the Linux user.

## Update or remove

Re-running an existing installation only stages a tested release. Activation and
restart are separate; see [operations](docs/operations.md#updates-and-rollback).

For full removal, first preview `bash install.sh --uninstall --dry-run`, then run
`bash install.sh --uninstall` and type the displayed confirmation from a separate
host terminal. `bash uninstall.sh` is equivalent. Removal includes recognized
bridge instructions, configuration, jobs/logs, releases, service and credential.
Accounts/homes, native agents/history, unrelated source clones and shared tunnels
stay. Unknown ownership or unresolved jobs can block removal. See
[full uninstall](docs/operations.md#full-uninstall); never run it through the bridge.

## Documentation

- [Setup](docs/setup.md): identity, credentials, HTTPS, client connection, settings.
- [Reference](docs/reference.md): 23 tools, permissions, models and session continuity.
- [Operations](docs/operations.md): updates, rollback, recovery and full uninstall.
- [Reliability](docs/system-hardening.md): logging, execution limits and verification.
- [Host configuration](ai_bridge/host_defaults/README.md): MCP guidance, resources and prompts.
- [Shared instructions](docs/instructions-guide.md): organize documents for discovery.

Run tests from the checkout with the installed environment:

```bash
~/.local/share/ai-bridge/current/venv/bin/python -m unittest discover -s tests -v
```

Licensed under the [MIT License](LICENSE).

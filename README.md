# AI Bridge

We built AI Bridge because we wanted to manage our Linux server from ChatGPT or
Claude on a phone or laptop without switching back and forth to SSH. We also
wanted to use the coding agents already installed on that server and continue
their existing conversations from the same chat.

The bridge is a small Python MCP server. We keep it lightweight: it connects an
MCP client to shell commands, files, background jobs, and optional local coding
agents. Quick commands return directly; longer work runs in the background so
we can check progress without holding a chat request open.

No local AI models are needed. The connected client supplies the AI; AI Bridge
does not run a model or need an AI API key of its own. Claude Code, Codex CLI,
and Cursor CLI are optional and use their own installation and authentication.
Without them, shell, file, job, system information, instruction discovery, and
skill discovery tools still work.

```text
ChatGPT / Claude / another MCP client
                | HTTPS + access token
          public HTTPS tunnel
                |
        127.0.0.1:8765 / MCP
          | shell and sudo
          | files and background jobs
          | local Claude Code / Codex / Cursor CLI
```

We use this for server administration and delegating work to existing agents.
For example, we can ask what is using disk space, start a build and check it
later, read a configuration file, or resume a selected coding-agent conversation.
Shared instructions and local skills are discovered and read when needed.
Optional MCP resources and prompt templates are editable on the host.

This is privileged remote access. An access token grants the service user's
shell and file access, and **potential root access** when sudo is available.
Trust the client, the tunnel provider, and the content the assistant reads.
Prompt injection and credential disclosure remain risks; redaction is best effort.
Read [SECURITY.md](SECURITY.md) before exposing an endpoint.

## Quick start

On a Linux host with systemd, Python 3.10+, and sudo, obtain the source for
[numanzahid/ai-bridge](https://github.com/numanzahid/ai-bridge), review it, and run
these five lines from the project folder:

```bash
python3 --version
bash install.sh
systemctl status ai-bridge --no-pager
curl --fail http://127.0.0.1:8765/healthz
# Follow docs/setup.md to add HTTPS and configure your MCP client securely.
```

On a fresh install, `install.sh` bootstraps a versioned app and virtual
environment, installs dependencies, runs tests, creates configuration and a
systemd service, and schedules startup. It can install missing Python venv
support through the host package manager. It asks about storing a sudo password.
On an existing install it only stages a tested release; activation and restart
are separate. Non-root installation keeps using your current account. When root
runs `bash install.sh` interactively, choose a dedicated `ai-bridge` account
(recommended) or direct root execution (high risk). Dedicated bootstrap copies
public source into an isolated workspace and offers password-authenticated sudo;
it never grants blanket NOPASSWD. Root mode needs no sudo binary. Unattended
root installation requires explicit `--run-as-root`; unattended dedicated-account
bootstrap is refused. See [bootstrap choices](docs/setup.md#root-bootstrap-choices).

The service defaults to loopback. Configure a public HTTPS tunnel and keep token
authentication enabled before adding the endpoint to a client. Client account
and workspace policies determine whether remote MCP connectors are available.

## Sudo setup

The optional stored credential uses systemd credentials and a private askpass
helper. Read the [sudo credential mechanics](docs/setup.md#sudo-credentials) and
trust limitations before enabling privileged tools. Privileged command/file
results and job views report requested/attempted sudo, confirmed target launch,
and effective launch identity. Clients are instructed to announce the task,
target, and outcome. A root service is already root even with `sudo=false`;
agent `full` permission never changes the Linux user.

## Documentation

- [Setup](docs/setup.md): installation, HTTPS tunnels, connectors, credentials, and configuration.
- [Reference](docs/reference.md): all 17 tools, agent permissions, sessions, models, resources, and prompts.
- [Operations](docs/operations.md): staged updates, restart, rollback, backups, token rotation, and troubleshooting.
- [Security and reliability](docs/system-hardening.md): trust boundaries, logging, cleanup, and deployment checks.
- [Shared instructions](docs/instructions-guide.md) and [host configuration formats](ai_bridge/host_defaults/README.md).
- [Responsible disclosure](SECURITY.md).

Run the regression suite from the source folder with the installed environment:

```bash
~/.local/share/ai-bridge/current/venv/bin/python -m unittest discover -s tests -v
```

AI Bridge is licensed under the [MIT License](LICENSE).

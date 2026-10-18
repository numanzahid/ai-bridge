# AI Bridge

Manage a Linux machine from an AI chat on your phone or laptop, and continue chats
with coding agents (Claude Code, Codex, Cursor) already installed on it.

AI Bridge is a small Python MCP server that gives your MCP client shell commands,
files, background jobs, SSH and optional coding agents. The client supplies the AI;
no local model or extra API key is needed.

```text
MCP client → HTTPS + token → tunnel → 127.0.0.1:8765 → shell, files, jobs, agents
```

> **This is remote admin access.** The token grants the service user's access,
> including root through sudo. Read [SECURITY.md](SECURITY.md) before exposing it.

## Install

Linux with systemd and Python 3.10+. From the reviewed source folder:

```bash
bash install.sh
systemctl status ai-bridge --no-pager
curl --fail http://127.0.0.1:8765/healthz
```

Then set up HTTPS and your client: [setup](docs/setup.md).

- **Normal user:** installs as that user; sudo is used when needed.
- **Root:** offers a dedicated `ai-bridge` account (recommended) or direct root (high risk).
  Unattended root install needs `--run-as-root`.

## Update or remove

- **Update:** re-running `install.sh` only stages a tested release; activation and
  restart are separate. See [releasing](docs/releasing.md).
- **Remove:** `bash install.sh --uninstall --dry-run`, then `bash install.sh --uninstall`,
  from a host terminal (never through the bridge). See [full uninstall](docs/operations.md#full-uninstall).

## Docs

- [Setup](docs/setup.md): install choices, HTTPS, client connection, settings
- [Tools](docs/reference.md): the 17 tools with examples
- [Sudo approval](docs/sudo-approval.md): privileged calls wait for your approval on the host
- [Operations](docs/operations.md): deploy, rollback, troubleshooting, uninstall
- [Releasing](docs/releasing.md): versions and release steps
- [Reliability](docs/system-hardening.md): limits, logging, containment
- [Host configuration](ai_bridge/host_defaults/README.md): client guidance, resources, prompts
- [Instructions guide](docs/instructions-guide.md) · [Agent session contract](docs/agent-management.md)

Run the tests with the installed environment:

```bash
~/.local/share/ai-bridge/current/venv/bin/python -m unittest discover -s tests
```

MIT licensed. See [LICENSE](LICENSE).

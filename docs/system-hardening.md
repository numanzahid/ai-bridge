# Security and reliability

What the bridge does and doesn't protect. Trust boundary: [SECURITY.md](../SECURITY.md).

## Access

- One shared token reaches every tool. There are no per-client roles, path limits or sandbox.
- Use a dedicated service account, keep the listener on loopback behind HTTPS, and set allowed hosts.
- Members of the service user's primary group can read the env file (0640); review that group.
- Keep client approvals on: files, web pages, skills and agent replies can carry prompt injection.
- Redaction masks known bridge secrets only. Child processes don't get `AI_BRIDGE_*` variables,
  but other secrets in the environment remain visible.

## Logs and retention

- Audit log: `~/.local/state/ai-bridge/audit.log`, private and rotated, but not tamper-proof.
- Job output, agent histories and backups can contain secrets. Redact before sharing.
- Job cleanup is off by default (`AI_BRIDGE_RETENTION_DAYS=0`). When on, it never removes
  running or uncertain jobs. Old releases and native histories are never cleaned.

## Execution limits

- Commands and output are bounded. Jobs run detached with head/tail logs.
- Cancel reports how complete the cleanup was. Privilege boundaries or escaped
  child processes can survive.
- Jobs outlive restarts (`KillMode=process`). Keep their release until they finish.
- `NoNewPrivileges=yes` breaks sudo. Test any unit hardening on a spare host.
- Nothing (cancel, rollback, uninstall) undoes work already done.

## Containment

If access may be compromised, from a host terminal:

1. Disable the bridge's public route and run `sudo systemctl stop ai-bridge`.
2. Check for detached jobs still running and stop them deliberately.
3. Rotate the bridge token and any exposed credentials.
4. Review affected files and logs, and restore from backups if needed.

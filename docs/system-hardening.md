# Security and reliability guide

AI Bridge is a remote administration interface. It intentionally exposes shell,
files, background jobs, local instructions/skills, and agent histories. Read
[SECURITY.md](../SECURITY.md) for disclosure and the privileged-tool threat model.

## Trust and access boundaries

Use a dedicated service account and a non-critical host where practical. Keep
valuable work backed up. The bridge runs as a normal user, but that user's
files, credentials, and sudo capabilities are reachable through authenticated
tools. There is no per-client identity, filesystem jail, or per-tool authorization
boundary behind the shared token. Agent permission settings affect agent turns;
they do not restrict direct shell/file access.

Keep the listener on loopback, use a public HTTPS tunnel you trust, and configure
`AI_BRIDGE_ALLOWED_HOSTS=bridge.example.com` for your endpoint. Follow the
[setup guide](setup.md) for host/origin rules and credential mechanics. Allowing
an origin is not authorization. TLS terminators can inspect traffic; path tokens
can enter provider logs, browser history, screenshots, and client settings.

Limit who can read the environment file and service credentials. A primary group
shared with other users expands access to the 0640 environment file. Review the
actual ownership, mode, and group membership locally. Do not add blanket sudo
rules to solve an authentication problem. Shell access as the service user is
already a broad capability, including potential access to the credential socket
or the service's loaded credentials.

Keep client action approvals enabled and enable the connector only when needed.
Treat web pages, files, agent output, skills, resources, and prompt templates as
untrusted inputs that may contain prompt injection. Server instructions and
read-only annotations guide clients; they do not enforce an execution policy.
Neither `readonly` agent mode nor output redaction is a host security boundary.

## Credentials and logs

The response/audit redactor masks the configured token and sudo password and
selected encodings, including base64, hex, URL encoding, and reversal. This is
best effort: arbitrary transformations, third-party credentials, command-created
files, proxy logs, and external systems may escape redaction. Child process
environments remove `AI_BRIDGE_*` and `CREDENTIALS_DIRECTORY`; other inherited
credentials and files still require care. Do not deliberately test redaction by
asking a remote client to read a real secret.

The audit log defaults to `~/.local/state/ai-bridge/audit.log`. It records tool
activity and operation identifiers, and rotates within the configured budget.
It is local and mutable, not tamper-proof evidence. Job outputs, parsed summaries,
agent history databases, and backups can contain sensitive content even when
known bridge secrets are masked. Inspect locally, redact copies before sharing,
and avoid collecting more output than needed.

Set bounded output and log budgets appropriate to the host. Automatic job
retention is disabled by default (`AI_BRIDGE_RETENTION_DAYS=0`). When enabled,
cleanup is conservative about running or uncertain legacy jobs; it is not a
complete data-retention policy. Manage native agent history, external logs,
backups, and deployment artifacts separately.

## Execution and recovery

Synchronous commands have bounded timeouts and captured output. Long-running
work uses detached sidecars, bounded output snapshots, atomic metadata updates,
and process identity checks where available. Review cleanup status on timeout
or cancellation: signals can fail across privilege boundaries, and descendants
can escape a process group. PID reuse and legacy records require caution.

The systemd unit deliberately uses `KillMode=process` so jobs survive bridge
restarts. Do not add `NoNewPrivileges=yes` while expecting sudo to work. Tightening
unit settings can break intentional features; test a proposed policy on an
isolated host before deploying it. Preserve the originating release until its
jobs finish. Rolling back bridge code does not undo work already performed.

Atomic file writes are opt-in and reject unsupported file types/metadata.
Optimistic hash checks detect stale content but do not provide a lock. Use
backups and application-specific validation before changing important files.
Agent CLI output and storage formats can change; exercise the adapters with the
versions you deploy, without assuming unit tests prove provider availability.

Stage, verify checksums, select, restart, and validate authenticated tools as
separate steps. See [operations](operations.md) for commands and rollback. Test
health, unauthorized rejection, authenticated initialization, tool discovery,
and harmless shell/file/job behavior. Test sudo privately only when required,
without retrieving credentials through the connector. Test the actual public
route separately from loopback and keep a trusted recovery terminal available.

## Emergency containment

From a trusted host terminal, disable the tunnel and stop the service:

```bash
sudo systemctl stop ai-bridge
```

Detached jobs may continue. Inspect and terminate them separately using verified
process identities and appropriate privileges. Revoke/rotate exposed bridge and
other host credentials, inspect affected files and logs privately, and restore
from known-good backups when needed. Stopping the listener or rotating a token
cannot undo commands that already ran.

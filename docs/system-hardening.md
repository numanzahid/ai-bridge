# Security and reliability

AI Bridge intentionally exposes administrative shell/file/job access and optional
native agents. See [SECURITY.md](../SECURITY.md) for the trust boundary and
[operations](operations.md) for recovery commands.

## Access and credentials

A dedicated service account is recommended. Its files, credentials and sudo
capabilities remain reachable through authenticated tools; direct root mode
already has root identity. There is no per-client identity, filesystem jail or
per-tool authorization behind the shared token. Agent flags and read-only tool
annotations do not restrict the host administration tools.

Keep the listener on loopback behind trusted HTTPS and configure allowed hosts
for the endpoint. Origins are not authentication. A TLS terminator can inspect
requests, and path tokens can enter logs/history. Review primary group membership:
other members may read the root:service-group 0640 environment file. Same-user
shell execution can recover loaded credentials or access the private credential
channel. See [setup](setup.md) for delivery and precedence.

Keep client approvals enabled. Files, web content, skills, prompts and agent
output can contain prompt injection; server guidance is not an enforcement layer.
The redactor masks known bridge secrets and selected encodings, not arbitrary
credentials, transformations, command-created files or external logs. Child
environments strip `AI_BRIDGE_*` and `CREDENTIALS_DIRECTORY`; other inherited
secrets remain accessible. Never test redaction using a real secret remotely.

## Logging and retention

Audit defaults to `~/.local/state/ai-bridge/audit.log`: private local activity
records with operation identifiers and bounded rotation, not tamper-proof evidence.
Raw job output, summaries, native histories and backups may retain sensitive
content. Inspect locally and redact shared copies. Automatic job cleanup is off
by default (`AI_BRIDGE_RETENTION_DAYS=0`); when enabled it conservatively retains
running/uncertain jobs. Native history, external logs, archives and old releases
need separate retention decisions.

## Execution limits

Commands have bounded capture/timeouts; jobs use detached runners, bounded
head/tail logs and atomic metadata snapshots. Review timeout/cancel cleanup:
privilege boundaries can prevent signals and descendants can escape groups.
Process identity/boot checks reduce PID-reuse risk; old records remain uncertain.
Privilege disclosure confirms launch identity separately from task outcome.

`KillMode=process` allows jobs to outlive bridge restarts/stops. Keep their
originating releases. `NoNewPrivileges=yes` conflicts with intentional sudo use;
test unit hardening on an isolated host. Atomic file replacement is opt-in and
rejects unsupported types/metadata. Hash checking is optimistic, not a lock.
Neither cancellation nor release rollback reverses completed work.

Validate adapters against the deployed CLI versions; native storage/output can
change. Tests cannot establish live account availability, public reachability or
real systemd/sudo provisioning. Stage, verify, select, restart and check authenticated
tools separately. Keep a trusted recovery terminal available. Full uninstall has
additional checks but is not transactional; see its [procedure](operations.md#full-uninstall).

## Containment

From a trusted host terminal, disable only the bridge's public route and stop it
with `sudo systemctl stop ai-bridge` (or directly as root). Detached jobs may
continue: inspect them separately using verified identities and appropriate
privileges. Rotate exposed bridge/host credentials, inspect affected files/logs,
and recover from known-good backups as needed. Stopping or rotating access
cannot undo commands already run.

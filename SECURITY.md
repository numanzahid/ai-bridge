# Security

AI Bridge grants authenticated clients shell and file access as the service
user, plus potential root access through sudo. Treat an access token or token URL
as an administrative credential. This project is suitable only where that level
of remote access is intentional and the client and host are trusted.

## Responsible disclosure

Please do not disclose live credentials, private host details, or exploit steps
for an unpatched vulnerability in a public issue. Use GitHub's private
vulnerability reporting for `numanzahid/ai-bridge` if it is enabled. If that option
is unavailable, open a minimal issue requesting a private contact channel without
sensitive details, and wait for a private channel before sharing a report.

A useful private report describes the affected code/version, impact, conditions
required, and a minimal reproduction using synthetic data. Remove real tokens,
passwords, session IDs, hostnames, addresses, and deployment paths. Do not probe
systems you do not own or have authorization to test. No response-time or
supported-version guarantee is currently promised; include the version tested.

## Privileged tool security

- The shared token authorizes the whole tool surface. It is not scoped per user,
  project, or operation; read-only annotations are hints for clients.
- Shell/file tools can access the service user's data and credentials. Sudo can
  extend that access to root. Stored sudo credentials are accessible to trusted
  service processes; same-user code execution can compromise them.
- Agent permission levels are CLI flags, vary by provider, and do not constrain
  bridge shell/file tools. Cursor `edit` and `full` both use `--force`.
- Prompt injection in files, web content, instructions, skills, resources, prompts,
  or agent output can influence a connected assistant. Keep approvals enabled.
- HTTPS tunnel providers and clients can see sensitive requests and responses.
  URL-path tokens are particularly prone to logging. Prefer bearer headers where
  supported and rotate credentials after suspected exposure.
- Redaction covers known bridge credentials and selected encodings on supported
  output paths. It cannot guarantee secrecy of arbitrary data or external logs.
- Stopping/restarting the service does not necessarily stop detached jobs.
  Rollback does not reverse changes made by tools or agents.

Keep production configuration, runtime data, archives, and operator validation
records outside any public release. An ignore rule does not remove files already
tracked or erase history. Before publishing an existing history, its owner must
independently review that history and all proposed artifacts for disclosures.

See [setup](docs/setup.md), [operations](docs/operations.md), and
[security and reliability](docs/system-hardening.md) for generic host procedures.

## Safe removal boundary

Full uninstall is permanent and must run from a trusted host terminal, not the
MCP connection being stopped. Preview the manifest before typed confirmation.
It checks the installed identity, ownership, path traversal, effective unit,
job state and process groups; it never signals a stored job PID. Unknown/custom
ownership or unresolved jobs require manual review rather than force deletion.
New installs keep a root-private ownership record for dedicated alternate paths.
The removal is not transactional; keep the host quiescent, preserve the printed
manifest and inspect partial failures. Fully detached processes cannot always
be attributed from historical records. Accounts/homes, native agent data,
unrelated source, shared network/tunnel configuration, system journals and
external backups remain. Remove only an individually verified bridge tunnel
route manually; never reset shared infrastructure as part of uninstall.

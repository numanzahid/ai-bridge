# Security

A bridge token grants authenticated clients shell/file access as the service
user, including potential root access through sudo. Direct root mode already has
root identity. Use only where this remote administrative access is intentional
and the host/client are trusted; treat tokens and token URLs as credentials.

## Responsible disclosure

Do not publish live credentials, private host details or exploit steps for an
unpatched vulnerability. Use the repository's private vulnerability-reporting
channel if available. Otherwise open a minimal issue requesting a private contact
channel, without sensitive details, and wait before sending the report.

Include the affected version, impact, conditions and a minimal reproduction using
synthetic data. Remove real tokens, passwords, session IDs, hostnames, addresses
and deployment paths. Test only systems you own or are authorized to assess.
No response-time or supported-version guarantee is promised.

## Trust boundary

- One shared token authorizes the whole tool surface, without per-client roles,
  path confinement or per-tool authorization. Read-only annotations are hints.
- Same-user shell execution can reach files, native credentials and loaded sudo
  credentials. Redaction is not a vault or a sandbox.
- Agent permission flags vary by CLI and do not restrict bridge shell/file tools.
  They do not grant Linux root; agents inherit service identity. Some native edit
  and full settings have identical effective flags; see the [reference](docs/reference.md).
- Content in documents, web pages, skills, resources, prompts or agent replies
  can inject instructions. Keep client approvals enabled.
- HTTPS intermediaries/clients can inspect requests. Path tokens can enter logs;
  prefer bearer headers where supported and rotate after suspected exposure.
- Redaction covers known bridge secrets and selected encodings, not arbitrary
  data, transformations, external logs or command-created files.
- Task directories and metadata are private workspaces, not filesystem isolation.
  Same-user code can alter them; locking does not constrain unrelated writers.
  Cleanup refuses uncertain jobs and, by default, uninspectable same-user processes.
  CLI `--yes --allow-uninspectable` explicitly accepts the risk of deleting files
  those enumerated PIDs may use; it bypasses no other cleanup guards. Task
  workspaces remain after full uninstall; remove verified tasks separately.
- Restart/stop may leave detached jobs running. Cancellation and rollback cannot
  undo completed work. Privilege metadata reports launch, not task success.

Keep configuration, runtime data, backups and operator validation records out of
public releases. Ignore rules do not remove already published history; review
history and proposed artifacts independently before publication.

## Removal boundary

Full uninstall is permanent: preview and confirm from a separate trusted host
terminal, never the MCP connection being stopped. Unknown ownership, unsafe
paths/unit behavior or unresolved jobs cause refusal, not force deletion. A
root-private ownership record helps verify dedicated alternate paths when present.
Stored PIDs are never signalled by uninstall. Fully detached processes cannot
always be attributed; keep the host quiescent and inspect partial failures.
Removal is not transactional and may leave the service stopped after a failure.

Accounts/homes, native agents/history, unrelated source, shared networking,
global journals and external backups remain. Remove only an individually verified
bridge tunnel route manually, never shared infrastructure. See
[full uninstall](docs/operations.md#full-uninstall), [setup](docs/setup.md) and
[reliability](docs/system-hardening.md) for procedures and limitations.

## SSH trust

SSH extends token access to configured accounts; config and the authenticated
remote host must be trusted. Discovery is offline and hides routing/key metadata.
Conditional/executable config is not evaluated. Existing host-key trust and batch
mode are mandatory; no key seeding, agent forwarding or LOCAL sudo for access.
Remote sudo receipts trust the host, not cryptographic attestation; no passwords
are stored/forwarded. Transfers avoid replacement/deletion of existing destinations;
inspect ambiguous commits/staging cleanup. Remote output may still contain secrets.

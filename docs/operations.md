# Operations

Run administrative commands below from a trusted host terminal. These are
operator procedures, not actions performed by reading this guide. Use
`bridge.example.com` as a placeholder for your privately configured endpoint.

## Stage, select, then restart

From the source folder, as the installed service user (including root only if
root mode was deliberately selected):

```bash
bash install.sh --stage
```

This builds and tests a release at its final path under
`~/.local/share/ai-bridge/releases/`, records dependency versions and checksums,
and marks it READY only after validation succeeds. Incomplete candidates remain
for diagnosis and cannot be selected. Staging needs no sudo and does not change
the running process. A plain `bash install.sh` also stages only when it detects
an existing installation.

Use the candidate path printed by staging, represented below by `<RELEASE>`:

```bash
python3 deploy.py verify '<RELEASE>'
bash install.sh --activate '<RELEASE>'
```

Selection verifies app/venv checksums, updates `current` and `previous` symlinks,
rewrites the service unit's executable and PYTHONPATH, and reloads systemd. It
preserves environment and credential files and saves an original unit backup
as `ai-bridge.service.before-versioned` if absent. The running process still
uses its old release until restarted. `python3 deploy.py activate` alone only
updates release symlinks; use the installer wrapper to update the unit too.

Restart from a host terminal:

```bash
sudo systemctl restart ai-bridge
systemctl status ai-bridge --no-pager
curl --fail http://127.0.0.1:8765/healthz
```

If administering through MCP, a synchronous restart can terminate the request
serving it. Arrange a detached restart after the call returns instead:

```bash
sudo systemd-run --collect --on-active=3s /usr/bin/systemctl restart ai-bridge
```

Verify authenticated MCP initialization, `tools/list`, a harmless tool call,
and public HTTPS reachability from the intended client. Check unauthorized MCP
requests fail. Health alone does not prove authentication or tool compatibility.
Review journal errors privately and refresh client metadata after tool changes.

The unit uses `KillMode=process` so detached jobs can survive a bridge restart.
They continue using their originating release's code. Do not prune that release
while its sidecars run. Stopping the service does not necessarily stop jobs.

## Bootstrap ownership and privilege verification

Root bootstrap copies public source to a private service-user-owned directory
under `/var/lib/ai-bridge-bootstrap/`; it leaves the original checkout untouched.
Use that copy or another reviewed checkout accessible to the same account for
updates. Run staging with that account's HOME, not root's HOME for a dedicated
service. Do not recursively change ownership of a checkout or copy native CLI
credentials between accounts. Root-mode maintenance does not need the sudo
binary; documentation commands prefixed with sudo may be run directly as root.
The activation wrapper also handles root directly. Account creation is a fresh
bootstrap operation; staging/activation/rollback never create accounts or change
sudo passwords/policies.

Before deployment, validate on a disposable Linux/systemd VM: interactive default
and explicit root choices, a root-owned source tree, missing sudo handling,
existing-user/password policy, failed authentication, interrupted provisioning,
and restart/rollback as the selected account. Inspect unit User/Group, HOME,
source/config ownership, and root-only credential permissions. Tests use fake
commands and temporary files; they do not replace this real provisioning check.

After a coordinated restart, verify authenticated tool discovery still exposes
17 tools. With authorization, run a harmless ordinary identity command, then
`run_command` with `sudo=true` and `id -u`; inspect both outcome and `privilege`.
Test privileged file access in temporary paths, background job completion,
job status/log/list disclosure, and empty target stdin under password and
passwordless policies. Root services should report existing root execution,
not successful sudo. Check a historical job remains readable with actual-use
unknown. Never print credential contents to verify them. No live account,
credential, service, or network changes are part of source regression tests.

Rollback keeps the original staged semantics and existing state. New job
privilege metadata is additive and runner JSON remains inside the private
sidecar directory, so older root `*.json` readers still see only job records.
Old releases will not provide the new disclosure fields; refresh clients after
rollout or rollback. Retain releases used by running jobs. Changing service
identity is a separate migration requiring ownership and credential review;
bootstrap deliberately refuses to overwrite a different installed identity.

## Backups and rollback

Before an update, back up configuration, host guidance, relevant workspaces,
and state to private storage. Include:

- `/etc/ai-bridge/`, the service unit and any drop-ins.
- `~/.config/ai-bridge/` or the configured host root.
- `~/.local/state/ai-bridge/` or the configured state root.
- The last working release, release symlinks, and any release still used by jobs.
- Agent history/configuration if needed; these can contain credentials and private prompts.

Use access-restricted, preferably encrypted backups. Keep them outside the
public checkout. Coordinate snapshots of mutable agent databases and running
jobs; a copy during writes may be inconsistent. Test restoration on an isolated
host rather than overwriting a live installation as a first test.

Select the previous verified release and restart separately:

```bash
bash install.sh --rollback
sudo systemctl restart ai-bridge
```

An explicit verified release path can follow `--rollback`. Repeated selection
changes `previous`; record your intended rollback target privately. Old mutable
installations may need their saved unit restored manually for a first migration
rollback. Restore it from the private backup, reload systemd, and restart from
the host terminal. Release rollback does not restore environment files, host
configuration, job data, or changes made by commands and agents. Validate health,
authenticated tools, and sidecar status again.

## Rotate the token without displaying it

Use a trusted terminal. Pause the public tunnel while rotating if continued use
of the old token is a concern. Do not print the token or put it in command-line
arguments, chat, issue reports, or logs.

The following host-only procedure prompts for a new token with hidden input,
replaces exactly one simple token assignment, and preserves file permissions
and ownership. Generate and store a long random token in a password manager,
then paste it into the hidden prompt and private connector settings.

```bash
sudo python3 - <<'PY'
import getpass
import os
from pathlib import Path
import re
import tempfile

path = Path('/etc/ai-bridge/ai-bridge.env')
new_token = getpass.getpass('New random bridge token: ')
if not re.fullmatch(r'[A-Za-z0-9_-]{32,}', new_token):
    raise SystemExit('Use at least 32 random URL-safe characters.')
text = path.read_text()
updated, count = re.subn(r'^AI_BRIDGE_TOKEN=.*$',
                         'AI_BRIDGE_TOKEN=' + new_token, text, flags=re.M)
if count != 1:
    raise SystemExit('Expected exactly one token assignment; edit privately.')
metadata = path.stat()
fd, temporary = tempfile.mkstemp(prefix='.bridge-env-', dir=path.parent)
try:
    with os.fdopen(fd, 'w') as stream:
        os.fchown(stream.fileno(), metadata.st_uid, metadata.st_gid)
        os.fchmod(stream.fileno(), metadata.st_mode & 0o777)
        stream.write(updated)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
PY
sudo systemctl restart ai-bridge
```

Update each connector privately, restore the tunnel if paused, and verify that
the new credential works and the old one fails. Replace stored connector URLs,
headers, and any password-manager entries. Rotation does not remove old tokens
from provider logs or backups, and does not terminate already running commands.

## Troubleshooting

| Symptom | Check |
|---|---|
| Health fails | Service status, loopback port, and `sudo journalctl -u ai-bridge -n 50`; inspect locally before sharing. |
| MCP returns 404 | Token/header and `/mcp` path. `/healthz` is deliberately unauthenticated. |
| Host/origin rejected | Allowed hostname and explicit port, proxy Host handling, allowed origins; see [setup](setup.md). |
| Health works but connector fails | HTTPS route, authenticated initialization, client policy and metadata cache; health is not an MCP test. |
| CLI works in terminal but is missing in bridge | Captured service PATH/HOME; set an absolute `AI_BRIDGE_*_BIN` override if needed. |
| Agent authentication fails | Log in as the same user running the service, using the native CLI. |
| Sudo fails | Native sudo policy, current password, credential loading, and helper/shell policy. Staging does not refresh credentials. |
| Session cannot resume | Exact session ID, saved project, `resumable` and `history_note`; avoid concurrent terminal/editor use of that chat. |
| Adapter unavailable after CLI update | Storage/output format compatibility; host shell/files/jobs remain usable. |
| Old job remains uncertain | Inspect its process and cleanup report locally; do not kill a PID based only on stale metadata. |
| Configuration fails startup | Validate host manifests and tool override names; see [reference](reference.md). |

## Uninstall

`bash uninstall.sh` stops/disables the service and deletes the installed app,
configuration, and stored sudo password. It retains job state by default;
`--purge` deletes that state too. These are destructive procedures: back up first,
inspect running jobs, and turn off the public tunnel separately. Host guidance
and agent histories are not removed by the script.

See the [security and reliability guide](system-hardening.md) for logging,
retention, and emergency containment limits.

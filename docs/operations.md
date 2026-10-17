# Operations

Use a trusted host terminal for these procedures. Run release commands as the
installed service account with its HOME. In direct root mode, omit sudo prefixes
if sudo is absent. Never run full uninstall through the bridge being removed.

## Updates and rollback

From a reviewed source folder accessible to the service account:

```bash
bash install.sh --stage
python3 deploy.py verify '<RELEASE>'
bash install.sh --activate '<RELEASE>'
```

Replace `<RELEASE>` with the path printed by staging. Staging builds a separate
app/venv at its final path under `~/.local/share/ai-bridge/releases/`, installs
pinned dependencies, runs tests and records app/venv SHA256 checksums. Progress,
individual tests and failures are shown; build/test logs stay with the candidate.
Only a passing candidate gets READY. Staging needs no sudo or restart. A plain
`bash install.sh` also stages only when it detects an existing installation.

Activation verifies checksums, updates `current`/`previous`, rewrites the service
unit's executable/PYTHONPATH, and reloads systemd. It preserves environment and
credentials and saves the original unit as `ai-bridge.service.before-versioned`
if absent. **The running process does not change until restarted.** Direct
`deploy.py activate` only changes symlinks; use the installer wrapper for the unit.

Restart separately, then check health and authenticated client access:

```bash
sudo systemctl restart ai-bridge
systemctl status ai-bridge --no-pager
curl --fail http://127.0.0.1:8765/healthz
```

Use your configured port. For a coordinated restart requested through MCP, let
the current request return before restarting; a detached host job can do that:

```bash
sudo systemd-run --collect --on-active=3s /usr/bin/systemctl restart ai-bridge
```

Verify authenticated initialization, all 17 tools, a harmless call, unauthorized
rejection and HTTPS reachability from the intended client. Health alone is not
an MCP/authentication test. Refresh client metadata after updates. With explicit
authorization, test sudo using a harmless identity command and inspect both
`privilege` and outcome; never retrieve a credential to verify it.

Select the previous verified release with `bash install.sh --rollback`, or append
an explicit release path, then restart separately. Each selection changes
`previous`, so record your intended target. First migration from a mutable legacy
install may require restoring its backed-up unit manually and reloading systemd.
Rollback does not restore configuration, credentials, state, or work already
performed by tools. Old releases may omit new result fields.

The unit uses `KillMode=process`: detached jobs may outlive a restart/stop and
continue using their originating release. Keep that release until they finish.
Runner JSON lives inside `jobs/.sidecars/`, keeping legacy root `*.json` listings
compatible. Never change native conversation stores as part of deployment.

## Ownership, backups and verification

Dedicated bootstrap leaves the original checkout alone and creates a service-user
copy under `/var/lib/ai-bridge-bootstrap/`. Update from that copy or another
accessible checkout; do not recursively re-own unrelated source or copy native
CLI credentials between accounts. Staging/activation/rollback do not create users
or change sudo policy/passwords. Identity migration needs separate planning.

Before updates, keep private, preferably encrypted backups of:

- `/etc/ai-bridge/`, the service unit/drop-ins and any bridge-specific sudo policy.
- Host configuration and state, including alternate paths and
  `/var/lib/ai-bridge/installation.json` when present.
- Working releases/symlinks and releases still used by jobs; relevant workspaces.
- Native agent history/configuration only if needed; it can contain credentials.

Coordinate snapshots of running jobs or mutable databases; a live copy may be
inconsistent. Test recovery on an isolated host. Keep archives and validation
records outside the public checkout.

Run source regression tests with the installed environment:

```bash
~/.local/share/ai-bridge/current/venv/bin/python -m unittest discover -s tests -v
bash -n install.sh uninstall.sh
```

Tests use temporary files/fake commands. They do not replace clean Linux/systemd
VM validation of root/dedicated bootstrap, real password/passwordless sudo and
stdin isolation, interrupted setup, unit ownership, restart, rollback and removal.
Test the actual public route separately. See [reliability](system-hardening.md).

## Token and credential changes

From a trusted terminal, generate/store a long random URL-safe token in a password manager.
Use `sudoedit /etc/ai-bridge/ai-bridge.env` (or a trusted editor as root) to replace
its single `AI_BRIDGE_TOKEN` assignment privately, preserving ownership/mode. Do not put token values in
commands, chat, screenshots or logs. Restart from the host, update connector
settings privately, and verify new authentication works while the old token fails.
Pause only the bridge's public route during rotation if needed; do not reset a
shared tunnel. Rotation neither erases old logs/backups nor undoes running work.

Manage the root-only sudo credential/unit privately on the host; staging does not
refresh it. Restart after changes. See [sudo mechanics](setup.md#sudo-credentials).

## Troubleshooting

| Symptom | Check |
|---|---|
| Install completes with port warning | Change `AI_BRIDGE_PORT` or free that port; restart and update the tunnel backend. Startup was not scheduled, but the unit is enabled. |
| Health fails | Service status, configured port, and `sudo journalctl -u ai-bridge -n 50`; inspect locally before sharing. |
| MCP returns 404 | Token/header and `/mcp` path; `/healthz` needs no token. |
| Host/origin rejected | Configured hostname, explicit port, proxy Host handling, extra origins. |
| Health works but connector fails | HTTPS route, authenticated initialization, client policy/metadata cache. |
| CLI missing or authentication fails | Service PATH/HOME, executable override, and native login as that account. |
| Sudo fails | Native policy, current password, credential loading, and helper/shell permissions. |
| Resume fails | Exact session ID, saved project, continuity/history notes, concurrent editor/terminal activity. |
| Adapter breaks after CLI update | Storage/output compatibility; core shell/file/job tools remain available. |
| Old job is uncertain | Inspect identity/cleanup locally; never kill solely by a saved PID. |
| Host configuration fails | Validate manifests with the [host guide](../ai_bridge/host_defaults/README.md); startup also checks tool names. |

## Full uninstall

This permanently removes recognized **bridge artifacts**, not the whole server.
Back up wanted instructions/state first. From a separate trusted host terminal:

```bash
bash install.sh --uninstall --dry-run
bash install.sh --uninstall
```

`bash uninstall.sh` accepts the same removal options. Preview shows the installed
account/UID, exact directory/file targets (including their contents), activation
links, conditional empty-container cleanup, retained items, blockers and manifest
SHA256. Review it, then type `UNINSTALL AI-BRIDGE for <installed-user>` exactly as
shown. `--purge` is now a deprecated alias: full removal **always includes host
configuration and job state**, with no state-preserving default.

| Removed when recognized/verified | Retained |
|---|---|
| Main unit, activation links, exclusively marked bridge drop-ins, original unit backup | Accounts, groups and homes; no automatic account deletion option |
| `/etc/ai-bridge`, environment and sudo credential; exact installer sudo policy | Native CLIs, authentication/history and unrelated applications/containers |
| Service user's installed app/releases, bridge config/instructions/resources/prompts/overrides, state/jobs/audit/logs | Unrelated source clones, including the checkout used to uninstall |
| Recognized account-specific bootstrap copies; ownership registry; empty managed containers | Shared tunnels/network configuration, dependencies, global journals and external archives |

Identity comes from the unit and passwd entry, not caller HOME. The private
ownership registry can recover it if the unit is gone. For reviewed legacy
remnants without either, add `--user <account>` and preview again. Non-root callers
use sudo; without a terminal it uses `-n` rather than waiting for a password.

Fresh setup attempts a root-private record at
`/var/lib/ai-bridge/installation.json`; registration failure warns without breaking
installation. Alternate config/state/XDG directories require a matching recorded
path, inode, owner and permissions. Only dedicated directories named `ai-bridge`
are automatically registered. Shared/native/source locations or unverified
custom paths require manual review. Recognized defaults are checked too, even
when alternates are configured. Recreated directories may need manual review.

Unknown units/drop-ins, unsafe effective stop behavior, ownership mismatch,
symlinked target/parent paths, mounted subtrees, changed targets, running/uncertain
jobs, active runners or recorded live process groups block removal. There is no
force-kill or automatic job reconciliation. Finish/manage application jobs
explicitly first. Internal symlinks are unlinked without following their targets.

For deliberate noninteractive removal, all three flags must match a fresh preview:

```bash
bash uninstall.sh --yes \
  --confirm 'UNINSTALL AI-BRIDGE for <installed-user>' \
  --manifest-sha256 '<SHA256-from-current-dry-run>'
```

`--yes` alone is refused, as is removal from the bridge cgroup/environment, even
with automation flags. Do not use a detached bridge request to bypass this.
Keep the host quiescent: path checks are not a filesystem transaction or a lock
against other writers. Detached processes without reliable records cannot all
be attributed. Partial failures report progress; the service may remain stopped.
Keep the manifest, inspect locally, and fix the problem before a new dry-run.

Review any retained account, clone or archive separately before manually deleting
it. Remove only the bridge route/backend from tunnel configuration; **never reset
a shared tunnel**. Uninstall changes no firewall, SSH or unrelated service.

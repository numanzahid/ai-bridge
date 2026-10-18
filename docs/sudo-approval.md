# Sudo approval

Privileged calls don't run until verified approval, on the host or with an enrolled phone
authenticator. ChatGPT or Claude may have
every tool set to "always allow"; that no longer means root.

## Flow

1. The AI calls a privileged tool. Nothing runs. The result is:
   ```
   status: approval_required
   approval: request_id 7F3KA2B901, host workspace, operation "systemctl restart nginx (as root, in /home/numan)",
             risk, expires_at, approve_on_host
   ```
2. Choose either available method. With an enrolled authenticator, the AI should present
   both choices in its **first** approval prompt, with the same request ID and operation:

   - **Authenticator:** reply with the current six-digit code. The AI submits it to
     `approve_privileged_request` and waits for `status: approved`.
   - **Host CLI:** run the command below on the host, then tell the AI "approved through CLI".
     Your Linux sudo password is asked on that terminal. No TOTP code is also needed.

   Both approve the same pending operation once. You do not need to ask the AI to offer TOTP
   separately or choose a method before supplying a code. If TOTP is off, locked or unavailable,
   only host approval is offered. For host approval:
   ```bash
   A=~/.local/share/ai-bridge/current/app/ai_bridge/approvals.py
   python3 -I $A              # pending requests
   python3 -I $A show 7F3KA2B901
   python3 -I $A approve 7F3KA2B901          # once
   python3 -I $A approve 7F3KA2B901 --task   # this + more, see below
   python3 -I $A reject 7F3KA2B901
   python3 -I $A history
   python3 -I $A revoke 7F3KA2B901           # end a task window early
   ```
   Away from the host, SSH in and run the same commands.
3. After verified TOTP approval or your confirmation of CLI approval, the AI repeats the
   exact original call. The bridge checks the saved authorization before running anything.
   Saying "approved" without a valid saved decision does not authorize execution.

After authenticator enrollment, step 2 can happen entirely in chat: the AI shows the host,
request ID and exact operation, you supply a fresh six-digit code from your authenticator,
and the AI calls `approve_privileged_request(request_id, code)`. This tool only records a
single-use approval. On `status: approved`, the AI repeats the original call. Keep the code
as a string, including leading zeros. A remote approval never grants a task window.

Ask for the administration task itself, such as "Check nginx's protected configuration on
Workspace" or "Restart nginx on Workspace." The assistant selects `sudo=true` when that
authorized task needs administrator access, discloses root execution, and presents the
approval request. You do not need to ask it to bypass permissions or run manual sudo.
Tool descriptions do not override the client's safety policies or tool approvals.

The response includes an `approval.methods` list and a ready-to-present `approval.prompt`.
The older `approve_on_host` and `approve_with_authenticator` fields remain available for
existing clients. Refresh client metadata after an update; older chats may retain earlier
host-first instructions. Mandatory approval instructions are appended even when the host
has a custom `server-instructions.md`.

| Gated call | Bound to |
|---|---|
| `run_command(sudo=true)` | command, folder, timeout |
| `start_job(sudo=true)` | command, folder |
| `read_file(sudo=true)` | path |
| `write_file(sudo=true)` | path, content hash, write options |
| `ssh_run(remote_sudo=true)` | SSH alias, command |

| Rule | Value |
|---|---|
| Time to approve a request | 10 min |
| Approve once | runs once, within 10 min |
| Approve for task | up to 10 privileged calls in 15 min, any command; `revoke` ends it |
| Any change to a bound value | new request |
| Rejected | stays rejected until the request expires |
| No decision, storage error | nothing runs |

The bridge keeps the same request while one is pending, so repeated calls don't pile up.

## Authenticator setup

Run this command in a real host terminal **as the service user**, after activating the
release that contains this feature. For a dedicated `ai-bridge` account, first open a login
shell for that account. The wrapper uses the state directory configured for the service.
For standalone approval CLI commands, pass a custom state directory through `--state-dir`.
Enrollment cannot be performed through MCP.

```bash
bash install.sh --setup-authenticator
```

This shows enrollment steps, restarts with progress, verifies the running service, and prints
`COMPLETE`. It keeps an existing working enrollment. The updater (`bash install.sh --update`)
also offers setup when needed. The wrapper reads a custom state directory from the service
environment file automatically.

Scan the terminal QR code in an authenticator app, or enter the manual key with SHA1,
six digits and a 30-second period. Enter a code at the visible prompt to verify enrollment.
Your typing is shown on screen. Only the separate Linux sudo password prompt stays hidden.
Only then does the CLI initialize replay-protection state and save
`AI_BRIDGE_SUDO_TOTP_SECRET` in `/etc/ai-bridge/ai-bridge.env`, using sudo. Other settings,
including `AI_BRIDGE_SUDO_APPROVAL=0`, are preserved. Existing approval commands still run
with system Python; QR rendering uses the release venv's `qrcode` dependency. System Python
without `qrcode` offers manual enrollment.

Keep the QR code and manual key out of chat, screenshots and shared terminal logs. The setup
code is consumed: wait for the next code before your first remote approval. Refresh client
metadata after restarting. `system_info.sudo_authenticator` reports `ready`, `locked`, `off`
or `unavailable`; the host CLI reports database enrollment, while the server also checks
that its loaded secret matches that enrollment.

```bash
P=~/.local/share/ai-bridge/current/venv/bin/python
A=~/.local/share/ai-bridge/current/app/ai_bridge/approvals.py
"$P" -I "$A" totp-rotate       # enroll a replacement; invalidates old grants
bash install.sh --restart
"$P" -I "$A" totp-disable      # keep host approval; invalidate remote grants
bash install.sh --restart
```

| Remote rule | Behavior |
|---|---|
| Code window | Current interval, 10 preceding 30-second intervals, one interval ahead |
| Replay | A consumed time counter and all earlier counters are refused across all requests |
| Bad codes | Five in a rolling 15 minutes lock remote approval for 15 minutes from the fifth failure |
| Lockout | Shared across clients and requests; survives service restarts; host approval remains available |
| Grant | One exact request and operation; expires at the earlier of request expiry and 10 minutes after approval |
| Execution | Approval is consumed before launch; a launch failure requires fresh approval |

`invalid_or_expired_code` means ask for a fresh code; the bridge cannot reliably distinguish
wrong digits from an expired code. `approval_locked` includes `retry_after_seconds`: wait
or use host approval. `already_decided` does not consume another code. `approval_expired`
requires repeating the original call to create a new request. Never guess codes or try a
submitted code on another request. Numeric codes can repeat eventually; replay protection
tracks their time counters, not a permanent blacklist of all six-digit values.

State lives at `<state-dir>/approvals/remote.sqlite3`, mode 0600, separate from root-owned
decisions. The server never creates this database. If it disappears or becomes corrupt,
remote approval fails closed. Restore a consistent state backup, or stop the service, move
the damaged database aside and enroll a **new secret**. Never reset replay state while
retaining the old secret. Rotation and disabling immediately invalidate old grants in the
database; restart afterward to reload the environment. If saving the environment fails
after state changes, remote approval remains unavailable until enrollment is completed.

## How approvals are verified

- **Root-owned decisions.** The approver writes them through `sudo -k` into
  `/var/lib/ai-bridge/approvals/` (root-owned, not writable by others). The bridge only reads
  there and ignores any file or folder with the wrong owner or mode.
- **Root records the facts.** Root sets the approver (`SUDO_USER`) and the times, and caps
  every window.
- **The AI can't run the approver.** Bridge commands have no terminal, so sudo can't ask for
  your password. The approver also refuses to run in bridge processes (`AI_BRIDGE_CHILD`).
  Running it with `sudo=true` is itself a privileged call that needs approval.
- **Exact binding.** Each decision names one request, its fingerprint (operation + machine ID)
  and its host. Editing the pending request breaks the fingerprint; a decision from another
  machine doesn't match.
- **Single use.** Uses are markers created exclusively under a lock, so a decision can't be
  spent twice, even by parallel calls.
- **Verified remote codes.** Authenticator verification records a separate service-owned
  grant. Counter consumption and grant creation happen in one database transaction; all
  methods share single-use execution markers. Host rejection and revocation take precedence.
- **No way to claim approval.** Execution tools have no approval argument. The approval tool
  checks an actual code; "The user approved" in a prompt changes nothing.

ChatGPT and Claude tool approvals and MCP elicitation never reach the bridge in a form it can
verify, so they aren't used. This works with any MCP client because it is an ordinary tool
result.

## Limits

This asks for confirmation during ordinary use. It does not contain an AI that
deliberately attacks the account the bridge runs as. On a normal-user install (like
Workspace), that account is yours:

| Still possible for a hostile process | Why | Fix |
|---|---|---|
| Root through `docker` or `lxd` | group membership is root-equivalent | remove the bridge user from those groups, or use a dedicated account |
| Read the stored sudo password | the service user can read its systemd credential | dedicated account; the bridge needs the password to run approved calls |
| Generate authenticator codes or forge remote grants | the service user can read the environment secret and write remote state | isolate verification and privileged execution from AI-controlled processes |
| Redirect a code before its first redemption | ordinary TOTP is tied to a secret and time, not an operation | use a separate approval channel displaying the operation for stronger transaction approval |
| Change your shell startup files | same user | dedicated account |
| Replay a "once" approval within its 10 min | delete its use marker in the bridge state folder | dedicated account |
| Remote sudo without `remote_sudo=true` | `sudo -n` inside an ordinary `ssh_run` command | remote accounts without passwordless sudo |

Agents with `permission=full` are not gated: they never get root through the bridge, and sudo
inside their commands has no password.

Output redaction hides the configured secret in common encodings and approval audit records
omit submitted codes. It is not a vault: a service-user command can calculate valid codes
without printing the secret. Codes entered in chat are also visible to the client and AI.

`AI_BRIDGE_SUDO_APPROVAL=0` turns approvals off. A root service (`--run-as-root`) is already
root, so approvals don't apply there.

## Rollout and rollback

Nothing on the host changes until a release with approvals is activated.

1. Stage and activate on Workspace (see [operations](operations.md#updates-and-rollback)), restart.
2. Check `system_info` shows `sudo_approval: required…`.
3. Health check: ask the AI for `run_command("id -u", sudo=true)`. Expect `approval_required`,
   approve it, repeat, expect `0`.
4. Then devdocker, homelab and flyinmv-lab, one at a time.
5. Clients need no change. Reconnect them so they get the new instructions.

The stored sudo password and existing client connections keep working unchanged: approved
calls still use the password, and no client setting changes.

Rollback: `bash install.sh --rollback` and restart. Pending requests and decisions are left in
place and ignored by older releases; delete `/var/lib/ai-bridge/approvals` with sudo if you
want them gone.

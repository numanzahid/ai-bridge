# Sudo approval

Privileged calls don't run until you approve them on the host. ChatGPT or Claude may have
every tool set to "always allow"; that no longer means root.

## Flow

1. The AI calls a privileged tool. Nothing runs. The result is:
   ```
   status: approval_required
   approval: request_id 7F3KA2B901, host workspace, operation "systemctl restart nginx (as root, in /home/numan)",
             risk, expires_at, approve_on_host
   ```
2. You approve on the host. Your sudo password is asked every time:
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
3. You tell the AI. It repeats the exact same call, and it runs.

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

## Why the AI can't approve itself

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
- **No way to claim approval.** No tool takes an approval argument. "The user approved" in a
  prompt changes nothing.

ChatGPT and Claude tool approvals and MCP elicitation never reach the bridge in a form it can
verify, so they aren't used. This works with any MCP client because it is an ordinary tool
result.

## Limits

This stops an AI from using root without you knowing. It does not contain an AI that
deliberately attacks the account the bridge runs as. On a normal-user install (like
Workspace), that account is yours:

| Still possible for a hostile process | Why | Fix |
|---|---|---|
| Root through `docker` or `lxd` | group membership is root-equivalent | remove the bridge user from those groups, or use a dedicated account |
| Read the stored sudo password | the service user can read its systemd credential | dedicated account; the bridge needs the password to run approved calls |
| Change your shell startup files | same user | dedicated account |
| Replay a "once" approval within its 10 min | delete its use marker in the bridge state folder | dedicated account |
| Remote sudo without `remote_sudo=true` | `sudo -n` inside an ordinary `ssh_run` command | remote accounts without passwordless sudo |

Agents with `permission=full` are not gated: they never get root through the bridge, and sudo
inside their commands has no password.

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

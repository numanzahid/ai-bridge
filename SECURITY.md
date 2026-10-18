# Security

The bridge token gives shell and file access as the service user, including root
through sudo (direct root mode is already root). Use it only where remote admin
access is intended, and treat the token and token URLs as passwords.

## Reporting a vulnerability

Use the repository's private vulnerability reporting if available. Otherwise open a
short issue asking for a private contact, with no details. Include the affected
version, impact and a minimal reproduction using fake data; never real tokens, hosts
or paths. Test only systems you're authorized to test. No response time is promised.

## Trust boundary

- **One token, everything.** No per-client roles, path limits or per-tool permissions.
- **Same user, same reach.** Commands can read the service user's files, agent
  logins and loaded sudo password. Redaction is not a vault.
- **Agent permissions** are the CLIs' own flags. They don't limit bridge tools or
  change the Linux user.
- **Prompt injection:** documents, web pages, skills, prompts and agent output can
  contain instructions. Keep client approvals on.
- **Transport:** HTTPS proxies can see requests; path tokens can appear in logs.
  Prefer the bearer header and rotate after any exposure.
- **Task workspaces** are private folders, not isolation. They survive uninstall.
- **No undo:** stop, cancel and rollback can't reverse completed work. Detached jobs
  can outlive a restart.

## Sudo approval

Privileged calls (`sudo=true`, `remote_sudo=true`) wait until you approve them on the host with
your sudo password; client "always allow" settings don't bypass this. It guards against an AI
acting without you, not against a process that attacks the service account directly (Docker
group, stored password). See [sudo approval](docs/sudo-approval.md).

## Remote Control

`agent_send(remote=true)` makes a chat controllable from your Claude or ChatGPT account (phone or
browser), with the agent's permissions on this machine. For Codex it is daemon-wide: every Codex
thread on the daemon becomes reachable until you run `codex app-server daemon disable-remote-control`.

## Removal

Uninstall is permanent and not transactional. Run it from a host terminal, never
through the bridge. Unknown ownership, unsafe paths or unresolved jobs block it;
nothing is force-killed. Accounts, agent histories, source checkouts and shared
tunnels are kept. See [full uninstall](docs/operations.md#full-uninstall).

## SSH

SSH extends token access to the configured remote accounts. Host keys must already
be trusted; config with commands or conditions is refused. Remote sudo uses
`sudo -n` only; passwords are never stored or forwarded. Transfers never overwrite
files. Remote output can still contain secrets.

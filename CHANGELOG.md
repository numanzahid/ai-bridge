# Changelog

All notable changes to AI Bridge. Versions follow [Semantic Versioning](https://semver.org/);
see [releasing](docs/releasing.md) for when to bump which part.

## [Unreleased]

## [3.1.0] - 2026-10-19

### Added
- Server info for clients: title `AI Bridge (<hostname>)`, description, author, website
  (https://zahid.dev) and icons (`ai_bridge/assets/`, sent as data URIs).

## [3.0.2] - 2026-10-19

### Fixed
- The approval command shown to users only worked from a source checkout. It is now
  `python3 -I ~/.local/share/ai-bridge/current/app/ai_bridge/approvals.py` (standard library only).

## [3.0.1] - 2026-10-19

### Fixed
- A new approval request crashed the tool call instead of returning `approval_required`
  (audit field name clash). Nothing ran; the request was still recorded.

## [3.0.0] - 2026-10-19

### Changed (breaking)
- Privileged calls need approval on the host: `sudo=true` (shell, jobs, files) and
  `ssh_run(remote_sudo=true)` return `status: approval_required` with a request ID until the user
  approves with the host approval command (sudo password every time). Approve once, or for a
  task (10 calls, 15 min). Repeating the exact call runs it. `AI_BRIDGE_SUDO_APPROVAL=0` turns it off.
  See [sudo approval](docs/sudo-approval.md).

## [2.0.0] - 2026-10-18

### Changed (breaking)
- Tools reduced from 23 to 17. System tools stay the core and also manage agent turns:

  | Removed | Use instead |
  |---|---|
  | `agent_wait`, `agent_runtime`, `job_log` | `job_status` (`log_offset` pages full output) |
  | `agent_stop` | `kill_job` |
  | `agent_models`, `tasks_list` | `agent_list(show=models/tasks)` |
  | `list_skills` | `list_instructions(kind=skills)` |

  Host `tool-descriptions/` overrides for removed tools are ignored with a warning.

### Added
- Interactive agent turns (default for Claude and Codex): normal chats the user can open in the
  terminal (`claude attach`/`--resume`, `codex resume`). `mode=exec` keeps the headless runs.
- `agent_send(remote=true)`: Remote Control for Claude (claude.ai/code link) and Codex (daemon-wide).
- `agent_send(name=...)` sets the chat's display name.
- `agent_manage`: session aliases, archive/unarchive, and task workspace cleanup (preview, then `confirm`).
- `agent_sessions(include_archived)` and `list_jobs(kind)` filters.
- `AI_BRIDGE_TRUST_TASKS=1` (opt-in): new task workspaces are trusted for Claude, Codex and Cursor,
  so agents run there without a terminal prompt. Cleanup removes the trust.
- Version reported by MCP and `system_info` (`bridge_version`, `bridge_release`).
- `release.py` to bump the version and date changelog notes; [releasing](docs/releasing.md) guide.
- Releases record `RELEASE.json` (version, commit, staging time) and include the version in their folder name.
- Activation refuses changed code without a version bump, and older versions (use `--rollback`).

### Changed
- Shorter docs and client guidance; tool reference lists each tool with an example.
- Tests consolidated; they no longer depend on umask.

## [1.0.0] - 2026-10-18

Baseline: commit `0b969e0`, release `20261017T200345Z-3d4f0e52`.

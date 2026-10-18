# Tool reference

The bridge exposes 17 MCP tools. The system tools are the core; coding agents are an
optional add-on that reuses them (agent turns are jobs). `tools/list` has exact
parameters. Every tool runs as the service user; one token can call all of them.

## System

| Tool | Purpose | Example request |
|---|---|---|
| `system_info` | Host facts: OS, disk, memory, sudo setup, agents, bridge version | "What's this machine and what's installed?" |
| `run_command` | Run a bash command (timeout 1–600 s, default 120) | "Show the 10 largest dirs in /var" |
| `start_job` | Start a long command in the background; returns `job_id` | "Run the full backup in the background" |
| `job_status` | Status + output of any job, including agent turns; `wait_seconds` waits, `log_offset` pages the full log | "Is the backup done? Show its log" |
| `list_jobs` | Recent jobs; `kind=command` or `kind=agent` filters | "What's running?" |
| `kill_job` | Stop one job or agent turn by `job_id` | "Stop that backup" |
| `read_file` | Read a text file with line numbers (`sudo` for root files) | "Show /etc/nginx/nginx.conf" |
| `write_file` | Write or append a file; optional atomic write, backup, hash check | "Set `max_connections=200` in app.conf" |

- `sudo=true` waits for your approval on the host: the call returns `status: approval_required`
  with a `request_id`; after you approve, the AI repeats the exact call. See [sudo approval](sudo-approval.md).
- Long work: `start_job`, then poll `job_status`. Jobs survive chat end and restarts.
- Output is bounded. Long logs keep the head and tail; page with `job_status(log_offset=0)`.
- Job status: `running`, `done`, `failed`, `killed`, `lost`, `cleanup_incomplete`.
- Atomic writes refuse symlinks, hard links and special files; `expected_sha256` catches stale edits.

## SSH

| Tool | Purpose | Example request |
|---|---|---|
| `ssh_hosts` | List configured SSH aliases (offline, no addresses) | "Which servers can you reach?" |
| `ssh_run` | Run a command on an alias (`remote_sudo` uses remote `sudo -n`) | "Check disk space on `web1`" |
| `ssh_transfer` | Upload/download one file; never overwrites | "Copy backup.tar.gz from `db1` to ~/backups/" |

- Only named aliases from your SSH config; host keys must already be trusted.
- Executable or conditional config (`ProxyCommand`, `Match`) is refused.
- `remote_sudo=true` needs approval on this host first, like local `sudo=true`.
- `committed=null` means the result is unknown: check before retrying.
- Prepare hosts as in [setup](setup.md#remote-ssh-preparation).

## Guidance

| Tool | Purpose | Example request |
|---|---|---|
| `list_instructions` | Your docs in `~/.config/ai-bridge/instructions/`; `kind=skills` lists local skills | "Where do my projects live?" |

Returns names and paths only; the client reads what it needs with `read_file`.
See the [instruction guide](instructions-guide.md) and [host configuration](../ai_bridge/host_defaults/README.md).

## Coding agents (optional)

Adapters: `claude` (Claude Code), `codex` (Codex CLI), `cursor` (Cursor CLI).

| Tool | Purpose | Example request |
|---|---|---|
| `agent_send` | Start a turn (new chat, resumed chat or shared task); returns `job_id`. `remote=true` adds Remote Control | "Start a Claude chat in ~/app with remote on" |
| `agent_sessions` | Saved chats, including terminal ones; `include_archived` shows archived | "List my Claude chats for ~/app" |
| `agent_history` | Read a chat without running the agent | "Summarize that chat" |
| `agent_list` | Installed agents; `show=models` for model choices, `show=tasks` for task workspaces | "Which Codex models can I use?" |
| `agent_manage` | Alias, archive/unarchive a chat; clean up a task workspace (preview, then `confirm`) | "Archive that chat" / "Delete the old task" |

Manage running turns with the system job tools: `job_status` (result, outcome, session ID,
runner resources), `list_jobs(kind=agent)` and `kill_job`.

**Interactive vs exec.** By default (`mode=interactive`) Claude and Codex turns run in a normal chat you
can also open yourself: a Claude background session (`claude attach <id>`, `claude --resume`) or a thread
on the Codex daemon (`codex resume`). `mode=exec` is a one-shot headless run; Cursor is exec only.

**Remote Control** (`remote=true`, interactive only) lets you continue the chat from your phone or browser:

| | How | Result fields |
|---|---|---|
| Claude | `claude --bg --remote-control`; needs a claude.ai subscription login | `native.remote_url` (claude.ai/code link) |
| Codex | Turns on Remote Control for the shared Codex daemon (it restarts once, then stays on) | `native.remote_control` (`connected`) |

Resuming a Claude background session wakes it with its saved options, including Remote Control.
A session that is busy or open in a terminal is never interrupted.
Claude only runs interactive chats in folders it trusts. Run `claude` once in `~/.ai-bridge-tasks`
(and any project folder) to trust it; otherwise use `mode=exec`.

**Continue a chat:** `agent_sessions` → `agent_history` → `agent_send` with the exact
`session_id` (omit `cwd`) → poll `job_status`. Unknown IDs fail; they never start a new chat.

- No `cwd`, `session_id` or `task_id`: a new private workspace in `~/.ai-bridge-tasks/`.
- `continuity`: `confirmed` (same chat), `mismatch` (error), `unverified` (no ID reported).
- Limits and outages come back as results (`failure.code`, e.g. `usage_limit`), never retried.
- Aliases and archiving are bridge metadata only; native chat history is never changed.
- Task cleanup is blocked by running/uncertain jobs, processes using the workspace, or mounts.
- Don't resume a chat that's open in an editor or terminal.

| Permission | claude | codex | cursor |
|---|---|---|---|
| `readonly` | `--permission-mode plan` | `--sandbox read-only` | `--mode ask` |
| `edit` (default) | `--permission-mode acceptEdits` | `--sandbox workspace-write` | `--force` |
| `full` | `--dangerously-skip-permissions` | `--dangerously-bypass-approvals-and-sandbox` | `--force` |

These are the CLIs' own flags; they never change the Linux user or limit the system tools.

## Privilege disclosure

Shell, job and file tools report `privilege` when sudo is requested or the service is root:

| Field | Meaning |
|---|---|
| `sudo_requested` | Sudo was asked for (not proof it ran) |
| `sudo_attempted` | Sudo was started |
| `sudo_used` | The command was confirmed launched through sudo |
| `target_started` | The command launched; says nothing about success |
| `effective_uid`, `effective_root` | Confirmed identity, or null if unknown |

A root service skips sudo (`method: service_root`). SSH results use `remote_privilege` the same way.

## Replaced in 2.0.0

| Old tool | Use instead |
|---|---|
| `agent_wait`, `agent_runtime` | `job_status` |
| `job_log` | `job_status(log_offset=0)` |
| `agent_stop` | `kill_job` |
| `agent_models` | `agent_list(show=models, agent=…)` |
| `tasks_list` | `agent_list(show=tasks)` |
| `list_skills` | `list_instructions(kind=skills)` |

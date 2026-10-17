# Tool reference

The bridge exposes 23 MCP tools. `tools/list` is the authoritative parameter
schema; examples below describe defaults and limits. Read-only annotations are
client hints, not authorization. The shared token can invoke mutating tools too.
All paths and native agents use the service identity unless a tool requests sudo.

## Shell, files and jobs

| Tool | Parameters and behavior |
|---|---|
| `system_info()` | OS, user/effective UID, resource summaries, configured sudo status and installed agents. No elevation probe. |
| `run_command(command, cwd=null, timeout=120, sudo=false)` | Login Bash, no TTY. Timeout clamped to 1–600 seconds. Returns stdout/stderr, exit/timeout and capture details. |
| `start_job(command, cwd=null, sudo=false)` | Detached long-running command; returns `job_id` and launch status. |
| `job_status(job_id, wait_seconds=0, tail_lines=60)` | Wait up to 110 seconds; tail up to 400 lines. Inspect status, exit and cleanup. |
| `list_jobs(limit=20)` | Recent command/agent jobs, at most 100. |
| `kill_job(job_id)` | Attempts SIGTERM then SIGKILL with identity checks. Inspect cleanup; privileges can prevent termination. |
| `job_log(job_id, offset_bytes=0, max_bytes=12000, generation=null)` | Bounded retained-output chunk, capped at 65536 bytes. Follow `next_offset_bytes` with the returned generation. |
| `read_file(path, start_line=1, max_lines=400, sudo=false, include_hash=false)` | Bounded regular text read; `max_lines` capped at 3000. Optional SHA256 scans the whole file. |
| `write_file(path, content, append=false, sudo=false, make_dirs=true, atomic=false, expected_sha256=null, backup=false, mode=null)` | Direct write by default; optional atomic replacement, stale-content check, private backup and integer permission bits. |

Use `start_job` for long work, then poll `job_status`; shell/file tools do not
need any coding CLI. Missing `cwd` uses the configured default directory.
Jobs can outlive a bridge restart and use their originating release's code;
retain that release until they finish. Status can include `running`, `done`,
`failed`, `killed`, `lost` or `cleanup_incomplete`. Legacy identity may be unknown.
No cancellation or rollback undoes completed file changes.

Output is bounded, not an archival transcript. Job logs retain head/tail bytes
when truncated; the middle is unavailable. Log offsets refer to retained bytes.
If generation changes during paging, restart at offset 0. Busy waits may return
`wait_deferred=true`; poll again instead of assuming completion.

Atomic writes reject append, symlinks, hard links, ACLs/xattrs and special files.
`expected_sha256` is an optimistic check, not a lock against another editor.
`backup=true` creates a private local copy; `mode=384` means 0600. Inspect errors
and truncation details before assuming a read/write succeeded.

## SSH administration

| Tool | Parameters and behavior |
|---|---|
| `ssh_hosts(limit=30, offset=0)` | Offline literal alias discovery; safe source/client metadata, no addresses, users or identity paths. Page limit 100. |
| `ssh_run(host, command, timeout=120, remote_sudo=false)` | Bounded remote shell command; stdout/stderr/exit, host alias and method `ssh`. |
| `ssh_transfer(host, direction, local_path, remote_path, method="auto", timeout=120, dry_run=false)` | Upload/download one regular file. Methods: auto, rsync, sftp, scp. Offline dry-run never verifies remote capabilities. |

Hosts must be discovered named aliases, not arbitrary addresses, user@host or
option strings. Static user/system config and bounded global Includes are read;
symlinked config files work. Wildcards/Match-only aliases are not listed. Match
and conditional Includes are skipped; aliases that could depend on them or
executable config directives are refused, rather than evaluated or guessed.
Static ProxyJump is supported; ProxyCommand/LocalCommand/RemoteCommand are not.
Offline `ssh -G` inspects the sanitized snapshot, never returns raw options.

Connections require existing trusted host keys, batch authentication, no agent
forwarding, no host-key updates and no shared control socket. No prompts or key
seeding. Timeouts are clamped to 1–600 seconds across operation steps; output is
bounded/redacted. SSH config-derived private metadata is masked best effort;
remote command output itself remains sensitive.

`remote_privilege` distinguishes sudo requested, invocation reached, and a remote
root-shell receipt. Missing receipts mean unknown/unconfirmed, not proof nothing
ran. Confirmation trusts the authenticated host and does not imply task success.
Only remote `sudo -n` is used: no bridge password forwarding or LOCAL sudo.
`local_service_euid` describes the transport process, not remote privilege.

Auto prefers rsync when installed locally and found remotely, otherwise local
SFTP then SCP. Missing remote subsystems can still fail; choose an explicit method
rather than assuming availability. No blind fallback after transfer starts.
Rsync uses protected arguments; SCP explicitly uses its legacy protocol with quoted
remote operands. Streaming, recursive sync and overwrite/delete modes are absent.

Use exact absolute remote file paths; local paths expand `~` and must be absolute.
Control/undecodable characters, glob/bracket syntax, local symlinks and dot path
components are refused. Parent directories must already exist. Uploads stage in
a private sibling directory and commit using remote GNU `ln -T` (same filesystem);
downloads stage locally and hard-link exclusively. Existing destinations are never
replaced. Remote source leaf symlinks are refused; keep paths/files quiescent.
`bytes`/`checksum` stay null without independent verification. `committed=null`
means a lost commit reply: inspect before retrying. `staging_cleanup` reports
upload cleanup; an unconfirmed cleanup can leave the reported private staging
path. Cleanup removes only this operation's payload/directory, not user files.
For long/custom workflows, use existing shell/jobs with native SSH deliberately.

## Privilege disclosure

`run_command`, `start_job`, `read_file` and `write_file` disclose requested sudo
or existing root execution through `privilege` and `privilege_message`. Job
status/list/log/cancel and normal agent result views inherit launch metadata;
reading a record does not itself escalate.

| Field | Meaning |
|---|---|
| `sudo_requested` | Caller requested sudo; not evidence of execution. |
| `sudo_attempted` | Sudo subprocess started; false if unavailable or already root. |
| `sudo_used` | Target creation confirmed through sudo; root identity is separate. |
| `target_started` | Launch confirmed, not success of the user's task. |
| `service_euid` | Effective UID of the executing bridge/runner. |
| `effective_uid`, `effective_root` | Confirmed launch identity; null if unknown. |
| `method` | Usually `sudo` or `service_root`; historical records may be `unknown`. |

Denied sudo reports no confirmed target launch/use and unknown effective identity.
A command can launch through sudo and still fail. A root service bypasses sudo,
reports existing root launch, and leaves `sudo_used=false`, even if requested.
Historical sudo jobs retain null actual-use fields; pending jobs may be unconfirmed.
Internal privilege changes within shell subcommands are not traced.

Ordinary non-root results keep their shape. A privileged plain-text file read is
wrapped as `{content, privilege, privilege_message}`; structured results keep
existing keys with added disclosure. Truncation preserves disclosure, reserving
at least 1024 characters for such dictionary responses when needed.

Clients are instructed to announce the privileged task/target and report outcome
separately, without secrets. This guidance supplements custom server instructions;
it does not add a host-enforced approval prompt. Agent `full` permission never
changes the Linux user; a root service's agents already have root identity.

## Instructions and skills

| Tool | Parameters and behavior |
|---|---|
| `list_instructions(limit=30, offset=0)` | Discover host instruction names, descriptions and paths. |
| `list_skills(limit=30, offset=0)` | Discover existing `SKILL.md` files, not install or execute them. |

Both return `next_offset`; page limits are capped at 100 and response budgets can
reduce a page. The instruction root is `<host-config>/instructions/`, honoring
`AI_BRIDGE_CONFIG_DIR`, then XDG/home defaults. Markdown/text files and subfolders
are supported. A heading is sufficient; optional title/description frontmatter
improves discovery. Read relevant bodies with `read_file`, not every document.
See the [shared instruction guide](instructions-guide.md).

Default skill roots are `~/.codex/skills`, `~/.claude/skills`, `~/.cursor/skills`,
and `~/.agents/skills`; the first honors `CODEX_HOME`. Override them with
colon-separated `AI_BRIDGE_SKILL_DIRS`. Discovery follows linked directories,
avoids duplicate files/cycles, and reads a short prefix for catalog metadata.
It does not supply client-specific tools a listed skill might require.

Connection guidance and tool metadata are separate from these optional documents.
Instruction/skill bodies are loaded only on demand; edits appear on subsequent
calls. Clients decide what to read. Named MCP resources and reusable prompts are
also supported: `resources/list` → `resources/read`, `prompts/list` → `prompts/get`.
See [host configuration](../ai_bridge/host_defaults/README.md) for all file formats,
examples, refresh rules and validation. There is no `prompts/read` method.

## Native agents

Adapters: `claude`, `codex`, `cursor`. Each CLI keeps ownership of authentication,
settings and native chats. Missing CLIs or unavailable parsers do not disable
host shell/file/job tools. CLI output/storage formats can change independently.

| Tool | Parameters and behavior |
|---|---|
| `agent_list()` | Installation/discovery and configured permission information. |
| `agent_models(agent, limit=30, offset=0)` | On-demand live/cached/unavailable model catalog; page limit up to 100. |
| `agent_send(agent, prompt, cwd=null, session_id=null, permission=null, model=null, wait_seconds=30, task_id=null, timeout=null)` | Start a turn; wait up to 110 seconds. Omit session ID for a new chat. Default permission comes from config (`edit`). |
| `agent_wait(job_id, wait_seconds=60, progress_lines=15)` | Poll a turn up to 110 seconds; recent activity capped at 100 lines. Returns lifecycle, outcome, result and known session ID. |
| `agent_sessions(agent, cwd=null, limit=15, offset=0)` | Saved local chats, newest first; optional project filter, page limit up to 100. |
| `agent_runtime(job_id=null)` | Managed agent jobs or one exact job; verifies runner/target identities and reports observed lineage and runner resources. Unrelated native services are not managed. |
| `agent_stop(job_id)` | Cancel an exact managed agent job; returns cleanup scope and any uncertainty. No session/name-based kill. |
| `tasks_list(limit=30, offset=0)` | Discover private task IDs/workspaces for reuse; page limit up to 100. Cleanup is a local management operation. |
| `agent_history(agent, session_id, last_n=30, include_tools=true, max_chars_per_message=2000, offset=0)` | Read stored conversation without a model turn. Up to 300 entries and 10000 chars/message; overall response budget may reduce the page. |

Without `cwd`, `session_id` or `task_id`, agent_send creates a unique private task
under `~/.ai-bridge-tasks/`. Explicit cwd remains a project; task_id reuses shared
files with peer-job warnings. Resume always uses the original workspace and rejects
conflicts. Optional timeout is 1..604800 seconds; omission leaves the turn unlimited.
Tasks are workspaces, not sandboxes. New replies include task/workspace linkage.

Listings add `mode`, `session_type`, `native_source`, ownership, bridge-job status,
task linkage and optional bridge display aliases. Unknown provenance stays unknown;
`idle` means no matching bridge job, not proof no native process is using the chat.
Archived overlays hide records from default lists. `bridge_origin` is `mcp` only
when durable evidence of MCP session creation exists, otherwise null; native origin metadata is
preserved. See the [provenance contract](agent-management.md). Host tooling is
moving to [ai-bridge-cli](https://github.com/numanzahid/ai-bridge-cli).

### Continue an existing chat

1. Call `agent_sessions`, filtering by project if useful; follow `next_offset`.
   Rows contain full IDs, project paths, titles, storage/model metadata and resume
   eligibility. The list includes chats started outside the bridge.
2. Call `agent_history` with the exact ID to summarize saved history. Its offset
   pages backward from the end. `include_tools=false` focuses on conversation text.
3. Call `agent_send` with the same ID. Omit `cwd` to use the saved project. Unknown
   or partial IDs, conflicting/missing projects and unavailable state cause errors,
   not a replacement chat.
4. Poll `agent_wait` while `status=running`; then inspect outcome and continuity.
   `confirmed` means the CLI reported the requested ID, `mismatch` is an error,
   and `unverified` means no confirming ID was reported. A new turn without a
   returned ID is not proof of a resumable chat.

Codex `parent_session_id` identifies recorded subagent activity. A null parent
only means none was recorded: tests/automated executions can also lack parents.
Cursor supports local CLI chats, not editor/cloud stores. Subagent stores are
excluded; history may include compaction summaries rather than discarded turns.
Moved/mismatched project stores can be readable but non-resumable. The bridge
performs no native history migration, import or synchronization.

Do not resume the same chat concurrently from an editor/terminal. Busy checks
serialize bridge-launched resumes, not all external activity. Preserve session
IDs in follow-ups; omitting one requests a new chat.

### Permission flags

| Permission | claude | codex | cursor |
|---|---|---|---|
| `readonly` | `--permission-mode plan` | `--sandbox read-only` | `--mode ask` |
| `edit` (default) | `--permission-mode acceptEdits` | `--sandbox workspace-write` | `--force` |
| `full` | `--dangerously-skip-permissions` | `--dangerously-bypass-approvals-and-sandbox` | `--force` |

Codex readonly/edit disable interactive approvals; sandbox options precede its
resume subcommand. These are native flags, not bridge-enforced host restrictions.
Cursor edit/full have the same effective force setting. Extra operator arguments
can affect native behavior; inspect the deployed CLI and configuration.

### Models

Session/history metadata exposes `last_used_model` and `model_source`, or null.
Codex reads recorded turn selection, claude assistant-message model, and cursor
saved `lastUsedModel`. JSONL lookup is bounded to the latest 2 MB. These are
historical values/settings, not guaranteed active or next-turn models. Aliases
stay unresolved; unsupported or missing data remains unknown.

`agent_models` returns source, freshness (`fetched_at`, `age_seconds`), status and
pagination. Cursor queries its account-aware CLI `models` command with a 20-second
timeout; `is_cli_current` is its setting, not every chat's model. Codex reads local
`models_cache.json`: cached availability/visibility does not prove account access.
Claude has no verified account-aware catalog here and returns unavailable. Changed
formats, missing credentials or CLI failures do not trigger hardcoded guesses.

Pass `model` to request a selection on a new/resumed turn; omission leaves native
configuration/resume behavior in charge. `agent_wait` separates `requested_model`
from explicitly observed `reported_model`, which may be null on success. Requests
are not proof of alias resolution or absence of native fallback. Discovery runs
only when requested, not at MCP startup.

### Failures

Recognized agent limits/outages are ordinary tool results, not intentional MCP
transport failures. `status` is job lifecycle; `outcome` can be `running`,
`succeeded`, `blocked`, `failed` or `interrupted`. Adapter/parser failures can
return `unavailable`. `failure.scope` distinguishes agent from adapter problems;
codes include `usage_limit`, `rate_limit`, `authentication_required`,
`service_unavailable`, `execution_failed`, `interrupted` and `parser_unavailable`.
Classification is best effort from diagnostics, not a live quota check.

No automatic retry, model switch or replacement chat occurs. Retain the known
session ID and inspect guidance before explicitly continuing. For parser failures,
use `job_status`/`job_log` to inspect the underlying job.

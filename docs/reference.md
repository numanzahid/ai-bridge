# Tool and configuration reference

| Tool | What it does |
|---|---|
| `system_info` | OS, user, disk, memory, sudo status, installed agents |
| `run_command` | Run bash (timeout up to 600s). `sudo=true` for root |
| `start_job` / `job_status` / `list_jobs` / `kill_job` | Long-running commands in the background |
| `read_file` / `write_file` | Bounded text reads / write or append; opt-in atomic/hash/backup options. Both support `sudo=true` |
| `job_log` | Read retained job output in bounded chunks with snapshot generation checks |
| `list_instructions` | Discover shared instruction documents; read selected files with `read_file` |
| `list_skills` | Discover local skill names, descriptions, and paths; read selected skills with `read_file` |
| `agent_list` | Which of claude / codex / cursor are installed |
| `agent_models` | On-demand model catalog with source/freshness: live Cursor CLI, cached Codex metadata, or unavailable |
| `agent_send` | Start a turn: agent, prompt, cwd, optional `session_id` to resume, `permission`, `model` |
| `agent_wait` | Poll a turn. Returns progress while running, final answer + `session_id` when done |
| `agent_sessions` | List past sessions on the machine (including ones you started by hand in the terminal) |
| `agent_history` | Read a session: your prompts, agent replies, commands/edits it made |

Privileged `run_command`, `start_job`, `read_file`, and `write_file` results
include `privilege` plus `privilege_message`. Job status/list/log/cancel views
inherit the **launch** disclosure; reading a job record does not itself use sudo.

| Privilege field | Meaning |
|---|---|
| `sudo_requested` | Caller passed `sudo=true`; not evidence of execution |
| `sudo_attempted` | Sudo subprocess was started; false if unavailable or service is already root |
| `sudo_used` | Target creation was confirmed through sudo; inspect `effective_root` separately |
| `target_started` | Target process creation was confirmed; not success of the task |
| `service_euid` | Bridge process effective UID |
| `effective_uid` / `effective_root` | Confirmed identity at target launch; null when unknown |
| `method` | `sudo`, `service_user`, `service_root`, or historical `unknown` |

Sudo denial leaves `target_started=false`, `sudo_used=false`, and launch identity
unknown. A target that starts but fails can have `sudo_used=true` and nonzero
`exit_code`. A root service bypasses sudo even when requested and reports root
launch with `sudo_used=false`. Internal privilege changes within a shell/command
are not traced; this records launch identity, not a sandbox or proof of every
subcommand's UID. Historical sudo jobs retain null/unknown actual-use fields.
Pending jobs can briefly report an unconfirmed launch; poll status for updates.

Ordinary non-root results keep their existing shape. A privileged plain-text
`read_file` result is wrapped as `{content, privilege, privilege_message}`;
structured file results keep their existing keys with additive disclosure.
Truncation retains disclosure, reserving at least 1024 characters when needed.
Clients must announce the privileged task/target and report outcome separately,
without exposing secrets. This instruction also supplements custom host server
instructions; it adds no host-enforced approval prompt. Agent processes always
use the service identity: `permission=full` does not grant Linux root.

Agent limits and provider outages are returned as normal MCP tool results, not
transport failures. `status` retains the job lifecycle (for example, `failed`
for exit code 1); `outcome` distinguishes `running`, `succeeded`, `blocked`,
`failed`, or `interrupted`. Recognized errors include `failure.scope="agent"`
and `failure.code` values `usage_limit`, `rate_limit`, `authentication_required`,
`service_unavailable`, or `execution_failed` when the cause is unknown.
Interrupted jobs use `interrupted`; an unavailable parser reports adapter scope
with outcome `unavailable`. Classification is best-effort from error diagnostics,
not a live account-quota check. The response includes guidance and preserves the
known session ID. No automatic retries, model switches, or replacement chats occur.

The defaults, bounds, and result details below supplement this overview.

### Shared instructions and skills

Put as many Markdown (`.md`) or text (`.txt`) instruction files as you need in
`~/.config/ai-bridge/instructions/`. Subfolders work too. The installer creates this
folder with a starter README; it uses `$XDG_CONFIG_HOME/ai-bridge/instructions/`
if `XDG_CONFIG_HOME` is set. For example, use `projects.md` for project locations,
`documents.md` for resource/document storage, and `preferences.md` for working rules.

Give each file a clear heading. Optional frontmatter supplies a short catalog description:

```markdown
---
title: Project locations
description: Where to find existing projects and create new ones.
---

# Project locations

Add your actual folder locations and conventions here.
```

**Contents are loaded on demand.** Connecting sends brief bridge guidance and tool
descriptions, not your instruction files or skills. The assistant calls
`list_instructions` or `list_skills` to get names, short descriptions, and absolute
paths, then uses `read_file` for relevant documents. Listings are paginated with
`next_offset`. Discovery reads only a short file prefix for metadata and headings;
it does not return document bodies. New files and edits appear on the next call,
without reinstalling or restarting the bridge. Each client decides when to call
tools; server guidance does not enforce automatic reading.

Skill discovery finds `SKILL.md` recursively in `~/.codex/skills/`,
`~/.claude/skills/`, `~/.cursor/skills/`, and `~/.agents/skills/` (Codex honors
`CODEX_HOME`). It follows linked skill directories and avoids duplicate files and
symlink cycles. Override the roots with `AI_BRIDGE_SKILL_DIRS`, using colons to
separate directories in `/etc/ai-bridge/ai-bridge.env`, then restart the service.
Listing a skill does not execute it, install it in the connected client, or provide
any client-specific tools it requires. The assistant can read referenced scripts
and documents relative to the selected skill's directory as needed.

### Host-editable MCP configuration

All the supported MCP guidance surfaces can be configured with local files before
starting the server. The default root is `~/.config/ai-bridge/`; `XDG_CONFIG_HOME`
is honored, and `AI_BRIDGE_CONFIG_DIR` can explicitly select another root.

```text
~/.config/ai-bridge/
  server-instructions.md
  tool-descriptions/
    run_command.md             # optional override for an existing tool
  instructions/               # workspace documents discovered on demand
  resources.json
  resources/
    bridge-guide.md
  prompts.json
  prompts/
    workspace-task.md
```

| Host file | Standard MCP field or operation | Changes take effect |
|---|---|---|
| `server-instructions.md` | `InitializeResult.instructions` | After restart and client initialization |
| `tool-descriptions/<tool_name>.md` | `Tool.description` in `tools/list` | After restart and client metadata refresh |
| `resources.json` | Metadata for `resources/list` | After restart and client metadata refresh |
| Registered resource files | Text returned by `resources/read` | On the next read |
| `prompts.json` | Metadata and arguments for `prompts/list` | After restart and client metadata refresh |
| Registered prompt message files | Messages returned by `prompts/get` | On the next retrieval |

The whole `server-instructions.md` file replaces built-in connection guidance. If
it is absent, the packaged defaults are used; an existing empty file intentionally
provides no server instructions. A tool-description file replaces only the named
tool's description, keeping its implementation, parameters, and annotations.
Absent tool overrides use the built-in descriptions. The installer preserves edits;
delete an override file and restart if you want to return to its packaged default.

Register UTF-8 text resources explicitly in `resources.json`, a JSON array:

```json
[
  {
    "uri": "ai-bridge://documents/project-layout",
    "name": "project-layout",
    "title": "Project layout",
    "description": "Where to find projects and documents.",
    "mime_type": "text/markdown",
    "path": "resources/project-layout.md"
  }
]
```

The required fields are `uri`, `name`, and `path`. Other shown fields are optional.
URIs must be absolute static URIs. Paths are relative to the host configuration root,
or may be absolute or start with `~`. Only registered files become MCP resources;
putting a file in `resources/` alone does not publish it. Use `[]` to publish none.

Register prompt templates in `prompts.json`, also a JSON array:

```json
[
  {
    "name": "review-project",
    "description": "Review a selected project.",
    "arguments": [
      {"name": "project", "description": "Project directory", "required": true}
    ],
    "messages": [{"role": "user", "path": "prompts/review-project.md"}]
  }
]
```

The referenced Markdown file can contain `Review {{project}} using its local
instructions.` The manifest requires `name` and a nonempty `messages` array;
`title`, `description`, and `arguments` are optional. Each message requires `path`,
and its role is `user` (default) or `assistant`. Templates can return multiple
messages. Arguments have a name, optional description, and `required` flag
(default `false`). Placeholders use `{{argument_name}}`, with letter/underscore
names containing letters, digits, or underscores. Missing optional values become
empty text. Substitution treats values as plain text and never evaluates code.
Missing required arguments, unknown supplied arguments, or undeclared placeholders
produce retrieval errors. Use `[]` in `prompts.json` to publish no prompts.
The protocol method is **`prompts/get`**, not `prompts/read`.

Initialize missing defaults or validate configuration using the installed environment:

```bash
PYTHONPATH="$HOME/.local/share/ai-bridge/current/app" \
  ~/.local/share/ai-bridge/current/venv/bin/python -m ai_bridge.host_config --init
PYTHONPATH="$HOME/.local/share/ai-bridge/current/app" \
  ~/.local/share/ai-bridge/current/venv/bin/python -m ai_bridge.host_config --check
sudo systemctl restart ai-bridge
```

`--init` preserves existing files. `--check` validates manifest syntax, supported
fields, duplicate identifiers, and referenced file paths; full server startup also
checks tool override names. An invalid definition fails validation/startup rather
than silently publishing an incomplete catalog. Prompt placeholders are checked
when the prompt is requested. The installed host `README.md` documents these formats.

Connecting exposes server guidance and available metadata, while resource and prompt
bodies remain on demand. Clients decide whether to use MCP resources and prompts;
support varies by client, and cached metadata may need a refresh/reconnection.
The existing `list_instructions`, `list_skills`, and `read_file` tools remain available.

### Agent permission levels

| permission | Claude Code | Codex | Cursor |
|---|---|---|---|
| `readonly` | `--permission-mode plan` | `--sandbox read-only` | `--mode ask` |
| `edit` (default) | `--permission-mode acceptEdits` | `--sandbox workspace-write` with approvals disabled | `--force` |
| `full` | `--dangerously-skip-permissions` | `--dangerously-bypass-approvals-and-sandbox` | `--force` |

Codex resumes support all three permission levels. Sandbox options are passed to `codex exec` before its `resume` subcommand. Both Codex `readonly` and `edit` disable interactive approvals.

Change the default with `AI_BRIDGE_AGENT_DEFAULT_PERMISSION` in the env file, or just tell the assistant "use full permission" per task. Note `edit` mode may block some commands the agent wants to run (for example installs or network access in Codex); the result will say so.

### Model information and selection

Session listings and the session metadata included with history expose:

- `last_used_model`: the latest recognized saved model value, or `null`.
- `model_source`: the explicit metadata field it came from, or `null`.

Codex uses recent `turn_context.payload.model` (the recorded turn selection), Claude
uses assistant `message.model`, and Cursor uses `meta.lastUsedModel` (its saved
selection). These are historical records/settings, not proof of an active backend
or the model the next turn will use. Aliases such as auto are returned as recorded.
Synthetic messages and provider names are not treated as model IDs. JSONL lookup
is bounded to the last 2 MB; missing, older-outside-window, or unrecognized model
metadata stays unknown. No binary/private database model decoding is added.

Call `agent_models(agent="cursor")` or `agent_models(agent="codex")` when model
choices are needed. Results contain `status` (`live`, `cached`, or `unavailable`),
`source`, `fetched_at`, `age_seconds`, model IDs/names, and pagination fields.

- Cursor: invokes the CLI's account-aware `models` command with a 20-second timeout.
  Its text format is parsed conservatively; changed output returns unavailable.
  `is_cli_current` describes the CLI setting, not every existing chat.
- Codex: reads its local `models_cache.json` and reports its original fetch timestamp
  and age when known. Cached entries, including visibility metadata, do not establish
  current account access. Missing or changed cache formats return unavailable.
- Claude: returns unavailable because the bridge has no verified account-aware model
  catalog interface for it. Previously used models are not presented as available.

Use `agent_send(..., model="<model-id>")` to request a model for a new or resumed
turn. Omitting it leaves model selection to that CLI's configuration/resume behavior.
`agent_wait` returns `requested_model` separately from `reported_model`; the latter
is populated only from recognized explicit assistant-message model metadata in the
job output. It may be null even when the turn succeeds. The bridge does not assume
that the requested alias resolves to a particular backend or survived a fallback.

Discovery runs only on request, never during MCP startup. No model catalog is
hardcoded, and missing CLIs, auth failures, unsupported formats, or timeouts do not
disable the bridge's other tools. Refresh client tool metadata to discover the new
`agent_models` tool after upgrading.

### Continue existing chats across projects

1. Ask for `agent_sessions(agent="cursor")` (or `codex` / `claude`). Each row includes the full session ID, project `cwd`, title, storage source, and whether it can be resumed. Use `cwd` to filter by project and `next_offset` to page through older chats.
2. Ask for `agent_history` with that exact session ID to summarize it. This only reads local storage; it does not start a model turn. `offset` pages backward from the end, and `include_tools=false` focuses on conversation text. Cursor exposes active structured turns plus any saved compaction summary; it does not claim to recover discarded context.
3. Call `agent_send` with the same `session_id` and your follow-up prompt. Omit `cwd`: the bridge selects the saved project automatically. A conflicting project, unknown/partial ID, missing project, or unavailable CLI state produces an error instead of starting a new chat.
4. Poll `agent_wait`. It checks that the CLI returned the requested session ID and reports a continuity error if it did not.

Codex session rows include `parent_session_id` when a parent thread is recorded.
Group those records as subagent activity under their parent rather than counting
them as independent main chats. A null parent means none was recorded; it does
not establish that a record is a user-started development conversation. Tests and
automated executions may also have no parent.

Cursor support covers **local Cursor CLI chats**, including chats created outside the bridge. Cursor editor/cloud chats are separate; this bridge does not import or synchronize them. Subagent stores are excluded from the Cursor chat list. A moved Cursor project whose store hash no longer matches its saved workspace is listed as readable but not resumable; no automatic database migration is performed.

Do not send simultaneous turns from an editor/terminal and the bridge into the same chat. The bridge prevents overlapping bridge-launched resumes, but cannot reliably detect all external CLI/editor activity. Keep the returned session ID in follow-up requests; omitting it explicitly starts a new chat.

Session parsing depends on local CLI storage formats. Validate compatibility with the CLI versions you deploy; unit tests do not establish public connector connectivity.

Run the regression suite from the source folder in the installed Python environment:

```bash
~/.local/share/ai-bridge/current/venv/bin/python -m unittest discover -s tests -v
```


## Execution and reliability

The table above lists all 17 tools. Tool schemas returned by `tools/list` define
the parameters and defaults. Read-only annotations are client hints, not access
controls; the same authenticated connection can invoke mutating tools.

`run_command` uses a login Bash shell without a TTY and clamps timeouts to
1–600 seconds (default 120). `start_job` returns a job ID; `job_status` can wait
up to 110 seconds and include up to 400 tail lines. `list_jobs` returns at most
100 records. `kill_job` attempts SIGTERM followed by SIGKILL; inspect cleanup
results because privilege boundaries can prevent termination.

`job_log` defaults to 12,000 bytes per chunk. Follow `next_offset_bytes` and pass
the returned `generation`; restart pagination if the snapshot changes. Truncated
logs retain the head and tail, with the middle omitted. Output capture is bounded
and is not a complete archival transcript.

`read_file` defaults to line 1 and 400 lines, with bounded byte capture.
`include_hash=true` scans the whole file for SHA256. `write_file` defaults to a
direct write, creates parent directories, and can append. `atomic=true` opts
into replacement of supported regular files; `expected_sha256` detects stale
content, `backup=true` creates a private local copy, and `mode` accepts integer
permission bits (384 means 0600). Atomic writes reject symlinks, hard links,
ACLs/xattrs, and special files; they cannot append. Hash checking is optimistic,
not a lock against another editor. Both file tools accept `sudo=true`.

`agent_send` defaults to waiting 30 seconds (maximum 110); follow its job ID with
`agent_wait` (default 60 seconds). `agent_sessions` defaults to 15 rows; follow
`next_offset`. `agent_history` defaults to the last 30 entries with tool actions,
clips each message to 2,000 characters, and pages backward using `offset` and
`next_offset`. It allows up to 300 entries and 10,000 characters per message; the
overall response budget may further reduce a page.

The shell, job, file, authentication, and audit core is separate from optional
agent adapters. Missing CLIs or unavailable parsers do not disable host tools.
Legacy/uncertain jobs may lack verified process identity; cleanup and retention
are conservative. Detached sidecars keep running through a bridge restart and
use the code of the release that launched them. Retain that release until they
finish. No restart, rollback, or `kill_job` undoes completed filesystem changes.

Agent permissions apply to the agent CLI invocation, not to the bridge's shell
and file tools. Cursor maps both `edit` and `full` to `--force`; do not interpret
`edit` as an equivalent sandbox across providers. Extra CLI arguments configured
by the operator can also affect behavior.

See [setup](setup.md), [operations](operations.md), and the
[security and reliability guide](system-hardening.md).

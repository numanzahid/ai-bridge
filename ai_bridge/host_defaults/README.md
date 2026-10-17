# Host configuration

Default root: `~/.config/ai-bridge/`. `AI_BRIDGE_CONFIG_DIR` overrides it;
otherwise `XDG_CONFIG_HOME` is honored. The installer creates missing defaults,
including example resource/prompt manifests, without overwriting existing files.

## Choose the right surface

| File | MCP surface / purpose | Apply edits |
|---|---|---|
| `server-instructions.md` | `InitializeResult.instructions`: brief connection guidance | Restart and reconnect |
| `tool-descriptions/TOOL_NAME.md` | `Tool.description` in `tools/list` | Restart and refresh metadata |
| `instructions/*.md` or `*.txt` (subfolders allowed) | Optional workspace guidance via `list_instructions` → `read_file` | Next discovery/read |
| `resources.json` | Definitions for `resources/list` | Restart and refresh metadata |
| Registered resource files | UTF-8 text returned by `resources/read` | Next read |
| `prompts.json` | Definitions/arguments for `prompts/list` | Restart and refresh metadata |
| Registered prompt files | Messages rendered by `prompts/get` | Next retrieval |

Keep connection guidance short: discover relevant documents, use jobs for long
commands, preserve session IDs and report privilege use. A custom file replaces
the packaged guidance, **but the server always appends privilege-disclosure
instructions**, even when the file is empty. Missing files use packaged guidance.
These instructions guide clients; they do not enforce permissions or override
the client's own instruction hierarchy.

Use the exact existing tool name for an override, such as `run_command.md`.
The whole file replaces its description, so retain necessary usage details.
Parameters, behavior and annotations stay defined by code. Missing overrides use
built-in descriptions; empty overrides and unknown tool names fail startup.

## Optional workspace documents and skills

Put project locations in `instructions/projects.md`, document/resource locations
in `instructions/documents.md`, and working preferences in
`instructions/preferences.md`. Names are conventions; choose useful topics and
subfolders. Keep project-specific rules with their project and reference them
rather than copying every rule into connection guidance.

A heading is sufficient; optional frontmatter improves catalog descriptions:

```markdown
---
title: Project locations
description: Where to find projects and create new ones.
---
# Project locations
Describe your actual directories and conventions here.
```

`list_instructions` returns names, short descriptions and paths, not bodies.
Read selected files with `read_file`. `list_skills` separately discovers existing
`SKILL.md` files for on-demand reading. Discovery does not install skills or
supply tools another client may require. Clients choose what to retrieve.

## Resources

`resources.json` is an array. Required fields: `uri`, `name`, `path`.
Optional: `title`, `description`, `mime_type`. Use absolute static URIs and UTF-8
files. Paths are relative to the host root unless absolute or prefixed with `~`.
Only registered files are published; `[]` or a missing manifest publishes none.

```json
[
  {
    "uri": "ai-bridge://documents/project-layout",
    "name": "project-layout",
    "description": "Where projects and documents belong.",
    "mime_type": "text/markdown",
    "path": "instructions/projects.md"
  }
]
```

A resource is reference content. It may reuse an instruction file, as above;
putting an unregistered file in `resources/` alone does not expose it through MCP.

## Prompts

`prompts.json` is an array. Each entry requires `name` and nonempty `messages`;
`title`, `description` and `arguments` are optional. Messages require `path` and
may set `role` to `user` (default) or `assistant`. Arguments require `name` and
may set `description` and `required` (default false).

```json
[
  {
    "name": "review-project",
    "description": "Review a selected project.",
    "arguments": [{"name": "project", "required": true}],
    "messages": [{"role": "user", "path": "prompts/review-project.md"}]
  }
]
```

The message file could contain `Review {{project}} using its local instructions.`
Templates return one or more messages. Argument names start with a letter or
underscore, followed by letters/digits/underscores. Declare all placeholders;
missing optional values become empty text. Missing required arguments, unknown
supplied arguments or undeclared placeholders produce retrieval errors.
Substitution is plain text, never code evaluation. `[]` or a missing manifest
publishes no prompts. Retrieval is `prompts/get`, not `prompts/read`.

## Initialize and validate

Run as the service account, using its HOME and the same host-root environment
settings as the service. From an installed versioned environment:

```bash
PYTHONPATH="$HOME/.local/share/ai-bridge/current/app" \
  ~/.local/share/ai-bridge/current/venv/bin/python -m ai_bridge.host_config --init
PYTHONPATH="$HOME/.local/share/ai-bridge/current/app" \
  ~/.local/share/ai-bridge/current/venv/bin/python -m ai_bridge.host_config --check
```

`--init` fills missing files; `--check` validates manifests, supported fields,
duplicate identifiers and referenced paths. Full startup also checks tool names.
Prompt placeholders are checked at retrieval. Invalid definitions fail rather
than silently publishing an incomplete catalog. Restart separately for metadata
changes; clients may also need to refresh/reconnect. Existing body-file edits are
read on demand. See the bundled [overview](resources/bridge-guide.md).

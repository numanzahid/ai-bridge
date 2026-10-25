# Host configuration

Lives in `~/.config/ai-bridge/` (or `AI_BRIDGE_CONFIG_DIR` / `$XDG_CONFIG_HOME/ai-bridge`).
The installer creates missing defaults and never overwrites your edits.

## What each file controls

| File | Shows up as | Apply edits |
|---|---|---|
| `server-instructions.md` | Connection guidance sent to clients | Restart + reconnect |
| `tool-descriptions/<tool>.md` | That tool's description | Restart + refresh |
| `instructions/*.md` / `*.txt` | Docs found via `list_instructions` | Immediately |
| `resources.json` | MCP resource list | Restart + refresh |
| `prompts.json` | MCP prompt list | Restart + refresh |
| Resource / prompt files | Their content | Immediately |

- Keep `server-instructions.md` short. The server always prepends execution-target guidance
  and appends privilege-disclosure guidance, even if your file is empty. Local Codex and
  Claude Code tool preferences belong in their own instruction files, not shared MCP notes.
- A tool description file replaces the whole description; name it after the exact tool.
  Unknown names or empty files stop startup.

## Workspace instructions

Put guidance in `instructions/`, e.g. `projects.md`, `documents.md`, `preferences.md`.
A heading is enough; frontmatter improves the listing:

```markdown
---
title: Project locations
description: Where projects live and where to create new ones.
---
# Project locations
```

Clients list names and paths, then read only what's relevant. See the
[instructions guide](../../docs/instructions-guide.md).

## Resources

`resources.json`: an array. Required: `uri`, `name`, `path`. Optional: `title`,
`description`, `mime_type`. Paths are relative to this folder unless absolute or `~`.

```json
[{"uri": "ai-bridge://documents/project-layout", "name": "project-layout",
  "mime_type": "text/markdown", "path": "instructions/projects.md"}]
```

Only listed files are published.

## Prompts

`prompts.json`: an array. Required: `name`, `messages` (each with `path`, optional
`role`: `user`/`assistant`). Optional: `title`, `description`, `arguments`
(`name`, `description`, `required`).

```json
[{"name": "review-project", "arguments": [{"name": "project", "required": true}],
  "messages": [{"path": "prompts/review-project.md"}]}]
```

The file might say `Review {{project}} using its local instructions.` Placeholders
are plain text substitution and must be declared. Clients fetch with `prompts/get`.

## Check your config

As the service user:

```bash
PYTHONPATH="$HOME/.local/share/ai-bridge/current/app" \
  ~/.local/share/ai-bridge/current/venv/bin/python -m ai_bridge.host_config --check
```

`--init` instead of `--check` fills in missing default files.

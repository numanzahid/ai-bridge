---
title: Shared instruction folder guide
description: How to organize workspace instructions that AI clients discover through AI Bridge.
---

# Shared workspace instructions

## Local Codex, Claude Code and shared MCP connections

Keep the bridge connection enabled for chatgpt.com and claude.ai. Those clients use MCP to
reach the host; a shared account does not put their cloud sandboxes on the same machine.
Local tool preferences belong only in the local coding agents' instruction files:

- Codex: `~/.codex/AGENTS.md` (or `$CODEX_HOME/AGENTS.md`). A non-empty global
  `AGENTS.override.md` takes precedence and also needs the preference.
- Claude Code: `~/.claude/CLAUDE.md` (or `$CLAUDE_CONFIG_DIR/CLAUDE.md`).

From the source folder, apply both without restarting the bridge or entering a password:

```bash
python3 -m ai_bridge.local_instructions
```

The interactive installer/update also applies these preferences. Existing instructions are
preserved, changed files get private backups beside them, and repeating the command does
not duplicate the managed section. The command reports paths/status, never existing content.
It updates the current user's profile; run it as the account that uses the coding agent.

The local preference says:

```markdown
Only when Codex or Claude Code has native tools on the user's target machine:
use native terminal, file search/read, and patch/edit tools for ordinary local work.
Keep AI Bridge for remote targets, user-requested bridge job/agent/session management,
or explicit bridge requests. Do not reroute local work or bypass native approvals through MCP.
Do not use agent_send to relay your own local task to another agent unless I request delegation.
This does not apply to chatgpt.com or claude.ai using MCP to reach the host.
```

Start new Codex and Claude Code sessions to load changed instructions. This is a preference,
not an access restriction. Shared MCP instructions and tool descriptions describe the host
and keep web-client access available; they do not carry the local native-tool preference.
Do not copy local agent instruction files into shared MCP notes, resources, or prompts.
See the official [Codex instructions guide](https://learn.chatgpt.com/docs/agent-configuration/agents-md)
and [Claude Code memory guide](https://code.claude.com/docs/en/memory).

## Bridge instruction documents

Put Markdown or text files in `~/.config/ai-bridge/instructions/` (subfolders are fine). Good starters:

- `projects.md`: where projects live and where new ones go
- `documents.md`: where documents and resources belong
- `preferences.md`: naming and working preferences

Start each file with a clear heading. Optional frontmatter improves discovery:

```markdown
---
title: Document storage
description: Where to find and save documents.
---
# Document storage
```

Clients call `list_instructions`, then `read_file` only what's relevant. Edits apply
on the next call. The user's current request always wins. Skills are found
with `list_instructions(kind=skills)`.

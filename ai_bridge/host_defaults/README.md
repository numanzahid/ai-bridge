# AI Bridge host configuration

Edit these files locally before starting or restarting AI Bridge.
The installer creates defaults only for files that do not already exist.

| File/folder | MCP surface | When contents are used |
|---|---|---|
| server-instructions.md | initialize result: instructions | At server startup, sent during connection |
| tool-descriptions/TOOL_NAME.md | tools/list: description | At server startup, sent during tool discovery |
| resources.json | resources/list metadata | Definitions registered at startup |
| resources/ | Files referenced by resources.json | Contents read on resources/read |
| prompts.json | prompts/list metadata and arguments | Definitions registered at startup |
| prompts/ | Files referenced by prompts.json | Contents rendered on prompts/get |
| instructions/ | list_instructions and read_file tools | Discovered/read on demand |

## Which instructions belong where?

| What you want to describe | Put it in | Example content |
|---|---|---|
| Brief rules for operating the bridge in every connected conversation | server-instructions.md | Discover workspace instructions before working; use background jobs for long commands; preserve session IDs when continuing chats |
| How to use one existing tool correctly | tool-descriptions/TOOL_NAME.md | agent_sessions.md explains pagination and grouping subagent records under parent_session_id |
| Where projects live and where new projects should be created | instructions/projects.md | Your actual project root, naming convention, and how to check for an existing project |
| Where resources, source documents, and generated files belong | instructions/documents.md | Reference-material directories, output directories, and temporary-file conventions |
| Your working preferences | instructions/preferences.md | Git operations only when requested, preferred package manager, and use of Trash for deletion |
| Guidance for a particular area of work | instructions/deployment.md or instructions/team/frontend.md | Your deployment workflow, service locations, or frontend conventions |
| A named reference document a client can explicitly retrieve through MCP | resources/FILE.md, registered in resources.json | A workspace map, architecture overview, glossary, or operating guide |
| A reusable request the user/client selects and fills in | prompts/FILE.md, registered in prompts.json | Review a project, plan a deployment, or summarize a selected chat |

The suggested instruction filenames are conventions; choose your own names and
subfolders. Use your actual paths and preferences rather than the example names.

### Connection-level instructions: server-instructions.md

Keep this file short because it is sent during connection initialization. Put
general operating guidance here and point to optional documents for detail.
For example:

```text
When first working on this machine, discover shared documents with list_instructions.
Read documents relevant to the task before choosing project or output locations.
For long commands, start a background job and check its status.
When continuing an existing coding-agent chat, reuse its exact session ID.
```

This file replaces all default server instructions, so retain any default guidance
you still want. The client decides how to use it; it does not become an enforced
filesystem policy or override the client's own instruction hierarchy.

### Tool-specific instructions: tool-descriptions/TOOL_NAME.md

Use the exact existing tool name as the filename, such as run_command.md,
agent_sessions.md, or read_file.md. The whole file becomes that tool's description.
Describe its purpose, when to use it, and important parameter or result details.
For example, agent_sessions.md might explain that the result is paginated and
that a recorded parent_session_id identifies activity belonging under another chat.

An override replaces the original description rather than appending to it. Include
the original usage details you still need. Editing this file changes guidance only;
the tool's actual parameters, execution behavior, and permissions are defined in code.

### Optional workspace documents: instructions/

Use separate documents for separate topics so the AI can select relevant files.
A clear heading is enough; optional frontmatter adds a description to the catalog:

```markdown
---
title: Project locations
description: Where to find projects and where to create new ones.
---

# Project locations

Describe your actual directories and project naming rules here.
```

list_instructions returns names, descriptions, and paths. The AI then calls
read_file for selected documents. Their bodies are not sent at connection or
included in the catalog. Edits are visible on the next discovery/read call.
Project-specific rules can remain in the project's own instruction files; shared
documents can explain where those live.

### Reference content versus reusable requests

A resource normally contains facts or reference material, such as a workspace map.
A prompt normally contains a task request with optional placeholders, such as
"Review {{project}} and focus on {{focus}}." Register them using the formats below.
These are separate from connection instructions and are retrieved only when used.

You can register an instruction document as a resource without duplicating it:
set a resource's path to instructions/projects.md in resources.json. It remains
available through list_instructions/read_file and also through resources/read.
Skill discovery remains separate: list_skills locates existing SKILL.md files,
and read_file loads selected skills and any needed referenced documents.

## Resources

resources.json is a JSON array. Each entry requires uri, name, and path. Optional
fields are title, description, and mime_type. URIs must be absolute static URIs,
such as ai-bridge://documents/workspace. Files must contain UTF-8 text.

Paths are relative to this configuration folder unless absolute or prefixed with ~.
Only listed files are exposed; adding a file to resources/ does not publish it.
Use [] in resources.json to expose no resources.

## Prompts

prompts.json is a JSON array. Each entry requires name and a nonempty messages array.
Optional fields are title, description, and arguments. Each message has a path and
a role (user or assistant; defaults to user). Each argument has a name, an optional
description, and required (defaults to false).

Use {{argument_name}} in the message files. Names must begin with a letter or
underscore and contain only letters, digits, and underscores. All placeholders
must be declared in the manifest. Missing optional arguments become empty text;
missing required arguments, unknown supplied arguments, and undeclared placeholders
produce errors. This is plain text substitution, with no code execution or expressions.
Use [] in prompts.json to expose no prompts.

## Applying changes

Restart after editing server-instructions.md, tool descriptions, or manifests.
Edits to existing resource/prompt body files and instruction documents are read
on the next request. Clients may cache metadata and require a refresh/reconnection.
MCP prompt retrieval is prompts/get; there is no prompts/read operation.

If server-instructions.md is absent, built-in guidance is used. An existing file
fully replaces it (including an intentionally empty file). Missing tool overrides
keep built-in descriptions; unknown tool names and empty overrides are rejected.
Missing manifests register no entries. Invalid manifests, duplicate identifiers,
unsupported fields, and missing referenced files fail startup with a configuration
error rather than silently publishing an incomplete catalog.

Creating these files does not enforce AI behavior or install skills in a client.
Clients control when to use instructions, resources, and prompts.

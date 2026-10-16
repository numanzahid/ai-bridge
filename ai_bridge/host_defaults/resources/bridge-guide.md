# Host-side MCP configuration

AI Bridge reads its host configuration from ~/.config/ai-bridge/ by default.
AI_BRIDGE_CONFIG_DIR can select another directory; XDG_CONFIG_HOME is also honored.

- server-instructions.md replaces the guidance sent during MCP initialization.
- tool-descriptions/<tool_name>.md replaces that existing tool's description.
- instructions/ contains optional workspace guidance discovered through list_instructions.
- resources.json registers named UTF-8 text files for resources/list and resources/read.
- prompts.json defines named prompt templates for prompts/list and prompts/get.
- resources/ and prompts/ are convenient places for the files referenced by those manifests.

Only files explicitly registered in the manifests become MCP resources or prompts.
Prompt placeholders use double braces, for example {{task}}. Template arguments
must be declared in prompts.json; inserted values are treated as plain text.

Restart the server after changing connection instructions, tool descriptions, or
manifest definitions. Resource and prompt bodies are read on request, so edits to
the contents of existing files do not need a restart. Clients may cache metadata
and need to refresh their tool/resource/prompt lists after a configuration change.

These descriptions and documents are guidance. They do not grant new tools or
replace the user's requests and the connected client's own instruction policies.

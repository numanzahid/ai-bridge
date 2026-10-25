# Host-side MCP configuration

AI Bridge defaults to `~/.config/ai-bridge/`. `AI_BRIDGE_CONFIG_DIR` overrides it;
otherwise `XDG_CONFIG_HOME` is honored.

- `server-instructions.md`: connection guidance, preceded by mandatory execution-target
  instructions and followed by privilege-disclosure instructions even if the file is empty.
- `tool-descriptions/<tool_name>.md`: an existing tool's description, not its schema.
- `instructions/`: optional workspace documents discovered by `list_instructions`.
- `resources.json`: named UTF-8 files for `resources/list` and `resources/read`.
- `prompts.json`: templates for `prompts/list` and `prompts/get`.

Only registered files become resources/prompts. `resources/` and `prompts/` are
convenient storage folders; instruction files can also be registered as resources.
Prompt placeholders such as `{{task}}` require declared arguments. Values are
inserted as plain text, never evaluated as code.

Restart for connection guidance, tool descriptions or manifest changes, then
refresh client metadata. Instruction documents and existing resource/prompt body
edits are read on demand. Clients decide what to retrieve; these documents do not
enforce behavior or supply new tools. See the host root's `README.md` for formats.

To override a tool description, create a UTF-8 Markdown file named after the tool.
For example, run_command.md replaces the description for run_command in tools/list.
The entire file is the description; do not add frontmatter.

Only existing tools can be customized. Tool behavior, parameters, permissions,
and annotations are not changed by editing a description. Missing override files
keep the built-in descriptions. Empty overrides are rejected at startup.
Restart AI Bridge and refresh the client's tool list after editing.

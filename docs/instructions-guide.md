---
title: Shared instruction folder guide
description: How to organize and discover workspace instructions through AI Bridge.
---

# Shared workspace instructions

Place your Markdown (.md) or text (.txt) instruction files in this folder.
You can use multiple files and subfolders. Suggested topics:

- projects.md: locations of existing projects and where to create new projects.
- documents.md: where resources, reference material, and generated documents belong.
- preferences.md: naming conventions and working preferences.

Use a clear heading in each document. You may add a short description for discovery:

```markdown
---
title: Document storage
description: Where to find and save documents and shared resources.
---

# Document storage

Describe your actual directories and conventions here.
```

AI clients discover the catalog with list_instructions, then read only relevant
documents using read_file. Files are not loaded into the conversation at connection.
Edits are visible on the next tool call. The user’s current request takes precedence.
Project-specific instructions should also be checked when working in that project.

Skills are discovered separately with list_skills and read on demand with read_file.

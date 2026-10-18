---
title: Shared instruction folder guide
description: How to organize workspace instructions that AI clients discover through AI Bridge.
---

# Shared workspace instructions

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

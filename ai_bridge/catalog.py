"""On-demand discovery of instruction documents and local SKILL.md files."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path


def config_dir() -> Path:
    override = os.environ.get("AI_BRIDGE_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "ai-bridge"


def instructions_dir() -> Path:
    return config_dir() / "instructions"


def skill_dirs() -> list[Path]:
    configured = os.environ.get("AI_BRIDGE_SKILL_DIRS")
    if configured is not None:
        return [Path(p.strip()).expanduser() for p in configured.split(os.pathsep) if p.strip()]
    home = Path.home()
    return [
        Path(os.environ.get("CODEX_HOME") or home / ".codex") / "skills",
        home / ".claude/skills",
        home / ".cursor/skills",
        home / ".agents/skills",
    ]


def _metadata(path: Path) -> dict:
    # Read only a short prefix on discovery, never a complete skill or document.
    with path.open(encoding="utf-8", errors="replace") as stream:
        prefix = stream.read(8192)
    lines = prefix.lstrip("\ufeff").splitlines()
    meta = {}
    # Recognize common scalar frontmatter without depending on a YAML parser.
    if lines and lines[0].strip() == "---":
        end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
        if end is not None:
            header, lines = lines[1:end], lines[end + 1:]
            for i, line in enumerate(header):
                match = re.match(r"^(name|title|description):\s*(.*)$", line)
                if not match:
                    continue
                key, value = match.groups()
                if value in ("", ">", ">-", ">+", "|", "|-", "|+"):
                    parts = []
                    for following in header[i + 1:]:
                        if following and not following[0].isspace():
                            break
                        parts.append(following.strip())
                    value = " ".join(parts)
                elif value.startswith('"'):
                    try:
                        value = json.loads(value)
                    except ValueError:
                        value = value.strip('"')
                elif value.startswith("'") and value.endswith("'"):
                    value = value[1:-1].replace("''", "'")
                meta[key] = value
    heading = next((line.lstrip("# ").strip() for line in lines if line.startswith("# ")), None)
    return {
        "name": str(meta.get("name") or meta.get("title") or heading or path.stem)[:200],
        "description": str(meta.get("description") or "")[:800],
    }


def discover(roots: list[Path], skills: bool = False) -> list[dict]:
    rows = []
    visited_dirs = set()
    visited_files = set()
    for root in roots:
        # Follow skill-directory symlinks, but avoid duplicate trees and cycles.
        for directory, dirs, files in os.walk(root, followlinks=True):
            resolved = Path(directory).resolve()
            if resolved in visited_dirs:
                dirs[:] = []
                continue
            visited_dirs.add(resolved)
            dirs.sort()
            for name in sorted(files):
                path = Path(directory) / name
                matches = name == "SKILL.md" if skills else path.suffix.lower() in (".md", ".txt")
                if not matches:
                    continue
                resolved_file = path.resolve()
                if resolved_file in visited_files:
                    continue
                visited_files.add(resolved_file)
                row = {"path": str(path.absolute()), "directory": str(root.absolute())}
                try:
                    row.update(_metadata(path))
                except OSError as error:
                    row.update(name=path.stem, description="", error=str(error))
                rows.append(row)
    return sorted(rows, key=lambda row: row["path"])

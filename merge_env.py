"""Preserve a systemd environment file and append missing installer defaults."""
import re
import sys
from pathlib import Path


def merge(existing: str, defaults: str) -> str:
    keys = set(re.findall(r"^\s*([A-Za-z_][A-Za-z_0-9]*)\s*=", existing, re.MULTILINE))
    additions = []
    for line in defaults.splitlines():
        match = re.match(r"([A-Za-z_][A-Za-z_0-9]*)=", line)
        if match and match[1] not in keys:
            additions.append(line)
            keys.add(match[1])
    if not additions:
        return existing
    return existing + ("\n" if existing and not existing.endswith("\n") else "") + "\n".join(additions) + "\n"


if __name__ == "__main__":
    existing, defaults = map(Path, sys.argv[1:])
    defaults.write_text(merge(existing.read_text(), defaults.read_text()))

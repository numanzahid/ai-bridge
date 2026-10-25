"""Install local Codex/Claude Code preferences without publishing them through MCP."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import tempfile


START = '<!-- ai-bridge:local-agent-tools:start -->'
END = '<!-- ai-bridge:local-agent-tools:end -->'
BLOCK = f"""{START}
## Local agent tools and AI Bridge

- This section applies only to Codex or Claude Code running with native terminal and file tools on the user's target machine. It does not tell chatgpt.com or claude.ai to avoid MCP: their cloud sandboxes are separate environments and should use AI Bridge to reach this host.
- For ordinary work on this machine and in the current workspace, use native terminal, search/read, and patch/edit tools. Do not call AI Bridge discovery, shell or file tools for the same local task.
- Keep AI Bridge available for a different explicitly targeted host, an explicit bridge request, or user-requested bridge jobs and agent/session management. Do not relay your own local task to another local Codex or Claude Code session through agent_send unless the user asks for delegation.
- If a native tool fails or needs sandbox, permission or approval handling, follow its native approval process. Do not switch to MCP, another account or another agent to get around that restriction.
- Identify the actual execution target; sharing a ChatGPT or Claude account does not make a cloud sandbox and this machine the same environment. These preferences concern AI Bridge only; use other connected apps normally when they help the task.
{END}
"""


def update_file(path):
    """Preserve other guidance, keep private backups, and replace only our marked block."""
    path = Path(path)
    if path.is_symlink() or (path.exists() and not stat.S_ISREG(path.stat().st_mode)):
        raise ValueError(f'Refusing non-regular instruction file: {path}')
    original = path.read_text() if path.exists() else ''
    if original.count(START) != original.count(END) or original.count(START) > 1:
        raise ValueError(f'Invalid AI Bridge instruction markers: {path}')
    if START in original:
        start, end = original.index(START), original.index(END)
        if end < start:
            raise ValueError(f'Invalid AI Bridge instruction markers: {path}')
        updated = original[:start] + BLOCK.rstrip('\n') + original[end + len(END):]
    else:
        updated = original + ('\n\n' if original and not original.endswith('\n\n') else '') + BLOCK
    if updated == original:
        return {'path': str(path), 'status': 'unchanged'}
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    backup = None
    if path.exists():
        fd, backup = tempfile.mkstemp(prefix=path.name + '.before-ai-bridge-', dir=path.parent)
        with os.fdopen(fd, 'w') as stream:
            stream.write(original)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(updated)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return {'path': str(path), 'status': 'updated', 'backup': backup}


def install(home=None, environ=None):
    home = Path(home or Path.home())
    env = os.environ if environ is None else environ
    codex = Path(env.get('CODEX_HOME') or home / '.codex').expanduser()
    claude = Path(env.get('CLAUDE_CONFIG_DIR') or home / '.claude').expanduser()
    paths = [codex / 'AGENTS.md', claude / 'CLAUDE.md']
    override = codex / 'AGENTS.override.md'
    # Codex uses the first non-empty global override instead of AGENTS.md.
    if override.exists() and override.stat().st_size:
        paths.insert(1, override)
    return [update_file(path) for path in paths]


def main():
    for result in install():
        # Return paths/status only; never export existing personal instructions.
        print(json.dumps(result))


if __name__ == '__main__':
    main()

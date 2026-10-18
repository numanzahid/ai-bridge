"""Grant or revoke coding-agent folder trust without the CLIs' interactive prompts.

Each CLI refuses (or limits) work in a folder the user has not trusted, and only asks in a terminal.
These helpers write the same records the CLIs write when the user accepts:

- Claude: ~/.claude.json projects["<path>"].hasTrustDialogAccepted (documented)
- Codex:  ~/.codex/config.toml [projects."<path>"] trust_level = "trusted" (documented)
- Cursor: ~/.cursor/projects/<slug>/.workspace-trusted marker (the CLI's own format)

Trust is keyed on the git repository root when the folder is inside one, else the folder itself.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time

AGENTS = ('claude', 'codex', 'cursor')


def trust_key(path: str) -> str:
    """Folder whose trust covers `path`; refuses home, / and their ancestors."""
    folder = Path(os.path.realpath(os.path.expanduser(path)))
    if not folder.is_dir():
        raise ValueError(f'Not a directory: {folder}')
    result = subprocess.run(['git', '-C', str(folder), 'rev-parse', '--show-toplevel'],
                            capture_output=True, text=True, timeout=10)
    key = Path(os.path.realpath(result.stdout.strip())) if result.returncode == 0 and result.stdout.strip() else folder
    home = Path(os.path.realpath(Path.home()))
    if key == home or key in home.parents or key == Path('/'):
        raise ValueError(f'Refusing to trust {key}: it would cover the whole home directory.')
    return str(key)


def _write_atomic(path: Path, text: str) -> None:
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name + '.ai-bridge-')
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# Claude ----------------------------------------------------------------------

def _claude_file() -> Path:
    base = os.environ.get('CLAUDE_CONFIG_DIR')
    return Path(base) / '.claude.json' if base else Path.home() / '.claude.json'


def _claude(key: str, trusted: bool) -> str:
    path = _claude_file()
    if not path.exists():
        return 'not_installed'
    # Claude rewrites this file often; re-read, change one key, write atomically, then verify.
    for _ in range(3):
        data = json.loads(path.read_text() or '{}')
        entry = data.setdefault('projects', {}).setdefault(key, {})
        if bool(entry.get('hasTrustDialogAccepted')) == trusted:
            return 'unchanged'
        if trusted:
            entry['hasTrustDialogAccepted'] = True
        else:
            entry.pop('hasTrustDialogAccepted', None)
        _write_atomic(path, json.dumps(data, indent=2, ensure_ascii=False) + '\n')
        time.sleep(0.2)
        check = json.loads(path.read_text() or '{}').get('projects', {}).get(key, {})
        if bool(check.get('hasTrustDialogAccepted')) == trusted:
            return 'trusted' if trusted else 'untrusted'
    raise OSError(f'{path} kept changing; trust for {key} was not saved.')


# Codex -----------------------------------------------------------------------

def _codex_file() -> Path:
    return Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex') / 'config.toml'


def _codex(key: str, trusted: bool) -> str:
    path = _codex_file()
    if not path.parent.exists():
        return 'not_installed'
    text = path.read_text() if path.exists() else ''
    header = '[projects.' + json.dumps(key) + ']'
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.strip() == header), None)
    if start is None:
        if not trusted:
            return 'unchanged'
        new = text.rstrip('\n') + ('\n\n' if text.strip() else '') + header + '\ntrust_level = "trusted"\n'
    else:
        end = next((i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith('[')), len(lines))
        body = [line for line in lines[start + 1:end] if not re.match(r'\s*trust_level\s*=', line)]
        current = any(re.match(r'\s*trust_level\s*=\s*"trusted"', line) for line in lines[start + 1:end])
        if current == trusted:
            return 'unchanged'
        if trusted:
            section = [header, 'trust_level = "trusted"'] + body
        else:
            section = [] if not any(line.strip() for line in body) else [header] + body
        new = '\n'.join(lines[:start] + section + lines[end:]).rstrip('\n') + '\n'
    try:
        import tomllib
        tomllib.loads(new)
    except ImportError:
        pass
    except ValueError as error:
        raise ValueError(f'Refusing to write invalid TOML to {path}: {error}') from error
    _write_atomic(path, new)
    return 'trusted' if trusted else 'untrusted'


# Cursor ----------------------------------------------------------------------

def _cursor_marker(key: str) -> Path:
    base = Path(os.environ.get('AI_BRIDGE_CURSOR_HOME') or Path.home() / '.cursor')
    slug = re.sub(r'-+', '-', re.sub(r'[^a-zA-Z0-9]', '-', key)).strip('-')
    return base / 'projects' / slug / '.workspace-trusted'


def _cursor(key: str, trusted: bool) -> str:
    marker = _cursor_marker(key)
    if not marker.parent.parent.parent.exists():
        return 'not_installed'
    if marker.exists() == trusted:
        return 'unchanged'
    if trusted:
        marker.parent.mkdir(parents=True, exist_ok=True)
        _write_atomic(marker, json.dumps({'trustedAt': time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime()),
                                          'workspacePath': key, 'trustMethod': 'ai-bridge'}, indent=2))
        return 'trusted'
    marker.unlink()
    return 'untrusted'


HANDLERS = {'claude': _claude, 'codex': _codex, 'cursor': _cursor}


def set_trust(path: str, agents=AGENTS, trusted: bool = True) -> dict:
    """Grant or revoke trust for each agent; failures are reported per agent, never raised."""
    key = trust_key(path)
    results = {}
    for agent in agents:
        try:
            results[agent] = HANDLERS[agent](key, trusted)
        except (OSError, ValueError) as error:
            results[agent] = f'error: {error}'
    return {'path': key, 'trusted': trusted, 'agents': results}

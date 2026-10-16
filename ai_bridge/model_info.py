"""Best-effort model metadata. Unknown formats stay unknown; no model IDs are hardcoded."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess

from .core import ANSI_RE
from .execution import probe

TAIL_BYTES = 2_000_000


def model_name(value) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > 512 or (value.startswith('<') and value.endswith('>')):
        return None
    return value


def saved_model(path: Path, agent: str) -> dict:
    """Read recent explicit metadata only, bounded to avoid scanning huge histories."""
    unknown = {"last_used_model": None, "model_source": None}
    try:
        with path.open('rb') as stream:
            stream.seek(0, 2)
            offset = max(0, stream.tell() - TAIL_BYTES)
            stream.seek(offset)
            lines = stream.read(TAIL_BYTES).splitlines()
        if offset:
            lines = lines[1:]  # The first record may be incomplete.
        for line in reversed(lines):
            try:
                entry = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(entry, dict):
                continue
            if agent == 'codex' and entry.get('type') == 'turn_context':
                payload = entry.get('payload')
                value = model_name(payload.get('model')) if isinstance(payload, dict) else None
                return {"last_used_model": value, "model_source": "codex.turn_context.model" if value else None}
            if agent == 'claude' and entry.get('type') == 'assistant' and not entry.get('isSidechain'):
                message = entry.get('message')
                raw = message.get('model') if isinstance(message, dict) else None
                # Synthetic messages are not model invocations.
                if isinstance(raw, str) and raw.startswith('<') and raw.endswith('>'):
                    continue
                value = model_name(raw)
                return {"last_used_model": value, "model_source": "claude.message.model" if value else None}
    except OSError:
        pass
    return unknown


def _age(timestamp) -> float | None:
    try:
        date = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
        if date.tzinfo is None:
            return None
        age = (datetime.now(timezone.utc) - date).total_seconds()
        return round(age, 1) if age >= 0 else None
    except (ValueError, TypeError, AttributeError):
        return None


def parse_cursor_models(output: str) -> list[dict]:
    """Isolate the CLI's text format; reject changed output instead of guessing."""
    lines = ANSI_RE.sub('', output).splitlines()
    lines = [line.strip() for line in lines if line.strip()]
    if not lines or lines[0] != 'Available models':
        raise ValueError('Unrecognized Cursor model-list format')
    models = []
    for line in lines[1:]:
        if line.startswith('Tip:'):
            break
        match = re.fullmatch(r'([A-Za-z0-9][A-Za-z0-9._:/+\[\]-]*) - (.+)', line)
        if not match:
            raise ValueError('Unrecognized Cursor model-list entry')
        identifier, name = match.groups()
        row = {"id": identifier, "name": name}
        for suffix, flag in [(' (default)', 'is_default'), (' (current)', 'is_cli_current')]:
            if name.endswith(suffix):
                row['name'] = name[:-len(suffix)]
                row[flag] = True
        models.append(row)
    if not models or len({row['id'] for row in models}) != len(models):
        raise ValueError('Empty or duplicate Cursor model-list entries')
    return models


def discover_models(agent: str, *, codex_dir: Path | None = None, binary: str | None = None) -> dict:
    result = {"agent": agent, "status": "unavailable", "source": None,
              "fetched_at": None, "age_seconds": None, "models": []}
    if agent == 'codex':
        result['source'] = 'codex.models_cache.json'
        result['note'] = 'Cached catalog, not verified live availability. Entries and aliases do not guarantee account access or the next turn model.'
        try:
            path = codex_dir / 'models_cache.json' if codex_dir is not None else None
            if path is None:
                return result
            with path.open('rb') as stream:
                data = stream.read(20_000_001)
            if len(data) > 20_000_000:
                raise ValueError('Catalog exceeds size limit')
            cached = json.loads(data)
            if not isinstance(cached, dict) or not isinstance(cached.get('models'), list):
                raise ValueError('Unrecognized cache format')
            models = []
            for entry in cached['models']:
                if not isinstance(entry, dict):
                    continue
                identifier = model_name(entry.get('slug'))
                if identifier:
                    row = {"id": identifier, "name": model_name(entry.get('display_name')) or identifier}
                    if isinstance(entry.get('visibility'), str):
                        row['visibility'] = entry['visibility']
                    models.append(row)
            if not models:
                raise ValueError('No recognized model entries')
            models = list({row['id']: row for row in models}.values())
            fetched = cached.get('fetched_at')
            result.update(status='cached', models=models, fetched_at=fetched if isinstance(fetched, str) else None,
                          age_seconds=_age(fetched))
        except (OSError, ValueError):
            result['note'] = 'No readable, recognized Codex model cache. No model list is inferred from chat history.'
    elif agent == 'cursor':
        result['source'] = 'cursor.models_cli'
        if not binary:
            result['note'] = 'Cursor CLI is not installed or not on PATH.'
            return result
        try:
            process = probe([binary, 'models'], timeout=20)
            if process['exit_code'] or process['timed_out'] or process['stdout_truncated']:
                raise ValueError('Model discovery failed')
            models = parse_cursor_models(process['stdout'])
            result.update(status='live', models=models, fetched_at=datetime.now(timezone.utc).isoformat(), age_seconds=0)
            result['note'] = 'Account catalog reported by the CLI; identifiers may be aliases. is_cli_current is the CLI setting, not the active model of every saved chat.'
        except (OSError, ValueError, subprocess.SubprocessError):
            result['note'] = 'Cursor model discovery failed, timed out, or returned an unsupported format. Check CLI login/version; no model IDs are guessed.'
    else:
        result['note'] = 'No reliable account-aware model catalog interface is configured for this agent. Use a known supported model ID; historical usage does not establish current availability.'
    return result

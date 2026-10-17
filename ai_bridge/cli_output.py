"""Dependency-free terminal presentation; dispatch values are never modified."""
from datetime import datetime
import json
import math
import re
import time
import unicodedata


UUID = re.compile(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}')


def safe(value):
    """Prevent stored titles/paths from injecting terminal control sequences."""
    text = str(value)
    return ''.join(c if not unicodedata.category(c).startswith('C') else
                   (f'\\u{ord(c):04x}') for c in text)


def cell_width(text):
    return sum(0 if unicodedata.combining(c) else
               2 if unicodedata.east_asian_width(c) in ('W', 'F') else 1 for c in text)


def clip(text, width):
    if cell_width(text) <= width:
        return text
    result = ''
    for c in text:
        if cell_width(result + c) > width - 1:
            break
        result += c
    return result + '…'


def wrap(text, width):
    """Wrap by terminal cells, including paths without spaces."""
    line = ''
    for c in text:
        if line and cell_width(line + c) > width:
            yield line
            line = ''
        line += c
    yield line


def relative(value, now):
    if value is None or value == '':
        return '-'
    try:
        stamp = float(value) if isinstance(value, (int, float)) else datetime.fromisoformat(
            str(value).replace('Z', '+00:00')).timestamp()
        if not math.isfinite(stamp):
            return safe(value)
        seconds = now - stamp
        amount = abs(seconds)
        for unit, size in (('y', 31536000), ('mo', 2592000), ('d', 86400), ('h', 3600), ('m', 60)):
            if amount >= size:
                age = f'{int(amount // size)}{unit}'
                return f'{age} ago' if seconds >= 0 else f'in {age}'
        return 'just now' if seconds >= 0 else 'in <1m'
    except (ValueError, TypeError, OverflowError, OSError):
        return safe(value)


def value_text(value):
    if value is None:
        return '-'
    if isinstance(value, (dict, list, bool)):
        return safe(json.dumps(value, ensure_ascii=False))
    return safe(value)


def short_id(value):
    text = value_text(value)
    return text[:8] if UUID.fullmatch(text) else text


def details(data, width):
    if not isinstance(data, dict):
        return list(wrap(value_text(data), width))
    lines = []
    for key, value in data.items():
        label = safe(key) + ': '
        text = value_text(value)
        # Identity fields remain exact and copyable, even in very narrow terminals.
        if key in ('session_id', 'selector', 'id', 'job_id', 'task_id', 'parent_session_id'):
            lines.append(label + text)
        else:
            lines.extend(wrap(label + text, width))
    return lines


def table(headers, rows, width, color=False):
    if not rows:
        return ['(none)']
    if width < 70:
        lines = []
        for i, row in enumerate(rows):
            if i:
                lines.append('')
            for header, text in zip(headers, row):
                lines.extend(wrap(f'{header}: {text}', width))
        return lines
    widths = [max(cell_width(h), *(cell_width(r[i]) for r in rows)) for i, h in enumerate(headers)]
    while sum(widths) + 2 * (len(widths) - 1) > width:
        index = max(range(len(widths)), key=widths.__getitem__)
        widths[index] -= 1
    def line(row):
        cells = []
        for text, size in zip(row, widths):
            shown = clip(text, size)
            padding = ' ' * (size - cell_width(shown))
            code = {'running': '32', 'active_job': '32', 'completed': '36',
                    'uncertain': '33', 'lost': '33', 'cleanup_incomplete': '33', 'failed': '31'}.get(text)
            if color and code:
                shown = f'\x1b[{code}m{shown}\x1b[0m'
            cells.append(shown + padding)
        return '  '.join(cells).rstrip()
    return [line(headers), line(['-' * n for n in widths]), *(line(row) for row in rows)]


def render(args, result, *, width=100, color=False, now=None):
    width = max(10, width)
    now = time.time() if now is None else now
    lines = []
    if args.group == 'sessions' and args.action == 'list':
        rows = result['sessions']
        headers = ['ID', 'AGENT', 'STATUS', 'UPDATED', 'MODE/TYPE', 'TITLE', 'WORKSPACE']
        cells = [[short_id(r.get('session_id')), value_text(r.get('agent')),
                  value_text(r.get('status')), relative(r.get('updated'), now),
                  value_text(r.get('mode', 'unknown')) + '/' + value_text(r.get('session_type', 'unknown')),
                  value_text(r.get('title')), value_text(r.get('cwd'))] for r in rows]
        lines += table(headers, cells, width, color)
        count, total, offset = len(rows), result['total'], result['offset']
        lines += list(wrap(f'Sessions: {count} shown, {total} total (offset {offset}).', width))
        if result['next_offset'] is not None:
            lines += list(wrap(f"Next page: --offset {result['next_offset']} (keep the same filters and --limit).", width))
        lines += list(wrap('IDs shortened; use --json for full IDs, then sessions inspect agent:UUID.', width))
        lines += list(wrap('Status reflects bridge jobs only.', width))
        for row in rows:
            if row.get('error'):
                lines += list(wrap(f"Warning: {safe(row.get('agent', '?'))}:"
                                   f"{safe(row.get('session_id', '?'))}: {value_text(row['error'])}", width))
        for agent, error in result['unavailable_agents'].items():
            lines += list(wrap(f'Warning: {safe(agent)} unavailable: {safe(error)}', width))
    elif args.action in ('list', 'status') and isinstance(result, list):
        if args.group == 'tasks':
            headers = ['TASK ID', 'CREATED', 'WORKSPACE']
            cells = [[value_text(r.get('task_id')), relative(r.get('created'), now),
                      value_text(r.get('workspace'))] for r in result]
            noun = 'Tasks'
        else:
            headers = ['JOB ID', 'AGENT', 'STATUS', 'SESSION', 'WORKSPACE']
            cells = [[value_text(r.get('job_id', r.get('id'))), value_text(r.get('agent')),
                      value_text(r.get('status')), short_id(r.get('session_id')),
                      value_text(r.get('cwd'))] for r in result]
            noun = 'Agent jobs'
        lines += table(headers, cells, width, color)
        lines += list(wrap(f'{noun}: {len(result)} total.', width))
        if args.group == 'agents':
            lines += list(wrap('Scope: bridge-managed jobs only. Use agents inspect JOB_ID for full details.', width))
    else:
        if args.group == 'sessions' and args.action == 'inspect' and isinstance(result, dict):
            agent = args.agent or args.session.split(':', 1)[0]
            lines.append(f"selector: {agent}:{result['session_id']}")
        lines += details(result, width)
    return '\n'.join(lines)

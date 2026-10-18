"""Out-of-band approval for privileged bridge calls (sudo=true, remote_sudo=true).

A privileged call never runs on the assistant's word. The bridge records a pending request and
returns its ID. The user approves on the host with this module's CLI, which writes the decision
through `sudo` (password typed on a real terminal) into a root-owned folder. The bridge only reads
that folder, so its own tools cannot approve their requests. Repeating the exact call after
approval runs it; "once" decisions are single use, "task" decisions allow a short window.

Run `python3 -I ~/.local/share/ai-bridge/current/app/ai_bridge/approvals.py --help` on the host.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import socket
import stat
import subprocess
import sys
import time

DECISION_DIR = Path('/var/lib/ai-bridge/approvals')
PENDING_TTL = 10 * 60          # time to approve a request
ONCE_TTL = 10 * 60             # time to repeat the call after approval
TASK_TTL = 15 * 60             # task window
TASK_USES = 10                 # privileged calls per task window
ID = re.compile(r'[0-9A-F]{10}')
HIGH_RISK = re.compile(r'\brm\s+-[a-zA-Z]*[rf]|\bmkfs|\bdd\s|\bshutdown\b|\breboot\b|\bpoweroff\b|\bwipefs\b'
                       r'|\bfdisk\b|\bparted\b|\bchmod\s+-R|\bchown\s+-R|\buserdel\b|\biptables\b|\bnft\b|\bufw\b'
                       r'|/etc/(sudoers|ssh|passwd|shadow)|\bsystemctl\s+(stop|disable|mask)')


def machine_id() -> str:
    try:
        return Path('/etc/machine-id').read_text().strip() or socket.gethostname()
    except OSError:
        return socket.gethostname()


def fingerprint(machine: str, tool: str, args: dict) -> str:
    """Exact identity of an operation; any material change needs a new approval."""
    text = json.dumps({'machine': machine, 'tool': tool, 'args': args}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()


def operation_args(tool: str, args: dict) -> dict:
    """The arguments that define what runs; content is bound by hash."""
    if tool == 'run_command':
        return {'command': args['command'], 'cwd': args['cwd'], 'timeout': args['timeout']}
    if tool == 'start_job':
        return {'command': args['command'], 'cwd': args['cwd']}
    if tool == 'read_file':
        return {'path': args['path']}
    if tool == 'write_file':
        bound = {k: args.get(k) for k in ('path', 'append', 'make_dirs', 'atomic', 'expected_sha256', 'backup', 'mode')}
        return dict(bound, content_sha256=hashlib.sha256(args['content'].encode()).hexdigest())
    if tool == 'ssh_run':
        return {'host': args['host'], 'command': args['command']}
    raise ValueError(f'{tool} is not a privileged tool.')


def describe(tool: str, args: dict) -> tuple[str, str]:
    """(one-line operation, risk note) for the assistant and the approver."""
    if tool in ('run_command', 'start_job'):
        operation = f"{args['command']}  (as root, in {args['cwd']})"
        text = args['command']
    elif tool == 'read_file':
        return f"read {args['path']} as root", 'Reads a file as root; contents go to the assistant.'
    elif tool == 'write_file':
        verb = 'append to' if args.get('append') else 'write'
        size = len(args['content'].encode())
        operation, text = f"{verb} {args['path']} as root ({size} bytes)", args['path']
    else:
        operation, text = f"{args['command']}  (remote sudo on {args['host']})", args['command']
    risk = 'high: destructive or security-sensitive' if HIGH_RISK.search(text) else 'normal'
    return operation, f'Runs with root privileges. Risk: {risk}.'


def visible(text) -> str:
    """Escape terminal control characters so a request cannot hide part of itself on screen."""
    return ''.join(c if c in '\n\t' or (ord(c) >= 32 and ord(c) != 127 and not 0x80 <= ord(c) < 0xa0)
                   else f'\\x{ord(c):02x}' for c in str(text))


def _now() -> float:
    return time.time()


def _stamp(ts: float) -> str:
    return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts))


def _write_new(path: Path, data: dict) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(data, stream, ensure_ascii=False)


class Approvals:
    """Bridge side: create requests, read root-owned decisions, consume them exactly once."""

    def __init__(self, state_dir: Path, decision_dir: Path = DECISION_DIR, owner: int = 0,
                 machine: str | None = None, log=None):
        self.base = Path(state_dir) / 'approvals'
        self.requests, self.used = self.base / 'requests', self.base / 'used'
        self.decisions, self.owner = Path(decision_dir), owner
        self.machine = machine or machine_id()
        self.log = log or (lambda event, **fields: None)

    def _dirs(self):
        for path in (self.base, self.requests, self.used):
            path.mkdir(mode=0o700, parents=True, exist_ok=True)

    @contextlib.contextmanager
    def _lock(self):
        self._dirs()
        fd = os.open(self.base / '.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    # Decisions -------------------------------------------------------------

    def _trusted(self, st) -> bool:
        return st.st_uid == self.owner and not st.st_mode & 0o022

    def _root_file(self, name: str) -> dict | None:
        """A decision file only counts if the folder and file are owned by root and not writable by others."""
        try:
            folder = os.lstat(self.decisions)
            if not stat.S_ISDIR(folder.st_mode) or not self._trusted(folder):
                return None
            fd = os.open(self.decisions / name, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:
            return None
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or not self._trusted(st) or st.st_size > 65536:
                return None
            with os.fdopen(fd, 'r') as stream:
                fd = None
                return json.load(stream)
        except (OSError, ValueError):
            return None
        finally:
            if fd is not None:
                os.close(fd)

    def decision(self, request: dict) -> dict | None:
        """The valid decision for this exact request, or None. Malformed records never authorize."""
        data = self._root_file(request['id'] + '.json')
        if not isinstance(data, dict) or data.get('id') != request['id'] \
                or data.get('fingerprint') != request['fingerprint'] or data.get('machine') != self.machine:
            return None
        if data.get('decision') == 'rejected':
            return data
        try:
            decided, expires, uses = float(data['decided_at']), float(data['expires_at']), int(data['max_uses'])
        except (KeyError, TypeError, ValueError):
            return None
        limit = TASK_TTL if data.get('scope') == 'task' else ONCE_TTL
        if data.get('decision') != 'approved' or data.get('scope') not in ('once', 'task') \
                or decided > request['expires_at'] or expires - decided > limit or not 1 <= uses <= TASK_USES \
                or (data['scope'] == 'once' and uses != 1):
            return None
        data['revoked'] = self._root_file(request['id'] + '.revoked') is not None
        return data

    def _uses(self, decision: dict) -> int:
        return sum(1 for n in range(int(decision['max_uses'])) if (self.used / f"{decision['id']}.{n}").exists())

    def _usable(self, decision: dict | None) -> bool:
        return bool(decision and decision.get('decision') == 'approved' and not decision.get('revoked')
                    and decision['expires_at'] > _now() and self._uses(decision) < int(decision['max_uses']))

    def _consume(self, decision: dict) -> bool:
        for n in range(int(decision['max_uses'])):
            try:
                os.close(os.open(self.used / f"{decision['id']}.{n}", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
                return True
            except FileExistsError:
                continue
        return False

    # Requests --------------------------------------------------------------

    def _load(self, path: Path) -> dict | None:
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) and ID.fullmatch(str(data.get('id', ''))) else None

    def all_requests(self) -> list[dict]:
        self._dirs()
        rows = [r for r in (self._load(p) for p in self.requests.glob('*.json')) if r]
        return sorted(rows, key=lambda r: r.get('created', 0), reverse=True)

    def request(self, request_id: str) -> dict:
        if not ID.fullmatch(request_id or ''):
            raise KeyError(f'Invalid request ID: {request_id}')
        data = self._load(self.requests / f'{request_id}.json')
        if not data:
            raise KeyError(f'No approval request {request_id}')
        return data

    def _create(self, tool, args, fp, via=None) -> dict:
        operation, risk = describe(tool, args)
        now = _now()
        for _ in range(5):
            request = {'id': secrets.token_hex(5).upper(), 'host': socket.gethostname(), 'machine': self.machine,
                       'tool': tool, 'args': args, 'fingerprint': fp, 'operation': operation, 'risk': risk,
                       'created': now, 'expires_at': now + PENDING_TTL}
            if via:
                request['approved_via'] = via
            try:
                _write_new(self.requests / f"{request['id']}.json", request)
                return request
            except FileExistsError:
                continue
        raise OSError('Could not allocate an approval request ID.')

    def _active_task(self) -> dict | None:
        for request in self.all_requests():
            if request['created'] < _now() - PENDING_TTL - TASK_TTL:
                break
            decision = self.decision(request)
            if decision and decision.get('scope') == 'task' and self._usable(decision):
                return decision
        return None

    def gate(self, tool: str, args: dict) -> dict | None:
        """None when this exact call may run now (authorization consumed); otherwise the response."""
        bound = operation_args(tool, args)
        fp = fingerprint(self.machine, tool, bound)
        args = bound | ({'content': args['content']} if tool == 'write_file' else {})
        with self._lock():
            pending = None
            for request in self.all_requests():
                if request.get('fingerprint') != fp or request.get('approved_via'):
                    continue
                decision = self.decision(request)
                if self._usable(decision) and self._consume(decision):
                    self.log('approval_used', request_id=request['id'], scope=decision['scope'],
                             approver=decision.get('approver'))
                    return None
                if decision and decision.get('decision') == 'rejected' and request['expires_at'] > _now():
                    return self._view(request, 'rejected')
                if decision is None and request['expires_at'] > _now():
                    pending = pending or request
            if pending:
                return self._view(pending, 'approval_required')
            task = self._active_task()
            if task and self._consume(task):
                request = self._create(tool, args, fp, via=task['id'])
                self.log('approval_used', request_id=request['id'], scope='task', task=task['id'],
                         approver=task.get('approver'))
                return None
            request = self._create(tool, args, fp)
            self.log('approval_requested', request_id=request['id'], requested_tool=tool)
            return self._view(request, 'approval_required')

    def _view(self, request: dict, status: str) -> dict:
        approval = {'request_id': request['id'], 'host': request['host'], 'tool': request['tool'],
                    'operation': request['operation'], 'risk': request['risk'],
                    'expires_at': _stamp(request['expires_at']),
                    'approve_on_host': f"{approver_command()} approve {request['id']}"}
        if status == 'rejected':
            return {'status': 'rejected', 'approval': approval, 'automatic_retry': False,
                    'next': 'The user rejected this operation. Do not retry it; ask the user how to proceed.'}
        return {'status': 'approval_required', 'approval': approval, 'automatic_retry': False,
                'next': ('Nothing ran. Show the user the request_id, host and operation and ask them to approve '
                         'it on the host. After they confirm, repeat this exact call. Never approve it yourself.')}

    # Approver side -----------------------------------------------------------

    def state(self, request: dict) -> str:
        if request.get('approved_via'):
            return 'approved (task)'
        decision = self.decision(request)
        if decision is None:
            return 'pending' if request['expires_at'] > _now() else 'expired'
        if decision['decision'] == 'rejected':
            return 'rejected'
        if decision.get('revoked'):
            return 'revoked'
        used = self._uses(decision)
        if used >= int(decision['max_uses']):
            return 'used'
        if decision['expires_at'] <= _now():
            return 'expired'
        return f"approved ({decision['scope']}, {used}/{decision['max_uses']} used)"

    def summary(self, request: dict) -> dict:
        """What the approver sees, rebuilt from the bound arguments, never the stored description."""
        decision = None if request.get('approved_via') else self.decision(request)
        try:
            operation, risk = describe(request['tool'], request['args'])
        except (KeyError, TypeError, ValueError):
            operation, risk = 'unreadable request', 'Refuse it.'
        return {'id': request['id'], 'state': self.state(request), 'host': socket.gethostname(),
                'tool': request['tool'], 'operation': visible(operation), 'risk': risk,
                'created': _stamp(request['created']),
                'expires_at': _stamp(request['expires_at']), 'approved_via': request.get('approved_via'),
                'approver': decision.get('approver') if decision else None}

    def decide(self, request_id: str, decision: str, scope: str = 'once', writer=None) -> dict:
        request = self.request(request_id)
        if request.get('approved_via') or request['machine'] != self.machine:
            raise ValueError('This request cannot be decided here.')
        if request['expires_at'] <= _now():
            raise ValueError('This request expired; ask the assistant to make the call again.')
        if self.decision(request) is not None:
            raise ValueError(f'Request {request_id} is already decided.')
        try:
            matches = fingerprint(self.machine, request['tool'],
                                  operation_args(request['tool'], request['args'])) == request['fingerprint']
        except (KeyError, TypeError, ValueError):
            matches = False
        if not matches:
            raise ValueError('Request record does not match its fingerprint; refusing to decide it.')
        record = {'id': request_id, 'fingerprint': request['fingerprint'], 'machine': self.machine,
                  'decision': decision, 'scope': scope,
                  'ttl': TASK_TTL if scope == 'task' else ONCE_TTL,
                  'max_uses': TASK_USES if scope == 'task' else 1}
        (writer or sudo_writer)(self.decisions, request_id + '.json', record)
        return self.summary(request)

    def revoke(self, request_id: str, writer=None) -> dict:
        request = self.request(request_id)
        decision = self.decision(request)
        if not decision or decision['decision'] != 'approved':
            raise ValueError(f'Request {request_id} has no approval to revoke.')
        (writer or sudo_writer)(self.decisions, request_id + '.revoked', {'id': request_id})
        return self.summary(request)


# Runs as root through sudo. Root sets the approver and the times, never the caller.
ROOT_WRITER = r'''
import json, os, re, stat, sys, time
folder, name = sys.argv[1], sys.argv[2]
os.umask(0o022)
if not re.fullmatch(r"[0-9A-F]{10}\.(json|revoked)", name) or not os.path.isabs(folder):
    sys.exit("invalid decision name")
os.makedirs(folder, mode=0o755, exist_ok=True)
st = os.lstat(folder)
if not stat.S_ISDIR(st.st_mode) or st.st_uid != 0 or st.st_mode & 0o022:
    sys.exit("decision folder must be a root-owned directory not writable by others")
data = json.loads(sys.stdin.read(65536))
data["approver"] = os.environ.get("SUDO_USER") or "root"
data["decided_at"] = time.time()
if data.get("decision") == "approved":
    data["expires_at"] = data["decided_at"] + min(int(data.pop("ttl")), 900)
data.pop("ttl", None)
fd = os.open(os.path.join(folder, name), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
with os.fdopen(fd, "w") as stream:
    json.dump(data, stream)
'''


def sudo_writer(folder: Path, name: str, record: dict) -> None:
    """Write one decision as root; sudo asks for the user's password on the terminal every time."""
    python = '/usr/bin/python3' if os.path.exists('/usr/bin/python3') else sys.executable
    result = subprocess.run(['sudo', '-k', '-p', 'Password to confirm (sudo): ', '--',
                             python, '-I', '-c', ROOT_WRITER, str(folder), name],
                            input=json.dumps(record), text=True)
    if result.returncode != 0:
        raise PermissionError('Decision was not recorded (sudo authentication failed or was cancelled).')


def approver_command() -> str:
    """This file only needs the standard library, so system Python runs it straight from the release."""
    current = Path.home() / '.local/share/ai-bridge/current/app/ai_bridge/approvals.py'
    script = '~/.local/share/ai-bridge/current/app/ai_bridge/approvals.py' if current.exists() else __file__
    return f'python3 -I {script}'


# CLI -------------------------------------------------------------------------

def _print_rows(rows: list[dict]) -> None:
    if not rows:
        print('Nothing to show.')
    for row in rows:
        via = f" via task {row['approved_via']}" if row['approved_via'] else ''
        print(f"{row['id']}  {row['state']:<26} {row['created']}  {row['tool']}{via}\n    {row['operation']}")


def _show(store: Approvals, request: dict, full: bool) -> None:
    row = store.summary(request)
    print(f"Request   {row['id']}  ({row['state']})\nHost      {row['host']}\nTool      {row['tool']}\n"
          f"Operation {row['operation']}\n{row['risk']}\nExpires   {row['expires_at']}")
    for key, value in request['args'].items():
        if key == 'content':
            lines = value.splitlines()
            shown = lines if full else lines[:40]
            print(f"content   ({len(value.encode())} bytes, sha256 {hashlib.sha256(value.encode()).hexdigest()[:16]}…)")
            print('\n'.join('  | ' + visible(line) for line in shown))
            if len(shown) < len(lines):
                print(f'  … {len(lines) - len(shown)} more lines (use show --full)')
        else:
            print(f'{key:<9} {visible(value)}')


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog='ai_bridge.approvals', description='Approve privileged AI Bridge requests.')
    parser.add_argument('--state-dir', default=os.environ.get('AI_BRIDGE_STATE_DIR') or str(Path.home() / '.local/state/ai-bridge'))
    parser.add_argument('--json', action='store_true', help='machine-readable output (list, history, show)')
    sub = parser.add_subparsers(dest='command')
    sub.add_parser('list', help='pending requests (default)')
    history = sub.add_parser('history', help='recent requests and decisions')
    history.add_argument('--limit', type=int, default=30)
    show = sub.add_parser('show', help='full details of one request')
    show.add_argument('id')
    show.add_argument('--full', action='store_true')
    approve = sub.add_parser('approve', help='approve one request (sudo password required)')
    approve.add_argument('id')
    approve.add_argument('--task', action='store_true', help=f'also allow up to {TASK_USES} privileged calls for {TASK_TTL // 60} min')
    approve.add_argument('--yes', action='store_true', help='skip the y/N question (sudo still asks)')
    for name in ('reject', 'revoke'):
        sub.add_parser(name, help=f'{name} one request').add_argument('id')
    args = parser.parse_args(argv)
    store = Approvals(Path(args.state_dir))
    command = args.command or 'list'
    if command in ('list', 'history'):
        rows = [store.summary(r) for r in store.all_requests()]
        rows = [r for r in rows if r['state'] == 'pending'] if command == 'list' else rows[:args.limit]
        print(json.dumps(rows, indent=2)) if args.json else _print_rows(rows)
        return 0
    try:
        request = store.request(args.id.upper())
        if command == 'show':
            print(json.dumps(dict(request, state=store.state(request)), indent=2)) if args.json else _show(store, request, args.full)
            return 0
        if os.environ.get('AI_BRIDGE_CHILD') or not (sys.stdin.isatty() and sys.stdout.isatty()):
            print('Decisions need a real terminal on the host; they cannot be made through the bridge.', file=sys.stderr)
            return 2
        if command == 'approve':
            _show(store, request, False)
            if not args.yes and input(f"\nApprove{' for task' if args.task else ' once'}? [y/N] ").strip().lower() != 'y':
                print('Not approved.')
                return 1
            row = store.decide(request['id'], 'approved', 'task' if args.task else 'once')
        elif command == 'reject':
            row = store.decide(request['id'], 'rejected')
        else:
            row = store.revoke(request['id'])
    except (KeyError, ValueError, PermissionError) as error:
        print(str(error).strip("'\""), file=sys.stderr)
        return 1
    print(f"{row['id']}: {store.state(store.request(row['id']))}")
    return 0


if __name__ == '__main__':
    sys.exit(main())

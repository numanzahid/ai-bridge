"""Run one interactive (non-exec) agent turn as a bridge job; standalone, stdlib only.

The turn lives in a normal native chat: a Claude background session (`claude --bg`) or a thread on
the shared Codex app-server daemon. Both appear in the CLI's own resume lists and can carry Remote
Control. Progress is printed as the JSONL events the bridge's exec parsers already read, plus
`bridge.native` records describing the native session.
"""
import base64
import glob
import json
import os
from pathlib import Path
import re
import signal
import socket
import struct
import subprocess
import sys
import time

POLL = 2.0
ANSI = re.compile(r'\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07')


def emit(event):
    print(json.dumps(event, ensure_ascii=False), flush=True)


def native(**fields):
    emit({'type': 'bridge.native', **fields})


class Stop(Exception):
    pass


def on_term(signum, frame):
    raise Stop()


# ---------------------------------------------------------------------------
# Claude: background session
# ---------------------------------------------------------------------------

CLAUDE_PERMISSION = {'full': ['--dangerously-skip-permissions'], 'edit': ['--permission-mode', 'acceptEdits'],
                     'readonly': ['--permission-mode', 'plan']}


def claude_agents(binary):
    result = subprocess.run([binary, 'agents', '--json', '--all'], capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError('claude agents failed: ' + result.stderr.strip()[:300])
    return json.loads(result.stdout or '[]')


def claude_transcript(session_id):
    root = Path(os.environ.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude') / 'projects'
    found = glob.glob(str(root / '*' / (session_id + '.jsonl')))
    return Path(found[0]) if found else None


def claude_remote_url(binary, short_id):
    result = subprocess.run([binary, 'logs', short_id], capture_output=True, text=True, timeout=30)
    match = re.findall(r'https://claude\.ai/code/session_[A-Za-z0-9]+', ANSI.sub(' ', result.stdout))
    return match[-1] if match else None


def release_claude_session(binary, session_id):
    """Resuming a live background session would fork a copy; stop an idle one first (history is kept)."""
    live = [r for r in claude_agents(binary) if r.get('sessionId') == session_id and r.get('pid')]
    for row in live:
        if row.get('status') == 'busy' or row.get('state') == 'working':
            raise RuntimeError(f"Session is active in Claude ({row.get('kind')} {row.get('id')}); not interrupting it. "
                               'Wait for it to finish or continue it there.')
        if row.get('kind') != 'background':
            raise RuntimeError(f"Session is open in an interactive terminal ({row.get('id')}); continue it there.")
        subprocess.run([binary, 'stop', row['id']], capture_output=True, timeout=60)
    deadline = time.time() + 30
    while live and time.time() < deadline:
        if not [r for r in claude_agents(binary) if r.get('sessionId') == session_id and r.get('pid')]:
            # The service releases the session shortly after the process exits; resuming
            # earlier starts a copy under a new ID.
            time.sleep(3)
            return
        time.sleep(1)
    if live:
        raise RuntimeError('Timed out waiting for the previous background process to stop.')


def run_claude(spec):
    binary, cwd = spec['binary'], os.getcwd()
    known = False
    if spec.get('session_id'):
        release_claude_session(binary, spec['session_id'])
        known = any(r.get('sessionId') == spec['session_id'] and r.get('kind') == 'background'
                    for r in claude_agents(binary))
    argv = [binary, '--bg']
    if spec.get('session_id'):
        argv += ['--resume', spec['session_id']]
    if known:
        # Claude wakes a known background session with its saved options (name, Remote Control,
        # permission, model). Passing options again starts a copy under a new session ID.
        options = []
    else:
        options = (['--name', spec['name']] if spec.get('name') else []) + \
                  (['--remote-control'] + ([spec['name']] if spec.get('name') else []) if spec.get('remote') else []) + \
                  CLAUDE_PERMISSION[spec['permission']] + (['--model', spec['model']] if spec.get('model') else []) + \
                  spec.get('extra', [])
    started = time.time() * 1000
    # The caller guards prompts that start with '-' (agents._safe_prompt).
    result = subprocess.run(argv + options + [spec['prompt']],
                            capture_output=True, text=True, timeout=120)
    output = ANSI.sub('', result.stdout + result.stderr)
    if result.returncode:
        message = output.strip()[-2000:]
        if 'Workspace not trusted' in output:
            message += ('\nTrust this folder once by running `claude` there (trusting a parent such as '
                        '~/.ai-bridge-tasks covers all task workspaces), or use mode=exec.')
        emit({'type': 'result', 'subtype': 'error', 'is_error': True, 'result': message})
        return 1
    notes = [line.strip() for line in output.splitlines() if line.strip().lower().startswith('note:')]
    if notes:
        native(cli_note=' '.join(notes)[:500])
    match = re.search(r'backgrounded\s*·\s*([0-9a-f]{6,})', output)
    short_id, row = (match.group(1) if match else None), None
    deadline = time.time() + 60
    while row is None and time.time() < deadline:
        rows = [r for r in claude_agents(binary) if r.get('kind') == 'background' and r.get('cwd') == cwd]
        if short_id:
            row = next((r for r in rows if r.get('id') == short_id), None)
        else:
            rows = [r for r in rows if r.get('startedAt', 0) >= started - 5000]
            row = max(rows, key=lambda r: r['startedAt']) if rows else None
        if row is None:
            time.sleep(1)
    if row is None or not row.get('sessionId'):
        emit({'type': 'result', 'subtype': 'error', 'is_error': True,
              'result': 'Background session started but could not be identified: ' + output.strip()[-500:]})
        return 1
    short_id, session_id = row['id'], row['sessionId']
    emit({'type': 'system', 'subtype': 'init', 'session_id': session_id})
    native(native_id=short_id, mode='interactive', attach=f'claude attach {short_id}',
           resume=f'claude --resume {session_id}')
    seen, final, reported_remote = 0, None, False
    try:
        while True:
            transcript = claude_transcript(session_id)
            if transcript:
                lines = transcript.read_text(errors='replace').splitlines()
                for line in lines[seen:]:
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if record.get('type') == 'assistant' and not record.get('isSidechain'):
                        emit({'type': 'assistant', 'message': record.get('message') or {}, 'session_id': session_id})
                        content = (record.get('message') or {}).get('content') or []
                        texts = [b.get('text') for b in content if isinstance(b, dict) and b.get('type') == 'text' and b.get('text')]
                        final = texts[-1] if texts else final
                seen = len(lines)
            if spec.get('remote') and not reported_remote:
                url = claude_remote_url(binary, short_id)
                if url:
                    native(remote_control='active', remote_url=url)
                    reported_remote = True
            row = next((r for r in claude_agents(binary) if r.get('id') == short_id), row)
            state = row.get('state')
            if state in ('done', 'failed', 'stopped', 'blocked') and row.get('status') != 'busy':
                break
            time.sleep(POLL)
    except Stop:
        # The runner SIGKILLs this process group shortly after SIGTERM; detach the stop request.
        subprocess.Popen([binary, 'stop', short_id], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        emit({'type': 'result', 'subtype': 'interrupted', 'is_error': True, 'result': final,
              'session_id': session_id})
        return 143
    if spec.get('remote') and not reported_remote:
        native(remote_control='unconfirmed', note='No Remote Control URL seen; check `claude logs ' + short_id + '`.')
    if state == 'blocked':
        native(waiting_for=row.get('waitingFor') or 'input')
    emit({'type': 'result', 'subtype': 'success' if state in ('done', 'blocked') else 'error',
          'is_error': state not in ('done', 'blocked'), 'result': final, 'session_id': session_id})
    return 0 if state in ('done', 'blocked') else 1


# ---------------------------------------------------------------------------
# Codex: app-server daemon thread over WebSocket on its control socket
# ---------------------------------------------------------------------------

CODEX_SANDBOX = {'full': 'danger-full-access', 'edit': 'workspace-write', 'readonly': 'read-only'}


class Daemon:
    """Minimal JSON-RPC client for the Codex app-server control socket (WebSocket over UDS)."""

    def __init__(self, path, timeout=None):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(30)
        self.sock.connect(path)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(('GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
                           f'Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n').encode())
        head = b''
        while b'\r\n\r\n' not in head:
            chunk = self.sock.recv(1)
            if not chunk:
                raise OSError('Codex daemon closed the connection during upgrade.')
            head += chunk
        if b' 101 ' not in head.split(b'\r\n', 1)[0]:
            raise OSError('Codex daemon refused the WebSocket upgrade.')
        self.sock.settimeout(timeout)
        self.buffer, self.next_id, self.notes = b'', 0, []

    def _read(self, size):
        while len(self.buffer) < size:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise OSError('Codex daemon connection closed.')
            self.buffer += chunk
        data, self.buffer = self.buffer[:size], self.buffer[size:]
        return data

    def _frame(self, opcode, data):
        mask, size = os.urandom(4), len(data)
        if size < 126:
            head = bytes([0x80 | opcode, 0x80 | size])
        elif size < 65536:
            head = bytes([0x80 | opcode, 0x80 | 126]) + struct.pack('>H', size)
        else:
            head = bytes([0x80 | opcode, 0x80 | 127]) + struct.pack('>Q', size)
        self.sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def send(self, message):
        self._frame(0x1, json.dumps(message).encode())

    def recv(self):
        message = b''
        while True:
            first, second = self._read(2)
            size = second & 0x7f
            if size == 126:
                size = struct.unpack('>H', self._read(2))[0]
            elif size == 127:
                size = struct.unpack('>Q', self._read(8))[0]
            payload, opcode = self._read(size), first & 0x0f
            if opcode == 0x9:
                self._frame(0xA, payload)
                continue
            if opcode == 0x8:
                raise OSError('Codex daemon closed the connection.')
            if opcode == 0xA:
                continue
            message += payload
            if first & 0x80:
                return json.loads(message)

    def call(self, method, params):
        self.next_id += 1
        request_id = self.next_id
        self.send({'jsonrpc': '2.0', 'id': request_id, 'method': method, 'params': params})
        while True:
            message = self.recv()
            if message.get('id') == request_id and 'method' not in message:
                if 'error' in message:
                    raise RuntimeError(f"{method} failed: {message['error'].get('message', message['error'])}")
                return message.get('result') or {}
            self.handle(message)

    def handle(self, message):
        if 'method' in message and 'id' in message:
            # Server requests (approvals, user input) cannot be answered here; never hang on them.
            self.send({'jsonrpc': '2.0', 'id': message['id'],
                       'error': {'code': -32601, 'message': 'ai-bridge cannot answer interactive requests'}})
            emit({'type': 'error', 'message': f"Codex requested {message['method']}; declined by ai-bridge."})
        else:
            self.notes.append(message)

    def initialize(self):
        self.call('initialize', {'clientInfo': {'name': 'ai-bridge', 'version': '2'}, 'capabilities': {'experimentalApi': True}})
        self.send({'jsonrpc': '2.0', 'method': 'initialized'})

    def remote_status(self, wait=3.0):
        deadline = time.time() + wait
        while time.time() < deadline:
            for note in self.notes:
                if note.get('method') == 'remoteControl/status/changed':
                    return note['params']
            self.sock.settimeout(max(0.1, deadline - time.time()))
            try:
                self.handle(self.recv())
            except (socket.timeout, TimeoutError):
                break
        self.sock.settimeout(None)
        return None


def codex_socket(binary):
    result = subprocess.run([binary, 'app-server', 'daemon', 'start'], capture_output=True, text=True, timeout=120)
    path = None
    for line in reversed(result.stdout.splitlines()):
        try:
            path = json.loads(line).get('socketPath') or path
        except ValueError:
            continue
        if path:
            break
    codex_home = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')
    return path or str(codex_home / 'app-server-control' / 'app-server-control.sock')


def codex_event(note):
    """Translate daemon notifications into `codex exec --json` events."""
    method, params = note.get('method'), note.get('params') or {}
    item = params.get('item') or {}
    kind = item.get('type')
    if method == 'item/completed' and kind == 'agentMessage':
        return {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': item.get('text') or ''}}
    if method == 'item/started' and kind == 'commandExecution':
        return {'type': 'item.started', 'item': {'type': 'command_execution', 'command': item.get('command')}}
    if method == 'item/completed' and kind == 'fileChange':
        return {'type': 'item.completed', 'item': {'type': 'file_change', 'changes': item.get('changes') or []}}
    if method == 'item/started' and kind == 'mcpToolCall':
        return {'type': 'item.started', 'item': {'type': 'mcp_tool_call', 'tool': item.get('tool')}}
    if method == 'item/started' and kind == 'webSearch':
        return {'type': 'item.started', 'item': {'type': 'web_search', 'query': item.get('query')}}
    if method == 'error' and not params.get('willRetry'):
        return {'type': 'error', 'message': (params.get('error') or {}).get('message') or 'Codex error'}
    return None


def run_codex(spec):
    binary, cwd = spec['binary'], os.getcwd()
    path = codex_socket(binary)
    daemon = Daemon(path)
    daemon.initialize()
    if spec.get('remote'):
        status = daemon.remote_status()
        if not status or status.get('status') == 'disabled':
            # Remote Control is daemon-wide; enabling it restarts the daemon process.
            subprocess.run([binary, 'app-server', 'daemon', 'enable-remote-control'],
                           capture_output=True, text=True, timeout=120, check=True)
            native(remote_control_enabled_now=True,
                   note='Codex Remote Control was off; enabled for the shared daemon (it restarted).')
            time.sleep(2)
            daemon = Daemon(path)
            daemon.initialize()
            status = daemon.remote_status(wait=10)
        native(remote_control=(status or {}).get('status', 'unknown'),
               remote_server=(status or {}).get('serverName'), remote_environment=(status or {}).get('environmentId'))
    options = {'cwd': cwd, 'approvalPolicy': 'never', 'sandbox': CODEX_SANDBOX[spec['permission']]}
    if spec.get('model'):
        options['model'] = spec['model']
    if spec.get('session_id'):
        thread = daemon.call('thread/resume', dict(options, threadId=spec['session_id']))['thread']
    else:
        thread = daemon.call('thread/start', options)['thread']
    thread_id = thread['id']
    emit({'type': 'thread.started', 'thread_id': thread_id})
    if spec.get('name'):
        daemon.call('thread/name/set', {'threadId': thread_id, 'name': spec['name']})
    native(native_id=thread_id, mode='interactive', resume=f'codex resume {thread_id}')
    turn = daemon.call('turn/start', {'threadId': thread_id, 'input': [{'type': 'text', 'text': spec['prompt']}]})['turn']
    try:
        while True:
            note = daemon.notes.pop(0) if daemon.notes else daemon.recv()
            if 'id' in note and 'method' in note:
                daemon.handle(note)
                continue
            params = note.get('params') or {}
            if params.get('threadId') not in (None, thread_id):
                continue
            event = codex_event(note)
            if event:
                emit(event)
            if note.get('method') == 'turn/completed':
                done = params.get('turn') or {}
                status = done.get('status')
                if status == 'completed':
                    emit({'type': 'turn.completed', 'usage': None})
                    return 0
                error = (done.get('error') or {}).get('message') or f'Turn {status}.'
                emit({'type': 'turn.failed', 'error': {'message': error}})
                return 1
    except Stop:
        try:
            daemon.call('turn/interrupt', {'threadId': thread_id, 'turnId': turn['id']})
        except (OSError, RuntimeError):
            pass
        emit({'type': 'turn.failed', 'error': {'message': 'Interrupted by ai-bridge.'}})
        return 143


def main():
    signal.signal(signal.SIGTERM, on_term)
    spec = json.loads(sys.argv[1])
    try:
        return {'claude': run_claude, 'codex': run_codex}[spec['agent']](spec)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        emit({'type': 'error', 'message': f'{type(error).__name__}: {error}'})
        if spec['agent'] == 'claude':
            emit({'type': 'result', 'subtype': 'error', 'is_error': True, 'result': str(error)})
        return 1


if __name__ == '__main__':
    sys.exit(main())

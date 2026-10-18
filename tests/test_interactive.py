"""Interactive agent turns against fake CLIs and a fake Codex daemon; no real agent turns."""
import base64
import hashlib
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from ai_bridge import agents
from ai_bridge.core import Config, Jobs
from ai_bridge.management import start_agent

SID = '11111111-2222-4333-8444-555555555555'
HELPER = Path(agents.__file__).with_name('interactive.py')

FAKE_CLAUDE = r'''
import json, os, pathlib, sys
root = pathlib.Path(os.environ['FAKE_ROOT']); args = sys.argv[1:]
with (root / 'calls').open('a') as log: log.write(json.dumps(args) + '\n')
if args[:1] == ['--bg']:
    project = pathlib.Path(os.environ['CLAUDE_CONFIG_DIR']) / 'projects' / 'fixture'
    project.mkdir(parents=True, exist_ok=True)
    with (project / (os.environ['FAKE_SID'] + '.jsonl')).open('w') as f:
        f.write(json.dumps({'type': 'user', 'entrypoint': 'cli', 'message': {'content': args[-1]}}) + '\n')
        f.write(json.dumps({'type': 'assistant', 'isSidechain': True, 'message': {'content': [{'type': 'text', 'text': 'child'}]}}) + '\n')
        f.write(json.dumps({'type': 'assistant', 'message': {'model': 'fake-model', 'content': [{'type': 'text', 'text': 'fixture answer'}]}}) + '\n')
    print('backgrounded · abcd1234 · fixture')
elif args[:1] == ['agents']:
    print(json.dumps([{'id': 'abcd1234', 'kind': 'background', 'cwd': os.getcwd(), 'sessionId': os.environ['FAKE_SID'],
                       'state': os.environ.get('FAKE_STATE', 'done'), 'status': os.environ.get('FAKE_STATUS', 'idle'),
                       'startedAt': 1, 'waitingFor': os.environ.get('FAKE_WAITING'),
                       **({'pid': 4242} if os.environ.get('FAKE_LIVE') else {})}]))
elif args[:1] == ['logs']:
    print('\x1b[32m/rc active\x1b[0m Continue at https://claude.ai/code/session_FAKE123')
'''


class FakeDaemon(threading.Thread):
    """A Codex app-server stand-in: WebSocket over a Unix socket, scripted JSON-RPC replies."""

    def __init__(self, path, remote='disabled'):
        super().__init__(daemon=True)
        self.server = socket.socket(socket.AF_UNIX)
        self.server.bind(path)
        self.server.listen(4)
        self.remote, self.requests = remote, []

    def frame(self, conn, message):
        data = json.dumps(message).encode()
        head = bytes([0x81, len(data)]) if len(data) < 126 else bytes([0x81, 126]) + struct.pack('>H', len(data))
        conn.sendall(head + data)

    def read(self, conn):
        first, second = conn.recv(1)[0], conn.recv(1)[0]
        size = second & 0x7f
        if size == 126:
            size = struct.unpack('>H', conn.recv(2))[0]
        mask, data = conn.recv(4), b''
        while len(data) < size:
            data += conn.recv(size - len(data))
        return json.loads(bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def run(self):
        while True:
            conn, _ = self.server.accept()
            threading.Thread(target=self.serve, args=(conn,), daemon=True).start()

    def serve(self, conn):
        request = b''
        while b'\r\n\r\n' not in request:
            request += conn.recv(1)
        key = [l.split(b': ')[1] for l in request.split(b'\r\n') if l.lower().startswith(b'sec-websocket-key')][0]
        accept = base64.b64encode(hashlib.sha1(key + b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest())
        conn.sendall(b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
                     b'Sec-WebSocket-Accept: ' + accept + b'\r\n\r\n')
        try:
            while True:
                message = self.read(conn)
                self.requests.append(message)
                method, rid = message.get('method'), message.get('id')
                if method == 'initialize':
                    self.frame(conn, {'id': rid, 'result': {}})
                    self.frame(conn, {'method': 'remoteControl/status/changed',
                                      'params': {'status': self.remote, 'serverName': 'fixture'}})
                elif method in ('thread/start', 'thread/resume'):
                    tid = message['params'].get('threadId', SID)
                    self.frame(conn, {'id': rid, 'result': {'thread': {'id': tid}}})
                elif method == 'thread/name/set':
                    self.frame(conn, {'id': rid, 'result': {}})
                elif method == 'turn/start':
                    self.frame(conn, {'id': rid, 'result': {'turn': {'id': 'turn-1'}}})
                    tid = message['params']['threadId']
                    self.frame(conn, {'method': 'item/started', 'params': {'threadId': tid, 'item': {'type': 'commandExecution', 'command': 'ls'}}})
                    self.frame(conn, {'method': 'item/completed', 'params': {'threadId': tid, 'item': {'type': 'agentMessage', 'text': 'daemon answer'}}})
                    self.frame(conn, {'method': 'turn/completed', 'params': {'threadId': tid, 'turn': {'id': 'turn-1', 'status': 'completed'}}})
        except (OSError, IndexError):
            conn.close()


class InteractiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='aib-int-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()

    def fake(self, name, body):
        path = self.bin / name
        path.write_text('#!' + sys.executable + '\n' + body)
        path.chmod(0o700)
        return str(path)

    def run_helper(self, spec, env):
        result = subprocess.run([sys.executable, str(HELPER), json.dumps(spec)], cwd=self.root, env=dict(os.environ, **env),
                                capture_output=True, text=True, timeout=30)
        return result, agents.parse_output(spec['agent'], result.stdout)

    def test_claude_background_session_with_remote_control(self):
        binary = self.fake('claude', FAKE_CLAUDE)
        env = {'FAKE_ROOT': str(self.root), 'FAKE_SID': SID, 'CLAUDE_CONFIG_DIR': str(self.root / 'claude')}
        spec = {'agent': 'claude', 'binary': binary, 'prompt': 'do it', 'session_id': None, 'permission': 'edit',
                'model': None, 'name': 'fixture', 'remote': True}
        result, parsed = self.run_helper(spec, env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(parsed['session_id'], SID)
        self.assertEqual(parsed['final'], 'fixture answer')
        self.assertFalse(parsed['is_error'])
        self.assertEqual(parsed['native']['remote_url'], 'https://claude.ai/code/session_FAKE123')
        self.assertEqual(parsed['native']['resume'], f'claude --resume {SID}')
        first = json.loads((self.root / 'calls').read_text().splitlines()[0])
        self.assertEqual(first[:6], ['--bg', '--name', 'fixture', '--remote-control', 'fixture', '--permission-mode'])
        self.assertNotIn('-p', first)

    def test_claude_waiting_session_is_reported_not_failed(self):
        binary = self.fake('claude', FAKE_CLAUDE)
        env = {'FAKE_ROOT': str(self.root), 'FAKE_SID': SID, 'CLAUDE_CONFIG_DIR': str(self.root / 'claude'),
               'FAKE_STATE': 'blocked', 'FAKE_WAITING': 'permission prompt'}
        spec = {'agent': 'claude', 'binary': binary, 'prompt': 'do it', 'session_id': SID, 'permission': 'readonly',
                'model': None, 'name': None, 'remote': False}
        result, parsed = self.run_helper(spec, env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(parsed['native']['waiting_for'], 'permission prompt')
        self.assertFalse(parsed['is_error'])
        calls = [json.loads(line) for line in (self.root / 'calls').read_text().splitlines()]
        # A known background session is woken with its saved options; extra flags would fork a copy.
        self.assertIn(['--bg', '--resume', SID, 'do it'], calls)

    def test_claude_resume_refuses_a_busy_session(self):
        binary = self.fake('claude', FAKE_CLAUDE)
        env = {'FAKE_ROOT': str(self.root), 'FAKE_SID': SID, 'CLAUDE_CONFIG_DIR': str(self.root / 'claude'),
               'FAKE_LIVE': '1', 'FAKE_STATUS': 'busy', 'FAKE_STATE': 'working'}
        spec = {'agent': 'claude', 'binary': binary, 'prompt': 'do it', 'session_id': SID, 'permission': 'edit',
                'model': None, 'name': None, 'remote': False}
        result, parsed = self.run_helper(spec, env)
        self.assertEqual(result.returncode, 1)
        self.assertIn('not interrupting', ' '.join(parsed['other']))
        calls = [json.loads(line) for line in (self.root / 'calls').read_text().splitlines()]
        self.assertFalse([c for c in calls if c[:1] in (['--bg'], ['stop'])])

    def daemon(self, remote='disabled'):
        sock = str(self.root / 'd.sock')
        daemon = FakeDaemon(sock, remote)
        daemon.start()
        codex = self.fake('codex', 'import json,sys\nif sys.argv[1:4]==["app-server","daemon","start"]:print(json.dumps({"socketPath":'
                          + json.dumps(sock) + '}))\nelse:open(' + json.dumps(str(self.root / 'enabled')) + ',"w").write("1")\n')
        return daemon, codex

    def test_codex_daemon_thread_translates_events(self):
        daemon, codex = self.daemon()
        spec = {'agent': 'codex', 'binary': codex, 'prompt': 'do it', 'session_id': None, 'permission': 'readonly',
                'model': 'fixture-model', 'name': 'Fixture chat', 'remote': False}
        result, parsed = self.run_helper(spec, {})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(parsed['session_id'], SID)
        self.assertEqual(parsed['final'], 'daemon answer')
        self.assertIn('[shell] ls', parsed['progress'])
        start = next(r for r in daemon.requests if r.get('method') == 'thread/start')['params']
        self.assertEqual((start['sandbox'], start['approvalPolicy'], start['model'], start['cwd']),
                         ('read-only', 'never', 'fixture-model', str(self.root)))
        self.assertIn({'threadId': SID, 'name': 'Fixture chat'},
                      [r['params'] for r in daemon.requests if r.get('method') == 'thread/name/set'])
        self.assertFalse((self.root / 'enabled').exists())

    def test_codex_remote_enables_daemon_remote_control_when_off(self):
        daemon, codex = self.daemon(remote='disabled')
        spec = {'agent': 'codex', 'binary': codex, 'prompt': 'do it', 'session_id': SID, 'permission': 'edit',
                'model': None, 'name': None, 'remote': True}
        result, parsed = self.run_helper(spec, {})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / 'enabled').exists())
        self.assertTrue(parsed['native']['remote_control_enabled_now'])
        self.assertTrue(any(r.get('method') == 'thread/resume' for r in daemon.requests))

    def test_mode_validation_and_default(self):
        cfg = Config(token='', state_dir=self.root / 'state', tasks_dir=self.root / 'tasks')
        cfg.state_dir.mkdir(mode=0o700)
        (cfg.state_dir / 'jobs').mkdir(mode=0o700)
        jobs = Jobs(cfg)
        captured = {}
        def start(argv, cwd, **kwargs):
            captured.update(argv=argv, extra=kwargs['extra'])
            return {'id': 'job'}
        with patch.object(agents, 'find_binary', return_value='/fake'), patch.object(jobs, 'start', side_effect=start):
            start_agent(cfg, jobs, 'codex', 'hi', cwd=str(self.root))
            self.assertEqual(captured['extra']['mode'], 'interactive')
            self.assertEqual(captured['argv'][1], str(HELPER))
            start_agent(cfg, jobs, 'codex', 'hi', cwd=str(self.root), mode='exec')
            self.assertEqual(captured['argv'][1], 'exec')
            start_agent(cfg, jobs, 'cursor', 'hi', cwd=str(self.root))
            self.assertEqual(captured['extra']['mode'], 'exec')
            for kwargs, message in (({'mode': 'exec', 'remote': True}, 'remote'), ({'agent': 'cursor', 'mode': 'interactive'}, 'exec only'),
                                    ({'name': 'x' * 101}, 'name')):
                agent = kwargs.pop('agent', 'claude')
                with self.subTest(kwargs=kwargs), self.assertRaisesRegex(ValueError, message):
                    start_agent(cfg, jobs, agent, 'hi', cwd=str(self.root), **kwargs)


if __name__ == '__main__':
    unittest.main()

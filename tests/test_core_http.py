"""Actual loopback HTTP tests with missing/broken optional agents; no real agent turns."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

TOKEN = 'integration-token-' * 3
TOTP_SECRET = 'JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP'
PASSWORD = 'fake-test-password-"\\-é'
SID = '11111111-2222-4333-8444-555555555555'

BOOT = '''
from contextlib import ExitStack
import sys
from unittest.mock import patch
if sys.argv[1] == 'cursor_reader_failed':
    sys.modules['ai_bridge.cursor_store'] = None
from ai_bridge import server
from ai_bridge import optional_agents
assert 'ai_bridge.agents' not in sys.modules, 'Core startup eagerly loaded agent helpers'
with ExitStack() as stack:
    if sys.argv[1] == 'slow_shutdown':
        import asyncio
        normal = server.build_app()
        async def app(scope, receive, send):
            if scope.get('path') == '/hold':
                await send({'type': 'http.response.start', 'status': 200, 'headers': []})
                await send({'type': 'http.response.body', 'body': b'waiting\\n', 'more_body': True})
                await asyncio.sleep(60)
                await send({'type': 'http.response.body', 'body': b'done'})
            else:
                await normal(scope, receive, send)
        stack.enter_context(patch.object(server, 'build_app', return_value=app))
    if sys.argv[1] == 'totp':
        with server.APPROVALS._lock():
            server.APPROVALS.remote.configure(server.CFG.sudo_totp_secret)
        stack.enter_context(patch.object(server, 'run_sync', return_value=dict(exit_code=0, stdout='approved-fixture', stderr='', timed_out=False)))
    if sys.argv[1] == 'missing':
        stack.enter_context(patch.object(server.ag, 'find_binary', return_value=None))
    elif sys.argv[1] == 'backend_failed':
        stack.enter_context(patch.object(optional_agents, '_load', side_effect=ImportError('test adapter unavailable')))
    server.main()
'''


class CoreHTTPTests(unittest.TestCase):
    @contextmanager
    def running(self, scenario):
        with tempfile.TemporaryDirectory(prefix='ai-bridge-http-') as tmp:
            root = Path(tmp)
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            fake = root/'failing-agent'
            fake.write_text('#!/bin/sh\nprintf "simulated CLI failure\\n" >&2\nexit 1\n')
            if scenario == 'usage_limit':
                fake.write_text('#!/bin/sh\nprintf "%s\\n" "ActionRequiredError: Increase limits for faster responses You\'re out of usage. Switch to Auto, or ask your admin to increase your limit to continue." >&2\nexit 1\n')
            if scenario == 'mcp_session':
                fake.write_text('#!' + sys.executable + '\nimport json,time\nprint(json.dumps({"type":"thread.started","thread_id":"' + SID + '"}), flush=True)\ntime.sleep(1)\n')
            fake.chmod(0o755)
            (root/'sudo-test.pass').write_text(PASSWORD)
            (root/'config/instructions').mkdir(parents=True)
            (root/'config/instructions/workspace.md').write_text('# Test workspace\n')
            (root/'config/server-instructions.md').write_text('Custom host instructions for test.\n')
            codex = root/'codex/sessions/2026'
            codex.mkdir(parents=True)
            (codex/f'rollout-test-{SID}.jsonl').write_text(json.dumps({'type': 'session_meta', 'payload': {'id': SID, 'cwd': str(root)}}))
            claude = root/'claude/projects/test'
            claude.mkdir(parents=True)
            (claude/f'{SID}.jsonl').write_text(json.dumps({'type': 'user', 'cwd': str(root), 'message': {'content': 'test'}}))
            cursor = root/f'cursor/chats/test/{SID}'
            cursor.mkdir(parents=True)
            (cursor/'store.db').touch()
            env = os.environ.copy()
            env.pop('CREDENTIALS_DIRECTORY', None)
            env.update(AI_BRIDGE_TOKEN=TOKEN, AI_BRIDGE_HOST='127.0.0.1', AI_BRIDGE_PORT=str(port),
                       AI_BRIDGE_TASKS_DIR=str(root/'tasks'), AI_BRIDGE_STATE_DIR=str(root/'state'), AI_BRIDGE_CONFIG_DIR=str(root/'config'),
                       AI_BRIDGE_DEFAULT_CWD=str(root), AI_BRIDGE_SKILL_DIRS=str(root/'skills'),
                       AI_BRIDGE_ALLOWED_HOSTS='bridge.test,bridge.test:443,bridge.test:8443',
                       AI_BRIDGE_ALLOWED_ORIGINS='https://extra.test',
                       AI_BRIDGE_DISABLED_AGENTS='cursor' if scenario == 'disabled' else '',
                       AI_BRIDGE_SUDO_PASSWORD_FILE=str(root/'sudo-test.pass'),
                       AI_BRIDGE_SUDO_TOTP_SECRET=TOTP_SECRET if scenario == 'totp' else '',
                       AI_BRIDGE_CLAUDE_BIN=str(fake), AI_BRIDGE_CODEX_BIN=str(fake), AI_BRIDGE_CURSOR_BIN=str(fake),
                       CODEX_HOME=str(root/'codex'), CLAUDE_CONFIG_DIR=str(root/'claude'), AI_BRIDGE_CURSOR_HOME=str(root/'cursor'),
                       PYTHONPATH=str(Path(__file__).resolve().parents[1]))
            process = subprocess.Popen([sys.executable, '-c', BOOT, scenario], env=env,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.server_process = process
            base = f'http://127.0.0.1:{port}'
            try:
                for _ in range(100):
                    if process.poll() is not None:
                        out, err = process.communicate()
                        self.fail('Test server exited: '+(out+err).decode(errors='replace'))
                    try:
                        with urllib.request.urlopen(base+'/healthz', timeout=.2) as response:
                            if response.read() == b'ok':
                                break
                    except (OSError, urllib.error.URLError):
                        time.sleep(.05)
                else:
                    self.fail('Test server did not become healthy')
                yield root, base
            finally:
                process.terminate()
                try:
                    process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate(timeout=5)

    def rpc(self, base, method, params, path='/mcp', headers=None, token=TOKEN):
        values = {'Accept': 'application/json, text/event-stream', 'Content-Type': 'application/json'}
        if token is not None:
            values['Authorization'] = 'Bearer '+token
        values.update(headers or {})
        request = urllib.request.Request(base+path, data=json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}).encode(), headers=values)
        with urllib.request.urlopen(request, timeout=10) as response:
            value = json.load(response)
            self.assertNotIn('error', value)
            return value['result']

    def tool(self, base, name, arguments):
        value = self.rpc(base, 'tools/call', {'name': name, 'arguments': arguments})
        self.assertFalse(value.get('isError'), value)
        return json.loads(value['content'][0]['text'])

    def core_checks(self, root, base):
        init = {'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 'integration', 'version': '1'}}
        for bad in (None, 'wrong-token'):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.rpc(base, 'initialize', init, token=bad)
            self.assertEqual(error.exception.code, 404)
        initialized = self.rpc(base, 'initialize', init, path='/'+TOKEN+'/mcp', token=None)
        self.assertIn('serverInfo', initialized)
        self.assertIn('Custom host instructions for test.', initialized['instructions'])
        self.assertTrue(initialized['instructions'].startswith('Execution target:'))
        self.assertIn('chatgpt.com, claude.ai', initialized['instructions'])
        self.assertNotIn('use those tools for ordinary local work', initialized['instructions'])
        self.assertIn('tell the user the task and target', initialized['instructions'])
        self.assertIn('choices together in the FIRST approval prompt', initialized['instructions'])
        self.assertIn('Do not also require a TOTP code', initialized['instructions'])
        tools = self.rpc(base, 'tools/list', {})['tools']
        self.assertIn('run_command', {tool['name'] for tool in tools})
        approval_tool = next(tool for tool in tools if tool['name'] == 'approve_privileged_request')
        self.assertEqual(approval_tool['inputSchema']['properties']['code']['type'], 'string')
        self.assertIn('Host CLI approval is an equal alternative', approval_tool['description'])
        command_tool = next(tool for tool in tools if tool['name'] == 'run_command')
        self.assertIn('administrator (root)', command_tool['description'])
        self.assertIn('Client approval', command_tool['description'])
        self.assertNotIn('Use native terminal tools', command_tool['description'])
        self.assertEqual(len(tools), 18)
        result = self.tool(base, 'run_command', {'command': 'printf core-ok'})
        self.assertEqual(result['stdout'], 'core-ok')
        self.assertIn('agents', self.tool(base, 'system_info', {}))
        file = str(root/'document.txt')
        self.tool(base, 'write_file', {'path': file, 'content': TOKEN+' '+PASSWORD})
        response = self.rpc(base, 'tools/call', {'name': 'read_file', 'arguments': {'path': file}})
        rendered = json.dumps(response)
        self.assertNotIn(TOKEN, rendered)
        self.assertNotIn(json.dumps(PASSWORD)[1:-1], rendered)
        self.assertIn('[REDACTED]', rendered)
        self.assertEqual(self.tool(base, 'list_instructions', {})['total'], 1)
        job = self.tool(base, 'start_job', {'command': 'printf background-ok'})
        result = self.tool(base, 'job_status', {'job_id': job['job_id'], 'wait_seconds': 2})
        self.assertEqual(result['exit_code'], 0)
        self.assertIn('background-ok', result['output_tail'])
        self.assertEqual((root/'state/audit.log').stat().st_mode & 0o777, 0o600)
        self.assertNotIn(TOKEN, (root/'state/audit.log').read_text())

    def core_alive(self, base):
        # Full core coverage runs once in test_core_http_without_any_coding_clis;
        # other scenarios only prove agent-side failures leave the core usable.
        tools = self.rpc(base, 'tools/list', {})['tools']
        self.assertEqual(len(tools), 18)
        self.assertEqual(self.tool(base, 'run_command', {'command': 'printf core-ok'})['stdout'], 'core-ok')
        self.assertIn('agents', self.tool(base, 'system_info', {}))

    def test_managed_task_and_runtime_tools_without_live_agents(self):
        with self.running('usage_limit') as (root, base):
            job = self.tool(base, 'agent_send', {'agent': 'cursor', 'prompt': 'fixture', 'wait_seconds': 0})
            self.assertTrue(job['task_id'].startswith('task-'))
            self.assertTrue(Path(job['cwd']).is_relative_to(root / 'tasks'))
            tasks = self.tool(base, 'agent_list', {'show': 'tasks'})
            self.assertEqual(tasks['total'], 1)
            self.assertEqual(tasks['tasks'][0]['task_id'], job['task_id'])
            self.assertEqual([row['job_id'] for row in self.tool(base, 'list_jobs', {'kind': 'agent'})], [job['job_id']])
            self.assertEqual(self.tool(base, 'list_jobs', {'kind': 'command'}), [])
            result = self.tool(base, 'job_status', {'job_id': job['job_id'], 'wait_seconds': 2})
            self.assertEqual(result['kind'], 'agent')
            self.assertNotEqual(result['status'], 'running')
            self.assertIn('log', self.tool(base, 'job_status', {'job_id': job['job_id'], 'log_offset': 0}))
            stopped = self.tool(base, 'kill_job', {'job_id': job['job_id']})
            self.assertTrue(stopped['managed'])
            self.assertEqual(stopped['job_id'], job['job_id'])
            second = self.tool(base, 'agent_send', {'agent': 'cursor', 'prompt': 'fixture',
                               'task_id': job['task_id'], 'wait_seconds': 1})
            self.assertEqual(second['cwd'], job['cwd'])
            self.assertIn('error', self.tool(base, 'agent_send', {'agent': 'cursor', 'prompt': 'fixture', 'task_id': ''}))

    def test_mcp_boundary_persists_session_evidence(self):
        import hashlib
        with self.running('mcp_session') as (root, base):
            result = self.tool(base, 'agent_send', {'agent': 'codex', 'prompt': 'fixture', 'wait_seconds': 0, 'mode': 'exec'})
            jid = result['job_id']
            meta = json.loads((root / 'state/jobs' / (jid + '.json')).read_text())
            self.assertEqual(meta['invocation_origin'], 'mcp')
            path = root / 'state/management/provenance' / (hashlib.sha256(f'codex:{SID}'.encode()).hexdigest() + '.json')
            deadline = time.monotonic() + 5
            while not path.exists() and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertIn(jid, json.loads(path.read_text())['job_ids'])
            row = self.tool(base, 'agent_sessions', {'agent': 'codex'})['sessions'][0]
            self.assertEqual(row['bridge_origin'], 'mcp')
            self.assertIsNone(row['native_source'])
            self.tool(base, 'job_status', {'job_id': jid, 'wait_seconds': 2})

    def test_core_http_without_any_coding_clis(self):
        with self.running('missing') as (root, base):
            self.core_checks(root, base)
            self.assertTrue(all(not row['installed'] for row in self.tool(base, 'agent_list', {})['agents'].values()))
            self.assertIn('error', self.tool(base, 'agent_send', {'agent': 'codex', 'prompt': 'not run'}))

    @unittest.skipIf(os.getuid() == 0, 'Root-service tools intentionally bypass approval')
    def test_authenticator_approval_through_mcp_executes_once_and_hides_secret(self):
        from ai_bridge.approvals import totp, totp_key
        with self.running('totp') as (root, base):
            args = {'command': 'fixture privileged command', 'cwd': str(root), 'sudo': True}
            pending = self.tool(base, 'run_command', args)
            self.assertEqual([m['method'] for m in pending['approval']['methods']], ['totp', 'host_cli'])
            self.assertFalse(pending['execution_started'])
            request_id = pending['approval']['request_id']
            result = self.tool(base, 'approve_privileged_request',
                               {'request_id': request_id, 'code': 'never-log-this-code'})
            self.assertEqual(result['status'], 'invalid_or_expired_code')
            code = totp(totp_key(TOTP_SECRET), int(time.time() // 30))
            result = self.tool(base, 'approve_privileged_request', {'request_id': request_id, 'code': code})
            self.assertEqual(result['status'], 'approved')
            self.assertEqual(self.tool(base, 'run_command', args)['stdout'], 'approved-fixture')
            self.assertEqual(self.tool(base, 'run_command', args)['status'], 'approval_required')
            audit = (root / 'state/audit.log').read_text()
            self.assertNotIn('never-log-this-code', audit)
            self.assertNotIn(TOTP_SECRET, audit)
            file = root / 'fake-secret.txt'
            file.write_text(TOTP_SECRET)
            rendered = json.dumps(self.rpc(base, 'tools/call', {'name': 'read_file', 'arguments': {'path': str(file)}}))
            self.assertNotIn(TOTP_SECRET, rendered)
            self.assertIn('[REDACTED]', rendered)
            self.assertEqual(self.tool(base, 'system_info', {})['sudo_authenticator'], 'ready')

    def test_core_http_when_agent_helpers_fail_to_load(self):
        with self.running('backend_failed') as (root, base):
            self.core_alive(base)
            self.assertIn('error', self.tool(base, 'agent_send', {'agent': 'codex', 'prompt': 'not run'}))
            self.assertEqual(self.tool(base, 'agent_list', {'show': 'models', 'agent': 'cursor'})['status'], 'unavailable')

    def test_broken_cursor_reader_does_not_break_other_histories(self):
        with self.running('cursor_reader_failed') as (root, base):
            self.core_alive(base)
            for agent in ('codex', 'claude'):
                self.assertEqual(self.tool(base, 'agent_sessions', {'agent': agent})['total'], 1)
            rows = self.tool(base, 'agent_sessions', {'agent': 'cursor'})['sessions']
            self.assertTrue(rows)
            self.assertIn('error', rows[0])
            self.assertFalse(rows[0]['resumable'])

    def test_disabled_adapter_is_independent(self):
        with self.running('disabled') as (root, base):
            self.core_alive(base)
            self.assertIn('disabled', self.tool(base, 'agent_sessions', {'agent': 'cursor'})['error'])
            self.assertEqual(self.tool(base, 'agent_sessions', {'agent': 'codex'})['total'], 1)

    def test_broken_cli_and_host_origin_checks(self):
        with self.running('broken_cli') as (root, base):
            self.core_alive(base)
            job = self.tool(base, 'agent_send', {'agent': 'codex', 'prompt': 'test stub', 'wait_seconds': 1})
            if job['status'] == 'running':
                job = self.tool(base, 'job_status', {'job_id': job['job_id'], 'wait_seconds': 2})
            self.assertTrue(job['is_error'])
            self.assertEqual(job['exit_code'], 1)
            for host in ('bridge.test', 'bridge.test:443', 'bridge.test:8443'):
                for origin in (None, 'https://chatgpt.com', 'https://claude.ai', 'https://claude.com', 'https://extra.test'):
                    headers = {'Host': host}
                    if origin:
                        headers['Origin'] = origin
                    self.assertIn('tools', self.rpc(base, 'tools/list', {}, headers=headers))
            for headers, status in (({'Host': 'untrusted.test'}, 421), ({'Origin': 'https://untrusted.test'}, 403)):
                with self.assertRaises(urllib.error.HTTPError) as error:
                    self.rpc(base, 'tools/list', {}, headers=headers)
                self.assertEqual(error.exception.code, status)

    def test_provider_failures_are_normal_mcp_results_and_core_stays_usable(self):
        # Classification of each failure kind is unit-tested in test_server_sessions.
        for scenario, outcome in (('usage_limit', 'blocked'),):
            with self.subTest(scenario=scenario), self.running(scenario) as (root, base):
                job = self.tool(base, 'agent_send', {'agent': 'cursor', 'prompt': 'test stub', 'wait_seconds': 1})
                if job['status'] == 'running':
                    job = self.tool(base, 'job_status', {'job_id': job['job_id'], 'wait_seconds': 2})
                self.assertEqual(job['status'], 'failed')
                self.assertEqual(job['exit_code'], 1)
                self.assertEqual(job['outcome'], outcome)
                self.assertEqual(job['failure']['scope'], 'agent')
                self.assertEqual(job['failure']['code'], scenario)
                self.assertFalse(job['failure']['automatic_retry'])
                polled = self.tool(base, 'job_status', {'job_id': job['job_id'], 'wait_seconds': 0})
                self.assertEqual(polled['failure'], job['failure'])
                self.assertEqual(len(self.tool(base, 'list_jobs', {})), 1)
                self.assertEqual(self.tool(base, 'run_command', {'command': 'printf still-available'})['stdout'], 'still-available')
                self.assertIn('tools', self.rpc(base, 'tools/list', {}))

    def test_wait_saturation_preserves_command_and_cancellation_capacity(self):
        from concurrent.futures import ThreadPoolExecutor
        with self.running('missing') as (root,base),ThreadPoolExecutor(max_workers=48) as pool:
            job=self.tool(base,'start_job',{'command':'sleep 8'})
            waits=[pool.submit(self.tool,base,'job_status',{'job_id':job['job_id'],'wait_seconds':3}) for _ in range(44)]
            time.sleep(.4)
            started=time.monotonic()
            self.assertEqual(self.tool(base,'run_command',{'command':'printf responsive'})['stdout'],'responsive')
            elapsed=time.monotonic()-started
            stopped=self.tool(base,'kill_job',{'job_id':job['job_id']})
            self.assertEqual(stopped['status'],'killed')
            for future in waits:future.result(timeout=5)
            self.assertLess(elapsed,1.5,'Wait requests exhausted workers needed for control operations')

    def test_shutdown_does_not_wait_for_long_lived_connection_indefinitely(self):
        with self.running('slow_shutdown') as (_, base):
            with urllib.request.urlopen(base + '/hold', timeout=20) as response:
                self.assertEqual(response.readline(), b'waiting\n')
                started = time.monotonic()
                self.server_process.terminate()
                self.server_process.wait(timeout=15)
                self.assertLess(time.monotonic() - started, 15)

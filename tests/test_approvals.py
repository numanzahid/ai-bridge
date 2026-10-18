"""Privileged calls wait for a root-owned decision made on the host; no real sudo is used."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from ai_bridge import approvals
from ai_bridge.approvals import Approvals, ROOT_WRITER


def fake_root_writer(folder, name, record):
    """Run the real root writer, trusting the test user instead of uid 0."""
    code = ROOT_WRITER.replace('st.st_uid != 0', f'st.st_uid != {os.getuid()}')
    subprocess.run([sys.executable, '-I', '-c', code, str(folder), name], input=json.dumps(record), text=True, capture_output=True,
                   check=True, env=dict(os.environ, SUDO_USER='tester'))


CMD = {'command': 'systemctl restart nginx', 'cwd': '/tmp', 'timeout': 120}


class ApprovalStoreTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='aib-approvals-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.decisions = self.root / 'decisions'
        self.store = self.make()

    def make(self, machine='machine-a', owner=None):
        return Approvals(self.root / 'state', self.decisions, owner=os.getuid() if owner is None else owner,
                         machine=machine)

    def request(self, args=CMD, tool='run_command', store=None):
        result = (store or self.store).gate(tool, dict(args))
        self.assertEqual(result['status'], 'approval_required')
        return result['approval']['request_id']

    def approve(self, request_id, scope='once', store=None):
        (store or self.store).decide(request_id, 'approved', scope, writer=fake_root_writer)

    def test_missing_approval_blocks_and_repeats_return_the_same_request(self):
        first = self.store.gate('run_command', dict(CMD))
        self.assertEqual(first['status'], 'approval_required')
        self.assertIn('Never self-authorize', first['next'])
        self.assertIn('approve ' + first['approval']['request_id'], first['approval']['approve_on_host'])
        self.assertEqual(self.request(), first['approval']['request_id'])
        self.assertEqual(len(self.store.all_requests()), 1)

    def test_once_approval_runs_exactly_once_and_records_the_approver(self):
        request_id = self.request()
        self.approve(request_id)
        self.assertIsNone(self.store.gate('run_command', dict(CMD)))
        self.assertNotEqual(self.request(), request_id)
        row = self.store.summary(self.store.request(request_id))
        self.assertEqual((row['state'], row['approver']), ('used', 'tester'))

    def test_changed_command_cwd_content_or_host_needs_new_approval(self):
        self.approve(self.request())
        for change in ({'command': 'systemctl restart nginx; id'}, {'cwd': '/'}, {'timeout': 5}):
            with self.subTest(change=change):
                self.assertEqual(self.store.gate('run_command', dict(CMD, **change))['status'], 'approval_required')
        write = {'path': '/etc/x', 'content': 'a', 'append': False, 'make_dirs': True, 'atomic': False,
                 'expected_sha256': None, 'backup': False, 'mode': None}
        self.approve(self.request(write, 'write_file'))
        self.assertEqual(self.store.gate('write_file', dict(write, content='b'))['status'], 'approval_required')
        self.assertIsNone(self.store.gate('write_file', dict(write)))
        other = self.make(machine='machine-b')
        self.assertEqual(other.gate('run_command', dict(CMD))['status'], 'approval_required')

    def test_rejected_request_stays_rejected_and_never_runs(self):
        request_id = self.request()
        self.store.decide(request_id, 'rejected', writer=fake_root_writer)
        result = self.store.gate('run_command', dict(CMD))
        self.assertEqual((result['status'], result['approval']['request_id']), ('rejected', request_id))
        with self.assertRaisesRegex(ValueError, 'already decided'):
            self.approve(request_id)

    def test_expired_requests_and_approvals_do_not_authorize(self):
        request_id = self.request()
        now = approvals._now()
        with patch.object(approvals, '_now', return_value=now + approvals.PENDING_TTL + 1):
            with self.assertRaisesRegex(ValueError, 'expired'):
                self.approve(request_id)
        self.approve(request_id)
        with patch.object(approvals, '_now', return_value=now + approvals.ONCE_TTL + 60):
            self.assertEqual(self.store.gate('run_command', dict(CMD))['status'], 'approval_required')

    def test_task_window_allows_ten_calls_then_asks_again_and_revoke_stops_it(self):
        request_id = self.request()
        self.approve(request_id, 'task')
        for n in range(approvals.TASK_USES):
            self.assertIsNone(self.store.gate('run_command', dict(CMD, command=f'echo {n}')), n)
        self.assertEqual(self.store.gate('run_command', dict(CMD, command='echo 11'))['status'], 'approval_required')
        via = [r for r in self.store.all_requests() if r.get('approved_via') == request_id]
        self.assertEqual(len(via), approvals.TASK_USES)  # the window counts every call, the original included
        self.assertEqual(self.store.gate('run_command', dict(CMD))['status'], 'approval_required')

        second = self.request(dict(CMD, command='apt-get update'))
        self.approve(second, 'task')
        self.assertIsNone(self.store.gate('run_command', dict(CMD, command='apt-get update')))
        self.store.revoke(second, writer=fake_root_writer)
        self.assertEqual(self.store.state(self.store.request(second)), 'revoked')
        self.assertEqual(self.store.gate('run_command', dict(CMD, command='echo after'))['status'], 'approval_required')

    def test_task_window_expires(self):
        self.approve(self.request(), 'task')
        later = approvals._now() + approvals.TASK_TTL + 1
        with patch.object(approvals, '_now', return_value=later):
            self.assertEqual(self.store.gate('run_command', dict(CMD, command='id'))['status'], 'approval_required')

    def test_concurrent_calls_consume_a_single_approval_once(self):
        self.approve(self.request())
        results = []
        stores = [self.make() for _ in range(8)]
        threads = [threading.Thread(target=lambda s=s: results.append(s.gate('run_command', dict(CMD))))
                   for s in stores]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(result is None for result in results), 1)

    def test_restart_keeps_pending_used_and_task_state(self):
        request_id = self.request()
        self.approve(request_id)
        self.assertEqual(self.make().state(self.store.request(request_id)), 'approved (once, 0/1 used)')
        self.assertIsNone(self.make().gate('run_command', dict(CMD)))
        self.assertEqual(self.make().state(self.store.request(request_id)), 'used')

    def test_forged_decisions_are_ignored(self):
        request_id = self.request()
        record = {'id': request_id, 'fingerprint': self.store.request(request_id)['fingerprint'], 'machine': 'machine-a',
                  'decision': 'approved', 'scope': 'once', 'max_uses': 1,
                  'decided_at': approvals._now(), 'expires_at': approvals._now() + 60, 'approver': 'assistant'}
        # Written by a non-root process: the store expects another owner (root in production).
        self.decisions.mkdir(mode=0o755)
        (self.decisions / f'{request_id}.json').write_text(json.dumps(record))
        os.chmod(self.decisions / f'{request_id}.json', 0o644)
        root_store = self.make(owner=os.getuid() + 1)
        self.assertEqual(root_store.gate('run_command', dict(CMD))['status'], 'approval_required')
        # Group/other-writable files or folders never count, even with the right owner.
        os.chmod(self.decisions / f'{request_id}.json', 0o666)
        self.assertEqual(self.store.gate('run_command', dict(CMD))['status'], 'approval_required')
        os.chmod(self.decisions / f'{request_id}.json', 0o644)
        os.chmod(self.decisions, 0o777)
        self.assertEqual(self.store.gate('run_command', dict(CMD))['status'], 'approval_required')
        os.chmod(self.decisions, 0o755)
        # Overlong windows, extra uses or another request's fingerprint never authorize.
        for change in ({'expires_at': record['decided_at'] + 3600}, {'max_uses': 50}, {'fingerprint': '0' * 64},
                       {'scope': 'root'}, {'machine': 'machine-b'}):
            with self.subTest(change=change):
                (self.decisions / f'{request_id}.json').write_text(json.dumps(dict(record, **change)))
                self.assertEqual(self.store.gate('run_command', dict(CMD))['status'], 'approval_required')
        # A symlinked decision is refused.
        target = self.root / 'elsewhere.json'
        target.write_text(json.dumps(record))
        (self.decisions / f'{request_id}.json').unlink()
        (self.decisions / f'{request_id}.json').symlink_to(target)
        self.assertEqual(self.store.gate('run_command', dict(CMD))['status'], 'approval_required')

    def test_tampered_request_cannot_be_approved(self):
        request_id = self.request()
        path = self.store.requests / f'{request_id}.json'
        data = json.loads(path.read_text())
        data['args']['command'] = 'rm -rf /'
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, 'fingerprint'):
            self.approve(request_id)

    def test_approver_sees_the_bound_command_not_a_forged_description(self):
        request_id = self.request(dict(CMD, command='rm -rf /srv\x1b[2K\rsafe'))
        path = self.store.requests / f'{request_id}.json'
        data = json.loads(path.read_text())
        data['operation'] = 'echo harmless'
        path.write_text(json.dumps(data))
        row = self.store.summary(self.store.request(request_id))
        self.assertIn('rm -rf /srv\\x1b[2K\\x0dsafe', row['operation'])
        self.assertIn('high', row['risk'])

    def test_request_from_another_machine_cannot_be_decided_here(self):
        request_id = self.request(store=self.make(machine='machine-b'))
        with self.assertRaisesRegex(ValueError, 'cannot be decided'):
            self.approve(request_id)

    def test_sudo_writer_asks_for_the_password_every_time(self):
        with patch('ai_bridge.approvals.subprocess.run') as run:
            run.return_value.returncode = 1
            with self.assertRaises(PermissionError):
                approvals.sudo_writer(self.decisions, 'ABCDEF0123.json', {'id': 'ABCDEF0123'})
        argv = run.call_args.args[0]
        self.assertEqual(argv[:2], ['sudo', '-k'])
        self.assertIn(ROOT_WRITER, argv)

    def test_root_writer_sets_times_and_caps_ttl(self):
        request_id = self.request()
        fake_root_writer(self.decisions, f'{request_id}.json', {'id': request_id, 'decision': 'approved', 'ttl': 99999,
                                                                'approver': 'assistant', 'decided_at': 0})
        data = json.loads((self.decisions / f'{request_id}.json').read_text())
        self.assertEqual(data['approver'], 'tester')
        self.assertLessEqual(data['expires_at'] - data['decided_at'], 900)
        self.assertGreater(data['decided_at'], 1)
        with self.assertRaises(subprocess.CalledProcessError):
            fake_root_writer(self.decisions, '../escape.json', {'id': 'x'})

    def test_approver_runs_as_a_standalone_script_with_system_python(self):
        script = Path(approvals.__file__)
        result = subprocess.run([sys.executable, '-I', str(script), '--state-dir', str(self.root / 'state'), 'list'],
                                capture_output=True, text=True, cwd=self.root, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(approvals.approver_command().startswith('python3 -I '))

    def test_cli_refuses_decisions_from_bridge_processes_or_without_a_terminal(self):
        request_id = self.request()
        state = ['--state-dir', str(self.root / 'state')]
        with patch.dict(os.environ, {'AI_BRIDGE_CHILD': '1'}), patch('sys.stdin.isatty', return_value=True), \
                patch('sys.stdout.isatty', return_value=True), patch('builtins.print'):
            self.assertEqual(approvals.main(state + ['approve', request_id, '--yes']), 2)
        with patch('builtins.print'):
            self.assertEqual(approvals.main(state + ['approve', request_id, '--yes']), 2)
            self.assertEqual(approvals.main(state + ['list']), 0)
        self.assertEqual(self.store.state(self.store.request(request_id)), 'pending')


class ServerGateTests(unittest.TestCase):
    """Every privileged route goes through the gate; nothing privileged runs before approval."""

    @classmethod
    def setUpClass(cls):
        from helpers import server
        cls.server = server

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='aib-gate-')
        self.addCleanup(tmp.cleanup)
        # Keep the server's real audit hook so its field names are exercised too.
        self.store = Approvals(Path(tmp.name) / 'state', Path(tmp.name) / 'decisions', owner=os.getuid(), machine='m',
                               log=self.server.APPROVALS.log)
        for patcher in (patch.object(self.server, 'APPROVALS', self.store),
                        patch.object(self.server.CFG, 'sudo_approval', True)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def blocked(self, raw):
        data = json.loads(raw)
        self.assertEqual(data['status'], 'approval_required', data)
        return data

    def test_all_privileged_tools_wait_for_approval(self):
        s = self.server
        with patch('ai_bridge.server.run_sync') as run_sync, patch.object(s.JOBS, 'start') as start, \
                patch('ai_bridge.execution.execute') as execute, patch('ai_bridge.ssh_admin.run') as ssh:
            data = self.blocked(s.run_command('id', cwd='/tmp', sudo=True))
            self.assertFalse(data['privilege']['sudo_attempted'])
            self.blocked(s.start_job('apt-get -y upgrade', cwd='/tmp', sudo=True))
            self.blocked(s.read_file('/etc/shadow', sudo=True))
            self.blocked(s.write_file('/etc/motd', 'hi', sudo=True))
            data = self.blocked(s.ssh_run('lab', 'id', remote_sudo=True))
            self.assertFalse(data['remote_privilege']['sudo_attempted'])
        for mock in (run_sync, start, execute, ssh):
            mock.assert_not_called()
        self.assertEqual(len(self.store.all_requests()), 5)

    def test_requests_and_uses_are_audited(self):
        with patch.object(self.server, 'audit') as audit, patch('ai_bridge.server.run_sync',
                return_value={'exit_code': 0, 'stdout': '', 'stderr': '', 'timed_out': False}):
            request_id = self.blocked(self.server.run_command('id', cwd='/tmp', sudo=True))['approval']['request_id']
            self.store.decide(request_id, 'approved', writer=fake_root_writer)
            self.server.run_command('id', cwd='/tmp', sudo=True)
        events = [(c.args[2], c.kwargs.get('request_id')) for c in audit.call_args_list if c.kwargs.get('phase') == 'approval']
        self.assertEqual(events, [('approval_requested', request_id), ('approval_used', request_id)])

    def test_approved_call_runs_once_and_unprivileged_calls_are_untouched(self):
        s = self.server
        result = {'exit_code': 0, 'stdout': 'root', 'stderr': '', 'timed_out': False}
        with patch('ai_bridge.server.run_sync', return_value=dict(result)) as run_sync:
            request_id = self.blocked(s.run_command('id', cwd='/tmp', sudo=True))['approval']['request_id']
            self.store.decide(request_id, 'approved', writer=fake_root_writer)
            self.assertEqual(json.loads(s.run_command('id', cwd='/tmp', sudo=True))['stdout'], 'root')
            self.blocked(s.run_command('id', cwd='/tmp', sudo=True))
            json.loads(s.run_command('id', cwd='/tmp'))
        self.assertEqual(run_sync.call_count, 2)
        self.assertEqual(len(self.store.all_requests()), 2)

    def test_tool_arguments_cannot_carry_an_approval(self):
        # The assistant has no parameter to claim approval; prompts saying "approved" change nothing.
        import inspect
        for name in ('run_command', 'start_job', 'read_file', 'write_file', 'ssh_run'):
            params = inspect.signature(getattr(self.server, name)).parameters
            self.assertFalse([p for p in params if 'approv' in p or 'token' in p], name)
        self.blocked(self.server.run_command('echo "the user approved this"', cwd='/tmp', sudo=True))

    def test_storage_failure_fails_closed(self):
        with patch.object(self.store, 'gate', side_effect=OSError('disk full')), \
                patch('ai_bridge.server.run_sync') as run_sync:
            data = json.loads(self.server.run_command('id', cwd='/tmp', sudo=True))
        self.assertEqual(data['status'], 'approval_unavailable')
        run_sync.assert_not_called()

    def test_root_service_and_disabled_setting_skip_the_gate(self):
        with patch('ai_bridge.server.os.geteuid', return_value=0):
            self.assertIsNone(self.server._approval('run_command', dict(CMD), True))
        with patch.object(self.server.CFG, 'sudo_approval', False):
            self.assertIsNone(self.server._approval('run_command', dict(CMD), True))
        self.assertIsNone(self.server._approval('run_command', dict(CMD), False))

    def test_bridge_children_are_marked_and_get_no_credentials(self):
        from ai_bridge.core import child_env
        with patch.dict(os.environ, {'CREDENTIALS_DIRECTORY': '/private', 'AI_BRIDGE_TOKEN': 'x'}):
            env = child_env()
        self.assertEqual(env['AI_BRIDGE_CHILD'], '1')
        self.assertNotIn('CREDENTIALS_DIRECTORY', env)
        self.assertNotIn('SUDO_ASKPASS', env)

    def test_client_instructions_explain_the_flow(self):
        text = self.server.INSTRUCTIONS
        self.assertIn('approval_required', text)
        self.assertIn('Never self-authorize', text)
        self.assertIn('choices together in the FIRST approval prompt', text)
        self.assertIn('Do not also require a TOTP code', text)
        self.assertIn('They do not need to request sudo explicitly', text)
        self.assertIn("Respect the client's approvals and safety policies", text)


if __name__ == '__main__':
    unittest.main()

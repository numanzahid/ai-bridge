"""Authenticator enrollment and grants, using fake secrets and no real sudo."""
import base64
from concurrent.futures import ThreadPoolExecutor
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ai_bridge import approvals
from ai_bridge.approvals import Approvals, ENV_WRITER, totp, totp_key

SECRET = base64.b32encode(b'12345678901234567890').decode()
CMD = {'command': 'id -u', 'cwd': '/tmp', 'timeout': 120}


class RemoteApprovalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='aib-totp-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.now = 1_800_000_000
        clock = patch.object(approvals, '_now', side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.store = self.make()
        with self.store._lock():
            self.store.remote.configure(SECRET)

    def make(self, secret=SECRET):
        return Approvals(self.root / 'state', self.root / 'decisions', owner=os.getuid(),
                         machine='test-machine', totp_secret=secret)

    def request(self, command='id -u'):
        result = self.store.gate('run_command', dict(CMD, command=command))
        self.assertEqual(result['status'], 'approval_required')
        return result['approval']['request_id']

    def code(self, offset=0, secret=SECRET):
        return totp(totp_key(secret), self.now // 30 + offset)

    def wrong_code(self):
        valid = {self.code(n) for n in range(-10, 2)}
        return next(str(n).zfill(6) for n in range(100) if str(n).zfill(6) not in valid)

    def writer(self, folder, name, record):
        folder.mkdir(mode=0o755, exist_ok=True)
        record = dict(record, approver='host-user', decided_at=self.now)
        if record.get('decision') == 'approved':
            record['expires_at'] = self.now + record.pop('ttl')
        approvals._write_new(folder / name, record)

    def approve(self, request_id):
        result = self.store.approve_remote(request_id, self.code())
        self.assertEqual(result['status'], 'approved', result)

    def test_rfc6238_vectors(self):
        key = b'12345678901234567890'
        for seconds, expected in [(59, '94287082'), (1111111109, '07081804'),
                                  (1111111111, '14050471'), (1234567890, '89005924'),
                                  (2000000000, '69279037'), (20000000000, '65353130')]:
            self.assertEqual(totp(key, seconds // 30, digits=8), expected)

    def test_first_prompt_offers_both_methods_for_the_same_request(self):
        result = self.store.gate('run_command', CMD)
        approval = result['approval']
        self.assertFalse(result['execution_started'])
        self.assertEqual([m['method'] for m in approval['methods']], ['totp', 'host_cli'])
        self.assertTrue(all(m['scope'] == 'once' for m in approval['methods']))
        self.assertEqual(approval['methods'][0]['request_id'], approval['request_id'])
        self.assertEqual(approval['methods'][0]['tool'], 'approve_privileged_request')
        self.assertEqual(approval['methods'][1]['command'], approval['approve_on_host'])
        self.assertIn(approval['request_id'], approval['methods'][1]['command'])
        self.assertIn('authenticator code', approval['prompt'])
        self.assertIn('host CLI', approval['prompt'])
        self.assertIn('BOTH methods together in the FIRST', result['next'])
        self.assertIn('do not also require a code', result['next'])

    def test_both_methods_authorize_every_privileged_route_once(self):
        routes = [
            ('run_command', dict(CMD)),
            ('start_job', {'command': 'id -u', 'cwd': '/tmp'}),
            ('read_file', {'path': '/etc/example'}),
            ('write_file', dict(path='/etc/example', content='test', append=False, make_dirs=True,
                                atomic=False, expected_sha256=None, backup=False, mode=None)),
            ('ssh_run', {'host': 'example-alias', 'command': 'id -u'}),
        ]
        for tool, args in routes:
            for method in ('totp', 'host_cli'):
                with self.subTest(tool=tool, method=method):
                    pending = self.store.gate(tool, args)
                    request_id = pending['approval']['request_id']
                    # The assistant retrying after a bare "approved" is not authorization.
                    self.assertEqual(self.store.gate(tool, args)['status'], 'approval_required')
                    if method == 'totp':
                        self.approve(request_id)
                    else:
                        self.store.decide(request_id, 'approved', writer=self.writer)
                    self.assertIsNone(self.store.gate(tool, args))
                    self.assertEqual(self.store.gate(tool, args)['status'], 'approval_required')
                    self.now += 30

    def test_only_available_methods_are_offered_and_rejection_stops_both(self):
        request_id = self.request()
        for store in (self.make(''), self.make('invalid-secret')):
            pending = store.gate('run_command', CMD)
            self.assertEqual([m['method'] for m in pending['approval']['methods']], ['host_cli'])
            self.assertNotIn('authenticator code', pending['approval']['prompt'])
        for _ in range(5):
            self.store.approve_remote(request_id, self.wrong_code())
        locked = self.store.gate('run_command', CMD)
        self.assertEqual([m['method'] for m in locked['approval']['methods']], ['host_cli'])
        self.store.decide(request_id, 'rejected', writer=self.writer)
        rejected = self.store.gate('run_command', CMD)
        self.assertEqual(rejected['status'], 'rejected')
        self.assertFalse(rejected['execution_started'])
        self.assertEqual(rejected['approval']['methods'], [])
        self.assertNotIn('approve_with_authenticator', rejected['approval'])

    def test_grant_runs_only_original_call_once_and_survives_restart(self):
        request_id = self.request()
        self.approve(request_id)
        self.assertEqual(self.make().state(self.store.request(request_id)), 'approved (once, 0/1 used)')
        self.assertEqual(self.make().gate('run_command', dict(CMD, command='whoami'))['status'], 'approval_required')
        self.assertIsNone(self.make().gate('run_command', dict(CMD)))
        self.assertEqual(self.make().state(self.store.request(request_id)), 'used')
        self.assertEqual(self.make().gate('run_command', dict(CMD))['status'], 'approval_required')

    def test_consumed_counter_and_every_older_counter_are_rejected_across_requests(self):
        self.approve(self.request())
        other = self.request('whoami')
        for code in (self.code(), self.code(-1), self.code(-10)):
            self.assertEqual(self.make().approve_remote(other, code)['status'], 'invalid_or_expired_code')
        self.now += 30
        self.assertEqual(self.make().approve_remote(other, self.code())['status'], 'approved')

    def test_acceptance_window_and_future_tolerance(self):
        for offset, expected in [(-10, 'approved'), (-11, 'invalid_or_expired_code'),
                                 (0, 'approved'), (1, 'approved'), (2, 'invalid_or_expired_code')]:
            with self.subTest(offset=offset):
                with self.store._lock():
                    self.store.remote.configure(SECRET)
                request_id = self.request('offset ' + str(offset))
                self.assertEqual(self.store.approve_remote(request_id, self.code(offset))['status'], expected)

    def test_leading_zero_codes_are_strings(self):
        while not self.code().startswith('0'):
            self.now += 30
        request_id = self.request()
        self.approve(request_id)

    def test_global_failures_lock_all_requests_and_persist_across_restart(self):
        for n in range(5):
            request_id = self.request('request ' + str(n))
            result = self.make().approve_remote(request_id, self.wrong_code())
            self.assertEqual(result['status'], 'approval_locked' if n == 4 else 'invalid_or_expired_code')
        other = self.request('different client')
        self.assertEqual(self.make().remote_status(), 'locked')
        pending = self.store.gate('run_command', dict(CMD, command='different client'))
        self.assertEqual(pending['approval']['authenticator_status'], 'locked')
        self.assertNotIn('approve_with_authenticator', pending['approval'])
        self.assertEqual(self.make().approve_remote(other, self.code())['status'], 'approval_locked')
        self.store.decide(other, 'approved', writer=self.writer)
        self.assertIsNone(self.make().gate('run_command', dict(CMD, command='different client')))
        self.now += 901
        self.assertEqual(self.make().remote_status(), 'ready')
        self.approve(self.request('after lockout'))

    def test_malformed_codes_use_attempt_budget_without_echoing_input(self):
        request_id = self.request()
        for code in ('abc123', '12345', '１２３４５６', '1234567', 'sensitive-input'):
            result = self.store.approve_remote(request_id, code)
            self.assertNotIn(code, json.dumps(result))
        self.assertEqual(result['status'], 'approval_locked')

    def test_parallel_verification_and_execution_spend_one_code_once(self):
        ids = [self.request('parallel ' + str(n)) for n in range(8)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda rid: self.make().approve_remote(rid, self.code()), ids))
        approved = [row for row in results if row['status'] == 'approved']
        self.assertEqual(len(approved), 1)
        command = self.store.request(approved[0]['request_id'])['args']['command']
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.make().gate('run_command', dict(CMD, command=command)), range(8)))
        self.assertEqual(results.count(None), 1)

    def test_host_rejection_and_revocation_override_remote_grants(self):
        for decision in ('rejected', 'revoked'):
            request_id = self.request(decision)
            self.approve(request_id)
            if decision == 'rejected':
                self.store.decide(request_id, 'rejected', writer=self.writer)
            else:
                self.make(None).revoke(request_id, writer=self.writer)
            result = self.store.gate('run_command', dict(CMD, command=decision))
            self.assertEqual(result['status'], 'rejected')
            self.assertEqual(self.store.approve_remote(request_id, self.code())['status'], 'already_decided')
            self.now += 30

    def test_host_and_remote_once_grants_share_consumption(self):
        request_id = self.request()
        self.approve(request_id)
        request = self.store.request(request_id)
        # Simulate a host decision being published concurrently with remote approval.
        self.writer(self.store.decisions, request_id + '.json',
                    dict(id=request_id, fingerprint=request['fingerprint'], machine=self.store.machine,
                         decision='approved', scope='once', max_uses=1, ttl=600))
        self.assertIsNone(self.store.gate('run_command', dict(CMD)))
        self.assertEqual(self.store.gate('run_command', dict(CMD))['status'], 'approval_required')

    def test_expired_tampered_unknown_or_foreign_requests_cannot_be_approved(self):
        request_id = self.request()
        path = self.store.requests / (request_id + '.json')
        original = self.store.request(request_id)
        for change in ({'machine': 'other'}, {'args': dict(CMD, command='different')}, {'expires_at': self.now - 1}):
            path.write_text(json.dumps(original | change))
            result = self.store.approve_remote(request_id, self.code())
            self.assertIn(result['status'], ('invalid_approval_request', 'approval_expired'))
        self.assertEqual(self.store.approve_remote('../bad', self.code())['status'], 'invalid_approval_request')
        self.assertEqual(self.store.approve_remote('0000000000', self.code())['status'], 'invalid_approval_request')

    def test_write_grant_is_bound_to_content_and_options(self):
        args = dict(path='/etc/test', content='old', append=False, make_dirs=True, atomic=False,
                    expected_sha256=None, backup=False, mode=None)
        request_id = self.store.gate('write_file', args)['approval']['request_id']
        self.approve(request_id)
        for change in ({'content': 'new'}, {'append': True}, {'mode': '0600'}):
            self.assertEqual(self.store.gate('write_file', args | change)['status'], 'approval_required')
        self.assertIsNone(self.store.gate('write_file', args))

    def test_missing_or_corrupt_database_fails_closed_but_host_approval_works(self):
        request_id = self.request()
        self.store.remote.path.rename(self.store.remote.path.with_suffix('.saved'))
        self.assertEqual(self.store.approve_remote(request_id, self.code())['status'], 'remote_approval_unavailable')
        self.assertFalse(self.store.remote.path.exists())
        self.store.remote.path.write_bytes(b'corrupt database')
        self.store.remote.path.chmod(0o600)
        self.assertEqual(self.store.approve_remote(request_id, self.code())['status'], 'remote_approval_unavailable')
        pending = self.store.gate('run_command', dict(CMD, command='host recovery'))
        self.assertEqual(pending['status'], 'approval_required')
        self.assertEqual(pending['approval']['authenticator_status'], 'unavailable')
        self.assertNotIn('approve_with_authenticator', pending['approval'])
        self.store.decide(request_id, 'approved', writer=self.writer)
        self.assertIsNone(self.store.gate('run_command', dict(CMD)))

    def test_rotation_and_disable_invalidate_old_server_and_outstanding_grants(self):
        request_id = self.request()
        self.approve(request_id)
        new_secret = base64.b32encode(b'abcdefghijklmnopqrst').decode()
        with self.store._lock():
            self.store.remote.configure(new_secret)
        self.assertEqual(self.store.approve_remote(request_id, self.code())['status'], 'remote_approval_unavailable')
        self.assertEqual(self.make(new_secret).gate('run_command', dict(CMD))['status'], 'approval_required')
        with self.store._lock():
            self.store.remote.configure(None)
        self.assertEqual(self.make(new_secret).approve_remote(request_id, self.code(secret=new_secret))['status'],
                         'remote_approval_unavailable')

    def test_unsafe_database_mode_and_symlink_are_rejected(self):
        request_id = self.request()
        self.store.remote.path.chmod(0o644)
        self.assertEqual(self.store.approve_remote(request_id, self.code())['status'], 'remote_approval_unavailable')
        target = self.store.remote.path.with_suffix('.target')
        self.store.remote.path.rename(target)
        self.store.remote.path.symlink_to(target)
        self.assertEqual(self.store.approve_remote(request_id, self.code())['status'], 'remote_approval_unavailable')

    def test_secret_and_codes_are_not_stored_as_plaintext(self):
        request_id = self.request()
        self.approve(request_id)
        contents = self.store.remote.path.read_bytes()
        self.assertNotIn(SECRET.encode(), contents)
        with self.store.remote.connect() as db:
            columns = [row[1] for row in db.execute('PRAGMA table_info(grants)')]
        self.assertNotIn('code', columns)

    def test_failed_grant_commit_does_not_spend_counter_or_authorize_execution(self):
        request_id = self.request()
        with self.store.remote.connect() as db:
            db.execute("CREATE TRIGGER fail_grant BEFORE INSERT ON grants BEGIN SELECT RAISE(ABORT, 'disk failure'); END")
        self.assertEqual(self.store.approve_remote(request_id, self.code())['status'], 'remote_approval_unavailable')
        self.assertEqual(self.store.gate('run_command', CMD)['status'], 'approval_required')
        with self.store.remote.connect() as db:
            self.assertEqual(db.execute('SELECT counter FROM config').fetchone()[0], -1)
            db.execute('DROP TRIGGER fail_grant')
        self.approve(request_id)


class EnrollmentTests(unittest.TestCase):
    def test_qr_contains_provisioning_uri_and_matching_host_label(self):
        import importlib.util
        if importlib.util.find_spec('qrcode') is None:
            self.skipTest('QR dependency is validated in the isolated dependency environment.')
        import qrcode
        now = 1_800_000_000
        with tempfile.TemporaryDirectory() as tmp, patch.object(approvals, '_now', return_value=now), \
                patch.object(approvals.secrets, 'token_bytes', return_value=b'12345678901234567890'), \
                patch('builtins.input', return_value=totp(totp_key(SECRET), now // 30)), \
                patch.object(approvals, 'write_totp_env'), \
                patch.object(qrcode.QRCode, 'add_data', autospec=True, side_effect=qrcode.QRCode.add_data) as add_data, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            approvals.enroll_totp(Approvals(Path(tmp)), Path('/etc/example'), 'totp-setup')
            uri = add_data.call_args.args[1]
            self.assertIn('otpauth://totp/AI%20Bridge%3A', uri)
            self.assertIn('secret=' + SECRET, uri)
            self.assertIn('issuer=AI+Bridge', uri)
            self.assertIn('Manual key: ' + SECRET, output.getvalue())

    def test_bridge_and_nonterminal_setup_cannot_reveal_or_change_secrets(self):
        for command in ('totp-setup', 'totp-rotate', 'totp-disable'):
            with tempfile.TemporaryDirectory() as tmp, patch.object(approvals, 'enroll_totp') as enroll, \
                    patch.dict(os.environ, {'AI_BRIDGE_CHILD': '1'}), \
                    patch('sys.stdin.isatty', return_value=True), patch('sys.stdout.isatty', return_value=True), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(approvals.main(['--state-dir', tmp, command]), 2)
                enroll.assert_not_called()
            with patch('sys.stdin.isatty', return_value=False), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(approvals.main([command]), 2)

    def test_enrollment_verifies_before_saving_and_consumes_setup_code(self):
        now = 1_800_000_000
        with tempfile.TemporaryDirectory() as tmp, patch.object(approvals, '_now', return_value=now), \
                patch('builtins.input', return_value=totp(totp_key(SECRET), now // 30)), \
                patch.object(approvals, 'write_totp_env') as writer, patch.dict(sys.modules, {'qrcode': None}), \
                contextlib.redirect_stdout(io.StringIO()):
            store = Approvals(Path(tmp), machine='machine', totp_secret=SECRET)
            with patch.object(approvals.secrets, 'token_bytes', return_value=b'12345678901234567890'):
                approvals.enroll_totp(store, Path('/etc/example'), 'totp-setup')
            writer.assert_called_once_with(Path('/etc/example'), SECRET)
            request_id = store.gate('run_command', CMD)['approval']['request_id']
            self.assertEqual(store.approve_remote(request_id, totp(totp_key(SECRET), now // 30))['status'],
                             'invalid_or_expired_code')

    def test_bad_setup_codes_leave_existing_configuration_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp, patch('builtins.input', return_value='bad'), \
                patch.object(approvals, 'write_totp_env') as writer, patch.dict(sys.modules, {'qrcode': None}), \
                contextlib.redirect_stdout(io.StringIO()):
            store = Approvals(Path(tmp))
            with self.assertRaises(ValueError):
                approvals.enroll_totp(store, Path('/etc/example'), 'totp-setup')
            writer.assert_not_called()
            self.assertFalse(store.remote.path.exists())

    def test_env_writer_preserves_other_settings_mode_and_removes_duplicate_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'bridge.env'
            path.write_text('AI_BRIDGE_TOKEN=keep-me\nAI_BRIDGE_SUDO_APPROVAL=0\n'
                            'AI_BRIDGE_SUDO_TOTP_SECRET=old\n AI_BRIDGE_SUDO_TOTP_SECRET=duplicate\nCUSTOM="literal"\n')
            path.chmod(0o640)
            code = ENV_WRITER.replace('st_uid != 0', 'st_uid != ' + str(os.getuid()))
            for secret in (SECRET, ''):
                result = subprocess.run([sys.executable, '-I', '-c', code, str(path)],
                                        input=secret, text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                content = path.read_text()
                self.assertEqual(content.count('AI_BRIDGE_SUDO_TOTP_SECRET='), 1)
                self.assertIn('AI_BRIDGE_TOKEN=keep-me\nAI_BRIDGE_SUDO_APPROVAL=0\n', content)
                self.assertIn('CUSTOM="literal"\n', content)
                self.assertEqual(path.stat().st_mode & 0o777, 0o640)
                self.assertIn('AI_BRIDGE_SUDO_TOTP_SECRET=' + secret + '\n', content)

    def test_enrollment_rotation_disable_and_state_status(self):
        now = 1_800_000_000
        with tempfile.TemporaryDirectory() as tmp, patch.object(approvals, '_now', return_value=now), \
                patch.object(approvals.secrets, 'token_bytes', return_value=b'12345678901234567890'), \
                patch('builtins.input', return_value=totp(totp_key(SECRET), now // 30)), \
                patch.object(approvals, 'write_totp_env') as writer, patch.dict(sys.modules, {'qrcode': None}), \
                contextlib.redirect_stdout(io.StringIO()):
            store = Approvals(Path(tmp))
            path = Path('/etc/example')
            approvals.enroll_totp(store, path, 'totp-setup')
            self.assertEqual(store.remote_status(), 'ready')
            with self.assertRaisesRegex(ValueError, 'already enrolled'):
                approvals.enroll_totp(store, path, 'totp-setup')
            approvals.enroll_totp(store, path, 'totp-rotate')
            approvals.enroll_totp(store, path, 'totp-disable')
            self.assertEqual(store.remote_status(), 'off')
            writer.assert_called_with(path, '')

    def test_env_writer_refuses_world_readable_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'bridge.env'
            path.write_text('AI_BRIDGE_TOKEN=keep-me\n')
            path.chmod(0o644)
            code = ENV_WRITER.replace('st_uid != 0', 'st_uid != ' + str(os.getuid()))
            result = subprocess.run([sys.executable, '-I', '-c', code, str(path)],
                                    input=SECRET, text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn(SECRET, path.read_text() + result.stdout + result.stderr)
            self.assertEqual(path.read_text(), 'AI_BRIDGE_TOKEN=keep-me\n')

    def test_secret_does_not_travel_in_sudo_arguments(self):
        with patch.object(approvals.subprocess, 'run') as run:
            run.return_value.returncode = 0
            approvals.write_totp_env(Path('/etc/example'), SECRET)
            self.assertNotIn(SECRET, repr(run.call_args.args))
            self.assertEqual(run.call_args.kwargs['input'], SECRET)


class RemoteToolTests(unittest.TestCase):
    def test_tool_verifies_then_original_call_executes_and_never_audits_code(self):
        from helpers import server
        with tempfile.TemporaryDirectory() as tmp:
            store = Approvals(Path(tmp), Path(tmp) / 'host', owner=os.getuid(), totp_secret=SECRET)
            with store._lock():
                store.remote.configure(SECRET)
            code = totp(totp_key(SECRET), int(approvals._now() // 30))
            with patch.object(server, 'APPROVALS', store), patch.object(server.CFG, 'sudo_approval', True), \
                    patch('ai_bridge.server.os.geteuid', return_value=os.getuid()), \
                    patch.object(server, 'audit') as audit, patch.object(server, 'run_sync') as execute:
                if os.getuid() == 0:
                    self.skipTest('Root-service tools intentionally bypass approval; store behavior is covered above.')
                execute.return_value = dict(exit_code=0, stdout='0', stderr='', timed_out=False)
                request_id = json.loads(server.run_command('id -u', cwd='/tmp', sudo=True))['approval']['request_id']
                result = json.loads(server.approve_privileged_request(request_id, code))
                self.assertEqual(result['status'], 'approved')
                execute.assert_not_called()
                self.assertNotIn(code, repr(audit.call_args_list))
                self.assertNotIn(SECRET, repr(audit.call_args_list))
                self.assertEqual(json.loads(server.run_command('id -u', cwd='/tmp', sudo=True))['stdout'], '0')
                self.assertEqual(json.loads(server.run_command('id -u', cwd='/tmp', sudo=True))['status'], 'approval_required')
                execute.assert_called_once()

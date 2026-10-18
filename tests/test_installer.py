"""User-visible update steps, enrollment preservation, restart progress and test totals."""
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

import deploy
import installer


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.release = self.root / 'releases/new'
        self.release.mkdir(parents=True)
        (self.release / 'RELEASE.json').write_text(json.dumps({'version': '3.2.1'}))
        (self.root / 'current').symlink_to(self.release)
        self.env = self.root / 'bridge.env'
        self.env.write_text('AI_BRIDGE_TOKEN=fake-token-never-print\nAI_BRIDGE_SUDO_TOTP_SECRET=fake-key-never-print\n'
                            'AI_BRIDGE_STATE_DIR=/custom/state\n')
        self.flow = installer.Installer(source=self.root / 'source', root=self.root, env_file=self.env)

    def test_update_runs_all_steps_and_reports_verified_completion(self):
        (self.release / 'test-summary.json').write_text(json.dumps(dict(passed=9, failed=0, errors=0, skipped=1)))
        with patch.object(self.flow, 'authenticate'), patch.object(deploy.versioning, 'check', return_value='3.2.1'), \
                patch.object(deploy, 'stage', return_value=self.release), patch.object(installer, 'run_command') as command, \
                patch.object(self.flow, 'enroll') as enroll, \
                patch.object(self.flow, 'restart', return_value={'bridge_version': '3.2.1', 'sudo_authenticator': 'ready'}), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.flow.update()
        command.assert_called_once_with(['bash', str(self.flow.source / 'install.sh'), '--activate', str(self.release)],
                                        'Activating release')
        enroll.assert_called_once_with(offer=True)
        for step in range(1, 6):
            self.assertIn(f'[{step}/5]', output.getvalue())
        self.assertIn('COMPLETE', output.getvalue())
        self.assertIn('9 passed, 0 failed, 0 errors, 1 skipped', output.getvalue())
        self.assertIn('Version: 3.2.1', output.getvalue())

    def test_existing_authenticator_is_kept_without_new_prompt_or_code(self):
        with patch.object(installer.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '{"authenticator":"ready"}')) as status, \
                patch.object(installer, 'run_command') as command, patch('builtins.input') as prompt, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.flow.enroll(offer=True)
        prompt.assert_not_called()
        command.assert_not_called()
        self.assertIn('--state-dir', status.call_args.args[0])
        self.assertIn('/custom/state', status.call_args.args[0])
        self.assertIn('Keeping your existing setup', output.getvalue())
        self.assertNotIn('fake-key-never-print', output.getvalue())

    def test_enrollment_can_be_skipped_and_uses_visible_terminal_when_selected(self):
        self.env.write_text('AI_BRIDGE_TOKEN=fake-token-never-print\n')
        with patch.object(installer.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '{"authenticator":"off"}')), \
                patch.object(installer, 'run_command') as command, patch('builtins.input', return_value='n'), \
                contextlib.redirect_stdout(io.StringIO()):
            self.flow.enroll(offer=True)
            command.assert_not_called()
            self.flow.enroll()
            self.assertEqual(command.call_args.args[0][-1], 'totp-setup')
            self.assertTrue(command.call_args.kwargs['interactive'])

    def test_restart_does_not_accept_old_process_and_reports_service_states(self):
        current = {'ActiveState': 'active', 'SubState': 'running', 'MainPID': '11'}
        stopping = dict(current, ActiveState='deactivating', SubState='stop-sigterm')
        started = dict(current, MainPID='22', ExecStart=str(self.release / 'venv/bin/python'))
        with patch.object(installer, 'service_state', side_effect=[current, current, stopping, started]), \
                patch.object(installer, 'live_info', return_value={'bridge_version': '3.2.1'}) as check, \
                patch.object(installer, 'run_command') as command, patch.object(installer.time, 'sleep'), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            info = self.flow.restart()
        self.assertEqual(info['bridge_version'], '3.2.1')
        check.assert_called_once()
        self.assertIn('--no-block', command.call_args.args[0])
        self.assertIn('deactivating/stop-sigterm', output.getvalue())
        self.assertIn('elapsed', output.getvalue())

    def test_restart_fails_clearly_on_timeout_or_failed_service(self):
        clock = [0]
        def advance(seconds):
            clock[0] += seconds
        with patch.object(installer, 'service_state', return_value={'MainPID': '11', 'ActiveState': 'active', 'SubState': 'running'}), \
                patch.object(installer, 'run_command'), patch.object(installer.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(installer.time, 'sleep', side_effect=advance), contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaisesRegex(RuntimeError, 'Restart timed out'):
                self.flow.restart(timeout=4)
            self.assertNotIn('COMPLETE', output.getvalue())
        with patch.object(installer, 'service_state', return_value={'ActiveState': 'failed'}), \
                patch.object(installer, 'run_command'), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, 'failed to start'):
                self.flow.restart()

    def test_setup_failure_never_restarts_or_reports_success(self):
        with patch.object(self.flow, 'authenticate'), patch.object(self.flow, 'enroll', side_effect=RuntimeError('Setup cancelled')), \
                patch.object(self.flow, 'restart') as restart, contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(RuntimeError):
                self.flow.setup()
        restart.assert_not_called()
        self.assertNotIn('COMPLETE', output.getvalue())

    def test_terminal_only_and_main_failure_names_current_step(self):
        with patch.dict(os.environ, {'AI_BRIDGE_CHILD': '1'}), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(installer.main(['update']), 2)
        with contextlib.redirect_stdout(io.StringIO()) as output, \
                patch.dict(os.environ, {'AI_BRIDGE_CHILD': ''}), patch('sys.stdin.isatty', return_value=True), \
                patch('sys.stdout.isatty', return_value=True), patch.object(installer, 'Installer', return_value=self.flow), \
                patch.object(self.flow, 'update', side_effect=RuntimeError('Test failure')):
            self.flow.current_step = 'Building the new release and running tests'
            self.assertEqual(installer.main(['update']), 1)
        self.assertIn('FAILED or cancelled during: Building', output.getvalue())
        self.assertIn('Test failure', output.getvalue())


class TestProgressTests(unittest.TestCase):
    def test_counts_results_current_test_and_summary_with_failures_and_skips(self):
        class Samples(unittest.TestCase):
            def test_pass(self):
                pass
            def test_failure(self):
                with self.subTest(part='first'):
                    self.fail('sample failure')
                with self.subTest(part='second'):
                    self.fail('another failure')
            def test_error(self):
                raise RuntimeError('sample error')
            @unittest.skip('sample skip')
            def test_skip(self):
                pass
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(Samples)
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stderr(io.StringIO()) as output:
            path = Path(tmp) / 'summary.json'
            self.assertEqual(deploy.run_tests(suite=suite, summary_path=path), 1)
            summary = json.loads(path.read_text())
        self.assertEqual({k: summary[k] for k in ('total', 'completed', 'passed', 'failed', 'errors', 'skipped')},
                         dict(total=4, completed=4, passed=1, failed=1, errors=1, skipped=1))
        for text in ('[1/4] RUN', '[4/4]', 'PASS', 'FAIL', 'ERROR', 'SKIP', 'sample failure', 'sample error', 'Tests complete: 4/4'):
            self.assertIn(text, output.getvalue())

    def test_empty_discovery_cannot_mark_release_as_tested(self):
        with contextlib.redirect_stderr(io.StringIO()) as output:
            self.assertEqual(deploy.run_tests(suite=unittest.TestSuite()), 1)
        self.assertIn('no tests were discovered', output.getvalue())

    def test_silent_command_and_closed_output_both_print_heartbeat(self):
        for script in ('import time; time.sleep(.2)', 'import os,time; os.close(1); os.close(2); time.sleep(.2)'):
            with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stderr(io.StringIO()) as output:
                deploy.run_logged([sys.executable, '-c', script], Path(tmp) / 'build.log',
                                  step='Installing dependencies', heartbeat=.05)
                self.assertIn('Installing dependencies: still running', output.getvalue())

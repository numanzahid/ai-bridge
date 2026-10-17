"""Disposable management fixtures; never touch native user stores or live agents."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from ai_bridge.core import Config, Jobs
from ai_bridge import agents, cli
from ai_bridge.management import Sessions, Tasks, runtime, start_agent, stop_agent
from ai_bridge.execution import Descendants, process_identity

SID = '11111111-2222-4333-8444-555555555555'


class ManagementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='aib-management-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cfg = Config(token='', state_dir=self.root / 'state', tasks_dir=self.root / 'tasks')
        self.cfg.state_dir.mkdir(mode=0o700)
        (self.cfg.state_dir / 'jobs').mkdir(mode=0o700)
        self.jobs = Jobs(self.cfg)
        self.tasks = Tasks(self.cfg)
        self.store = self.root / 'session.jsonl'
        self.store.write_text('{}')
        self.info = {'session_id': SID, 'cwd': str(self.root), 'resumable': True,
                     'storage_path': str(self.store), 'title': 'native', 'mode': 'noninteractive'}

    def fake(self, body, name='fake-agent'):
        path = self.root / name
        path.write_text('#!' + sys.executable + '\n' + body)
        path.chmod(0o700)
        return str(path)

    def turn(self, **kwargs):
        with patch.object(agents, 'find_binary', return_value='/fake'), \
             patch.object(agents, 'build_argv', return_value=['/fake']), \
             patch.object(self.jobs, 'start', side_effect=lambda *a, **k: {'cwd': a[1], **k['extra']}):
            return start_agent(self.cfg, self.jobs, 'codex', 'test', **kwargs)

    def test_default_unique_private_tasks_and_reuse(self):
        a, b = self.turn(), self.turn()
        self.assertNotEqual(a['task_id'], b['task_id'])
        for row in (a, b):
            self.assertEqual(Path(row['cwd']).stat().st_mode & 0o777, 0o700)
            self.assertEqual(Path(row['cwd']).parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.turn(task_id=a['task_id'])['cwd'], a['cwd'])
        self.assertEqual(len(self.tasks.list()), 2)
        self.assertEqual(self.tasks.root.stat().st_mode & 0o777, 0o700)

    def test_explicit_project_not_inferred_or_moved(self):
        row = self.turn(cwd=str(self.root))
        self.assertEqual(row['workspace_type'], 'project')
        self.assertIsNone(row['task_id'])
        self.assertFalse(self.tasks.root.exists())

    def test_resume_original_workspace_and_task_conflicts(self):
        task = self.tasks.create()
        with patch.object(agents, 'resume_cwd', return_value=str(self.root)):
            self.assertEqual(self.turn(session_id=SID)['cwd'], str(self.root))
            with self.assertRaisesRegex(ValueError, 'conflicts'):
                self.turn(session_id=SID, task_id=task['task_id'])
        with self.assertRaisesRegex(ValueError, 'conflicts'):
            self.turn(cwd=str(self.root), task_id=task['task_id'])

    def test_busy_and_shared_workspace_notice(self):
        task = self.tasks.create()
        with patch.object(self.jobs, 'running_for_session', return_value={'id': 'busy-job'}), \
             patch.object(agents, 'resume_cwd', return_value=task['workspace']):
            with self.assertRaisesRegex(ValueError, 'busy'):
                self.turn(session_id=SID)
        peer = {'id': 'peer-job', 'status': 'running', 'cwd': task['workspace']}
        with patch.object(self.jobs, 'list', return_value=[peer]):
            result = self.turn(task_id=task['task_id'])
        self.assertEqual(result['workspace_peers'], ['peer-job'])
        self.assertIn('coordinate', result['workspace_note'])

    def test_task_traversal_symlink_and_permissions_refused(self):
        for bad in ('../outside', 'task-no', '/tmp/task'):
            with self.assertRaises(ValueError):
                self.tasks.inspect(bad)
        task = self.tasks.create()
        workspace = Path(task['workspace'])
        workspace.rmdir()
        workspace.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            self.tasks.inspect(task['task_id'])
        workspace.unlink()
        workspace.mkdir(mode=0o755)
        with self.assertRaisesRegex(ValueError, 'private'):
            self.tasks.inspect(task['task_id'])

    def test_root_symlink_and_metadata_tampering_refused(self):
        target = self.root / 'other'
        target.mkdir(mode=0o700)
        self.tasks.root.symlink_to(target, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.tasks.create()
        self.tasks.root.unlink()
        task = self.tasks.create()
        marker = Path(task['workspace']).parent / '.task.json'
        marker.unlink()
        marker.symlink_to(self.store)
        with self.assertRaises(OSError):
            self.tasks.inspect(task['task_id'])

    def test_special_metadata_refused_without_blocking(self):
        task = self.tasks.create()
        marker = Path(task['workspace']).parent / '.task.json'
        marker.unlink()
        os.mkfifo(marker, 0o600)
        with self.assertRaisesRegex(ValueError, 'metadata'):
            self.tasks.inspect(task['task_id'])

    def test_cleanup_dry_run_and_no_follow(self):
        task = self.tasks.create()
        outside = self.root / 'keep'
        outside.write_text('keep')
        (Path(task['workspace']) / 'link').symlink_to(outside)
        result = self.tasks.cleanup(task['task_id'], self.jobs)
        self.assertTrue(result['dry_run'])
        self.assertTrue(Path(task['workspace']).exists())
        with patch('ai_bridge.management.observed_task_processes', return_value=[]):
            self.tasks.cleanup(task['task_id'], self.jobs, False)
        self.assertTrue(outside.exists())
        self.assertFalse(Path(task['workspace']).exists())

    def test_cleanup_uncertain_or_unmanaged_nested_job_refused(self):
        task = self.tasks.create()
        for status in ('running', 'cleanup_incomplete', 'lost'):
            with patch.object(self.jobs, 'list', return_value=[{'id': 'test-job', 'status': status,
                          'cwd': str(Path(task['workspace']) / 'nested')}]), self.assertRaisesRegex(ValueError, 'refused'):
                self.tasks.cleanup(task['task_id'], self.jobs, False)
        self.assertTrue(Path(task['workspace']).exists())

    def test_cleanup_observed_native_process_refused(self):
        task = self.tasks.create()
        proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(10)'], cwd=task['workspace'])
        try:
            with self.assertRaisesRegex(ValueError, 'observed_native_process'):
                self.tasks.cleanup(task['task_id'], self.jobs, False)
        finally:
            proc.terminate()
            proc.wait()

    def cleanup_observations(self):
        return [{'pid': 101, 'status': 'uninspectable_process'},
                {'pid': 202, 'status': 'uninspectable_process'}]

    def test_uninspectable_cleanup_defaults_and_explicit_preview(self):
        task = self.tasks.create()
        with patch('ai_bridge.management.observed_task_processes', return_value=self.cleanup_observations()):
            ordinary = self.tasks.cleanup(task['task_id'], self.jobs)
            self.assertFalse(ordinary['cleanup_allowed'])
            self.assertEqual(len(ordinary['blockers']), 2)
            with self.assertRaisesRegex(ValueError, 'refused'):
                self.tasks.cleanup(task['task_id'], self.jobs, False, confirmed=True)
            preview = self.tasks.cleanup(task['task_id'], self.jobs, allow_uninspectable=True)
            self.assertTrue(preview['cleanup_allowed'])
            self.assertEqual(preview['blockers'], [])
            self.assertEqual(preview['ignored_uninspectable_pids'], [101, 202])
            self.assertIn('HIGH RISK', preview['risk'])
            self.assertTrue(preview['dry_run'])
            self.assertTrue(Path(task['workspace']).exists())
            with self.assertRaisesRegex(ValueError, '--yes'):
                self.tasks.cleanup(task['task_id'], self.jobs, False, allow_uninspectable=True)

    def test_cli_uninspectable_override_requires_confirmation_and_preserves_native_history(self):
        task = self.tasks.create()
        native = self.root / '.codex' / 'saved.jsonl'
        native.parent.mkdir()
        native.write_text('native history fixture')
        original = native.read_bytes()
        prefix = ['tasks', 'cleanup', task['task_id']]
        with patch('ai_bridge.management.observed_task_processes', return_value=self.cleanup_observations()), \
             patch('os.kill') as kill:
            args = cli.parser().parse_args(prefix + ['--allow-uninspectable'])
            with self.assertRaisesRegex(ValueError, '--yes'):
                cli.dispatch(args, self.cfg, self.jobs)
            preview = cli.dispatch(cli.parser().parse_args(prefix + ['--dry-run', '--allow-uninspectable']), self.cfg, self.jobs)
            self.assertTrue(preview['cleanup_allowed'])
            self.assertTrue(Path(task['workspace']).exists())
            result = cli.dispatch(cli.parser().parse_args(prefix + ['--yes', '--allow-uninspectable']), self.cfg, self.jobs)
            self.assertEqual(result['ignored_uninspectable_pids'], [101, 202])
            self.assertIn('HIGH RISK', result['risk'])
            self.assertTrue(result['removed'])
            self.assertFalse(result['native_history_deleted'])
            self.assertFalse(Path(task['workspace']).exists())
            self.assertEqual(native.read_bytes(), original)
            kill.assert_not_called()

    def test_uninspectable_override_still_blocks_known_native_process(self):
        task = self.tasks.create()
        observations = self.cleanup_observations() + [{'pid': 303, 'status': 'observed_native_process'}]
        with patch('ai_bridge.management.observed_task_processes', return_value=observations):
            preview = self.tasks.cleanup(task['task_id'], self.jobs, allow_uninspectable=True)
            self.assertFalse(preview['cleanup_allowed'])
            self.assertEqual(preview['ignored_uninspectable_pids'], [101, 202])
            self.assertEqual(preview['blockers'], [{'pid': 303, 'status': 'observed_native_process'}])
            with self.assertRaisesRegex(ValueError, 'observed_native_process') as caught:
                self.tasks.cleanup(task['task_id'], self.jobs, False, allow_uninspectable=True, confirmed=True)
            self.assertIn('ignored_uninspectable_pids', str(caught.exception))
            self.assertIn('HIGH RISK', str(caught.exception))
            self.assertTrue(Path(task['workspace']).exists())

    def test_uninspectable_override_never_filters_managed_jobs(self):
        task = self.tasks.create()
        for status in ('running', 'cleanup_incomplete', 'lost'):
            job = {'id': 'blocked-job', 'status': status, 'task_id': task['task_id']}
            with self.subTest(status=status), patch.object(self.jobs, 'list', return_value=[job]), \
                 patch('ai_bridge.management.observed_task_processes', return_value=self.cleanup_observations()):
                preview = self.tasks.cleanup(task['task_id'], self.jobs, allow_uninspectable=True)
                self.assertFalse(preview['cleanup_allowed'])
                self.assertEqual(preview['blockers'], [{'job_id': job['id'], 'status': status}])
                with self.assertRaisesRegex(ValueError, 'blocked-job'):
                    self.tasks.cleanup(task['task_id'], self.jobs, False, allow_uninspectable=True, confirmed=True)
        self.assertTrue(Path(task['workspace']).exists())

    def test_uninspectable_override_never_filters_mounts_or_other_observations(self):
        task = self.tasks.create()
        for status in ('mounted_subtree', 'uninspectable_process'):
            # Even a mount check returning the same text is never eligible.
            with self.subTest(status=status), \
                 patch('ai_bridge.management.observed_task_processes', return_value=self.cleanup_observations()), \
                 patch('ai_bridge.management.task_mounts', return_value=[{'path': task['workspace'], 'status': status}]):
                preview = self.tasks.cleanup(task['task_id'], self.jobs, allow_uninspectable=True)
                self.assertFalse(preview['cleanup_allowed'])
                with self.assertRaisesRegex(ValueError, 'refused'):
                    self.tasks.cleanup(task['task_id'], self.jobs, False, allow_uninspectable=True, confirmed=True)
        with patch('ai_bridge.management.observed_task_processes', return_value=[{'pid': 404, 'status': 'uninspectable_process_extra'}]):
            self.assertFalse(self.tasks.cleanup(task['task_id'], self.jobs, allow_uninspectable=True)['cleanup_allowed'])

    def test_uninspectable_override_rejects_changed_metadata_before_removal(self):
        task = self.tasks.create()
        marker = Path(task['workspace']).parent / '.task.json'
        def observations(workspace):
            metadata = json.loads(marker.read_text())
            metadata['created'] += 1
            marker.write_text(json.dumps(metadata))
            return self.cleanup_observations()
        with patch('ai_bridge.management.observed_task_processes', side_effect=observations):
            with self.assertRaisesRegex(ValueError, 'state changed'):
                self.tasks.cleanup(task['task_id'], self.jobs, False, allow_uninspectable=True, confirmed=True)
        self.assertTrue(Path(task['workspace']).exists())

    def test_uninspectable_override_still_rejects_symlinked_or_unowned_task(self):
        task = self.tasks.create()
        marker = Path(task['workspace']).parent / '.task.json'
        metadata = json.loads(marker.read_text())
        metadata['owner_uid'] += 1
        marker.write_text(json.dumps(metadata))
        with self.assertRaisesRegex(ValueError, 'ownership'):
            self.tasks.cleanup(task['task_id'], self.jobs, False, allow_uninspectable=True, confirmed=True)
        metadata['owner_uid'] -= 1
        marker.write_text(json.dumps(metadata))
        workspace = Path(task['workspace'])
        workspace.rmdir()
        workspace.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            self.tasks.cleanup(task['task_id'], self.jobs, False, allow_uninspectable=True, confirmed=True)

    def test_alias_archive_reversible_native_unchanged(self):
        before = self.store.read_bytes()
        sessions = Sessions(self.cfg, self.jobs)
        with patch.object(agents, 'session_info', return_value=self.info):
            sessions.overlay('codex', SID, alias='Display only')
            row = sessions.enrich('codex', [self.info])[0]
            self.assertEqual(row['title'], 'Display only')
            self.assertEqual(row['native_title'], 'native')
            sessions.overlay('codex', SID, archived=True)
            self.assertEqual(sessions.enrich('codex', [self.info]), [])
            self.assertTrue(sessions.enrich('codex', [self.info], True)[0]['archived'])
            sessions.overlay('codex', SID, archived=False)
        self.assertEqual(self.store.read_bytes(), before)

    def test_sudo_not_available_through_agent_or_task_metadata(self):
        with self.assertRaises(ValueError):
            self.turn(timeout=0)
        with self.assertRaises(ValueError):
            self.turn(timeout=604801)
        with self.assertRaises(ValueError):
            cli.target(SID, None)
        self.assertEqual(cli.target('codex:' + SID, None), ('codex', SID))
        with self.assertRaises(ValueError):
            cli.target('codex:' + SID, 'claude')

    def test_native_open_preflight_does_not_rewrite(self):
        sessions = Sessions(self.cfg, self.jobs)
        help_result = {'stdout': 'SESSION_ID --include-non-interactive', 'stderr': '',
                       'exit_code': 0, 'timed_out': False, 'stdout_truncated': False}
        with patch.object(agents, 'session_info', return_value=self.info), \
             patch.object(agents, 'resume_cwd', return_value=str(self.root)), \
             patch.object(agents, 'find_binary', return_value='/fake'), \
             patch('ai_bridge.execution.probe', return_value=help_result):
            plan = sessions.interactive('codex', SID, 'promote')
            self.assertEqual(plan['argv'], ['/fake', 'resume', '--include-non-interactive', SID])
            self.assertFalse(plan['history_rewritten'])
            self.assertFalse(plan['verified_execution'])
            with self.assertRaisesRegex(ValueError, 'unsupported'):
                sessions.interactive('cursor', SID, 'fork')
            help_result['stdout'] = 'resume'
            with self.assertRaisesRegex(ValueError, 'unsupported'):
                sessions.interactive('codex', SID)
        self.assertEqual(self.store.read_text(), '{}')

    def test_non_tty_open_refused_before_preflight(self):
        args = cli.parser().parse_args(['sessions', 'open', 'codex:' + SID])
        with patch('sys.stdin.isatty', return_value=False), patch.object(Sessions, 'interactive') as opening:
            with self.assertRaisesRegex(ValueError, 'terminal'):
                cli.dispatch(args, self.cfg, self.jobs)
            opening.assert_not_called()

    def test_managed_runtime_identity_and_command_refusal(self):
        meta = {'id': 'test-job', 'kind': 'agent', 'status': 'running', 'pid': os.getpid(),
                'identity': process_identity(os.getpid()), 'agent': 'codex'}
        with patch.object(self.jobs, 'load', return_value=meta):
            row = runtime(self.jobs, 'test-job')
            self.assertTrue(row['runner_identity_verified'])
            self.assertIn('rss_bytes', row['runner_resources'])
            meta['identity'] = None
            self.assertFalse(runtime(self.jobs, 'test-job')['runner_identity_verified'])
            with self.assertRaises(ValueError):
                stop_agent(self.jobs, 'test-job')
            meta['kind'] = 'command'
            with self.assertRaises(ValueError):
                stop_agent(self.jobs, 'test-job')

    def test_runtime_new_job_session_is_observed_without_poll(self):
        meta = {'id': 'test-job', 'kind': 'agent', 'agent': 'codex', 'status': 'running', 'task_id': 'test-task'}
        with patch.object(self.jobs, 'list', return_value=[meta]), \
             patch.object(self.jobs, 'output_info', return_value={'parsed': {'session_id': SID}}):
            row = Sessions(self.cfg, self.jobs).enrich('codex', [self.info])[0]
        self.assertEqual(row['status'], 'active_job')
        self.assertEqual(row['task_id'], 'test-task')

    def test_timeout_survives_new_jobs_instance(self):
        meta = self.jobs.start([sys.executable, '-c', 'import time; time.sleep(10)'], str(self.root),
                               kind='agent', extra={'agent': 'codex', 'timeout': 1})
        result = Jobs(self.cfg).wait(meta['id'], 6)
        self.assertNotEqual(result['status'], 'running')
        self.jobs.load(meta['id'])
        self.assertTrue(result['timed_out'])
        self.assertFalse(result['cleanup']['complete'])
        self.assertIn('coverage', result['cleanup'])

    def test_stop_verified_agent_without_touching_other_process(self):
        other = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'])
        try:
            meta = self.jobs.start([sys.executable, '-c', 'import time; time.sleep(20)'], str(self.root),
                                   kind='agent', extra={'agent': 'codex'})
            result = stop_agent(self.jobs, meta['id'])
            self.assertNotEqual(result['status'], 'running')
            self.assertFalse(result['cleanup']['complete'])
            self.assertIsNone(other.poll())
        finally:
            other.terminate()
            other.wait()

    def test_lineage_pid_reuse_and_scope_change_not_signalled(self):
        lineage = Descendants(os.getpid())
        lineage.known[99999999] = process_identity(os.getpid())
        with patch('ai_bridge.execution.signal_verified') as send:
            lineage.cancel()
            send.assert_not_called()
        lineage.known = {os.getpid(): process_identity(os.getpid())}
        lineage.cgroup = 'different'
        with patch.object(lineage, 'sample', return_value={}), patch('ai_bridge.execution.signal_verified') as send:
            result = lineage.cancel()
            self.assertIn(os.getpid(), result['unsignalled_pids'])
            send.assert_not_called()

    def test_cli_tokenless_end_to_end_fake_agent_and_overlay(self):
        codex = self.root / 'codex' / 'sessions'
        codex.mkdir(parents=True)
        native = codex / ('rollout-test-' + SID + '.jsonl')
        native.write_text(json.dumps({'type': 'session_meta', 'payload': {
            'id': SID, 'cwd': str(self.root), 'source': 'exec', 'thread_source': 'user'}}))
        fake = self.fake('import json,sys\nif "--help" in sys.argv:\n print("SESSION_ID --include-non-interactive"); sys.exit()\nprint(json.dumps({"type":"thread.started","thread_id":"' + SID + '"}))\n')
        env = dict(os.environ, AI_BRIDGE_STATE_DIR=str(self.cfg.state_dir),
                   AI_BRIDGE_TASKS_DIR=str(self.cfg.tasks_dir), CODEX_HOME=str(codex.parent),
                   AI_BRIDGE_CODEX_BIN=fake)
        env.pop('AI_BRIDGE_TOKEN', None)
        def run(*args, ok=True):
            r = subprocess.run([sys.executable, '-m', 'ai_bridge', *args], env=env,
                               capture_output=True, text=True, timeout=15)
            self.assertEqual(r.returncode, 0 if ok else 1, r.stderr)
            return json.loads(r.stdout if ok else r.stderr)
        task = run('tasks', 'create')
        self.assertEqual(run('tasks', 'list')[0]['task_id'], task['task_id'])
        run('tasks', 'cleanup', task['task_id'], ok=False)
        self.assertTrue(run('tasks', 'cleanup', task['task_id'], '--dry-run')['dry_run'])
        preview = run('tasks', 'cleanup', task['task_id'], '--dry-run')
        run('tasks', 'cleanup', task['task_id'], '--yes', ok=preview['cleanup_allowed'])
        plan = run('sessions', 'open', 'codex:' + SID, '--dry-run')
        self.assertIn('--include-non-interactive', plan['argv'])
        run('sessions', 'rename', 'codex:' + SID, 'A display title')
        self.assertEqual(run('sessions', 'list', '--agent', 'codex', '--mode', 'noninteractive')['total'], 1)
        run('sessions', 'archive', 'codex:' + SID)
        self.assertEqual(run('sessions', 'list', '--agent', 'codex')['total'], 0)
        self.assertEqual(run('sessions', 'list', '--agent', 'codex', '--all')['total'], 1)
        run('sessions', 'unarchive', 'codex:' + SID)
        started = run('sessions', 'start', '--agent', 'codex', 'test')
        finished = self.jobs.wait(started['id'], 5)
        self.assertEqual(finished['status'], 'done')
        self.assertTrue(started['task_id'])
        self.assertEqual(run('agents', 'inspect', started['id'])['status'], 'done')
        continued = run('sessions', 'continue', 'codex:' + SID, 'continue')
        self.assertEqual(continued['cwd'], str(self.root))
        self.jobs.wait(continued['id'], 5)
        self.assertEqual(json.loads(native.read_text())['payload']['source'], 'exec')

    def test_busy_check_observes_unpolled_session_beyond_200(self):
        job = {'id': 'unpolled-job', 'agent': 'codex', 'status': 'running', 'session_id': None}
        rows = [{'id': f'old-{i}', 'status': 'done', 'agent': 'codex'} for i in range(220)] + [job]
        with patch.object(self.jobs, 'list', return_value=rows) as listing, \
             patch.object(self.jobs, 'output_info', return_value={'parsed': {'session_id': SID}}):
            self.assertEqual(self.jobs.running_for_session('codex', SID)['id'], job['id'])
            listing.assert_called_once_with(limit=None, kind='agent')

    def test_previous_boot_task_job_not_active_but_same_boot_lost_blocks(self):
        task = self.tasks.create()
        job = {'id': 'lost-job', 'status': 'lost', 'task_id': task['task_id'], 'identity_state': 'previous_boot'}
        with patch.object(self.jobs, 'list', return_value=[job]), \
             patch('ai_bridge.management.observed_task_processes', return_value=[]):
            self.assertTrue(self.tasks.cleanup(task['task_id'], self.jobs)['cleanup_allowed'])
            job['identity_state'] = 'not_running_or_changed'
            self.assertFalse(self.tasks.cleanup(task['task_id'], self.jobs)['cleanup_allowed'])

    def test_start_cleanup_cross_thread_lock(self):
        import concurrent.futures
        import threading
        task = self.tasks.create()
        entered, release = threading.Event(), threading.Event()
        rows = []
        def start(*a, **k):
            entered.set()
            release.wait(3)
            rows.append({'id': 'active-job', 'status': 'running', 'task_id': task['task_id']})
            return {'id': 'active-job'}
        with patch.object(agents, 'find_binary', return_value='/fake'), \
             patch.object(agents, 'build_argv', return_value=['/fake']), \
             patch.object(self.jobs, 'start', side_effect=start), \
             patch.object(self.jobs, 'list', side_effect=lambda **kw: list(rows)), \
             patch('ai_bridge.management.observed_task_processes', return_value=[]), \
             concurrent.futures.ThreadPoolExecutor(2) as pool:
            launching = pool.submit(start_agent, self.cfg, self.jobs, 'codex', 'test', task_id=task['task_id'])
            self.assertTrue(entered.wait(2))
            cleanup = pool.submit(self.tasks.cleanup, task['task_id'], self.jobs, True)
            time.sleep(.1)
            self.assertFalse(cleanup.done())
            release.set()
            launching.result(3)
            self.assertFalse(cleanup.result(3)['cleanup_allowed'])

    def test_saved_lineage_child_surviving_leader_exit_is_visible_and_stoppable(self):
        marker = self.root / 'detached.pid'
        code = ('import subprocess,sys,time; p=subprocess.Popen([sys.executable,"-c",'
                '"import time; time.sleep(20)"], start_new_session=True,'
                'stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); '
                f'open({str(marker)!r},"w").write(str(p.pid)); time.sleep(.8)')
        meta = self.jobs.start([sys.executable, '-c', code], str(self.root),
                               kind='agent', extra={'agent': 'codex'})
        identity = None
        try:
            result = self.jobs.wait(meta['id'], 5)
            child = int(marker.read_text())
            identity = process_identity(child)
            self.assertIsNotNone(identity)
            self.assertEqual(result['status'], 'cleanup_incomplete')
            view = runtime(self.jobs, meta['id'])
            self.assertIn(child, [item['pid'] for item in view['observed_descendants']])
            stopped = stop_agent(self.jobs, meta['id'])
            self.assertFalse(stopped['cleanup']['complete'])
            self.assertTrue(stopped['cleanup']['observed_descendants_complete'])
            self.assertIsNone(process_identity(child))
        finally:
            if marker.exists() and identity:
                from ai_bridge.execution import identity_matches, signal_verified
                child = int(marker.read_text())
                if identity_matches(child, identity):
                    signal_verified(child, identity, signal.SIGKILL)

    def test_mount_boundary_preview_and_refusal(self):
        from ai_bridge.management import task_mounts
        path = self.root / 'space here'
        encoded = str(path / 'mount').replace(' ', r'\040')
        self.assertEqual(task_mounts(path, f'1 0 0:1 / {encoded} rw - tmpfs tmpfs rw')[0]['path'], str(path / 'mount'))
        task = self.tasks.create()
        with patch('ai_bridge.management.task_mounts', return_value=[{'status': 'mounted_subtree'}]), \
             patch('ai_bridge.management.observed_task_processes', return_value=[]):
            self.assertFalse(self.tasks.cleanup(task['task_id'], self.jobs)['cleanup_allowed'])
            with self.assertRaisesRegex(ValueError, 'refused'):
                self.tasks.cleanup(task['task_id'], self.jobs, False)

    def test_reused_lineage_root_not_adopted(self):
        lineage = Descendants(os.getpid())
        lineage.root_identity = dict(lineage.root_identity, start_ticks='reused')
        with patch('ai_bridge.execution.signal_verified') as send:
            self.assertEqual(lineage.sample(), {})
            lineage.cancel()
            send.assert_not_called()

    def test_agent_timeout_never_broadcasts_to_process_group(self):
        from ai_bridge.execution import execute
        with patch('ai_bridge.execution.terminate_group') as group:
            result = execute(self.cfg, [sys.executable, '-c', 'import time; time.sleep(10)'],
                             str(self.root), .2, track_children=True)
            group.assert_not_called()
        self.assertTrue(result['timed_out'])
        self.assertFalse(result['cleanup']['complete'])
        self.assertIn('no group signalling', result['cleanup']['scope'])

    def test_unknown_cli_command_never_starts_server(self):
        env = dict(os.environ)
        env.pop('AI_BRIDGE_TOKEN', None)
        result = subprocess.run([sys.executable, '-m', 'ai_bridge', 'not-a-command'], env=env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 2)
        self.assertIn('invalid choice', result.stderr)
        self.assertNotIn('AI_BRIDGE_TOKEN', result.stderr)

    def test_native_resume_other_adapters_and_storage_safety(self):
        sessions = Sessions(self.cfg, self.jobs)
        result = {'stdout': '--resume SESSION_ID', 'stderr': '', 'exit_code': 0,
                  'timed_out': False, 'stdout_truncated': False}
        with patch.object(agents, 'session_info', return_value=self.info), \
             patch.object(agents, 'resume_cwd', return_value=str(self.root)), \
             patch.object(agents, 'find_binary', return_value='/fake'), \
             patch('ai_bridge.execution.probe', return_value=result):
            for agent in ('claude', 'cursor'):
                self.assertEqual(sessions.interactive(agent, SID)['argv'], ['/fake', '--resume', SID])
            result['timed_out'] = True
            with self.assertRaisesRegex(ValueError, 'capability'):
                sessions.interactive('claude', SID)
            self.store.chmod(0o666)
            with self.assertRaisesRegex(ValueError, 'permissions'):
                sessions.interactive('claude', SID)



class NativeModeTests(unittest.TestCase):
    def test_codex_unknown_main_exec_and_subagent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sessions = root / 'sessions'
            sessions.mkdir()
            file = sessions / ('rollout-test-' + SID + '.jsonl')
            with patch.object(agents, '_codex_dir', return_value=root):
                for source, thread, parent, mode, kind in (
                    ('exec', 'user', None, 'noninteractive', 'main'),
                    ('vscode', 'user', None, 'interactive', 'main'),
                    ({'subagent': 'review'}, None, SID, 'unknown', 'subagent'),
                    ('new-format', None, None, 'unknown', 'unknown')):
                    file.write_text(json.dumps({'type': 'session_meta', 'payload': {
                        'id': SID, 'cwd': tmp, 'source': source, 'thread_source': thread, 'parent_thread_id': parent}}))
                    row = agents.codex_sessions(None, None)[0]
                    self.assertEqual((row['mode'], row['session_type']), (mode, kind))
                    self.assertEqual(row['native_source'], source)

    def test_claude_sdk_is_evidence_missing_marker_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / 'projects' / 'fixture'
            project.mkdir(parents=True)
            file = project / (SID + '.jsonl')
            with patch.object(agents, '_claude_dir', return_value=root):
                for marker, expected in (({}, 'unknown'), ({'entrypoint': 'sdk-cli'}, 'noninteractive')):
                    file.write_text(json.dumps({'type': 'user', 'cwd': tmp, 'message': {'content': 'test'}, **marker}))
                    self.assertEqual(agents.claude_sessions(None, None)[0]['mode'], expected)

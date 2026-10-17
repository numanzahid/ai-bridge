import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from ai_bridge.core import Config, Jobs, SudoUnavailable
from ai_bridge.execution import execute
from ai_bridge.privilege import CLIENT_INSTRUCTIONS, fields, initial, job_fields
from ai_bridge.responses import render


class PrivilegeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root/'jobs').mkdir()
        self.cfg = Config(token='test', state_dir=self.root, capture_bytes=4096)

    def fake(self, body):
        path = self.root/'sudo'
        path.write_text('#!'+sys.executable+'\n'+body+'\n')
        path.chmod(0o700)
        return patch.dict(os.environ, PATH=str(self.root)+os.pathsep+os.environ['PATH'])

    def test_denied_sudo_is_attempted_not_used(self):
        with patch('os.geteuid', return_value=1000), self.fake('import sys; print("authentication rejected",file=sys.stderr); sys.exit(1)'):
            result = execute(self.cfg, [sys.executable, '-c', 'print("should not run")'], str(self.root), 3, True)
        value = result['privilege']
        self.assertTrue(value['sudo_requested'])
        self.assertTrue(value['sudo_attempted'])
        self.assertFalse(value['sudo_used'])
        self.assertFalse(value['target_started'])
        self.assertIsNone(value['effective_root'])
        self.assertEqual(result['exit_code'], 1)
        self.assertIn('not confirmed', result['privilege_message'])
        self.assertNotIn('should not run', result['stdout'])

    def test_missing_sudo_is_not_attempted(self):
        with patch('os.geteuid', return_value=1000), patch('shutil.which', return_value=None):
            with self.assertRaises(SudoUnavailable):
                execute(self.cfg, ['true'], str(self.root), 2, True)
        self.assertFalse(initial(True)['sudo_used'])

    def test_confirmed_launch_does_not_mean_command_succeeded(self):
        with patch('os.geteuid', return_value=1000), self.fake('import os,sys; args=sys.argv[sys.argv.index("--")+1:]; os.execvpe(args[0],args,os.environ)'):
            result = execute(self.cfg, [sys.executable, '-c', 'import sys; print(sys.stdin.read()); sys.exit(7)'], str(self.root), 3, True, b'target-input')
        self.assertTrue(result['privilege']['sudo_used'])
        self.assertTrue(result['privilege']['target_started'])
        self.assertEqual(result['privilege']['effective_uid'], os.geteuid())
        self.assertEqual(result['privilege']['effective_root'], os.geteuid() == 0)
        self.assertEqual(result['exit_code'], 7)
        self.assertEqual(result['stdout'].strip(), 'target-input')

    def test_missing_target_not_claimed_as_privileged_execution(self):
        with patch('os.geteuid', return_value=1000), self.fake('import os,sys; args=sys.argv[sys.argv.index("--")+1:]; os.execvpe(args[0],args,os.environ)'):
            result = execute(self.cfg, ['/nonexistent/bridge-test-command'], str(self.root), 3, True)
        self.assertFalse(result['privilege']['sudo_used'])
        self.assertFalse(result['privilege']['target_started'])
        self.assertEqual(result['exit_code'], 127)

    def test_root_service_bypasses_sudo_and_discloses_even_without_request(self):
        for requested in (True, False):
            with patch('os.geteuid', return_value=0), patch('shutil.which', side_effect=AssertionError('must not find sudo')):
                result = execute(self.cfg, [sys.executable, '-c', 'print("ok")'], str(self.root), 3, requested)
            value = result['privilege']
            self.assertEqual(value['method'], 'service_root')
            self.assertTrue(value['effective_root'])
            self.assertFalse(value['sudo_used'])
            self.assertFalse(value['sudo_attempted'])
            self.assertEqual(value['sudo_requested'], requested)

    def test_ordinary_unprivileged_execution_has_no_disclosure_noise(self):
        with patch('os.geteuid', return_value=1000):
            result = execute(self.cfg, [sys.executable, '-c', 'print("ok")'], str(self.root), 3)
        self.assertNotIn('privilege', result)

    def test_job_metadata_survives_new_reader_and_legacy_is_unknown(self):
        jobs = Jobs(self.cfg)
        with self.fake('import os,sys; args=sys.argv[sys.argv.index("--")+1:]; os.execvpe(args[0],args,os.environ)'):
            job = jobs.start([sys.executable, '-c', 'print("job output")'], str(self.root), sudo=True)
            done = jobs.wait(job['id'], 5)
        self.assertEqual(done['status'], 'done')
        self.assertTrue(done['privilege']['target_started'])
        self.assertEqual(done['privilege']['effective_uid'], os.geteuid())
        reloaded = Jobs(self.cfg).load(job['id'])
        self.assertEqual(job_fields(reloaded), job_fields(done))
        self.assertEqual(job_fields(Jobs(self.cfg).list()[0]), job_fields(done))
        self.assertIn('job', jobs.log_chunk(job['id'], 0, 3)['text'])
        self.assertEqual(job_fields(jobs.kill(job['id'])), job_fields(done))
        legacy = {'id': 'legacy-job', 'status': 'done', 'sudo': True, 'started': time.time()}
        jobs.save(legacy)
        value = job_fields(Jobs(self.cfg).load('legacy-job'))['privilege']
        self.assertIsNone(value['sudo_used'])
        self.assertIsNone(value['target_started'])

    @unittest.skipIf(os.geteuid() == 0, 'Runner correctly bypasses sudo when the test service is root')
    def test_denied_job_after_restart_is_not_reported_as_root(self):
        jobs = Jobs(self.cfg)
        with self.fake('import sys; sys.exit(1)'):
            job = jobs.start(['true'], str(self.root), sudo=True)
            done = jobs.wait(job['id'], 5)
        self.assertEqual(done['status'], 'failed')
        self.assertFalse(Jobs(self.cfg).load(job['id'])['privilege']['sudo_used'])
        self.assertTrue(done['privilege']['sudo_attempted'])

    def test_disclosure_survives_output_truncation(self):
        result = {'stdout': 'x'*50000, **fields(initial(True))}
        value = json.loads(render(result, 512, lambda text: text))
        self.assertIn('privilege', value)
        self.assertIn('privilege_message', value)
        self.assertTrue(value['truncation']['truncated'])

    def test_instructions_present_in_default_and_mandatory_override_layer(self):
        self.assertIn('tell the user the task and target', CLIENT_INSTRUCTIONS)
        source = Path('ai_bridge/server.py').read_text()
        self.assertIn('HOST_CONFIG.instructions + "\\n" + CLIENT_INSTRUCTIONS', source)
        default = Path('ai_bridge/host_defaults/server-instructions.md').read_text()
        for text in ('tell the user', 'sudo=false', 'Never reveal', 'service user'):
            self.assertIn(text, default)


class ToolPrivilegeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from test_server_sessions import server
        cls.server = server

    def test_file_helpers_disclose_and_keep_plain_reads_unchanged(self):
        value = initial(True)
        result = {'exit_code': 0, 'timed_out': False, 'stdout_truncated': False,
                  'stdout': json.dumps({'result': 'file contents'}), 'stderr': '', **fields(value)}
        with patch('ai_bridge.execution.execute', return_value=result):
            read = json.loads(self.server.read_file('/fake', sudo=True))
            self.assertEqual(read['content'], 'file contents')
            self.assertEqual(read['privilege'], value)
        result.pop('privilege'); result.pop('privilege_message')
        with patch('ai_bridge.execution.execute', return_value=result):
            self.assertEqual(self.server.read_file('/fake'), 'file contents')

    def test_file_errors_and_command_failure_include_truthful_disclosure(self):
        for tool, args in ((self.server.read_file, ('/fake',)), (self.server.write_file, ('/fake', 'test'))):
            with patch('ai_bridge.execution.execute', side_effect=SudoUnavailable('not installed')):
                value = json.loads(tool(*args, sudo=True))
            self.assertTrue(value['privilege']['sudo_requested'])
            self.assertFalse(value['privilege']['sudo_used'])
            self.assertFalse(value['privilege']['sudo_attempted'])
        with patch.object(self.server, 'run_sync', side_effect=SudoUnavailable('not installed')):
            result = json.loads(self.server.run_command('true', sudo=True))
        self.assertFalse(result['privilege']['sudo_used'])

    def test_all_job_views_inherit_launch_disclosure(self):
        value = initial(True)
        meta = {'id': 'test-job', 'status': 'done', 'started': time.time(), 'sudo': True, 'privilege': value}
        server = self.server
        with patch.object(server.JOBS, 'start', return_value=meta), patch.object(server.JOBS, 'wait', return_value=meta), \
             patch.object(server.JOBS, 'list', return_value=[meta]), patch.object(server.JOBS, 'kill', return_value=meta), \
             patch.object(server.JOBS, 'tail', return_value=''), patch.object(server.JOBS, 'load', return_value=meta), \
             patch.object(server.JOBS, 'log_chunk', return_value={'text': 'log'}):
            results = [json.loads(server.start_job('true', sudo=True)), json.loads(server.job_status('test-job')),
                       json.loads(server.list_jobs())[0], json.loads(server.kill_job('test-job')), json.loads(server.job_log('test-job'))]
        self.assertTrue(all(result['privilege'] == value for result in results))

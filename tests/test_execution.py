import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from ai_bridge.core import Config, Jobs
from ai_bridge.execution import execute, process_identity, terminate_group
from ai_bridge.job_paths import KINDS, sidecar_path

SID = '11111111-2222-4333-8444-555555555555'


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root/'jobs').mkdir()
        self.cfg = Config(token='fake-token-'*4, state_dir=self.root, capture_bytes=4096, job_log_bytes=8192)

    def test_large_stdout_and_stderr_are_drained_but_bounded(self):
        result = execute(self.cfg, [sys.executable, '-c', 'import os; os.write(1,b"a"*2000000); os.write(2,b"b"*2000000)'], str(self.root), 5)
        self.assertEqual(result['exit_code'], 0)
        for name in ('stdout','stderr'):
            self.assertEqual(result[name+'_bytes'], 2000000)
            self.assertTrue(result[name+'_truncated'])
            self.assertLessEqual(len(result[name]),4096)

    def test_timeout_kills_remaining_group_after_leader_exits(self):
        pidfile = self.root/'child'
        code = 'import os,time,signal; p=os.fork();\nif p: os._exit(0)\nopen('+repr(str(pidfile))+',"w").write(str(os.getpid())); signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(30)'
        started = time.monotonic()
        result = execute(self.cfg,[sys.executable,'-c',code],str(self.root),.2)
        self.assertLess(time.monotonic()-started,3)
        self.assertTrue(result['timed_out'])
        self.assertTrue(result['cleanup']['complete'])
        self.assertIsNone(process_identity(int(pidfile.read_text())))

    def fake_sudo(self, password_required):
        fake = self.root/'sudo'
        fake.write_text('#!'+sys.executable+'\nimport os,sys,subprocess\n'
            +( 'assert subprocess.check_output([os.environ["SUDO_ASKPASS"]]).strip()==b"fake-password"\n' if password_required else '')
            +'args=sys.argv[sys.argv.index("--")+1:]; os.execvpe(args[0],args,os.environ)\n')
        fake.chmod(0o700)
        return patch.dict(os.environ,{'PATH':str(self.root)+os.pathsep+os.environ['PATH']})

    def test_password_never_reaches_target_stdin_under_both_policies(self):
        self.cfg.sudo_password='fake-password'
        for required in (True,False):
            with self.subTest(password_required=required), self.fake_sudo(required):
                result=execute(self.cfg,[sys.executable,'-c','import sys; print(repr(sys.stdin.buffer.read()))'],str(self.root),3,True,b'target-input')
                self.assertEqual(result['exit_code'],0,result)
                self.assertEqual(result['stdout'].strip(),"b'target-input'")
                self.assertNotIn('fake-password',result['stdout']+result['stderr'])

    def test_passwordless_without_stored_credential(self):
        with self.fake_sudo(False):
            result=execute(self.cfg,[sys.executable,'-c','print("ok")'],str(self.root),3,True)
        self.assertEqual(result['stdout'].strip(),'ok')

    def test_job_identity_survives_new_jobs_object_and_cancellation(self):
        jobs=Jobs(self.cfg)
        job=jobs.start([sys.executable,'-c','import time; print("started",flush=True); time.sleep(20)'],str(self.root))
        self.addCleanup(lambda: jobs.kill(job['id']) if jobs.load(job['id'])['status']=='running' else None)
        runner=jobs._children[job['id']]
        reconnected=Jobs(self.cfg)
        self.assertEqual(reconnected.load(job['id'])['status'],'running')
        result=reconnected.kill(job['id'])
        self.assertEqual(result['status'],'killed',result)
        self.assertTrue(result['cleanup']['complete'])
        runner.wait(timeout=3)

    def test_sudo_job_uses_separate_auth_channel(self):
        self.cfg.sudo_password='fake-password'
        for required in (True,False):
            with self.subTest(required=required),self.fake_sudo(required):
                jobs=Jobs(self.cfg)
                job=jobs.start([sys.executable,'-c','import sys; print(repr(sys.stdin.buffer.read()))'],str(self.root),sudo=True)
                result=jobs.wait(job['id'],5)
                self.assertEqual(result['exit_code'],0,jobs.read_log(job['id']))
                self.assertEqual(jobs.read_log(job['id']).strip(),"b''")

    def test_old_or_reused_pid_cannot_be_cancelled(self):
        jobs=Jobs(self.cfg)
        old={'id':'test-legacy','pid':os.getpid(),'status':'running','started':time.time(),'kind':'agent','agent':'cursor','session_id':'same'}
        jobs.save(old)
        with patch('os.kill') as kill:
            self.assertEqual(jobs.load(old['id'])['identity_state'],'unverified_legacy')
            with self.assertRaisesRegex(ValueError,'identity'):
                jobs.kill(old['id'])
            kill.assert_not_called()
        changed=dict(old,id='test-reused',identity={'boot_id':'previous-boot','start_ticks':'1','pgid':os.getpgrp()})
        jobs.save(changed)
        with patch('os.kill') as kill:
            self.assertEqual(jobs.load(changed['id'])['status'],'lost')
            jobs.kill(changed['id'])
            kill.assert_not_called()

    def test_busy_session_beyond_200_newer_records(self):
        jobs=Jobs(self.cfg)
        jobs.save({'id':'test-running','status':'running','pid':os.getpid(),'identity':process_identity(os.getpid()),
                   'kind':'agent','agent':'cursor','session_id':'same','started':time.time()})
        for i in range(220):
            jobs.save({'id':f'test-done-{i}','status':'done','kind':'agent','agent':'cursor','session_id':str(i)})
        self.assertEqual(jobs.running_for_session('cursor','same')['id'],'test-running')
        # A running job whose session id is only in its unpolled output is also found.
        job={'id':'unpolled-job','agent':'codex','status':'running','session_id':None}
        rows=[{'id':f'old-{i}','status':'done','agent':'codex'} for i in range(220)]+[job]
        with patch.object(jobs,'list',return_value=rows) as listing, \
             patch.object(jobs,'output_info',return_value={'parsed':{'session_id':SID}}):
            self.assertEqual(jobs.running_for_session('codex',SID)['id'],job['id'])
            listing.assert_called_once_with(limit=None,kind='agent')

    def test_logs_bounded_summary_retains_first_id_and_final_error(self):
        jobs=Jobs(self.cfg)
        code='import json; print(json.dumps({"type":"system","session_id":"original"})); print("x"*30000); print(json.dumps({"type":"result","is_error":True,"errors":["out of usage"],"result":"quota exhausted"}))'
        job=jobs.start([sys.executable,'-c',code],str(self.root),kind='agent',extra={'agent':'cursor','prompt':'must not persist'})
        jobs.wait(job['id'],5)
        self.assertNotIn('prompt',jobs.load(job['id']))
        self.assertLessEqual(jobs.log_path(job['id']).stat().st_size,8192)
        info=jobs.output_info(job['id'])
        self.assertTrue(info['truncated'])
        self.assertEqual(info['parsed']['session_id'],'original')
        self.assertTrue(info['parsed']['is_error'])
        self.assertIn('out of usage',' '.join(info['parsed']['other']))
        chunk=jobs.log_chunk(job['id'],0,200)
        self.assertEqual(chunk['next_offset_bytes'],200)

    def test_retention_never_deletes_running_or_uncertain(self):
        self.cfg.retention_days=30
        jobs=Jobs(self.cfg)
        for status in ('running','cleanup_incomplete','done'):
            job_id='test-'+status.replace('_','-')
            jobs.save({'id':job_id,'status':status,'started':1,'ended':1,'pid':os.getpid()})
            for kind in KINDS:
                sidecar_path(jobs.dir,job_id,kind,create=True).write_text('{}')
        jobs.prune()
        for job_id,kept in (('test-running',True),('test-cleanup-incomplete',True),('test-done',False)):
            self.assertEqual(jobs._meta_path(job_id).exists(),kept,job_id)
            for kind in KINDS:
                self.assertEqual(sidecar_path(jobs.dir,job_id,kind).exists(),kept,(job_id,kind))

    def test_permission_denied_cleanup_is_not_success(self):
        with patch('os.killpg',side_effect=PermissionError),patch('ai_bridge.execution.group_alive',return_value=True):
            result=terminate_group(12345,grace=0)
        self.assertFalse(result['complete'])
        self.assertTrue(result['permission_denied'])

    def test_log_cursor_rejects_a_changed_snapshot(self):
        jobs=Jobs(self.cfg)
        path=jobs.log_path('test-log');path.write_text('original')
        first=jobs.log_chunk('test-log',0,2)
        other=self.root/'jobs/replacement';other.write_text('updated');os.replace(other,path)
        self.assertIn('changed',jobs.log_chunk('test-log',2,2,first['generation'])['error'])

    def test_corrupt_metadata_does_not_hide_other_jobs(self):
        jobs=Jobs(self.cfg)
        jobs.save({'id':'test-ok','status':'done'})
        jobs._meta_path('test-bad').write_text('{broken')
        self.assertEqual([row['id'] for row in jobs.list()],['test-ok'])

    def test_detached_job_survives_creator_process_exit(self):
        command=[sys.executable,'-c','import time; time.sleep(.5); print("survived")']
        code='from pathlib import Path; import sys; from ai_bridge.core import Config,Jobs; root=Path(sys.argv[1]); j=Jobs(Config(token="fake",state_dir=root)); m=j.start('+repr(command)+',str(root)); print(m["id"])'
        job_id=subprocess.check_output([sys.executable,'-c',code,str(self.root)],text=True).strip()
        jobs=Jobs(self.cfg)
        result=jobs.wait(job_id,5)
        self.assertEqual(result['status'],'done')
        self.assertIn('survived',jobs.read_log(job_id))

import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
import unittest

from ai_bridge.core import Config, Jobs
from ai_bridge.job_paths import KINDS, sidecar_path
from legacy_jobs_fixture import LegacyJobs


class SidecarCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root/'jobs').mkdir()
        self.cfg = Config(token='fixture', state_dir=self.root)
        self.jobs = Jobs(self.cfg)

    def legacy_list_jobs(self):
        # Match original server.list_jobs' required field access. The fixture's
        # list/_refresh methods are from the actual original source, not a new filter.
        return [{'job_id': row['id'], 'status': row['status']} for row in LegacyJobs(self.cfg).list()]

    def test_candidate_job_remains_listable_by_original_bridge_after_rollback(self):
        job = self.jobs.start([sys.executable, '-c', 'print("rollback-safe")'], str(self.root))
        self.assertEqual(self.jobs.wait(job['id'], 5)['status'], 'done')
        visible = list(self.jobs.dir.glob('*.json'))
        self.assertEqual([p.name for p in visible], [job['id']+'.json'])
        for kind in KINDS:
            path = sidecar_path(self.jobs.dir, job['id'], kind)
            self.assertTrue(path.is_file(), kind)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.jobs.dir/'.sidecars').stat().st_mode), 0o700)
        self.assertEqual(self.legacy_list_jobs(), [{'job_id': job['id'], 'status': 'done'}])
        # Prove this fixture catches the original bug rather than filtering it away.
        stray = self.jobs.dir/(job['id']+'.output.json')
        stray.write_text('{"parsed": null}')
        with self.assertRaises(KeyError):
            self.legacy_list_jobs()

    def test_new_instance_status_and_log_pagination_use_private_sidecars(self):
        job = self.jobs.start([sys.executable, '-c', 'print("abcdefghij")'], str(self.root))
        self.jobs.wait(job['id'], 5)
        reconnected = Jobs(self.cfg)
        self.assertEqual(reconnected.load(job['id'])['exit_code'], 0)
        self.assertEqual(reconnected.output_info(job['id'])['total_bytes'], 11)
        first = reconnected.log_chunk(job['id'], 0, 4)
        second = reconnected.log_chunk(job['id'], first['next_offset_bytes'], 100, first['generation'])
        self.assertEqual(first['text']+second['text'], 'abcdefghij\n')
        self.assertIsNone(second['next_offset_bytes'])
        # Legacy reconciliation also works if the new bridge has not saved done yet.
        meta = json.loads(self.jobs._meta_path(job['id']).read_text())
        meta['status'] = 'running'
        self.jobs.save(meta)
        self.assertEqual(self.legacy_list_jobs()[0]['status'], 'done')

    def test_original_job_state_without_sidecars_is_preserved(self):
        legacy = {'id': 'test-legacy', 'status': 'running', 'pid': os.getpid(), 'started': time.time()}
        self.jobs.save(legacy)
        self.assertEqual(self.jobs.load(legacy['id'])['identity_state'], 'unverified_legacy')
        with self.assertRaisesRegex(ValueError, 'identity'):
            self.jobs.kill(legacy['id'])
        self.jobs.exit_path(legacy['id']).write_text('0')
        self.assertEqual(Jobs(self.cfg).load(legacy['id'])['status'], 'done')
        self.assertFalse((self.jobs.dir/'.sidecars').exists())

    def test_superseded_candidate_root_layout_is_readable_without_moving_files(self):
        job_id = 'test-prototype'
        self.jobs.save({'id': job_id, 'status': 'running', 'pid': os.getpid(), 'started': time.time()})
        outcome = self.jobs.dir/(job_id+'.outcome.json')
        output = self.jobs.dir/(job_id+'.output.json')
        outcome.write_text(json.dumps({'exit_code': 0, 'ended': time.time()}))
        output.write_text(json.dumps({'total_bytes': 99, 'truncated': True}))
        self.assertEqual(self.jobs.load(job_id)['status'], 'done')
        self.assertEqual(self.jobs.output_info(job_id)['total_bytes'], 99)
        self.assertTrue(outcome.exists())
        self.assertTrue(output.exists())
        self.assertFalse((self.jobs.dir/'.sidecars').exists())

    def test_retention_cleans_only_finalized_jobs_sidecars(self):
        self.cfg.retention_days = 1
        for job_id, status in (('test-finished', 'done'), ('test-uncertain', 'cleanup_incomplete')):
            self.jobs.save({'id': job_id, 'status': status, 'ended': 1})
            for kind in KINDS:
                sidecar_path(self.jobs.dir, job_id, kind, create=True).write_text('{}')
        self.jobs.prune()
        for kind in KINDS:
            self.assertFalse(sidecar_path(self.jobs.dir, 'test-finished', kind).exists())
            self.assertTrue(sidecar_path(self.jobs.dir, 'test-uncertain', kind).exists())

    def test_sidecar_directory_symlink_is_rejected(self):
        other = self.root/'other'; other.mkdir()
        (self.jobs.dir/'.sidecars').symlink_to(other, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'real directory'):
            sidecar_path(self.jobs.dir, 'test-safe', 'output', create=True)
        self.assertFalse(list(other.iterdir()))

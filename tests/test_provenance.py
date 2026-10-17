"""Disposable MCP evidence lifecycle, compatibility and filesystem attack fixtures."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from ai_bridge.core import Config, Jobs
from ai_bridge.job_paths import sidecar_path
from ai_bridge.management import Sessions, write_json
from ai_bridge.provenance import Provenance, filename, MAX_JOBS, MAX_BYTES

SID = '11111111-2222-4333-8444-555555555555'
OTHER = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='bridge-provenance-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cfg = Config(token='', state_dir=self.root / 'state', tasks_dir=self.root / 'tasks')
        self.cfg.state_dir.mkdir(mode=0o700)
        (self.cfg.state_dir / 'jobs').mkdir(mode=0o700)
        self.jobs = Jobs(self.cfg)
        self.store = Provenance(self.cfg)
        self.directory = self.cfg.state_dir / 'management/provenance'
        self.path = self.directory / filename('codex', SID)
        self.job = {'id': 'test-job', 'kind': 'agent', 'agent': 'codex',
                    'invocation_origin': 'mcp', 'requested_session_id': None,
                    'session_id': None, 'status': 'done', 'ended': 1, 'exit_code': 0}

    def observe(self, **changes):
        return self.store.observe(dict(self.job, **changes), {'session_id': SID})

    def output(self, parsed=None):
        path = sidecar_path(self.jobs.dir, self.job['id'], 'output', create=True)
        write_json(path, {'parsed': parsed or {'session_id': SID}, 'parser_error': None,
                          'provenance_session_id': (parsed or {'session_id': SID}).get('session_id')})
        return path

    def test_contract_hash_permissions_and_native_metadata_preserved(self):
        self.assertEqual(self.path.name, hashlib.sha256(f'codex:{SID}'.encode()).hexdigest() + '.json')
        self.assertTrue(self.observe())
        record = self.store.read('codex', SID)
        self.assertEqual(set(record), {'schema', 'agent', 'session_id', 'origin', 'first_seen', 'job_ids'})
        self.assertLess(abs(time.time() - record['first_seen']), 10)
        for directory in (self.cfg.state_dir, self.directory.parent, self.directory):
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        row = Sessions(self.cfg, self.jobs).enrich('codex', [
            {'session_id': SID, 'origin': 'native', 'native_source': 'exec', 'mode': 'noninteractive'}])[0]
        self.assertEqual((row['bridge_origin'], row['origin'], row['native_source']), ('mcp', 'native', 'exec'))
        self.assertIsNone(Sessions(self.cfg, self.jobs).enrich('codex', [{'session_id': OTHER}])[0]['bridge_origin'])
        self.assertIsNone(self.store.read('claude', SID))

    def test_no_false_positives_or_requested_only_resume(self):
        for changes in ({'invocation_origin': None}, {'invocation_origin': 'headless'},
                        {'kind': 'command'}, {'agent': 'unknown'}, {'id': '../../escape'},
                        {'requested_session_id': OTHER}):
            with self.subTest(changes=changes):
                self.assertFalse(self.observe(**changes))
        for sid in (None, '', SID[:8], '../' + SID, 'x' * 36):
            self.assertFalse(self.store.observe(self.job, {'session_id': sid}))
        self.assertFalse(self.store.observe(dict(self.job, requested_session_id=SID, session_id=SID), {}))
        self.assertFalse(self.directory.exists())
        self.assertIsNone(Sessions(self.cfg, self.jobs).enrich('codex', [
            {'session_id': SID, 'origin': 'mcp', 'native_source': 'exec'}])[0]['bridge_origin'])

    def test_direct_native_chat_resumed_over_mcp_remains_unclassified(self):
        for agent in ('codex', 'claude', 'cursor'):
            with self.subTest(agent=agent):
                job = dict(self.job, id='resume-' + agent, agent=agent, requested_session_id=SID, session_id=SID)
                self.assertFalse(self.store.observe(job, {'session_id': SID}))
                self.assertIsNone(self.store.read(agent, SID))
                self.jobs.save(job)
                write_json(sidecar_path(self.jobs.dir, job['id'], 'output', create=True),
                           {'parsed': {'session_id': SID}, 'provenance_session_id': SID})
                self.jobs.list()
                row = Sessions(self.cfg, self.jobs).enrich(agent, [
                    {'session_id': SID, 'origin': 'native', 'native_source': 'cli'}])[0]
                self.assertIsNone(row['bridge_origin'])
                self.assertEqual(row['origin'], 'native')
                self.assertIsNone(self.store.read(agent, SID))

    def test_original_mcp_session_resume_preserves_origin_and_first_seen(self):
        self.assertTrue(self.observe())
        original = self.store.read('codex', SID)
        self.assertTrue(self.observe(id='resume-job', requested_session_id=SID, session_id=SID))
        row = self.store.read('codex', SID)
        self.assertEqual(row['first_seen'], original['first_seen'])
        self.assertEqual(row['origin'], 'mcp')
        self.assertEqual(row['job_ids'], ['test-job', 'resume-job'])
        self.assertEqual(Sessions(self.cfg, self.jobs).enrich('codex', [{'session_id': SID}])[0]['bridge_origin'], 'mcp')

    def test_mismatched_resume_cannot_create_or_modify_origin(self):
        self.observe()
        original = self.path.read_bytes()
        for requested, observed in ((SID, OTHER), (OTHER, SID)):
            with self.subTest(requested=requested, observed=observed):
                self.assertFalse(self.store.observe(
                    dict(self.job, id='mismatch-job', requested_session_id=requested),
                    {'session_id': observed}))
        self.assertEqual(self.path.read_bytes(), original)
        self.assertIsNone(self.store.read('codex', OTHER))

    def test_runner_mcp_resume_of_direct_chat_does_not_establish_origin(self):
        code = 'print(\'' + json.dumps({'type': 'thread.started', 'thread_id': SID}) + '\')'
        meta = self.jobs.start([sys.executable, '-c', code], str(self.root), kind='agent',
                               extra={'agent': 'codex', 'invocation_origin': 'mcp',
                                      'requested_session_id': SID, 'session_id': SID})
        self.assertEqual(self.jobs.wait(meta['id'], 5)['status'], 'done')
        self.assertIsNone(self.store.read('codex', SID))

    def test_first_seen_bounded_ids_concurrent_writers_and_resume(self):
        self.observe()
        first = self.store.read('codex', SID)['first_seen']
        # This exercises serialization and bounded IDs; avoid hundreds of disk syncs.
        with patch('ai_bridge.provenance.os.fsync'), ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda i: self.observe(id=f'job-{i:04}'), range(80)))
        row = self.store.read('codex', SID)
        self.assertEqual(len(row['job_ids']), MAX_JOBS)
        self.assertEqual(len(set(row['job_ids'])), MAX_JOBS)
        self.assertEqual(row['first_seen'], first)
        self.assertLess(self.path.stat().st_size, MAX_BYTES)
        self.assertTrue(self.observe(id=row['job_ids'][-1], requested_session_id=SID))
        self.assertEqual(self.store.read('codex', SID), row)

    def test_marked_completed_backfill_before_prune_and_restart(self):
        self.jobs.save(self.job)
        self.output()
        self.cfg.retention_days = 1
        self.jobs.prune()
        self.assertFalse(self.jobs._meta_path(self.job['id']).exists())
        self.assertEqual(Provenance(self.cfg).read('codex', SID)['job_ids'], ['test-job'])
        self.assertEqual(Sessions(self.cfg, Jobs(self.cfg)).enrich('codex', [{'session_id': SID}])[0]['bridge_origin'], 'mcp')

    def test_large_bounded_output_snapshot_can_backfill(self):
        self.jobs.save(self.job)
        path = self.output()
        write_json(path, {'parsed': {'session_id': SID, 'final': 'x' * 100000},
                          'provenance_session_id': SID, 'parser_error': None})
        self.jobs.list()
        self.assertIsNotNone(self.store.read('codex', SID))

    def test_active_backfill_and_unmarked_legacy_unknown(self):
        self.jobs.save(dict(self.job, status='running', session_id=SID))
        self.output()
        self.assertEqual(self.jobs.list(kind='agent')[0]['status'], 'running')
        self.assertIsNotNone(self.store.read('codex', SID))
        self.path.unlink()  # disposable record only
        self.jobs.save(dict(self.job, invocation_origin=None, session_id=SID))
        self.jobs.list()
        self.assertIsNone(self.store.read('codex', SID))

    def test_requested_id_and_parser_errors_do_not_backfill(self):
        self.jobs.save(dict(self.job, session_id=SID, requested_session_id=SID))
        self.jobs.list()
        self.assertFalse(self.path.exists())
        self.output({'session_id': OTHER})
        self.jobs.list()
        self.assertIsNone(self.store.read('codex', SID))
        self.assertIsNone(self.store.read('codex', OTHER))
        output = self.output()
        write_json(output, {'parsed': {'session_id': SID}, 'parser_error': 'unreliable'})
        self.jobs.list()
        self.assertFalse(self.path.exists())

    def test_runner_records_active_without_poll_and_completed_resume(self):
        code = 'import time; print(\'' + json.dumps({'type': 'thread.started', 'thread_id': SID}) + '\', flush=True); time.sleep(1.5)'
        for requested in (None, SID):
            meta = self.jobs.start([sys.executable, '-c', code], str(self.root), kind='agent',
                                   extra={'agent': 'codex', 'invocation_origin': 'mcp',
                                          'requested_session_id': requested, 'session_id': requested})
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                row = self.store.read('codex', SID)
                if row and meta['id'] in row['job_ids']:
                    break
                time.sleep(.02)
            self.assertIn(meta['id'], self.store.read('codex', SID)['job_ids'])
            # No Jobs polling or session listing was needed to persist evidence.
            self.assertIsNone(self.jobs._children[meta['id']].poll())
            self.assertEqual(self.jobs.wait(meta['id'], 5)['status'], 'done')
        self.assertEqual(len(self.store.read('codex', SID)['job_ids']), 2)

    def test_generic_headless_runner_never_marks(self):
        code = 'print(\'' + json.dumps({'type': 'thread.started', 'thread_id': SID}) + '\')'
        meta = self.jobs.start([sys.executable, '-c', code], str(self.root), kind='agent', extra={'agent': 'codex'})
        self.jobs.wait(meta['id'], 5)
        self.assertIsNone(self.store.read('codex', SID))

    def test_atomic_write_failure_preserves_record_and_retries(self):
        self.observe()
        before = self.path.read_bytes()
        with patch('ai_bridge.provenance.os.replace', side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):
                self.observe(id='another-job')
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(list(self.directory.glob('.provenance-*')))
        self.observe(id='another-job')
        self.assertIn('another-job', self.store.read('codex', SID)['job_ids'])

    def test_failed_persistence_prevents_retention_losing_evidence(self):
        self.jobs.save(self.job)
        self.output()
        self.observe()
        self.directory.chmod(0o755)
        self.cfg.retention_days = 1
        self.jobs.prune()
        self.assertTrue(self.jobs._meta_path(self.job['id']).exists())
        self.assertIn('provenance_error', self.jobs.load(self.job['id']))
        self.directory.chmod(0o700)
        self.jobs.prune()
        self.assertFalse(self.jobs._meta_path(self.job['id']).exists())

    def test_symlinked_directory_and_ancestor_refused(self):
        outside = self.root / 'outside'
        outside.mkdir(mode=0o700)
        self.cfg.state_dir.joinpath('management').symlink_to(outside)
        with self.assertRaises(OSError):
            self.observe()
        self.assertEqual(list(outside.iterdir()), [])
        self.cfg.state_dir.joinpath('management').unlink()
        alias = self.root / 'alias'
        alias.symlink_to(self.cfg.state_dir, target_is_directory=True)
        with self.assertRaises(OSError):
            Provenance(Config(token='', state_dir=alias)).observe(self.job, {'session_id': SID})

    def test_unsafe_record_types_permissions_and_sizes_refused(self):
        self.observe()
        original = self.path.read_bytes()
        outside = self.root / 'outside.json'
        outside.write_bytes(original)
        outside.chmod(0o600)
        for kind in ('symlink', 'hardlink', 'fifo', 'world-readable', 'oversize', 'invalid-schema', 'wrong-session'):
            with self.subTest(kind=kind):
                self.path.unlink()
                if kind == 'symlink':
                    self.path.symlink_to(outside)
                elif kind == 'hardlink':
                    os.link(outside, self.path)
                elif kind == 'fifo':
                    os.mkfifo(self.path, 0o600)
                else:
                    row = json.loads(original)
                    if kind == 'invalid-schema':
                        row['schema'] = True
                    if kind == 'wrong-session':
                        row['session_id'] = OTHER
                    self.path.write_bytes(b'x' * (MAX_BYTES + 1) if kind == 'oversize' else json.dumps(row).encode())
                    self.path.chmod(0o644 if kind == 'world-readable' else 0o600)
                with self.assertRaises((OSError, ValueError)):
                    self.store.read('codex', SID)
                with self.assertRaises((OSError, ValueError)):
                    self.observe(id='another-job')
                self.assertEqual(outside.read_bytes(), original)

    def test_invalid_provenance_does_not_break_native_session_listing(self):
        self.observe()
        self.path.write_text('{}')
        row = Sessions(self.cfg, self.jobs).enrich('codex', [{'session_id': SID, 'origin': 'native'}])[0]
        self.assertIsNone(row['bridge_origin'])
        self.assertIn('bridge_provenance_error', row)
        self.assertEqual(row['origin'], 'native')

    def test_lock_symlink_and_permissive_directories_refused(self):
        self.observe()
        lock = self.directory / '.lock'
        lock.unlink()
        target = self.root / 'lock-target'
        target.write_text('keep')
        lock.symlink_to(target)
        with self.assertRaises(OSError):
            self.observe()
        self.assertEqual(target.read_text(), 'keep')
        lock.unlink()
        for directory in (self.cfg.state_dir, self.directory):
            directory.chmod(0o755)
            with self.assertRaises(ValueError):
                self.observe()
            directory.chmod(0o700)

    def test_foreign_owner_refused(self):
        self.observe()
        original = os.fstat
        def foreign(fd):
            info = original(fd)
            fields = list(info)
            fields[4] = os.getuid() + 1
            return os.stat_result(fields)
        with patch('ai_bridge.provenance.os.fstat', side_effect=foreign), self.assertRaises(ValueError):
            self.store.read('codex', SID)

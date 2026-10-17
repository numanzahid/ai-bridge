"""Release regressions: legacy permissions, sticky errors and canonical evidence."""
import json
import os
from concurrent.futures import ThreadPoolExecutor
import stat
import unittest
from unittest.mock import patch

import test_provenance as fixtures

SID, OTHER = fixtures.SID, fixtures.OTHER
from ai_bridge.job_paths import sidecar_path
from ai_bridge.job_runner import Recorder
from ai_bridge.management import Sessions, write_json, management_lock
from ai_bridge.provenance import observed_session, prepare_management


def event(sid=SID, agent='codex'):
    if agent == 'codex':
        return json.dumps({'type': 'thread.started', 'thread_id': sid}).encode() + b'\n'
    return json.dumps({'type': 'system', 'subtype': 'init', 'session_id': sid}).encode() + b'\n'


class ReleaseReviewTests(unittest.TestCase):
    setUp = fixtures.ProvenanceTests.setUp
    observe = fixtures.ProvenanceTests.observe
    output = fixtures.ProvenanceTests.output

    def legacy_management(self):
        directory = self.cfg.state_dir / 'management'
        directory.mkdir(mode=0o755)
        directory.chmod(0o755)
        return directory

    def recorder(self):
        return Recorder(self.jobs.dir, self.job['id'], 8192, 'codex', (self.cfg, self.job))

    def test_legacy_listing_migrates_only_management_and_preserves_overlay(self):
        management = self.legacy_management()
        sessions = management / 'sessions'
        sessions.mkdir(mode=0o700)
        from ai_bridge.provenance import filename
        overlay = sessions / filename('codex', SID)
        write_json(overlay, {'alias': 'saved alias'})
        before = overlay.read_bytes()
        rows = Sessions(self.cfg, self.jobs).enrich('codex', [{'session_id': SID}])
        self.assertEqual(rows[0]['title'], 'saved alias')
        self.assertEqual(stat.S_IMODE(management.stat().st_mode), 0o700)
        self.assertEqual(overlay.read_bytes(), before)
        self.assertEqual(stat.S_IMODE(sessions.stat().st_mode), 0o700)
        with management_lock(self.cfg):
            pass

    def test_legacy_migration_precedes_first_provenance_write(self):
        management = self.legacy_management()
        self.assertTrue(self.observe())
        self.assertEqual(stat.S_IMODE(management.stat().st_mode), 0o700)
        self.assertIsNotNone(self.store.read('codex', SID))

    def test_concurrent_legacy_migration_is_idempotent(self):
        management = self.legacy_management()
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: prepare_management(self.cfg), range(16)))
        self.assertEqual(stat.S_IMODE(management.stat().st_mode), 0o700)

    def test_symlink_management_never_chmods_target(self):
        target = self.root / 'target'
        target.mkdir(mode=0o755)
        (self.cfg.state_dir / 'management').symlink_to(target)
        with patch('ai_bridge.provenance.os.fchmod') as chmod:
            for action in (lambda: prepare_management(self.cfg), self.observe,
                           lambda: Sessions(self.cfg, self.jobs).enrich('codex', [{'session_id': SID}])):
                with self.assertRaises(OSError):
                    action()
            chmod.assert_not_called()
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)
        self.assertEqual(list(target.iterdir()), [])

    def test_foreign_management_and_acl_or_unknown_acl_refused_without_chmod(self):
        management = self.legacy_management()
        inode = management.stat().st_ino
        original = os.fstat
        def foreign(fd):
            info = original(fd)
            if info.st_ino == inode:
                fields = list(info)
                fields[4] = os.getuid() + 1
                return os.stat_result(fields)
            return info
        with patch('ai_bridge.provenance.os.fstat', side_effect=foreign), \
             patch('ai_bridge.provenance.os.fchmod') as chmod:
            with self.assertRaises(ValueError):
                prepare_management(self.cfg)
            chmod.assert_not_called()
        for attributes in (['system.posix_acl_access'], ['system.posix_acl_default']):
            with patch('ai_bridge.provenance.os.listxattr', return_value=attributes), \
                 patch('ai_bridge.provenance.os.fchmod') as chmod:
                with self.assertRaises(ValueError):
                    self.observe()
                chmod.assert_not_called()
        with patch('ai_bridge.provenance.os.listxattr', side_effect=OSError('unknown ACL')), \
             patch('ai_bridge.provenance.os.fchmod') as chmod:
            with self.assertRaises(OSError):
                prepare_management(self.cfg)
            chmod.assert_not_called()
        self.assertEqual(stat.S_IMODE(management.stat().st_mode), 0o755)

    def test_other_modes_and_paths_are_not_migrated(self):
        management = self.legacy_management()
        for mode in (0o750, 0o775, 0o777, 0o1755):
            management.chmod(mode)
            with patch('ai_bridge.provenance.os.fchmod') as chmod:
                with self.assertRaises(ValueError):
                    prepare_management(self.cfg)
                chmod.assert_not_called()
            self.assertEqual(stat.S_IMODE(management.stat().st_mode), mode)
        management.chmod(0o700)
        for directory in (self.cfg.state_dir, management / 'provenance'):
            if not directory.exists():
                directory.mkdir(mode=0o755)
            directory.chmod(0o755)
            with patch('ai_bridge.provenance.os.fchmod') as chmod:
                with self.assertRaises(ValueError):
                    self.observe()
                chmod.assert_not_called()
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o755)
            directory.chmod(0o700)

    def test_migration_uses_open_descriptor_not_replaced_path(self):
        management = self.legacy_management()
        moved = self.cfg.state_dir / 'old-management'
        target = self.root / 'target'
        target.mkdir(mode=0o755)
        original = os.fchmod
        def replace_path(fd, mode):
            management.rename(moved)
            management.symlink_to(target)
            original(fd, mode)
        with patch('ai_bridge.provenance.os.fchmod', side_effect=replace_path):
            self.observe()
        self.assertEqual(stat.S_IMODE(moved.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)
        self.assertEqual(list(target.iterdir()), [])

    def test_recorder_failed_first_write_then_false_keeps_error_and_blocks_prune(self):
        recorder = self.recorder()
        with patch('ai_bridge.provenance.Provenance.observe', side_effect=[OSError('disk full'), False]):
            recorder.record(event())
            recorder.record(event())
        self.assertEqual(recorder.provenance_error, 'disk full')
        recorder.flush(final=True)
        self.jobs.save(self.job)
        self.cfg.retention_days = 1
        with patch('ai_bridge.provenance.Provenance.observe', return_value=False):
            self.jobs.prune()
        self.assertTrue(self.jobs._meta_path(self.job['id']).exists())
        self.assertEqual(json.loads(self.jobs._meta_path(self.job['id']).read_text())['provenance_error'], 'disk full')
        self.jobs.prune()
        self.assertIsNotNone(self.store.read('codex', SID))
        self.assertFalse(self.jobs._meta_path(self.job['id']).exists())

    def test_refresh_persists_first_failure_across_later_false_and_restart(self):
        self.jobs.save(self.job)
        self.output()
        self.cfg.retention_days = 1
        with patch('ai_bridge.provenance.Provenance.backfill', side_effect=OSError('first failure')):
            self.jobs.load(self.job['id'])
        with patch('ai_bridge.provenance.Provenance.backfill', return_value=False):
            self.jobs.prune()
            self.assertEqual(self.jobs.load(self.job['id'])['provenance_error'], 'first failure')
        self.assertTrue(self.jobs._meta_path(self.job['id']).exists())
        self.jobs.prune()
        self.assertFalse(self.jobs._meta_path(self.job['id']).exists())

    def test_recorder_clears_error_only_after_confirmed_persistence(self):
        recorder = self.recorder()
        with patch('ai_bridge.provenance.Provenance.observe', side_effect=[OSError('failure'), False, True]):
            recorder.record(event())
            recorder.record(event())
            self.assertEqual(recorder.provenance_error, 'failure')
            recorder.record(event())
        self.assertIsNone(recorder.provenance_error)

    def test_first_uuid_bound_across_stdout_stderr_and_backfill(self):
        recorder = self.recorder()
        recorder.add('stdout', event())
        recorder.add('stderr', event(OTHER))
        recorder.flush(final=True)
        self.assertEqual(recorder.parsed['session_id'], SID)
        self.assertEqual(recorder.provenance_session_id, SID)
        self.assertIsNotNone(self.store.read('codex', SID))
        self.assertIsNone(self.store.read('codex', OTHER))
        self.assertFalse(self.store.observe(self.job, {'session_id': OTHER}))
        self.jobs.save(self.job)
        self.jobs.list()
        self.assertIsNone(self.store.read('codex', OTHER))

    def test_failed_origin_write_cannot_rebind_job_to_later_uuid(self):
        recorder = self.recorder()
        from ai_bridge import provenance
        original = provenance._write
        def fail_origin(fd, name, row):
            if row.get('origin') == 'mcp':
                raise OSError('origin disk failure')
            return original(fd, name, row)
        with patch('ai_bridge.provenance._write', side_effect=fail_origin):
            recorder.add('stdout', event())
        recorder.add('stderr', event(OTHER))
        self.assertEqual(recorder.provenance_error, 'origin disk failure')
        self.assertFalse(self.store.observe(self.job, {'session_id': OTHER}))
        recorder.flush(final=True)
        self.jobs.save(self.job)
        self.jobs.list()
        self.assertIsNotNone(self.store.read('codex', SID))
        self.assertIsNone(self.store.read('codex', OTHER))

    def test_generic_json_cannot_supply_origin_but_legitimate_events_can(self):
        for agent in ('codex', 'claude', 'cursor'):
            for generic in ({'session_id': OTHER}, {'type': 'tool_call', 'session_id': OTHER},
                            {'type': 'error', 'chatId': OTHER},
                            {'type': 'system', 'subtype': 'status', 'session_id': OTHER}):
                self.assertIsNone(observed_session(agent, json.dumps(generic)))
            self.assertEqual(observed_session(agent, event(agent=agent)), SID)
            self.assertIsNone(observed_session(agent, json.dumps({
                'type': 'result', 'session_id': SID, 'chatId': OTHER})))
            if agent != 'codex':
                self.assertEqual(observed_session(agent, json.dumps({'type': 'result', 'session_id': SID})), SID)
        recorder = self.recorder()
        recorder.add('stdout', json.dumps({'session_id': OTHER}).encode() + b'\n')
        self.assertIsNone(self.store.read('codex', OTHER))
        recorder.add('stdout', event())
        self.assertEqual(recorder.parsed['session_id'], SID)
        self.assertIsNotNone(self.store.read('codex', SID))

    def test_legacy_backfill_uses_first_recognized_event_not_last_parsed_uuid(self):
        self.jobs.save(self.job)
        self.jobs.log_path(self.job['id']).write_bytes(event() + event(OTHER))
        self.jobs.log_path(self.job['id']).chmod(0o600)
        write_json(sidecar_path(self.jobs.dir, self.job['id'], 'output', create=True),
                   {'parsed': {'session_id': OTHER}})
        self.jobs.list()
        self.assertIsNotNone(self.store.read('codex', SID))
        self.assertIsNone(self.store.read('codex', OTHER))

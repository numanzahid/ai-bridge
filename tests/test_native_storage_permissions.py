"""Synthetic native records and account databases; no real sessions or turns."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ai_bridge.management import Sessions
from ai_bridge.core import Config, Jobs
from ai_bridge.permissions import native_session_storage_safe

SID = '11111111-2222-4333-8444-555555555555'


class NativeStoragePermissionsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='bridge-native-permissions-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / 'project'
        self.project.mkdir()
        self.codex = self.root / 'codex'
        sessions = self.codex / 'sessions'
        sessions.mkdir(parents=True)
        self.storage = sessions / ('rollout-fixture-' + SID + '.jsonl')
        self.storage.write_text(json.dumps({'type': 'session_meta', 'payload': {
            'id': SID, 'cwd': str(self.project), 'source': 'exec', 'thread_source': 'user'}}))
        self.cfg = Config(token='', state_dir=self.root / 'state', tasks_dir=self.root / 'tasks')
        self.cfg.state_dir.mkdir(mode=0o700)
        (self.cfg.state_dir / 'jobs').mkdir(mode=0o700)
        self.jobs = Jobs(self.cfg)

    @contextmanager
    def permissions(self, mode=0o664, members=(), other_primary=False, foreign=False,
                    wrong_group=False, attributes=()):
        uid, gid = os.getuid(), os.getgid()
        account = SimpleNamespace(pw_uid=uid, pw_gid=gid, pw_name='fixture-owner')
        group = SimpleNamespace(gr_gid=gid, gr_name='fixture-private', gr_mem=list(members))
        accounts = [account]
        if other_primary:
            accounts.append(SimpleNamespace(pw_uid=uid + 1, pw_gid=gid, pw_name='fixture-other'))
        original = Path.lstat
        def metadata(path, *args, **kwargs):
            result = original(path, *args, **kwargs)
            if path != self.storage:
                return result
            fields = list(result)
            fields[0] = stat.S_IFMT(result.st_mode) | mode
            fields[4] = uid + 1 if foreign else uid
            fields[5] = gid + 1 if wrong_group else gid
            return os.stat_result(fields)
        with patch.object(Path, 'lstat', metadata), \
             patch('pwd.getpwuid', return_value=account), \
             patch('pwd.getpwall', return_value=accounts), \
             patch('grp.getgrgid', return_value=group), \
             patch('grp.getgrall', return_value=[group]), \
             patch('os.listxattr', return_value=list(attributes)):
            yield

    def dry_run(self):
        return Sessions(self.cfg, self.jobs).interactive('codex', SID)

    @contextmanager
    def native_preflight(self):
        help_result = {'stdout': 'SESSION_ID --include-non-interactive', 'stderr': '',
                       'exit_code': 0, 'timed_out': False, 'stdout_truncated': False, 'stderr_truncated': False}
        with patch.dict(os.environ, CODEX_HOME=str(self.codex)), \
             patch('ai_bridge.agents.find_binary', return_value='/fake/codex'), \
             patch('ai_bridge.execution.probe', return_value=help_result) as probe, \
             patch('os.execvpe') as execute:
            yield probe, execute

    def test_private_primary_0664_preflight_preserves_native_record(self):
        before = (self.storage.read_bytes(), self.storage.stat().st_mtime_ns)
        with self.permissions(), self.native_preflight() as (probe, execute):
            result = self.dry_run()
            self.assertEqual(result['argv'], ['/fake/codex', 'resume', '--include-non-interactive', SID])
            self.assertEqual(result['cwd'], str(self.project))
            self.assertFalse(result['history_rewritten'])
            self.assertFalse(result['verified_execution'])
            probe.assert_called_once_with(['/fake/codex', 'resume', '--help'])
            execute.assert_not_called()
        self.assertEqual((self.storage.read_bytes(), self.storage.stat().st_mtime_ns), before)

    def test_shared_primary_or_supplementary_group_refuses_before_native_probe(self):
        for options in ({'other_primary': True}, {'members': ['fixture-other']}, {'wrong_group': True}):
            with self.subTest(options=options), self.permissions(**options), self.native_preflight() as (probe, execute):
                with self.assertRaisesRegex(ValueError, 'permissions are unsafe'):
                    self.dry_run()
                probe.assert_not_called()
                execute.assert_not_called()

    def test_foreign_owner_world_write_and_extended_acl_refused(self):
        for options in ({'foreign': True}, {'mode': 0o666},
                        {'attributes': ['system.posix_acl_access']},
                        {'attributes': ['system.posix_acl_default']}):
            with self.subTest(options=options), self.permissions(**options):
                self.assertFalse(native_session_storage_safe(self.storage, os.getuid()))

    def test_unknown_group_database_or_acl_state_fails_closed(self):
        with self.permissions():
            with patch('pwd.getpwall', return_value=[]):
                self.assertFalse(native_session_storage_safe(self.storage, os.getuid()))
            with patch('grp.getgrall', side_effect=OSError('unavailable')):
                self.assertFalse(native_session_storage_safe(self.storage, os.getuid()))
            with patch('os.listxattr', side_effect=OSError('unavailable')):
                self.assertFalse(native_session_storage_safe(self.storage, os.getuid()))

    def test_existing_0600_0644_and_private_0660_modes_remain_supported(self):
        for mode in (0o600, 0o644, 0o660, 0o664):
            with self.subTest(mode=mode), self.permissions(mode=mode):
                self.assertTrue(native_session_storage_safe(self.storage, os.getuid()))
        for mode in (0o600, 0o644):
            with self.permissions(mode=mode), patch('pwd.getpwuid', side_effect=KeyError('unused')):
                self.assertTrue(native_session_storage_safe(self.storage, os.getuid()))

    def test_symlink_special_file_and_suspicious_paths_refused(self):
        link = self.root / 'linked.jsonl'
        link.symlink_to(self.storage)
        directory = self.root / 'not-a-file'
        directory.mkdir()
        pipe = self.root / 'pipe'
        os.mkfifo(pipe, 0o600)
        with self.permissions():
            for path in (link, directory, pipe, Path('relative.jsonl'),
                         self.storage.parent / '..' / 'sessions' / self.storage.name):
                with self.subTest(path=path):
                    self.assertFalse(native_session_storage_safe(path, os.getuid()))
        original = self.storage.read_bytes()
        outside = self.root / 'external.jsonl'
        outside.write_bytes(original)
        self.storage.unlink()
        self.storage.symlink_to(outside)
        with self.native_preflight() as (probe, execute):
            with self.assertRaisesRegex(ValueError, 'symlink'):
                self.dry_run()
            probe.assert_not_called()
            execute.assert_not_called()
        # Restore only this disposable fixture to permit clean temporary removal.
        self.storage.unlink()
        self.storage.write_bytes(original)

    def test_installed_native_resume_capability_is_still_required(self):
        with self.permissions(), self.native_preflight() as (probe, execute):
            probe.return_value['stdout'] = 'Unsupported resume options'
            with self.assertRaisesRegex(ValueError, 'conversion unsupported'):
                self.dry_run()
            execute.assert_not_called()

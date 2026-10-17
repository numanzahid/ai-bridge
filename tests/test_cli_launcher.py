"""Executable launcher and registration tests confined to temporary homes/releases."""
import contextlib
import io
import json
import os
import runpy
import stat
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import cli_install
import deploy

SOURCE = Path(__file__).resolve().parents[1]


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='aib-launcher-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / 'home with spaces'
        self.home.mkdir(mode=0o700)
        self.root = self.home / '.local/share/ai-bridge'
        (self.root / 'releases').mkdir(parents=True)
        self.release = self.make_release('first')
        (self.root / 'current').symlink_to(self.release)
        self.env = dict(os.environ, HOME=str(self.home), PATH=str(self.home / '.local/bin') + os.pathsep + os.environ['PATH'],
                        AI_BRIDGE_STATE_DIR=str(self.home / 'state'), AI_BRIDGE_TASKS_DIR=str(self.home / 'tasks'))
        self.env.pop('AI_BRIDGE_TOKEN', None)
        self.env.pop('CREDENTIALS_DIRECTORY', None)

    def make_release(self, name, fake=False):
        release = self.root / 'releases' / name
        (release / 'app/ai_bridge').mkdir(parents=True)
        (release / 'app/ai_bridge/__init__.py').write_text('')
        if fake:
            (release / 'app/ai_bridge/cli.py').write_text('def main():\n print("' + name + '"); return 0\n')
        else:
            import shutil
            shutil.rmtree(release / 'app/ai_bridge')
            shutil.copytree(SOURCE / 'ai_bridge', release / 'app/ai_bridge', ignore=shutil.ignore_patterns('__pycache__'))
        (release / 'venv/bin').mkdir(parents=True)
        (release / 'venv/bin/python').symlink_to(sys.executable)
        (release / 'READY').write_text('Fixture staged validation passed\n')
        return release

    def install(self):
        return cli_install.install(self.home, os.geteuid(), SOURCE / 'ai-bridge')

    def run_cli(self, *args, ok=True):
        result = subprocess.run(['ai-bridge', *args], cwd=self.home, env=self.env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0 if ok else 1, result.stderr)
        return result

    def test_path_invocation_without_token_from_any_working_directory(self):
        target = self.install()
        self.assertEqual(target.stat().st_mode & 0o777, 0o755)
        for args in (('agents', 'status'), ('tasks', 'list'), ('sessions', 'list', '--agent', 'codex')):
            # Session fixture roots prevent any accidental read of real native stores.
            self.env.update(CODEX_HOME=str(self.home / 'codex'))
            json.loads(self.run_cli(*args).stdout)
        self.assertIn('usage:', self.run_cli().stdout)
        self.assertEqual(cli_install.registered_launcher(self.home, os.geteuid()), target)
        self.assertEqual(self.install(), target)
        self.assertEqual((self.root / '.cli-launcher.json').stat().st_mode & 0o777, 0o600)

    def test_restrictive_umask_still_creates_usable_private_registration(self):
        previous = os.umask(0o777)
        try:
            target = self.install()
        finally:
            os.umask(previous)
        self.assertEqual(target.stat().st_mode & 0o777, 0o755)
        self.assertEqual((self.root / '.cli-launcher.json').stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(self.run_cli('agents', 'status').stdout), [])

    def test_current_switch_and_rollback_do_not_modify_launcher(self):
        target = self.install()
        original = target.read_bytes()
        first = self.make_release('version-one', True)
        second = self.make_release('version-two', True)
        current = self.root / 'current'
        for release in (first, second, first):
            current.unlink()
            current.symlink_to(release)
            self.assertEqual(self.run_cli('agents', 'status').stdout.strip(), release.name)
            self.assertEqual(target.read_bytes(), original)

    def test_unavailable_or_old_release_fails_without_fallback_or_server(self):
        self.install()
        (self.release / 'app/ai_bridge/cli.py').unlink()
        result = self.run_cli('tasks', 'list', ok=False)
        self.assertIn('no service was changed', result.stderr)
        self.assertNotIn('AI_BRIDGE_TOKEN', result.stderr)

    def test_foreign_file_symlink_and_tampering_are_never_overwritten(self):
        target = self.home / '.local/bin/ai-bridge'
        target.parent.mkdir()
        target.write_text('foreign')
        with self.assertRaisesRegex(ValueError, 'foreign'):
            self.install()
        self.assertEqual(target.read_text(), 'foreign')
        target.unlink()
        target.symlink_to(self.home / 'outside')
        with self.assertRaises(ValueError):
            self.install()
        target.unlink()
        self.install()
        target.write_text('modified')
        with self.assertRaisesRegex(ValueError, 'differs'):
            self.install()
        self.assertEqual(target.read_text(), 'modified')

    def test_source_update_refused_preserving_rollback(self):
        target = self.install()
        alternative = self.home / 'new-source'
        alternative.write_bytes(target.read_bytes() + b'\n# changed fixture\n')
        before = target.read_bytes()
        with self.assertRaisesRegex(ValueError, 'No update'):
            cli_install.install(self.home, os.geteuid(), alternative)
        self.assertEqual(target.read_bytes(), before)

    def test_symlink_bin_and_unowned_path_refused(self):
        elsewhere = self.home / 'other-bin'
        elsewhere.mkdir()
        (self.home / '.local/bin').symlink_to(elsewhere, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.install()
        self.assertEqual(list(elsewhere.iterdir()), [])
        (self.home / '.local/bin').unlink()
        with self.assertRaisesRegex(ValueError, 'service account'):
            cli_install.install(self.home, os.geteuid() + 1, SOURCE / 'ai-bridge')
        self.home.chmod(0o777)
        with self.assertRaisesRegex(ValueError, 'ownership'):
            self.install()

    def test_release_escape_and_missing_current_refused(self):
        current = self.root / 'current'
        current.unlink()
        with self.assertRaises(ValueError):
            self.install()
        current.symlink_to(self.home)
        with self.assertRaisesRegex(ValueError, 'outside'):
            self.install()
        self.assertFalse((self.home / '.local/bin/ai-bridge').exists())

    def test_installer_dispatch_is_early_and_nonprivileged(self):
        source = (SOURCE / 'install.sh').read_text()
        self.assertLess(source.index('= --install-cli ]'), source.index('DATA_DIR='))
        fake_source = self.home / 'source'
        fake_source.mkdir()
        (fake_source / 'install.sh').write_text(source)
        (fake_source / 'cli_install.py').write_text('import sys; print("cli-only"); sys.exit(7)')
        result = subprocess.run(['bash', str(fake_source / 'install.sh'), '--install-cli'],
                                env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.stdout.strip(), 'cli-only')
        self.assertFalse((self.home / '.local/bin').exists())

    def test_main_rejects_mismatched_home_and_extra_args(self):
        with patch.object(sys, 'argv', ['cli_install.py', 'unexpected']):
            with self.assertRaises(ValueError):
                cli_install.main()
        with patch.object(sys, 'argv', ['cli_install.py']), patch.dict(os.environ, HOME=str(self.home)):
            with self.assertRaisesRegex(ValueError, 'passwd'):
                cli_install.main()

    def test_isolated_wrapper_ignores_pythonpath_and_home_override(self):
        self.install()
        bad = self.home / 'bad-pythonpath'
        bad.mkdir()
        (bad / 'ai_bridge.py').write_text('raise RuntimeError("ambient import")')
        (bad / 'sitecustomize.py').write_text('import sys; sys.stderr.write("UNSAFE_STARTUP\\n")')
        self.env.update(PYTHONPATH=str(bad), HOME=str(self.home / 'unrelated'))
        result = self.run_cli('agents', 'status')
        self.assertEqual(json.loads(result.stdout), [])
        self.assertNotIn('UNSAFE_STARTUP', result.stderr)

    @contextlib.contextmanager
    def selector_owner(self, uid):
        current = self.root / 'current'
        original = Path.lstat
        def metadata(path, *args, **kwargs):
            value = original(path, *args, **kwargs)
            if path == current:
                fields = list(value)
                fields[4] = uid
                return os.stat_result(fields)
            return value
        with patch.object(Path, 'lstat', metadata):
            yield

    def test_root_owned_selector_accepts_private_group_ancestors_and_launches(self):
        with self.group_fixture(), self.selector_owner(0):
            target = self.install()
            self.assertEqual(self.install(), target)
            with patch('os.execv') as launch, patch.object(sys, 'argv', ['ai-bridge', 'agents', 'status']):
                runpy.run_path(str(target), run_name='__main__')
            executable, argv = launch.call_args.args
        self.assertEqual(executable, str(self.release / 'venv/bin/python'))
        result = subprocess.run(argv, cwd=self.home, env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [])

    def test_foreign_owned_selector_refused_without_overwrite_or_launch(self):
        target = self.install()
        before = target.read_bytes()
        foreign_uid = 2000 if os.geteuid() != 2000 else 2001
        with self.selector_owner(foreign_uid):
            with self.assertRaisesRegex(ValueError, 'Select a verified'):
                self.install()
            with contextlib.redirect_stderr(io.StringIO()), patch('os.execv') as launch:
                with self.assertRaises(SystemExit) as result:
                    runpy.run_path(str(target), run_name='__main__')
                self.assertEqual(result.exception.code, 1)
                launch.assert_not_called()
        self.assertEqual(target.read_bytes(), before)

    def test_root_selector_cannot_escape_release_root(self):
        target = self.install()
        current = self.root / 'current'
        current.unlink()
        current.symlink_to(self.home)
        with self.selector_owner(0):
            with self.assertRaisesRegex(ValueError, 'outside'):
                self.install()
            with contextlib.redirect_stderr(io.StringIO()), patch('os.execv') as launch:
                with self.assertRaises(SystemExit):
                    runpy.run_path(str(target), run_name='__main__')
                launch.assert_not_called()

    def test_root_selector_does_not_relax_selected_file_or_directory_ownership(self):
        target = self.install()
        original = Path.stat
        for artifact in (self.release, self.release / 'app', self.release / 'app/ai_bridge',
                         self.release / 'venv', self.release / 'venv/bin', self.release / 'READY',
                         self.release / 'app/ai_bridge/cli.py'):
            def foreign(path, *args, **kwargs):
                value = original(path, *args, **kwargs)
                if path == artifact:
                    fields = list(value)
                    fields[4] = 2000 if os.geteuid() != 2000 else 2001
                    return os.stat_result(fields)
                return value
            with self.subTest(artifact=artifact), self.selector_owner(0), patch.object(Path, 'stat', foreign):
                with self.assertRaisesRegex(ValueError, 'ownership'):
                    self.install()
                with contextlib.redirect_stderr(io.StringIO()), patch('os.execv') as launch:
                    with self.assertRaises(SystemExit):
                        runpy.run_path(str(target), run_name='__main__')
                    launch.assert_not_called()

    @contextlib.contextmanager
    def group_fixture(self, mode=0o775, members=(), other_primary=False, missing=False, wrong_gid=False):
        uid, gid = os.geteuid(), os.getgid()
        account = SimpleNamespace(pw_name='fixture-owner', pw_uid=uid, pw_gid=gid)
        group = SimpleNamespace(gr_name='fixture-private', gr_gid=gid, gr_mem=list(members))
        accounts = [account]
        if other_primary:
            accounts.append(SimpleNamespace(pw_name='fixture-other', pw_uid=uid + 1, pw_gid=gid))
        ancestors = {self.home, self.home / '.local', self.home / '.local/bin',
                     self.home / '.local/share', self.root}
        original = Path.stat
        def permissions(path, *args, **kwargs):
            value = original(path, *args, **kwargs)
            if path not in ancestors:
                return value
            values = list(value)
            values[0] = stat.S_IFMT(value.st_mode) | mode
            values[5] = gid + 1 if wrong_gid else gid
            return os.stat_result(values)
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(Path, 'stat', permissions))
            stack.enter_context(patch('pwd.getpwuid', return_value=account))
            stack.enter_context(patch('pwd.getpwall', return_value=[] if missing else accounts))
            stack.enter_context(patch('grp.getgrgid', return_value=group))
            stack.enter_context(patch('grp.getgrall', return_value=[group]))
            yield

    def test_private_primary_0775_install_and_current_launcher_start(self):
        # Mock permission metadata only; no existing directory is chmodded.
        with self.group_fixture():
            target = self.install()
            self.assertEqual(self.install(), target)
            with patch('os.execv') as launch, patch.object(sys, 'argv', ['ai-bridge', 'agents', 'status']):
                runpy.run_path(str(target), run_name='__main__')
                executable, argv = launch.call_args.args
        self.assertEqual(executable, str(self.release / 'venv/bin/python'))
        # Execute the actual pinned app selected by the launcher preflight.
        result = subprocess.run(argv, cwd=self.home, env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [])
        self.assertEqual(cli_install.registered_launcher(self.home, os.geteuid()), target)

    def test_shared_primary_supplementary_and_world_write_refused_by_both(self):
        self.install()
        launcher = runpy.run_path(str(SOURCE / 'ai-bridge'), run_name='launcher_fixture')
        for options in ({'other_primary': True}, {'members': ['fixture-other']},
                        {'mode': 0o777}, {'missing': True}, {'wrong_gid': True}):
            with self.subTest(options=options), self.group_fixture(**options):
                with self.assertRaises(ValueError):
                    cli_install.checked(self.home / '.local', os.geteuid(), True, allow_private_group=True)
                with self.assertRaises(ValueError):
                    launcher['owned'](self.home / '.local', allow_private_group=True)
                with self.assertRaises(ValueError):
                    self.install()
        self.assertEqual((self.home / '.local/bin/ai-bridge').read_bytes(), (SOURCE / 'ai-bridge').read_bytes())

    def test_private_group_does_not_weaken_release_or_file_checks(self):
        launcher = runpy.run_path(str(SOURCE / 'ai-bridge'), run_name='launcher_fixture')
        with self.group_fixture():
            with self.assertRaises(ValueError):
                cli_install.checked(self.home / '.local', os.geteuid(), True)
            with self.assertRaises(ValueError):
                launcher['owned'](self.home / '.local')
            with self.assertRaises(ValueError):
                cli_install.checked(self.home / '.local', os.geteuid(), False, allow_private_group=True)
            with self.assertRaises(ValueError):
                launcher['owned'](self.home / '.local', False, allow_private_group=True)
        self.assertEqual(launcher['private_primary_group'].__code__.co_code,
                         cli_install.private_primary_group.__code__.co_code)

    def test_private_group_never_overwrites_foreign_or_modified_launcher(self):
        target = self.home / '.local/bin/ai-bridge'
        target.parent.mkdir()
        target.write_text('foreign')
        with self.group_fixture(), self.assertRaisesRegex(ValueError, 'foreign'):
            self.install()
        self.assertEqual(target.read_text(), 'foreign')
        target.unlink()
        self.install()
        target.write_text('modified')
        with self.group_fixture(), self.assertRaisesRegex(ValueError, 'differs'):
            self.install()
        self.assertEqual(target.read_text(), 'modified')

    def test_private_group_acl_or_unreadable_acl_metadata_is_not_accepted(self):
        launcher = runpy.run_path(str(SOURCE / 'ai-bridge'), run_name='launcher_fixture')
        for result in (['system.posix_acl_access'], ['system.posix_acl_default']):
            with self.group_fixture(), patch('os.listxattr', return_value=result):
                with self.assertRaises(ValueError):
                    cli_install.checked(self.home / '.local', os.geteuid(), True, allow_private_group=True)
                with self.assertRaises(ValueError):
                    launcher['owned'](self.home / '.local', allow_private_group=True)
        with self.group_fixture(), patch('os.listxattr', side_effect=OSError('unknown ACL state')):
            with self.assertRaises(OSError):
                cli_install.checked(self.home / '.local', os.geteuid(), True, allow_private_group=True)
            with self.assertRaises(OSError):
                launcher['owned'](self.home / '.local', allow_private_group=True)

    def test_group_database_errors_and_inconsistent_enumeration_fail_closed(self):
        launcher = runpy.run_path(str(SOURCE / 'ai-bridge'), run_name='launcher_fixture')
        for check in (cli_install.private_primary_group, launcher['private_primary_group']):
            with self.group_fixture(), patch('grp.getgrall', side_effect=OSError('unavailable')):
                self.assertFalse(check(os.geteuid(), os.getgid()))
            with self.group_fixture(), patch('grp.getgrall', return_value=[]):
                self.assertFalse(check(os.geteuid(), os.getgid()))
            with self.group_fixture(), patch('os.getegid', return_value=os.getgid() + 1):
                self.assertFalse(check(os.geteuid(), os.getgid()))

    def test_stage_checksums_launcher_without_registering_it(self):
        root = self.home / 'staging'
        def builder(release, source, python):
            (release / 'venv').mkdir()
        release = deploy.stage(root, SOURCE, builder=builder, tester=lambda *_: None)
        checksums = json.loads((release / 'SHA256.json').read_text())
        self.assertIn('app/ai-bridge', checksums)
        self.assertIn('app/cli_install.py', checksums)
        self.assertFalse((self.home / '.local/bin/ai-bridge').exists())
        self.assertFalse((root / 'current').exists())

import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import bootstrap


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root/'home'
        self.home.mkdir()
        self.account = SimpleNamespace(pw_name='bridge-test', pw_uid=os.getuid(), pw_gid=os.getgid(), pw_dir=str(self.home))

    def test_dedicated_bootstrap_refuses_unattended_before_account_changes(self):
        with patch('sys.argv', ['bootstrap.py', '--source', str(self.root)]), \
             patch('os.geteuid', return_value=0), patch('sys.stdin.isatty', return_value=False), \
             patch('bootstrap.dedicated') as start:
            with self.assertRaisesRegex(ValueError, 'interactive terminal'):
                bootstrap.main()
            start.assert_not_called()

    def test_existing_identity_and_incomplete_install_are_checked(self):
        etc, unit = self.root/'etc', self.root/'unit'
        etc.mkdir()
        with self.assertRaisesRegex(ValueError, 'another identity or is incomplete'):
            bootstrap.installation_matches(self.home, self.account.pw_name, etc, unit)
        (self.home/'.local/share/ai-bridge/app').mkdir(parents=True)
        unit.write_text('User=root\n')
        with self.assertRaises(ValueError):
            bootstrap.installation_matches(self.home, self.account.pw_name, etc, unit)
        unit.write_text('User=bridge-test\n')
        self.assertTrue(bootstrap.installation_matches(self.home, self.account.pw_name, etc, unit))

    def test_reused_user_must_own_home_and_not_be_privileged(self):
        self.account.pw_uid = max(1000, os.getuid())
        original = Path.stat
        def owned(path, *args, **kwargs):
            value = original(path, *args, **kwargs)
            return SimpleNamespace(st_uid=self.account.pw_uid, st_mode=value.st_mode) if path == self.home else value
        with patch.object(Path, 'stat', owned):
            bootstrap.validate_user(self.account)
        self.account.pw_uid = 0
        with self.assertRaises(ValueError):
            bootstrap.validate_user(self.account)
        self.account.pw_uid = os.getuid()+1
        with self.assertRaisesRegex(ValueError, 'own its home'):
            bootstrap.validate_user(self.account)

    def test_missing_dependency_never_creates_user_or_falls_back_to_root(self):
        with patch('shutil.which', return_value=None), patch('subprocess.run') as run:
            with self.assertRaisesRegex(ValueError, 'will not fall back'):
                bootstrap.dedicated(self.root, 'bridge-test')
            run.assert_not_called()

    def test_existing_install_skips_password_changes_and_switches_identity(self):
        with patch('bootstrap.validate_user'), patch('shutil.which', return_value='/fake'), patch('pwd.getpwnam', return_value=self.account), \
             patch('bootstrap.installation_matches', return_value=True), patch('bootstrap.configure_sudo') as configure, \
             patch('bootstrap.copy_source', return_value=self.root), patch('os.chdir'), \
             patch('os.execvpe') as execute:
            bootstrap.dedicated(self.root, 'bridge-test')
        configure.assert_not_called()
        args = execute.call_args.args
        self.assertEqual(args[0], 'runuser')
        self.assertEqual(args[1][:4], ['runuser', '-u', 'bridge-test', '--'])
        self.assertEqual(args[2]['HOME'], str(self.home))
        self.assertNotIn('SUDO_ASKPASS', args[2])

    def test_copy_excludes_secrets_and_never_changes_checkout_ownership(self):
        source, parent = self.root/'source', self.root/'copies'
        source.mkdir(); parent.mkdir(mode=0o700)
        (source/'install.sh').write_text('source')
        (source/'.env').write_text('not copied')
        (source/'.git').mkdir()
        with patch('os.chown') as chown:
            # Only simulate ownership of the root-controlled copy parent.
            original = Path.stat
            def stat(path, *args, **kwargs):
                value = original(path, *args, **kwargs)
                return SimpleNamespace(st_uid=0, st_mode=value.st_mode) if path == parent else value
            with patch.object(Path, 'stat', stat):
                result = bootstrap.copy_source(source, parent, self.account)
        self.assertEqual(parent.stat().st_mode & 0o777, 0o755)
        self.assertEqual((result/'install.sh').read_text(), 'source')
        self.assertFalse((result/'.env').exists())
        self.assertFalse((result/'.git').exists())
        self.assertTrue(all(result == call.args[0] or result in call.args[0].parents for call in chown.call_args_list))
        (source/'docs').symlink_to(self.home, target_is_directory=True)
        with patch.object(Path, 'stat', stat), patch('os.chown'):
            with self.assertRaisesRegex(ValueError, 'symlinks'):
                bootstrap.copy_source(source, parent, self.account)

    def test_password_policy_is_explicit_and_validated_without_nopasswd(self):
        policy = self.root/'policy'; policy.mkdir()
        with patch('shutil.which', return_value='/fake'), patch('builtins.input', return_value='y'), \
             patch('getpass.getpass', return_value='temporary-test-password'), patch('subprocess.run') as run:
            bootstrap.configure_sudo(self.account, True, policy)
        target = policy/('ai-bridge-'+self.account.pw_name)
        self.assertEqual(target.stat().st_mode & 0o777, 0o440)
        self.assertEqual(target.read_text(), 'bridge-test ALL=(root) PASSWD: ALL\n')
        self.assertNotIn('NOPASSWD', target.read_text())
        self.assertEqual(run.call_args_list[0].args[0], ['chpasswd'])
        self.assertEqual(run.call_args_list[1].args[0][:2], ['visudo', '-cf'])
        self.assertNotIn('temporary-test-password', target.read_text())

    def test_declined_or_invalid_policy_never_installs_it(self):
        policy = self.root/'policy'; policy.mkdir()
        with patch('shutil.which', return_value='/fake'), patch('builtins.input', return_value='n'), patch('subprocess.run') as run:
            with self.assertRaisesRegex(ValueError, 'declined'):
                bootstrap.configure_sudo(self.account, False, policy)
            run.assert_not_called()
        self.assertEqual(list(policy.iterdir()), [])
        with patch('shutil.which', return_value='/fake'), patch('builtins.input', return_value='y'), \
             patch('subprocess.run', side_effect=subprocess.CalledProcessError(1, ['visudo'])):
            with self.assertRaises(subprocess.CalledProcessError):
                bootstrap.configure_sudo(self.account, False, policy)
        self.assertEqual(list(policy.iterdir()), [])

    def test_reused_account_password_is_never_reset(self):
        policy = self.root/'policy'; policy.mkdir()
        with patch('shutil.which', return_value='/fake'), patch('builtins.input', return_value='y'), \
             patch('getpass.getpass', side_effect=AssertionError('do not reset existing password')), patch('subprocess.run') as run:
            bootstrap.configure_sudo(self.account, False, policy)
        self.assertTrue(all(call.args[0][0] != 'chpasswd' for call in run.call_args_list))

    def test_conflicting_existing_sudo_policy_is_not_overwritten(self):
        policy = self.root/'policy'; policy.mkdir()
        target = policy/('ai-bridge-'+self.account.pw_name)
        target.write_text('existing policy\n')
        with patch('shutil.which', return_value='/fake'), patch('builtins.input', return_value='y'), patch('subprocess.run') as run:
            with self.assertRaisesRegex(ValueError, 'Existing sudo policy differs'):
                bootstrap.configure_sudo(self.account, False, policy)
            run.assert_not_called()
        self.assertEqual(target.read_text(), 'existing policy\n')

    def test_missing_sudo_install_is_explicit_and_package_failure_stops(self):
        def discover(name):
            return '/fake/'+name if name in ('apt-get', 'runuser', 'useradd', 'chpasswd') else None
        with patch('shutil.which', side_effect=discover), patch('builtins.input', return_value='n'), patch('subprocess.run') as run:
            with self.assertRaisesRegex(ValueError, 'will not fall back'):
                bootstrap.prerequisites()
            run.assert_not_called()
        with patch('shutil.which', side_effect=discover), patch('builtins.input', return_value='y'), \
             patch('subprocess.run', side_effect=subprocess.CalledProcessError(1, ['apt-get'])) as run:
            with self.assertRaises(subprocess.CalledProcessError):
                bootstrap.prerequisites()
            self.assertEqual(run.call_args.args[0], ['apt-get', 'update'])

    def test_account_and_copy_failures_never_execute_installer(self):
        with patch('bootstrap.prerequisites'), patch('pwd.getpwnam', side_effect=KeyError), \
             patch('bootstrap.installation_matches', return_value=False), patch.object(Path, 'exists', return_value=False), \
             patch('subprocess.run', side_effect=subprocess.CalledProcessError(2, ['useradd'])), patch('os.execvpe') as execute:
            with self.assertRaises(subprocess.CalledProcessError):
                bootstrap.dedicated(self.root, 'bridge-test')
            execute.assert_not_called()
        with patch('bootstrap.prerequisites'), patch('pwd.getpwnam', return_value=self.account), \
             patch('bootstrap.validate_user'), patch('bootstrap.installation_matches', return_value=False), \
             patch('bootstrap.configure_sudo'), patch('bootstrap.copy_source', side_effect=OSError('copy failed')), patch('os.execvpe') as execute:
            with self.assertRaisesRegex(OSError, 'copy failed'):
                bootstrap.dedicated(self.root, 'bridge-test')
            execute.assert_not_called()

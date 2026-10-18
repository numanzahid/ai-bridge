import contextlib
import io
import json
import os
import shlex
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import uninstall as removal


class UninstallTests(unittest.TestCase):
    def setUp(self):
        # Trust checks refuse group-writable artifacts; do not inherit the invoker's umask.
        self.addCleanup(os.umask, os.umask(0o022))
        self.tmp = tempfile.TemporaryDirectory(prefix='bridge-uninstall-fixture-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.layout = removal.Layout(self.root)
        self.home = self.root/'home/service user'
        self.home.mkdir(parents=True)
        self.account = SimpleNamespace(pw_name='service-user', pw_uid=os.getuid(), pw_dir=str(self.home))
        self.lookup = lambda name: self.account if name == self.account.pw_name else (_ for _ in ()).throw(KeyError(name))
        # Model root-owned system artifacts using only this test user's temporary tree.
        def trusted(path):
            removal.safe_path(path)
            if not path.is_relative_to(self.root) or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o022:
                raise removal.Refusal('Untrusted fixture artifact')
        self.guard = patch.object(removal, 'trusted', side_effect=trusted)
        self.guard.start(); self.addCleanup(self.guard.stop)
        inventory = patch.object(removal, 'process_inventory', return_value={'runners': [], 'groups': set()})
        inventory.start(); self.addCleanup(inventory.stop)
        self.calls = []
        self.effective_user = self.account.pw_name
        self.dropins = ''
        self.main_pid = '0'
        self.app = self.home/'.local/share/ai-bridge'
        (self.app/'app/ai_bridge').mkdir(parents=True)
        (self.app/'app/ai_bridge/core.py').write_text('public source fixture')
        self.state = self.home/'.local/state/ai-bridge'
        (self.state/'jobs').mkdir(parents=True)
        self.config = self.home/'.config/ai-bridge'
        (self.config/'instructions').mkdir(parents=True)
        (self.config/'instructions/user.md').write_text('fixture guidance')
        self.layout.etc.mkdir(parents=True)
        (self.layout.etc/'ai-bridge.env').write_text('AI_BRIDGE_TOKEN=synthetic-never-print\n')
        (self.layout.etc/'sudo.pass').write_text('synthetic-password-never-print')
        self.layout.unit.parent.mkdir(parents=True)
        self.layout.unit.write_text(self.unit())

    def unit(self, user=None, executable=None):
        return ('[Unit]\nDescription=AI Bridge\nAfter=network-online.target\nWants=network-online.target\n'
                '[Service]\nUser='+str(user or self.account.pw_name)+'\nKillMode=process\n'
                'EnvironmentFile='+str(self.layout.etc/'ai-bridge.env')+'\n'
                'ExecStart='+shlex.quote(str(executable or self.app/'venv/bin/python'))+' -m ai_bridge\n'
                '[Install]\nWantedBy=multi-user.target\n')

    def systemctl(self, *args):
        self.calls.append(args)
        if '--property=KillMode' in args:
            return 'process'
        if '--property=User' in args:
            return self.effective_user
        if '--property=FragmentPath' in args:
            return str(self.layout.unit)
        if '--property=DropInPaths' in args:
            return self.dropins
        if '--property=MainPID' in args:
            return self.main_pid
        return ''

    def plan(self, user=None):
        return removal.Plan(self.layout, self.lookup, self.systemctl).build(user)

    def test_identity_comes_from_unit_not_invoker_home_and_full_manifest(self):
        with patch.dict(os.environ, HOME='/wrong/invoker', AI_BRIDGE_CONFIG_DIR='/unrelated'):
            plan = self.plan()
        paths = {row['path'] for row in plan.targets}
        for path in (self.app, self.state, self.config, self.layout.etc, self.layout.unit):
            self.assertIn(str(path), paths)
        manifest = json.dumps(plan.manifest())
        self.assertNotIn('synthetic-never-print', manifest)
        self.assertNotIn('synthetic-password', manifest)
        self.assertEqual(plan.account.pw_name, 'service-user')
        self.assertTrue(all(call[0] == 'show' for call in self.calls))

    def test_removal_deletes_only_fixture_targets_and_is_idempotent(self):
        native = self.home/'.codex'; native.mkdir(); (native/'keep').write_text('native')
        clone = self.root/'opt/ai-bridge'; clone.mkdir(parents=True); (clone/'keep').write_text('source')
        for directory in ('resources', 'prompts', 'overrides'):
            (self.config/directory).mkdir()
        backup = self.layout.unit.with_name('ai-bridge.service.before-versioned'); backup.write_text(self.unit())
        plan = self.plan()
        with contextlib.redirect_stdout(io.StringIO()):
            plan.execute()
        for row in plan.targets:
            self.assertFalse(Path(row['path']).exists())
        self.assertTrue(native.exists()); self.assertTrue(clone.exists()); self.assertTrue(self.home.exists())
        self.assertIn(('stop', 'ai-bridge.service'), self.calls)
        self.assertIn(('disable', 'ai-bridge.service'), self.calls)
        self.assertIn(('daemon-reload',), self.calls)
        self.assertEqual(self.plan().targets, [])

    def test_root_service_identity_uses_root_passwd_home(self):
        self.account.pw_name = 'root'; self.effective_user = 'root'
        self.layout.unit.write_text(self.unit())
        plan = self.plan()
        self.assertEqual(plan.account.pw_name, 'root')
        self.assertEqual(plan.account.pw_dir, str(self.home))
        self.assertIn(str(self.app), {row['path'] for row in plan.targets})

    def test_missing_unit_requires_explicit_legacy_identity(self):
        self.layout.unit.unlink()
        with self.assertRaisesRegex(removal.Refusal, 'Specify --user'):
            self.plan()
        plan = self.plan('service-user')
        self.assertFalse(plan.had_unit)
        self.assertIn(str(self.app), {row['path'] for row in plan.targets})
        self.assertFalse(self.calls)

    def test_registry_recovers_identity_without_unit(self):
        removal.record_install(self.layout, self.account)
        self.layout.unit.unlink()
        self.assertEqual(self.plan().account.pw_name, self.account.pw_name)
        with self.assertRaisesRegex(removal.Refusal, 'does not match'):
            self.plan('other-user')

    def test_false_unit_and_custom_exec_actions_refused(self):
        for value in (self.unit(executable=Path('/opt/unrelated/bin/python')), self.unit().replace('-m ai_bridge', '-m other'),
                      self.unit().replace('KillMode=process', 'KillMode=control-group'),
                      self.unit().replace('[Install]', 'ExecStop=/bin/false\n[Install]'),
                      self.unit()+'Alias=unrelated.service\n'):
            self.layout.unit.write_text(value)
            with self.assertRaises(removal.Refusal):
                self.plan()
        self.assertTrue(all(call[0] == 'show' for call in self.calls))

    def test_effective_identity_or_unmanaged_dropins_refused(self):
        self.effective_user = 'someone-else'
        with self.assertRaisesRegex(removal.Refusal, 'Effective service identity'):
            self.plan()
        self.effective_user = self.account.pw_name
        drops = self.layout.unit.with_name('ai-bridge.service.d'); drops.mkdir()
        entry = drops/'custom.conf'; entry.write_text('[Service]\nMemoryMax=1G\n')
        with self.assertRaisesRegex(removal.Refusal, 'Unmanaged service drop-in'):
            self.plan()
        entry.write_text('# AI-Bridge-owned drop-in\n[Service]\nMemoryMax=1G\n')
        self.dropins = str(entry)
        self.assertIn(str(drops), {row['path'] for row in self.plan().targets})
        entry.write_text('# AI-Bridge-owned drop-in\n[Service]\nUser=other\n')
        with self.assertRaisesRegex(removal.Refusal, 'lifecycle'):
            self.plan()

    def test_running_uncertain_and_corrupt_jobs_block_without_pid_signals(self):
        for meta in ({'status':'running', 'pid':os.getpid()}, {'status':'cleanup_incomplete'}, {'status':'lost'}):
            (self.state/'jobs/legacy-job.json').write_text(json.dumps(meta))
            plan = self.plan()
            self.assertTrue(plan.blockers)
            with patch('os.kill', side_effect=AssertionError('never signal a job')), self.assertRaises(removal.Refusal):
                plan.execute()
        (self.state/'jobs/legacy-job.json').write_text('{bad')
        self.assertTrue(self.plan().blockers)
        self.assertFalse(any(call[0] == 'stop' for call in self.calls))
        # Completed jobs, including the old root-level sidecar layout, do not block.
        (self.state/'jobs/legacy-job.json').write_text('{"status":"done"}')
        (self.state/'jobs/legacy-job.process.json').write_text('{"pid":123}')
        self.assertFalse(self.plan().blockers)

    def test_symlinked_targets_and_parent_paths_refused(self):
        alternate = self.root/'other'; alternate.mkdir()
        self.config.rename(self.home/'.config/original')
        self.config.symlink_to(alternate, target_is_directory=True)
        with self.assertRaisesRegex(removal.Refusal, 'Symlink'):
            self.plan()
        self.assertTrue(alternate.exists())

    def test_internal_links_are_unlinked_without_following(self):
        keep = self.root/'keep'; keep.mkdir(); (keep/'data').write_text('keep')
        (self.config/'resources').symlink_to(keep, target_is_directory=True)
        with contextlib.redirect_stdout(io.StringIO()):
            self.plan().execute()
        self.assertEqual((keep/'data').read_text(), 'keep')

    def test_unmanaged_app_and_source_clones_are_refused(self):
        (self.app/'app/ai_bridge/core.py').unlink()
        with self.assertRaisesRegex(removal.Refusal, 'Unmanaged application'):
            self.plan()
        (self.app/'app/ai_bridge/core.py').write_text('source')
        (self.app/'.git').mkdir()
        with self.assertRaisesRegex(removal.Refusal, 'Source repository retained'):
            self.plan()

    def test_alternate_paths_need_exact_trusted_record_not_environment_alone(self):
        alternate = self.root/'custom space/ai-bridge'; (alternate/'instructions').mkdir(parents=True)
        env = self.layout.etc/'ai-bridge.env'
        env.write_text('AI_BRIDGE_CONFIG_DIR="'+str(alternate)+'"\n')
        with self.assertRaisesRegex(removal.Refusal, 'trusted ownership record'):
            self.plan()
        removal.record_install(self.layout, self.account)
        self.assertIn(str(alternate), {row['path'] for row in self.plan().targets})
        alternate.rename(alternate.with_name('original'))
        alternate.mkdir()
        with self.assertRaisesRegex(removal.Refusal, 'trusted ownership record'):
            self.plan()
        base = self.root/'xdg'; (base/'ai-bridge/instructions').mkdir(parents=True)
        env.write_text('XDG_CONFIG_HOME='+str(base)+'\n')
        removal.record_install(self.layout, self.account)
        self.assertIn(str(base/'ai-bridge'), {row['path'] for row in self.plan().targets})

    def test_broad_custom_and_native_directories_are_never_registered(self):
        env = self.layout.etc/'ai-bridge.env'
        for directory in (self.home, self.home/'.codex/ai-bridge'):
            directory.mkdir(parents=True, exist_ok=True)
            env.write_text('AI_BRIDGE_CONFIG_DIR='+str(directory)+'\n')
            with contextlib.redirect_stderr(io.StringIO()):
                removal.record_install(self.layout, self.account)
            self.assertNotIn('config', json.loads(self.layout.registry.read_text())['directories'])
            with self.assertRaises(removal.Refusal):
                self.plan()

    def test_bootstrap_copies_bound_to_identity_siblings_retained(self):
        copy = self.layout.pool/'service-user-unique'; (copy/'ai_bridge').mkdir(parents=True)
        (copy/'install.sh').write_text('fixture'); (copy/'ai_bridge/core.py').write_text('fixture')
        sibling = self.layout.pool/'other-user-copy'; sibling.mkdir()
        plan = self.plan()
        self.assertIn(str(copy), {row['path'] for row in plan.targets})
        with contextlib.redirect_stdout(io.StringIO()):
            plan.execute()
        self.assertTrue(sibling.exists())
        self.assertFalse(copy.exists())

    def test_change_after_preview_and_partial_failure_reported(self):
        plan = self.plan()
        self.layout.unit.write_text(self.unit()+'# changed\n')
        with self.assertRaisesRegex(removal.Refusal, 'changed after preview'):
            plan.execute()
        plan = self.plan()
        with patch('shutil.rmtree', side_effect=OSError('fixture failure')) as delete:
            delete.avoids_symlink_attacks = True
            with self.assertRaisesRegex(removal.Refusal, 'PARTIAL REMOVAL'):
                plan.execute()
        self.assertTrue(self.app.exists())

    def test_main_dry_run_confirmation_and_bridge_guard(self):
        plan = self.plan()
        with patch('os.geteuid', return_value=0), patch.object(removal, 'Layout', return_value=self.layout), \
             patch.object(removal, 'Plan') as factory, patch.object(removal, 'inside_bridge', return_value=False), \
             patch.object(plan, 'execute') as execute, contextlib.redirect_stdout(io.StringIO()) as output:
            factory.return_value.build.return_value = plan
            self.assertEqual(removal.main(['--dry-run']), 0)
            execute.assert_not_called()
            self.assertIn(str(self.config), output.getvalue())
            with patch('sys.stdin.isatty', return_value=False), self.assertRaisesRegex(removal.Refusal, 'Noninteractive'):
                removal.main([])
            with self.assertRaisesRegex(removal.Refusal, 'Automation requires'):
                removal.main(['--yes'])
            phrase = 'UNINSTALL AI-BRIDGE for '+self.account.pw_name
            self.assertEqual(removal.main(['--yes', '--confirm', phrase, '--manifest-sha256', plan.digest()]), 0)
            execute.assert_called_once()
            execute.reset_mock()
            with patch('sys.stdin.isatty', return_value=True), patch('sys.stdout.isatty', return_value=True), \
                 patch('builtins.input', return_value='wrong'), self.assertRaisesRegex(removal.Refusal, 'Confirmation did not match'):
                removal.main([])
            with patch.object(removal, 'inside_bridge', return_value=True), self.assertRaisesRegex(removal.Refusal, 'bridge service/environment'):
                removal.main(['--yes', '--confirm', phrase, '--manifest-sha256', plan.digest()])
            execute.assert_not_called()

    def test_active_runner_or_residual_process_group_blocks_without_killing(self):
        with patch.object(removal, 'process_inventory', return_value={'runners':['123'], 'groups':set()}):
            self.assertTrue(self.plan().blockers)
        (self.state/'jobs/completed-job.json').write_text('{"status":"done"}')
        sidecars = self.state/'jobs/.sidecars'; sidecars.mkdir()
        identity = {'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(), 'pgid':456}
        (sidecars/'completed-job.process.json').write_text(json.dumps({'identity':identity}))
        with patch.object(removal, 'process_inventory', return_value={'runners':[], 'groups':{456}}):
            self.assertIn('process group remains active', ' '.join(self.plan().blockers))

    def test_service_transition_failure_retains_files(self):
        plan = self.plan()
        self.main_pid = '123'
        with self.assertRaisesRegex(removal.Refusal, 'files retained'):
            plan.execute()
        self.assertTrue(self.app.exists())
        self.assertTrue(self.layout.unit.exists())
        self.assertFalse(any(call[0] == 'disable' for call in self.calls))

    def test_activation_link_manifest_and_false_target_defense(self):
        parent = self.layout.unit.parent/'multi-user.target.wants'; parent.mkdir()
        link = parent/'ai-bridge.service'; link.symlink_to(self.layout.unit)
        original = Path.lstat
        def root_link(path):
            value = original(path)
            if path != link:
                return value
            return SimpleNamespace(st_uid=0, st_dev=value.st_dev, st_ino=value.st_ino,
                                   st_mode=value.st_mode, st_mtime_ns=value.st_mtime_ns, st_size=value.st_size)
        with patch.object(Path, 'lstat', root_link):
            self.assertEqual(self.plan().links[0]['path'], str(link))
            link.unlink(); link.symlink_to(self.layout.unit.with_name('unrelated.service'))
            with self.assertRaisesRegex(removal.Refusal, 'Unrecognized activation link'):
                self.plan()

    def test_mountpoint_and_changed_directory_cannot_be_removed(self):
        mountinfo = '1 0 0:1 / '+str(self.config).replace(' ', r'\040')+' rw - tmpfs tmpfs rw\n'
        original = Path.read_text
        def read(path, *args, **kwargs):
            return mountinfo if str(path) == '/proc/self/mountinfo' else original(path, *args, **kwargs)
        with patch.object(Path, 'read_text', read):
            with self.assertRaisesRegex(removal.Refusal, 'Mounted subtree'):
                self.plan()
        plan = self.plan()
        self.config.rename(self.config.with_name('previous'))
        self.config.symlink_to(self.config.with_name('previous'))
        with self.assertRaises(removal.Refusal):
            plan.execute()
        self.assertTrue((self.config.with_name('previous')/'instructions/user.md').exists())

    def test_root_ownership_and_mode_checks_are_enforced(self):
        file = self.root/'system-artifact'; file.write_text('fixture'); file.chmod(0o666)
        self.guard.stop()
        try:
            with self.assertRaises(removal.Refusal):
                removal.trusted(file)
        finally:
            self.guard.start()


if __name__ == '__main__':
    unittest.main()

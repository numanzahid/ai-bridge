"""Folder trust records for agent CLIs, written only into temporary CLI homes."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from ai_bridge import trust
from ai_bridge.core import Config
from ai_bridge.management import Tasks


class TrustTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='aib-trust-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.claude, self.codex, self.cursor = self.root / 'claude', self.root / 'codex', self.root / 'cursor'
        for path in (self.claude, self.codex, self.cursor):
            path.mkdir()
        (self.claude / '.claude.json').write_text(json.dumps({'projects': {'/keep': {'lastCost': 1}}, 'other': True}))
        (self.codex / 'config.toml').write_text('model = "x"\n\n[projects."/keep"]\ntrust_level = "trusted"\n\n[tui]\ntheme = "dark"\n')
        env = patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(self.claude), 'CODEX_HOME': str(self.codex),
                                      'AI_BRIDGE_CURSOR_HOME': str(self.cursor)})
        env.start()
        self.addCleanup(env.stop)
        self.project = self.root / 'new project'
        self.project.mkdir()

    def records(self, key):
        claude = json.loads((self.claude / '.claude.json').read_text())
        codex = tomllib.loads((self.codex / 'config.toml').read_text())
        return (claude['projects'].get(key, {}).get('hasTrustDialogAccepted'),
                codex.get('projects', {}).get(key, {}).get('trust_level'),
                trust._cursor_marker(key).exists(), claude, codex)

    def test_trust_and_untrust_all_agents_preserving_other_settings(self):
        result = trust.set_trust(str(self.project))
        key = str(self.project.resolve())
        self.assertEqual(result['agents'], {'claude': 'trusted', 'codex': 'trusted', 'cursor': 'trusted'})
        claude_ok, codex_level, marker, claude, codex = self.records(key)
        self.assertEqual((claude_ok, codex_level, marker), (True, 'trusted', True))
        self.assertEqual((claude['other'], claude['projects']['/keep']), (True, {'lastCost': 1}))
        self.assertEqual((codex['model'], codex['tui'], codex['projects']['/keep']), ('x', {'theme': 'dark'}, {'trust_level': 'trusted'}))
        marker_data = json.loads(trust._cursor_marker(key).read_text())
        self.assertEqual(marker_data['workspacePath'], key)
        self.assertEqual(trust.set_trust(str(self.project))['agents']['codex'], 'unchanged')
        self.assertEqual(trust.set_trust(str(self.project), trusted=False)['agents'],
                         {'claude': 'untrusted', 'codex': 'untrusted', 'cursor': 'untrusted'})
        claude_ok, codex_level, marker, _, codex = self.records(key)
        self.assertEqual((claude_ok, codex_level, marker), (None, None, False))
        self.assertEqual(codex['projects'], {'/keep': {'trust_level': 'trusted'}})

    def test_git_root_is_the_key_and_home_is_refused(self):
        subprocess.run(['git', 'init', '-q', str(self.project)], check=True)
        nested = self.project / 'src' / 'pkg'
        nested.mkdir(parents=True)
        self.assertEqual(trust.trust_key(str(nested)), str(self.project.resolve()))
        for path in (str(Path.home()), '/', str(Path.home().parent)):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, 'home'):
                trust.trust_key(path)
        with self.assertRaisesRegex(ValueError, 'Not a directory'):
            trust.trust_key(str(self.root / 'missing'))

    def test_missing_cli_home_is_skipped_and_errors_are_per_agent(self):
        (self.claude / '.claude.json').unlink()
        (self.codex / 'config.toml').write_text('broken = [\n')
        result = trust.set_trust(str(self.project), ['claude', 'codex', 'cursor'])['agents']
        self.assertEqual(result['claude'], 'not_installed')
        self.assertTrue(result['codex'].startswith('error'))
        self.assertEqual(result['cursor'], 'trusted')
        self.assertEqual((self.codex / 'config.toml').read_text(), 'broken = [\n')

    def test_task_trust_is_opt_in_and_removed_on_cleanup(self):
        tasks_dir = self.root / 'tasks'
        quiet = Tasks(Config(token='', state_dir=self.root / 'state', tasks_dir=tasks_dir))
        self.assertNotIn('trust', quiet.create())
        self.assertNotIn('projects."', (self.codex / 'config.toml').read_text().replace('projects."/keep"', ''))
        cfg = Config(token='', state_dir=self.root / 'state', tasks_dir=tasks_dir, trust_tasks=True)
        cfg.state_dir.mkdir(mode=0o700, exist_ok=True)
        (cfg.state_dir / 'jobs').mkdir(mode=0o700, exist_ok=True)
        tasks = Tasks(cfg)
        task = tasks.create()
        self.assertEqual(task['trust'], {'claude': 'trusted', 'codex': 'trusted', 'cursor': 'trusted'})
        self.assertEqual(self.records(task['workspace'])[:3], (True, 'trusted', True))
        from ai_bridge.core import Jobs
        with patch('ai_bridge.management.observed_task_processes', return_value=[]):
            result = tasks.cleanup(task['task_id'], Jobs(cfg), False)
        self.assertEqual(result['trust_removed'], {'claude': 'untrusted', 'codex': 'untrusted', 'cursor': 'untrusted'})
        self.assertEqual(self.records(task['workspace'])[:3], (None, None, False))

    def test_config_flag_defaults_off(self):
        with patch.dict(os.environ, {'AI_BRIDGE_TOKEN': 'x' * 40, 'AI_BRIDGE_STATE_DIR': str(self.root / 'state2')}):
            os.environ.pop('AI_BRIDGE_TRUST_TASKS', None)
            self.assertFalse(Config.load().trust_tasks)
            with patch.dict(os.environ, {'AI_BRIDGE_TRUST_TASKS': '1'}):
                self.assertTrue(Config.load().trust_tasks)


if __name__ == '__main__':
    unittest.main()

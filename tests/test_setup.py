import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from merge_env import merge
from ai_bridge.agents import build_argv


class SetupTests(unittest.TestCase):
    def test_preserve_custom_settings_and_literal_values(self):
        old = '# comment\nAI_BRIDGE_TOKEN="secret"\nAI_BRIDGE_CODEX_BIN="/custom path/codex"\nPATH=/custom/bin\nHOME=/custom\nEMPTY=\nCUSTOM=a&b|c\\d\n'
        defaults = 'AI_BRIDGE_TOKEN=new\nPATH=/bin\nHOME=/home/test\nEMPTY=default\nNEW=value\n'
        self.assertEqual(merge(old, defaults), old + 'NEW=value\n')
        self.assertEqual(merge(merge(old, defaults), defaults), merge(old, defaults))

    def test_comments_do_not_override_defaults(self):
        self.assertEqual(merge('# KEY=old', 'KEY=new\n'), '# KEY=old\nKEY=new\n')

    def test_sudo_probe_ignores_timestamp_and_tests_shell(self):
        source = Path('install.sh').read_text()
        probe = next(line for line in source.splitlines() if line.startswith('if sudo -k -n'))
        self.assertIn('-- bash -c true', probe)
        # Simulate cached auth that succeeds only when -k is absent.
        script = 'sudo() { [[ " $* " != *" -k "* ]]; }; ' + probe.split(' && ')[0] + '; then exit 1; else exit 0; fi'
        self.assertEqual(subprocess.run(['bash', '-c', script]).returncode, 0)

    @patch.dict('os.environ', {}, clear=True)
    def test_readonly_resume_policy_is_on_parent_command(self):
        args = build_argv('codex', 'codex', 'test', 'session', 'readonly', None)
        self.assertLess(args.index('--sandbox'), args.index('resume'))
        self.assertIn('read-only', args)
        self.assertIn('approval_policy="never"', args)

    @patch.dict('os.environ', {}, clear=True)
    def test_edit_resume_policy_is_on_parent_command(self):
        args = build_argv('codex', 'codex', 'test', 'session', 'edit', None)
        self.assertLess(args.index('--sandbox'), args.index('resume'))
        self.assertIn('workspace-write', args)
        self.assertIn('approval_policy="never"', args)
        self.assertNotIn('--full-auto', args)


if __name__ == '__main__':
    unittest.main()

import unittest
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

    @patch.dict('os.environ', {}, clear=True)
    def test_codex_resume_policy_is_on_parent_command(self):
        for permission, sandbox in (('readonly', 'read-only'), ('edit', 'workspace-write')):
            with self.subTest(permission=permission):
                args = build_argv('codex', 'codex', 'test', 'session', permission, None)
                self.assertLess(args.index('--sandbox'), args.index('resume'))
                self.assertIn(sandbox, args)
                self.assertIn('approval_policy="never"', args)
                self.assertNotIn('--full-auto', args)


if __name__ == '__main__':
    unittest.main()

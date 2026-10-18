import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from ai_bridge.core import Config, Redactor, audit, child_env


class SecurityTests(unittest.TestCase):
    def test_redaction_includes_json_escaped_secrets(self):
        secret = 'fake-password-"\\\n-unicode-é'
        redact = Redactor([secret])
        for text in (secret, json.dumps({'nested': {'error': secret}}),
                     json.dumps({'error': secret}, ensure_ascii=False)):
            result = redact(text)
            self.assertIn('[REDACTED]', result)
            self.assertNotIn(secret, result)
            self.assertNotIn(json.dumps(secret)[1:-1], result)

    def test_child_processes_do_not_inherit_bridge_credentials(self):
        with patch.dict(os.environ, {'AI_BRIDGE_TOKEN': 'secret', 'AI_BRIDGE_SUDO_PASSWORD_FILE': '/secret',
                                     'CREDENTIALS_DIRECTORY': '/private', 'KEEP_ME': 'visible'}):
            env = child_env()
            self.assertEqual([k for k in env if k.startswith('AI_BRIDGE_')], ['AI_BRIDGE_CHILD'])
            self.assertNotIn('CREDENTIALS_DIRECTORY', env)
            self.assertEqual(env['KEEP_ME'], 'visible')

    def test_version_and_generic_help_probes_filter_bridge_secrets(self):
        import sys
        from ai_bridge import agents
        with tempfile.TemporaryDirectory() as tmp:
            fake=Path(tmp)/'agent'
            fake.write_text('#!'+sys.executable+'\nimport os; print("Cursor "+" ".join(sorted(os.environ)))\n')
            fake.chmod(0o700)
            with patch.dict(os.environ, {'AI_BRIDGE_TOKEN':'secret-token','CREDENTIALS_DIRECTORY':'/secret'}):
                response=agents.probe([str(fake),'--version'])
                self.assertNotIn('AI_BRIDGE_TOKEN',response['stdout'])
                self.assertNotIn('CREDENTIALS_DIRECTORY',response['stdout'])
                self.assertIn('Cursor',agents.version_of(str(fake)))
                with patch.object(agents.shutil,'which',side_effect=lambda name:str(fake) if name=='agent' else None):
                    self.assertEqual(agents.find_binary('cursor'),str(fake))

    def test_audit_is_private_redacted_bounded_and_valid_json_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            # A pre-existing permissive log is tightened, not trusted.
            (root/'audit.log').touch()
            (root/'audit.log').chmod(0o644)
            secret='fake-quoted-"password'
            cfg=Config(token='fake-token-'*4,state_dir=root,audit_bytes=4096)
            for i in range(120):
                audit(cfg,Redactor([secret]),'test',{'secret':secret,'message':'x'*500},secret+'y'*300)
            logs=list(root.glob('audit.log*'))
            self.assertLessEqual(len(logs),4)
            for path in logs:
                self.assertEqual(stat.S_IMODE(path.stat().st_mode),0o600)
                self.assertLessEqual(path.stat().st_size,4096)
                self.assertNotIn('fake-quoted-',path.read_text())
                for line in path.read_text().splitlines():json.loads(line)
            last=json.loads((root/'audit.log').read_text().splitlines()[-1])
            self.assertEqual(last['args']['secret'],'[REDACTED]')
            self.assertTrue(last['result'].startswith('[REDACTED]'))

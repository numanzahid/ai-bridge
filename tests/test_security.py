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

    def test_audit_is_private_and_redacted_even_for_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root/'audit.log'
            path.touch()
            path.chmod(0o644)
            secret = 'fake-quoted-"password'
            cfg = Config(token='test-token-'*4, state_dir=root)
            audit(cfg, Redactor([secret]), 'test', {'secret': secret}, secret)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            result = json.loads(path.read_text())
            self.assertEqual(result['args']['secret'], '[REDACTED]')
            self.assertEqual(result['result'], '[REDACTED]')

    def test_child_processes_do_not_inherit_bridge_credentials(self):
        with patch.dict(os.environ, {'AI_BRIDGE_TOKEN': 'secret', 'AI_BRIDGE_SUDO_PASSWORD_FILE': '/secret',
                                     'CREDENTIALS_DIRECTORY': '/private', 'KEEP_ME': 'visible'}):
            env = child_env()
            self.assertFalse(any(key.startswith('AI_BRIDGE_') for key in env))
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

    def test_audit_rotation_is_private_bounded_and_valid_json_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            cfg=Config(token='fake-token-'*4,state_dir=root,audit_bytes=4096)
            for i in range(120):
                audit(cfg,Redactor(['secret-value']),'test',{'message':'secret-value'+'x'*500},'y'*300)
            logs=list(root.glob('audit.log*'))
            self.assertLessEqual(len(logs),4)
            for path in logs:
                self.assertEqual(stat.S_IMODE(path.stat().st_mode),0o600)
                self.assertLessEqual(path.stat().st_size,4096)
                self.assertNotIn('secret-value',path.read_text())
                for line in path.read_text().splitlines():json.loads(line)

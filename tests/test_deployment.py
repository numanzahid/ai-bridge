import json
from pathlib import Path
import tempfile
import unittest

import deploy


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'deployment'
        self.source=Path(__file__).resolve().parents[1]

    def builder(self,release,source,python):
        (release/'venv/bin').mkdir(parents=True)
        (release/'venv/bin/python').write_text('fake interpreter fixture')

    def validate_fixture(self,release,source):
        self.assertTrue((release/'app/ai_bridge/core.py').exists())
        self.assertTrue((release/'validation/tests').is_dir())
        self.assertFalse((self.root/'current').exists())

    def test_stage_does_not_select_then_activate_and_rollback(self):
        first=deploy.stage(self.root,self.source,builder=self.builder,tester=self.validate_fixture)
        self.assertFalse((self.root/'current').exists())
        deploy.activate(self.root,first)
        second=deploy.stage(self.root,self.source,builder=self.builder,tester=lambda *_:None)
        self.assertEqual((self.root/'current').resolve(),first)
        deploy.activate(self.root,second)
        self.assertEqual((self.root/'previous').resolve(),first)
        deploy.activate(self.root,(self.root/'previous').resolve())
        self.assertEqual((self.root/'current').resolve(),first)
        self.assertTrue(second.exists())

    def test_failed_tests_never_select_and_tampering_is_rejected(self):
        def fail(*_):raise RuntimeError('simulated failing suite')
        with self.assertRaises(RuntimeError):
            deploy.stage(self.root,self.source,builder=self.builder,tester=fail)
        failed=next((self.root/'releases').iterdir())
        with self.assertRaises(ValueError):deploy.activate(self.root,failed)
        ready=deploy.stage(self.root,self.source,builder=self.builder,tester=lambda *_:None)
        (ready/'app/ai_bridge/core.py').write_text('tampered')
        with self.assertRaisesRegex(ValueError,'checksum'):
            deploy.activate(self.root,ready)
        self.assertFalse((self.root/'current').exists())

    def test_release_outside_root_is_rejected(self):
        with self.assertRaises(ValueError):deploy.verify(self.root,Path(self.tmp.name))

    def test_cli_accepts_release_after_options(self):
        import subprocess,sys
        release=deploy.stage(self.root,self.source,builder=self.builder,tester=lambda *_:None)
        result=subprocess.run([sys.executable,str(self.source/'deploy.py'),'verify','--root',str(self.root),str(release)],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(result.stdout.strip(),str(release))

    def test_unit_migration_preserves_settings_and_original_rollback(self):
        import os,stat,subprocess,sys
        source=(self.source/'install.sh').read_text()
        script=source.split("<<'UNITPY'\n",1)[1].split('\nUNITPY',1)[0]
        unit=Path(self.tmp.name)/'ai-bridge.service'
        original='[Service]\nEnvironmentFile=/private/config.env\nLoadCredential=sudo_pass:/private/sudo.pass\nKillMode=process\nEnvironment=PYTHONPATH=/old/app\nExecStart=/old/venv/bin/python -m ai_bridge\n'
        unit.write_text(original)
        selected=Path(self.tmp.name)/'release'
        subprocess.run([sys.executable,'-',str(unit),str(selected)],input=script,text=True,check=True)
        self.assertIn('ExecStart='+str(selected/'venv/bin/python'),unit.read_text())
        self.assertIn('LoadCredential=sudo_pass:/private/sudo.pass',unit.read_text())
        self.assertIn('EnvironmentFile=/private/config.env',unit.read_text())
        backup=unit.with_name('ai-bridge.service.before-versioned')
        self.assertEqual(backup.read_text(),original)
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode),0o600)
        subprocess.run([sys.executable,'-',str(unit),str(selected)],input=script,text=True,check=True)
        self.assertEqual(backup.read_text(),original)

    def test_installer_stage_does_not_call_sudo_or_change_active_app(self):
        import os,subprocess
        bin_dir=Path(self.tmp.name)/'bin';bin_dir.mkdir()
        python=bin_dir/'python3';python.write_text('#!/bin/sh\nprintf "staged-fixture\\n"\n');python.chmod(0o700)
        sudo=bin_dir/'sudo';sudo.write_text('#!/bin/sh\necho unexpected-sudo >&2\nexit 99\n');sudo.chmod(0o700)
        result=subprocess.run(['bash',str(self.source/'install.sh'),'--stage'],env=dict(os.environ,HOME=self.tmp.name,PATH=str(bin_dir)+os.pathsep+os.environ['PATH']),capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(result.stdout.strip(),'staged-fixture')
        self.assertFalse((Path(self.tmp.name)/'.local/share/ai-bridge/current').exists())

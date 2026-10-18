import json
from pathlib import Path
import re
import shutil
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

    def test_busy_port_warning_allows_installation_to_continue(self):
        import socket,subprocess,sys
        with socket.socket() as listener:
            listener.bind(('127.0.0.1',0));listener.listen()
            port=listener.getsockname()[1]
            result=subprocess.run([sys.executable,str(self.source/'deploy.py'),'check-port','--port',str(port),'--warn-only'],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(result.stdout.strip(),'unavailable')
        self.assertIn(f'127.0.0.1:{port}',result.stderr)
        self.assertIn('Installation will continue',result.stderr)
        self.assertIn('/etc/ai-bridge/ai-bridge.env',result.stderr)
        self.assertIn('sudo systemctl restart ai-bridge',result.stderr)
        self.assertIn('tunnel backend',result.stderr)

    def test_port_check_accepts_free_port_and_rejects_invalid_port(self):
        import socket
        with socket.socket() as listener:
            listener.bind(('127.0.0.1',0));port=listener.getsockname()[1]
        deploy.check_port(port=port)
        with self.assertRaisesRegex(ValueError,'between 1 and 65535'):
            deploy.check_port(port=0)

    def test_test_failure_is_streamed_logged_and_not_marked_ready(self):
        from contextlib import redirect_stderr
        import io,sys
        release=self.root/'release';release.mkdir(parents=True)
        output=io.StringIO()
        with redirect_stderr(output),self.assertRaisesRegex(RuntimeError,'Regression tests FAILED.*Full log'):
            deploy.run_logged([sys.executable,'-u','-c','print("test_example ... FAIL"); print("FAILED (failures=1)"); raise SystemExit(1)'],
                              release/'tests.log',live=True,step='Regression tests')
        self.assertIn('test_example ... FAIL',output.getvalue())
        self.assertIn('FAILED (failures=1)',output.getvalue())
        self.assertIn('test_example ... FAIL',(release/'tests.log').read_text())
        self.assertFalse((release/'READY').exists())

    def test_staging_progress_uses_stderr_not_release_path_stdout(self):
        from contextlib import redirect_stderr,redirect_stdout
        import io
        stdout,stderr=io.StringIO(),io.StringIO()
        with redirect_stderr(stderr),redirect_stdout(stdout):
            release=deploy.stage(self.root,self.source,builder=self.builder,tester=self.validate_fixture)
        self.assertEqual(stdout.getvalue(),'')
        self.assertIn('Copying source',stderr.getvalue())
        self.assertIn('checksums',stderr.getvalue())
        self.assertIn('Staging PASSED',stderr.getvalue())
        self.assertTrue((release/'READY').exists())

    def source_copy(self,version,code_marker=None):
        """A disposable checkout declaring another version, optionally with changed application code."""
        source=Path(tempfile.mkdtemp(prefix='source-',dir=self.tmp.name))
        shutil.copytree(self.source/'ai_bridge',source/'ai_bridge',ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copytree(self.source/'tests',source/'tests',ignore=shutil.ignore_patterns('__pycache__'))
        for name in ('install.sh','bootstrap.py','uninstall.sh','uninstall.py','merge_env.py','requirements.txt',
                     'requirements.lock','deploy.py','release.py'):
            shutil.copy2(self.source/name,source/name)
        init=source/'ai_bridge/__init__.py'
        init.write_text(re.sub(r'__version__ = "[^"]+"','__version__ = "'+version+'"',init.read_text()))
        (source/'CHANGELOG.md').write_text('# Changelog\n\n## [Unreleased]\n\n## ['+version+'] - 2026-01-01\n')
        if code_marker:
            with (source/'ai_bridge/core.py').open('a') as stream:stream.write('\n# '+code_marker+'\n')
        return source

    def stage_copy(self,version,code_marker=None):
        return deploy.stage(self.root,self.source_copy(version,code_marker),builder=self.builder,tester=lambda *_:None)

    def test_release_records_version_and_provenance_under_checksums(self):
        release=self.stage_copy('2.3.4')
        self.assertRegex(release.name,r'^\d{8}T\d{6}Z-v2\.3\.4-[0-9a-f]{8}$')
        info=json.loads((release/'RELEASE.json').read_text())
        self.assertEqual(info['version'],'2.3.4')
        self.assertIsNone(info['git_commit'])  # the disposable copy is not a checkout
        self.assertIn('staged_at',info)
        self.assertIn('RELEASE.json',json.loads((release/'SHA256.json').read_text()))
        (release/'RELEASE.json').write_text(json.dumps(dict(info,version='9.9.9')))
        with self.assertRaisesRegex(ValueError,'checksum'):
            deploy.verify(self.root,release)

    def test_stage_requires_dated_changelog_entry_for_version(self):
        source=self.source_copy('2.3.4')
        (source/'CHANGELOG.md').write_text('# Changelog\n\n## [Unreleased]\n')
        with self.assertRaisesRegex(ValueError,'2.3.4'):
            deploy.stage(self.root,source,builder=self.builder,tester=lambda *_:None)
        self.assertFalse((self.root/'releases').exists())

    def test_activation_requires_version_bump_for_code_changes(self):
        base=self.stage_copy('2.0.0')
        deploy.activate(self.root,base)
        # Rebuilding identical application code under the same version may be reselected.
        deploy.activate(self.root,self.stage_copy('2.0.0'))
        changed=self.stage_copy('2.0.0','changed code')
        with self.assertRaisesRegex(ValueError,'bump'):
            deploy.activate(self.root,changed)
        newer=self.stage_copy('2.0.1','changed code')
        deploy.activate(self.root,newer)
        with self.assertRaisesRegex(ValueError,'--rollback'):
            deploy.activate(self.root,base)
        self.assertEqual((self.root/'current').resolve(),newer)
        deploy.activate(self.root,base,rollback=True)
        self.assertEqual((self.root/'current').resolve(),base)

    def test_legacy_release_without_metadata_uses_packaged_version(self):
        legacy=self.stage_copy('1.0.0')
        (legacy/'RELEASE.json').unlink()
        sums=json.loads((legacy/'SHA256.json').read_text()); sums.pop('RELEASE.json')
        (legacy/'SHA256.json').write_text(json.dumps(sums))
        self.assertEqual(deploy.release_info(legacy),{'version':'1.0.0','legacy':True})
        deploy.activate(self.root,legacy)
        deploy.activate(self.root,self.stage_copy('1.1.0','new feature'))

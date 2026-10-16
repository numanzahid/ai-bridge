import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from ai_bridge.file_ops import read_text,write_text
from ai_bridge.responses import render
from ai_bridge.core import Redactor


class FileTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.file=self.root/'config'
        self.file.write_text('old')
        self.file.chmod(0o640)

    def test_atomic_hash_backup_and_metadata(self):
        original=self.file.stat()
        result=write_text(self.file,'new',atomic=True,expected_sha256=hashlib.sha256(b'old').hexdigest(),backup=True)
        self.assertEqual(self.file.read_text(),'new')
        after=self.file.stat()
        self.assertEqual((after.st_uid,after.st_gid,stat.S_IMODE(after.st_mode)),(original.st_uid,original.st_gid,0o640))
        backup=Path(result['backup_path'])
        self.assertEqual(backup.read_text(),'old')
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode),0o600)
        with self.assertRaisesRegex(ValueError,'hash mismatch'):
            write_text(self.file,'bad',atomic=True,expected_sha256='stale')
        self.assertEqual(self.file.read_text(),'new')

    def test_failed_replace_keeps_old_file_and_cleans_staging(self):
        with patch('os.replace',side_effect=OSError('simulated failure')):
            with self.assertRaises(OSError):
                write_text(self.file,'bad',atomic=True)
        self.assertEqual(self.file.read_text(),'old')
        self.assertEqual(list(self.root.iterdir()),[self.file])

    def test_symlinks_hardlinks_special_and_xattrs_rejected(self):
        link=self.root/'link'; link.symlink_to(self.file)
        with self.assertRaises(ValueError):write_text(link,'bad',atomic=True)
        hard=self.root/'hard';os.link(self.file,hard)
        with self.assertRaises(ValueError):write_text(self.file,'bad',atomic=True)
        fifo=self.root/'fifo';os.mkfifo(fifo)
        with self.assertRaises(ValueError):write_text(fifo,'bad',atomic=True)
        with self.assertRaises(ValueError):read_text(fifo)
        self.assertEqual(self.file.read_text(),'old')

    def test_private_new_file_and_default_direct_write(self):
        path=self.root/'new'
        write_text(path,'secret',mode=0o600)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode),0o600)
        ino=self.file.stat().st_ino
        write_text(self.file,'direct')
        self.assertEqual(self.file.stat().st_ino,ino)
        self.assertEqual(stat.S_IMODE(self.file.stat().st_mode),0o640)
        with self.assertRaises(ValueError):write_text(self.file,'x',append=True,atomic=True)

    def test_read_limit_is_explicit_and_hash_is_real(self):
        self.file.write_text('x'*10000)
        result=read_text(self.file,max_bytes=100,include_hash=True)
        self.assertIn('captured prefix',result)
        self.assertIn(hashlib.sha256(self.file.read_bytes()).hexdigest(),result)
        self.assertLess(len(result),400)


class ResponseTests(unittest.TestCase):
    def test_large_results_always_remain_json_with_control_fields(self):
        value={'job_id':'test-id','status':'failed','outcome':'blocked','exit_code':1,
               'failure':{'scope':'agent','code':'usage_limit','message':'é"\\'*10000},
               'stdout':'a'*100000,'stderr':'b'*100000,'session_id':'original'}
        for limit in (512,1800,30000):
            result=render(value,limit,lambda x:x)
            data=json.loads(result)
            self.assertLessEqual(len(result),limit)
            self.assertEqual(data['failure']['code'],'usage_limit')
            self.assertEqual(data['session_id'],'original')
            self.assertTrue(data['truncation']['truncated'])

    def test_numeric_credential_does_not_break_json(self):
        data=json.loads(render({'message':'password 1234','count':1234},1000,Redactor(['1234'])))
        self.assertEqual(data['message'],'password [REDACTED]')
        self.assertEqual(data['count'],1234)

class AdditionalFileTests(unittest.TestCase):
    def test_requested_lines_beyond_capture_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'long'
            path.write_text(''.join(f'line {i}\n' for i in range(10000)))
            result=read_text(path,start_line=9000,max_lines=2,max_bytes=100)
            self.assertIn('line 8999',result)
            self.assertIn('line 9000',result)
            self.assertIn('more lines available',result)
            self.assertNotIn('line 9001',result)

    def test_atomic_write_refuses_xattrs(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'config';path.write_text('old')
            with patch('os.listxattr',return_value=['user.example']):
                with self.assertRaisesRegex(ValueError,'xattrs'):
                    write_text(path,'new',atomic=True)
            self.assertEqual(path.read_text(),'old')

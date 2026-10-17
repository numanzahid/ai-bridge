import json
import os
from pathlib import Path
import shlex
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ai_bridge import ssh_admin as ssh
from ai_bridge.core import Config


FAKE = r'''
import json,os,pathlib,shlex,shutil,subprocess,sys,time
name=pathlib.Path(sys.argv[0]).name; args=sys.argv[1:]
root=pathlib.Path(os.environ['SSH_TEST_ROOT'])
with (root/'calls').open('a') as log:log.write(json.dumps([name,args])+'\n')
if name=='ssh':
 if '-G' in args:
  print('hostname 192.0.2.9\nuser private-user\nidentityfile /private/key\n');sys.exit(0)
 if os.environ.get('SSH_TEST_REJECT'):
  print('Host key verification failed for 192.0.2.9 /private/key',file=sys.stderr);sys.exit(255)
 command=args[-1]
 if command=='command -v rsync >/dev/null 2>&1':sys.exit(int(os.environ.get('SSH_TEST_NO_RSYNC','0')))
 if os.environ.get('SSH_TEST_SLEEP'):time.sleep(3)
 result=subprocess.run(['/bin/sh','-c',command]);sys.exit(result.returncode)
if name=='sudo':
 if os.environ.get('SSH_TEST_DENY_SUDO'):
  print('sudo: password required',file=sys.stderr);sys.exit(1)
 args=args[args.index('--')+1:];os.execvpe(args[0],args,os.environ)
if name=='sftp':
 values=shlex.split(sys.stdin.read());source,dest=values[1:]
else:
 index=args.index('--');source,dest=args[index+1:]
 def remote(value):
  if value.startswith('lab:'):
   value=value[len('lab:'):]
   if name=='scp':value=shlex.split(value)[0]
  return value
 source,dest=remote(source),remote(dest)
assert pathlib.Path(source).is_relative_to(root),source
assert pathlib.Path(dest).is_relative_to(root) or pathlib.Path(dest).parent.name.startswith('aib-') or '.aib-transfer-' in dest,dest
if os.environ.get('SSH_TEST_TRANSFER_FAIL'):
 print('simulated failure',file=sys.stderr);sys.exit(12)
shutil.copyfile(source,dest)
'''


class SSHTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='bridge-ssh-fixture-');self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.home=self.root/'home';(self.home/'.ssh').mkdir(parents=True)
        self.file=self.home/'.ssh/config';self.file.write_text('Host lab\n HostName 192.0.2.9\n User private-user\n IdentityFile /private/key\n')
        self.system=self.root/'system';self.system.write_text('Host *\n ServerAliveInterval 5\n')
        self.cfg=Config(token='synthetic-token',state_dir=self.root,capture_bytes=8192)
        self.bin=self.root/'bin';self.bin.mkdir()
        for name in ('ssh','rsync','sftp','scp','sudo'):
            path=self.bin/name;path.write_text('#!'+sys.executable+'\n'+FAKE);path.chmod(0o700)
        self.env=patch.dict(os.environ,PATH=str(self.bin)+os.pathsep+os.environ['PATH'],SSH_TEST_ROOT=str(self.root))
        self.env.start();self.addCleanup(self.env.stop)
        self.source=self.root/'source odd ";$.txt';self.source.write_bytes(b'roundtrip\x00payload\xff')
        self.remote=self.root/'remote file ";$.txt'

    def config(self):return ssh.Configuration(self.home,self.system)

    def transfer(self,direction='upload',method='auto',**kwargs):
        return ssh.transfer(self.cfg,'lab',direction,str(kwargs.pop('local',self.source)),str(self.remote),method,configuration=self.config(),**kwargs)

    def calls(self):
        path=self.root/'calls'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_alias_inventory_is_offline_and_private(self):
        result=ssh.hosts(configuration=self.config())
        self.assertEqual(result['hosts'],[{'alias':'lab','source':'user'}])
        self.assertFalse(self.calls());self.assertFalse(result['connection_attempted'])
        for value in ('192.0.2.9','private-user','/private/key'):
            self.assertNotIn(value,json.dumps(result))

    def test_include_symlink_and_match_do_not_execute(self):
        fragment=self.home/'.ssh/part';fragment.write_text('Host included\n HostName hidden\n')
        actual=self.root/'linked-config';actual.write_text('Include part\nHost *\nMatch exec "touch '+str(self.root/'pwn')+'"\n HostName ignored\nInclude never\nHost lab\nHost *.wild !excluded\n')
        self.file.unlink();self.file.symlink_to(actual)
        config=self.config();self.assertEqual(set(config.aliases),{'included','lab'})
        self.assertTrue(config.skipped_match)
        with config.snapshot() as path:
            body=path.read_text();self.assertNotIn('Match',body);self.assertNotIn('touch',body)
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
        self.assertFalse((self.root/'pwn').exists())

    def test_include_cycles_and_conditional_includes_are_conservative(self):
        self.file.write_text('Include config\n')
        with self.assertRaisesRegex(ValueError,'cycle'):self.config()
        (self.home/'.ssh/part').write_text('Host hidden\n')
        self.file.write_text('Host lab\nInclude part\n')
        self.assertEqual(set(self.config().aliases),{'lab'})

    def test_host_injection_unknown_ip_and_controls_refused_without_calls(self):
        for host in ('-oProxyCommand=bad','user@lab','lab; touch nope','lab\nHost bad','192.0.2.9','unknown','lab:22'):
            with self.subTest(host=host),self.assertRaises(ValueError):
                ssh.run(self.cfg,host,'true',configuration=self.config())
        self.assertFalse(self.calls())

    def test_executable_config_directives_refused(self):
        self.file.write_text('Host lab\nProxyCommand touch '+str(self.root/'pwn')+'\n')
        with self.assertRaisesRegex(ValueError,'unsupported executable'):
            ssh.run(self.cfg,'lab','true',configuration=self.config())
        self.assertFalse(self.calls())

    def test_run_structured_and_no_local_sudo(self):
        result=ssh.run(self.cfg,'lab','printf hello; printf problem >&2; exit 7',configuration=self.config())
        self.assertEqual(result['stdout'],'hello');self.assertEqual(result['exit_code'],7)
        self.assertEqual(result['status'],'failed');self.assertFalse(result['local_sudo_used'])
        self.assertFalse(result['remote_privilege']['sudo_requested'])
        self.assertNotIn('privilege',result)
        argv=self.calls()[-1][1]
        for value in ('StrictHostKeyChecking=yes','BatchMode=yes','ForwardAgent=no','CheckHostIP=no','UpdateHostKeys=no'):
            self.assertIn(value,argv)
        self.assertFalse(any(call[0]=='sudo' for call in self.calls()))

    def test_remote_sudo_failure_is_not_confirmation(self):
        with patch.dict(os.environ,SSH_TEST_DENY_SUDO='1'):
            result=ssh.run(self.cfg,'lab','printf should-not-run',remote_sudo=True,configuration=self.config())
        self.assertTrue(result['remote_privilege']['sudo_attempted'])
        self.assertFalse(result['remote_privilege']['sudo_confirmed'])
        self.assertNotIn('should-not-run',result['stdout'])
        self.assertNotIn('AIB_',result['stderr'])

    def test_remote_sudo_confirmation_requires_root_receipt_not_exit_zero(self):
        result=ssh.run(self.cfg,'lab','exit 9',remote_sudo=True,configuration=self.config())
        self.assertEqual(result['remote_privilege']['sudo_confirmed'],os.geteuid()==0)
        self.assertEqual(result['exit_code'],9)
        self.assertNotIn('AIB_',result['stderr'])

    def test_host_key_failure_private_diagnostics_and_unknown_sudo(self):
        with patch.dict(os.environ,SSH_TEST_REJECT='1'):
            result=ssh.run(self.cfg,'lab','true',remote_sudo=True,configuration=self.config())
        self.assertEqual(result['exit_code'],255)
        self.assertIsNone(result['remote_privilege']['sudo_attempted'])
        self.assertFalse(result['remote_privilege']['sudo_confirmed'])
        self.assertNotIn('192.0.2.9',result['stderr']);self.assertNotIn('/private/key',result['stderr'])
        self.assertIn('Host key verification failed',result['stderr'])

    def test_timeout_and_large_output_are_bounded(self):
        result=ssh.run(self.cfg,'lab',"printf '%100000s' x",configuration=self.config())
        self.assertTrue(result['stdout_truncated']);self.assertLessEqual(len(result['stdout']),8192)
        with patch.dict(os.environ,SSH_TEST_SLEEP='1'):
            result=ssh.run(self.cfg,'lab','true',timeout=1,configuration=self.config())
        self.assertTrue(result['timed_out']);self.assertEqual(result['status'],'timed_out')

    def test_dry_run_never_calls_ssh_or_selects_unverified_auto(self):
        result=self.transfer(dry_run=True)
        self.assertEqual(result['status'],'dry_run');self.assertIsNone(result['method'])
        self.assertFalse(self.calls());self.assertFalse(self.remote.exists())

    def test_each_protocol_roundtrips_without_delete_or_verified_byte_claims(self):
        for method in ('rsync','sftp','scp'):
            with self.subTest(method=method):
                self.remote=self.root/(method+' remote ";$.txt')
                uploaded=self.transfer(method=method)
                self.assertTrue(uploaded['committed'],uploaded)
                self.assertEqual(uploaded['method'],method);self.assertIsNone(uploaded['bytes'])
                self.assertEqual(self.remote.read_bytes(),self.source.read_bytes())
                target=self.root/(method+' downloaded')
                downloaded=self.transfer('download',method=method,local=target)
                self.assertTrue(downloaded['committed'],downloaded)
                self.assertEqual(target.read_bytes(),self.source.read_bytes())
        self.assertNotIn('--delete',json.dumps(self.calls()))
        self.assertTrue(self.source.exists())

    def test_existing_destination_is_never_overwritten(self):
        self.remote.write_text('existing')
        result=self.transfer(method='sftp')
        self.assertNotEqual(result['status'],'succeeded')
        self.assertEqual(self.remote.read_text(),'existing')
        with self.assertRaises(ValueError):self.transfer('download',local=self.source)

    def test_rsync_auto_fallback_only_on_capability_absence(self):
        with patch.dict(os.environ,SSH_TEST_NO_RSYNC='1'):
            result=self.transfer()
        self.assertEqual(result['method'],'sftp');self.assertTrue(result['committed'])
        self.remote=self.root/'no-blind-retry'
        with patch.dict(os.environ,SSH_TEST_TRANSFER_FAIL='1'):
            result=self.transfer()
        self.assertEqual(result['method'],'rsync');self.assertEqual(result['status'],'failed')
        self.assertFalse(self.remote.exists())

    def test_missing_clients_and_explicit_method(self):
        with patch.object(ssh,'binaries',return_value={'ssh':str(self.bin/'ssh'),'rsync':None,'sftp':None,'scp':None}):
            with self.assertRaises(ValueError):self.transfer(dry_run=True)
        with patch.object(ssh,'binaries',return_value={'ssh':str(self.bin/'ssh'),'rsync':None,'sftp':str(self.bin/'sftp'),'scp':None}):
            with self.assertRaises(ValueError):self.transfer(method='rsync',dry_run=True)

    def test_ambiguous_paths_symlinks_and_odd_bytes_refused(self):
        for value in ('relative','/tmp/-/../oops','/tmp/new\nfile','/tmp/*','/tmp/\udcff','/tmp/dir/'):
            with self.subTest(path=repr(value)),self.assertRaises(ValueError):ssh.remote_path(value)
        linked=self.root/'linked';linked.symlink_to(self.source)
        with self.assertRaises(ValueError):self.transfer(local=linked,dry_run=True)

    def test_shell_metacharacters_in_paths_are_data(self):
        self.remote=self.root/("name'; touch "+str(self.root/'pwn').replace('/','_')+";#.txt")
        result=self.transfer(method='sftp')
        self.assertTrue(result['committed'],result)
        self.assertEqual(self.remote.read_bytes(),self.source.read_bytes())
        self.assertFalse((self.root/'pwn').exists())

    def test_confirmed_receipt_is_separate_from_failed_outcome(self):
        def remote(session, path, command, on_output=None):
            markers=re.findall(r'AIB_[a-f0-9]+_(?:ATTEMPT|ROOT)',command)
            data=('\n'.join(markers)+'\n').encode()
            on_output('stderr',data[:17]);on_output('stderr',data[17:])
            return {'stdout':'','stderr':data.decode(),'exit_code':9,'timed_out':False}
        with patch.object(ssh.Session,'remote',remote):
            result=ssh.run(self.cfg,'lab','exit 9',remote_sudo=True,configuration=self.config())
        self.assertTrue(result['remote_privilege']['sudo_confirmed'])
        self.assertEqual(result['status'],'failed');self.assertNotIn('AIB_',result['stderr'])

    def test_cleanup_and_ambiguous_commit_are_explicit(self):
        original=ssh.Session.remote
        def remote(session,path,command,on_output=None):
            if ' && ln -T -- ' in command:
                return {'stdout':'','stderr':'lost connection','exit_code':255,'timed_out':False}
            return original(session,path,command,on_output)
        with patch.object(ssh.Session,'remote',remote):result=self.transfer(method='sftp')
        self.assertIsNone(result['committed']);self.assertEqual(result['status'],'unconfirmed')
        self.assertEqual(result['staging_cleanup'],'completed')
        self.assertFalse(self.remote.exists())

    def test_local_destination_race_does_not_replace_existing_file(self):
        self.remote.write_bytes(b'remote')
        destination=self.root/'raced'
        original=os.link
        def link(source,target,**kwargs):
            destination.write_bytes(b'other writer')
            return original(source,target,**kwargs)
        with patch('os.link',side_effect=link):
            result=self.transfer('download',method='sftp',local=destination)
        self.assertEqual(result['error_type'],'FileExistsError')
        self.assertEqual(result['method'],'sftp');self.assertFalse(result['committed'])
        self.assertEqual(destination.read_bytes(),b'other writer')

    def test_pinned_options_parse_with_real_client_offline(self):
        native=Path('/usr/bin/ssh')
        if not native.exists():self.skipTest('Native SSH client not installed')
        with self.config().snapshot() as path:
            result=subprocess.run([str(native),'-F',str(path),'-G','lab'],capture_output=True,text=True,timeout=5)
        self.assertEqual(result.returncode,0)
        options=dict(line.split(' ',1) for line in result.stdout.splitlines() if ' ' in line)
        for key,value in (('stricthostkeychecking','true'),('batchmode','yes'),('forwardagent','no'),('updatehostkeys','false')):
            self.assertEqual(options[key],value)

    def test_tool_wrappers_return_safe_json_and_audited_alias(self):
        from test_server_sessions import server
        config=self.config()
        with patch.object(ssh,'Configuration',return_value=config),patch.object(server,'CFG',self.cfg), \
             patch.object(server,'audit') as audit:
            inventory=json.loads(server.ssh_hosts())
            result=json.loads(server.ssh_run('lab','printf tool-ok'))
            dry=json.loads(server.ssh_transfer('lab','upload',str(self.source),str(self.remote),dry_run=True))
            error=json.loads(server.ssh_run('unlisted','true',remote_sudo=True))
        self.assertEqual(inventory['total'],1);self.assertEqual(result['stdout'],'tool-ok')
        self.assertEqual(dry['status'],'dry_run');self.assertEqual(error['status'],'unavailable')
        self.assertFalse(error['remote_privilege']['sudo_confirmed'])
        self.assertTrue(any(len(call.args)>3 and call.args[2]=='ssh_run' and call.args[3].get('host')=='lab' for call in audit.call_args_list))

    def test_match_cannot_silently_change_connection_target(self):
        self.file.write_text('Host lab\nMatch all\n HostName secret-target\n')
        with self.assertRaisesRegex(ValueError,'conditional'):
            ssh.run(self.cfg,'lab','true',configuration=self.config())
        self.assertFalse(self.calls())

    def test_remote_source_symlink_is_refused_before_transfer(self):
        self.remote.symlink_to(self.source)
        result=self.transfer('download',method='sftp',local=self.root/'new-download')
        self.assertEqual(result['status'],'failed')
        self.assertEqual(result['phase'],'remote_file_check')
        self.assertFalse(any(name=='sftp' for name,args in self.calls()))

    def test_password_is_never_passed_to_ssh_transport(self):
        self.cfg.sudo_password='fake-local-credential-never-forward'
        result=ssh.run(self.cfg,'lab','printf safe',remote_sudo=True,configuration=self.config())
        self.assertNotIn(self.cfg.sudo_password,json.dumps(self.calls())+json.dumps(result))
        self.assertFalse(result['local_sudo_used'])

    def test_remote_privilege_survives_structured_response_truncation(self):
        from ai_bridge.responses import render
        value={'stdout':'x'*100000, 'host':'lab', 'method':'ssh',
               'remote_privilege':{'sudo_requested':True,'sudo_attempted':None,'sudo_confirmed':False},
               'remote_privilege_message':'Remote sudo requested but root execution is unconfirmed.'}
        result=json.loads(render(value,512,lambda text:text))
        self.assertEqual(result['remote_privilege'],value['remote_privilege'])
        self.assertTrue(result['truncation']['truncated'])

    def test_existing_directory_and_symlink_destinations_are_untouched(self):
        for kind in ('directory','symlink'):
            self.remote=self.root/kind
            if kind=='directory':
                self.remote.mkdir();(self.remote/'keep').write_text('keep')
            else:self.remote.symlink_to(self.source)
            result=self.transfer(method='sftp')
            self.assertFalse(result['committed'])
            self.assertTrue(self.remote.is_dir() if kind=='directory' else self.remote.is_symlink())
            self.assertEqual(self.source.read_bytes(),b'roundtrip\x00payload\xff')

    def test_include_tokens_are_not_silently_ignored(self):
        self.file.write_text('Include ${UNTRUSTED_CONFIG}\nHost lab\n')
        with self.assertRaisesRegex(ValueError,'static Include'):
            self.config()
        self.assertFalse(self.calls())

    def test_writable_config_cannot_create_trusted_aliases(self):
        self.file.chmod(0o666)
        with self.assertRaisesRegex(ValueError,'ownership/mode'):
            self.config()
        self.assertFalse(self.calls())

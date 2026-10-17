#!/usr/bin/env python3
"""Stage, verify and select versioned AI Bridge releases. Never restart services."""
import argparse
from contextlib import contextmanager
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import shutil
import subprocess
import sys
import venv


@contextmanager
def deployment_lock(root):
    root.mkdir(parents=True, exist_ok=True)
    fd=os.open(root/'.deploy.lock',os.O_CREAT|os.O_RDWR,0o600)
    with os.fdopen(fd,'w') as stream:
        fcntl.flock(stream,fcntl.LOCK_EX)
        yield


def checksums(release):
    result={}
    for directory in ('app','venv'):
        for path in sorted((release/directory).rglob('*')):
            if '__pycache__' in path.parts or path.suffix == '.pyc':
                continue
            if path.is_symlink():
                result[str(path.relative_to(release))]='symlink:'+os.readlink(path)
            elif path.is_file():
                result[str(path.relative_to(release))]=hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def progress(message):
    # stdout is reserved for the release path captured by install.sh.
    print('==> '+message, file=sys.stderr, flush=True)


def check_port(host='127.0.0.1', port=8765, warn_only=False):
    if not 1 <= port <= 65535:
        raise ValueError('Port must be between 1 and 65535.')
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError as error:
            message = (f'{host}:{port} is unavailable ({error.strerror}). '
                       'Choose an unused AI_BRIDGE_PORT in /etc/ai-bridge/ai-bridge.env, '
                       'then run: sudo systemctl restart ai-bridge. '
                       'Update your tunnel backend to the same port. No listener will be stopped.')
            if warn_only:
                print('WARNING: '+message+' Installation will continue.',file=sys.stderr,flush=True)
                return False
            raise ValueError(message) from error
    progress(f'Port {host}:{port} is available now; availability can change before startup.')
    return True


def run_logged(argv, log_path, *, cwd=None, env=None, live=False, step='Command'):
    with log_path.open('ab') as log:
        if live:
            process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            for line in process.stdout:
                log.write(line)
                log.flush()
                print(line.decode(errors='replace'), end='', file=sys.stderr, flush=True)
            process.stdout.close()
            code = process.wait()
        else:
            code = subprocess.run(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                  stdout=log, stderr=log).returncode
    if code:
        raise RuntimeError(f'{step} FAILED (exit {code}). Full log: {log_path}. '
                           'Release was not marked READY or selected; the active service is unchanged.')


def build_environment(release, source, python):
    log_path = release/'build.log'
    progress(f'Creating isolated Python environment. Log: {log_path}')
    run_logged([python,'-m','venv',str(release/'venv')],log_path,step='Virtualenv creation')
    progress(f'Installing pinned dependencies. Log: {log_path}')
    run_logged([str(release/'venv/bin/python'),'-m','pip','install','-r',
                str(source/('requirements.lock' if (source/'requirements.lock').exists() else 'requirements.txt'))],
               log_path,step='Dependency installation')


def test_release(release, source):
    env={k:v for k,v in os.environ.items() if not k.startswith('AI_BRIDGE_') and k != 'CREDENTIALS_DIRECTORY'}
    env['PYTHONPATH']=str(release/'app')
    progress(f'Running regression tests; individual results follow. Log: {release/"tests.log"}')
    run_logged([str(release/'venv/bin/python'),'-u','-m','unittest','discover','-s','tests','-v'],
               release/'tests.log',cwd=release/'validation',env=env,live=True,step='Regression tests')
    progress('Regression tests PASSED. Recording dependency versions.')
    with (release/'dependencies.txt').open('wb') as log:
        subprocess.run([str(release/'venv/bin/python'),'-m','pip','freeze'],stdout=log,check=True,env=env)


def stage(root, source, python=sys.executable, builder=build_environment, tester=test_release):
    root,source=Path(root).absolute(),Path(source).resolve()
    progress('Preparing isolated release; waiting for deployment lock if another stage is running.')
    with deployment_lock(root):
        releases=root/'releases'
        releases.mkdir(mode=0o700,exist_ok=True)
        release=releases/(datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+secrets.token_hex(4))
        release.mkdir(mode=0o700)
        progress(f'Copying source and test fixtures into {release}')
        # Build at its FINAL path; venv scripts contain absolute paths.
        # Incomplete releases are retained for diagnosis and never selectable.
        shutil.copytree(source/'ai_bridge',release/'app/ai_bridge',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        for name in ('ai-bridge', 'cli_install.py'):
            shutil.copy2(source/name, release/'app'/name)
        validation=release/'validation'
        validation.mkdir()
        shutil.copytree(source/'tests',validation/'tests',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        (validation/'ai_bridge').symlink_to(release/'app/ai_bridge', target_is_directory=True)
        for name in ('ai-bridge', 'cli_install.py', 'install.sh', 'bootstrap.py', 'uninstall.sh', 'uninstall.py', 'merge_env.py','requirements.txt','requirements.lock','deploy.py'):
            shutil.copy2(source/name,validation/name)
        builder(release,source,python)
        tester(release,source)
        progress('Recording application and environment checksums.')
        (release/'SHA256.json').write_text(json.dumps(checksums(release),indent=2)+'\n')
        (release/'READY').write_text('Staged tests passed. Selection does not restart the service.\n')
        progress(f'Staging PASSED. Release ready: {release}. Activation/restart remain separate.')
        return release


def verify(root, release):
    root,release=Path(root).absolute(),Path(release).absolute()
    if release.is_symlink() or release.resolve().parent != (root/'releases').resolve():
        raise ValueError('Release must be a direct directory under this deployment root/releases.')
    if not (release/'READY').is_file():
        raise ValueError('Release has not passed staged validation.')
    expected=json.loads((release/'SHA256.json').read_text())
    if not expected or checksums(release) != expected:
        raise ValueError('Release checksum validation failed.')
    return release


def activate(root, release):
    root=Path(root).absolute()
    with deployment_lock(root):
        release=verify(root,release)
        current=root/'current'
        if current.exists() and not current.is_symlink():
            raise ValueError('current must be absent or a release symlink.')
        old=os.readlink(current) if current.is_symlink() else None
        if old:
            previous=root/('.previous-'+secrets.token_hex(4))
            previous.symlink_to(old)
            os.replace(previous,root/'previous')
        temporary=root/('.current-'+secrets.token_hex(4))
        temporary.symlink_to(release)
        os.replace(temporary,current)
        fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)
        return release


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('stage','verify','activate','rollback','check-port'))
    parser.add_argument('release',nargs='?')
    parser.add_argument('--root',type=Path,default=Path.home()/'.local/share/ai-bridge')
    parser.add_argument('--source',type=Path,default=Path(__file__).resolve().parent)
    parser.add_argument('--python',default=sys.executable)
    parser.add_argument('--port',type=int,default=8765)
    parser.add_argument('--warn-only',action='store_true',help='Report port conflicts without failing installation.')
    args=parser.parse_intermixed_args()
    if args.action=='check-port':
        available=check_port(port=args.port,warn_only=args.warn_only)
        if args.warn_only:
            print('available' if available else 'unavailable')
        return
    if args.action=='stage':
        result=stage(args.root,args.source,args.python)
    else:
        release=Path(args.release) if args.release else ((args.root/'previous').resolve() if args.action=='rollback' else None)
        if release is None:
            parser.error('release path is required')
        result=verify(args.root,release) if args.action=='verify' else activate(args.root,release)
    print(result)


if __name__=='__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print('ERROR: '+str(error), file=sys.stderr, flush=True)
        raise SystemExit(1)

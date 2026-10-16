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


def build_environment(release, source, python):
    def run(argv):
        with (release/'build.log').open('ab') as log:
            subprocess.run(argv,stdout=log,stderr=log,check=True)
    run([python,'-m','venv',str(release/'venv')])
    run([str(release/'venv/bin/python'),'-m','pip','install','-r',str(source/('requirements.lock' if (source/'requirements.lock').exists() else 'requirements.txt'))])


def test_release(release, source):
    env={k:v for k,v in os.environ.items() if not k.startswith('AI_BRIDGE_') and k != 'CREDENTIALS_DIRECTORY'}
    env['PYTHONPATH']=str(release/'app')
    with (release/'tests.log').open('wb') as log:
        subprocess.run([str(release/'venv/bin/python'),'-m','unittest','discover','-s','tests','-v'],
                       cwd=release/'validation',env=env,stdout=log,stderr=log,check=True)
    with (release/'dependencies.txt').open('wb') as log:
        subprocess.run([str(release/'venv/bin/python'),'-m','pip','freeze'],stdout=log,check=True,env=env)


def stage(root, source, python=sys.executable, builder=build_environment, tester=test_release):
    root,source=Path(root).absolute(),Path(source).resolve()
    with deployment_lock(root):
        releases=root/'releases'
        releases.mkdir(mode=0o700,exist_ok=True)
        release=releases/(datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+secrets.token_hex(4))
        release.mkdir(mode=0o700)
        # Build at its FINAL path; venv scripts contain absolute paths.
        # Incomplete releases are retained for diagnosis and never selectable.
        shutil.copytree(source/'ai_bridge',release/'app/ai_bridge',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        validation=release/'validation'
        validation.mkdir()
        shutil.copytree(source/'tests',validation/'tests',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        (validation/'ai_bridge').symlink_to(release/'app/ai_bridge', target_is_directory=True)
        for name in ('install.sh','merge_env.py','requirements.txt','requirements.lock','deploy.py'):
            shutil.copy2(source/name,validation/name)
        builder(release,source,python)
        tester(release,source)
        (release/'SHA256.json').write_text(json.dumps(checksums(release),indent=2)+'\n')
        (release/'READY').write_text('Staged tests passed. Selection does not restart the service.\n')
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
    parser.add_argument('action',choices=('stage','verify','activate','rollback'))
    parser.add_argument('release',nargs='?')
    parser.add_argument('--root',type=Path,default=Path.home()/'.local/share/ai-bridge')
    parser.add_argument('--source',type=Path,default=Path(__file__).resolve().parent)
    parser.add_argument('--python',default=sys.executable)
    args=parser.parse_intermixed_args()
    if args.action=='stage':
        result=stage(args.root,args.source,args.python)
    else:
        release=Path(args.release) if args.release else ((args.root/'previous').resolve() if args.action=='rollback' else None)
        if release is None:
            parser.error('release path is required')
        result=verify(args.root,release) if args.action=='verify' else activate(args.root,release)
    print(result)


if __name__=='__main__':
    main()

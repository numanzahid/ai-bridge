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
import selectors
import secrets
import signal
import socket
import shutil
import subprocess
import sys
import time
import unittest
import venv

import release as versioning


@contextmanager
def deployment_lock(root):
    root.mkdir(parents=True, exist_ok=True)
    fd=os.open(root/'.deploy.lock',os.O_CREAT|os.O_RDWR,0o600)
    with os.fdopen(fd,'w') as stream:
        fcntl.flock(stream,fcntl.LOCK_EX)
        yield


def checksums(release):
    result={}
    if (release/'RELEASE.json').is_file():
        result['RELEASE.json']=hashlib.sha256((release/'RELEASE.json').read_bytes()).hexdigest()
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


def run_logged(argv, log_path, *, cwd=None, env=None, live=False, step='Command', heartbeat=5):
    started = last_progress = time.monotonic()
    with log_path.open('ab') as log:
        process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while selector.get_map() or process.poll() is None:
                    if selector.get_map():
                        events = selector.select(timeout=min(1, heartbeat))
                    else:
                        events = []
                        try:
                            process.wait(timeout=min(1, heartbeat))
                        except subprocess.TimeoutExpired:
                            pass
                    for key, _ in events:
                        chunk = os.read(key.fileobj.fileno(), 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        log.write(chunk)
                        log.flush()
                        if live:
                            print(chunk.decode(errors='replace'), end='', file=sys.stderr, flush=True)
                            last_progress = time.monotonic()
                    now = time.monotonic()
                    if now - last_progress >= heartbeat:
                        progress(f'{step}: still running ({int(now - started)}s elapsed). Log: {log_path}')
                        last_progress = now
                    if not events and process.poll() is not None:
                        break
            code = process.wait()
        except BaseException:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
            raise
        finally:
            process.stdout.close()
        if code and not live:
            with log_path.open('rb') as detail:
                detail.seek(max(0, detail.seek(0, os.SEEK_END) - 4000))
                print(detail.read().decode(errors='replace'), file=sys.stderr, flush=True)
    if code:
        raise RuntimeError(f'{step} FAILED (exit {code}). Full log: {log_path}. '
                           'Release was not marked READY or selected; the active service is unchanged.')


class ProgressTestResult(unittest.TextTestResult):
    """Count test cases (including subtest failures) and name the current test."""

    def __init__(self, *args, total, **kwargs):
        super().__init__(*args, **kwargs)
        self.total = total
        self.counts = dict(passed=0, failed=0, errors=0, skipped=0, expected_failures=0, unexpected_successes=0)

    def startTest(self, test):
        super().startTest(test)
        self.outcome = 'passed'
        self.stream.writeln(f'[{self.testsRun}/{self.total}] RUN {test.id()}')
        self.stream.flush()

    def addFailure(self, test, err):
        if self.outcome != 'errors':
            self.outcome = 'failed'
        super().addFailure(test, err)

    def addError(self, test, err):
        self.outcome = 'errors'
        super().addError(test, err)

    def addSkip(self, test, reason):
        if self.outcome == 'passed':
            self.outcome = 'skipped'
        super().addSkip(test, reason)

    def addExpectedFailure(self, test, err):
        self.outcome = 'expected_failures'
        super().addExpectedFailure(test, err)

    def addUnexpectedSuccess(self, test):
        self.outcome = 'unexpected_successes'
        super().addUnexpectedSuccess(test)

    def addSubTest(self, test, subtest, err):
        if err is not None:
            if self.outcome != 'errors':
                self.outcome = 'failed' if issubclass(err[0], test.failureException) else 'errors'
        super().addSubTest(test, subtest, err)

    def stopTest(self, test):
        self.counts[self.outcome] += 1
        labels = dict(passed='PASS', failed='FAIL', errors='ERROR', skipped='SKIP',
                      expected_failures='EXPECTED FAIL', unexpected_successes='UNEXPECTED PASS')
        self.stream.writeln(f'[{self.testsRun}/{self.total}] {labels[self.outcome]} {test.id()} '
                            f'| passed {self.counts["passed"]}, failed {self.counts["failed"]}, '
                            f'errors {self.counts["errors"]}, skipped {self.counts["skipped"]}')
        self.stream.flush()
        super().stopTest(test)


def run_tests(directory='tests', pattern='test*.py', summary_path=None, suite=None):
    suite = suite if suite is not None else unittest.defaultTestLoader.discover(str(directory), pattern=pattern)
    total = suite.countTestCases()
    progress(f'Running {total} tests. Each line shows the current test and totals.')
    started = time.monotonic()
    runner = unittest.TextTestRunner(stream=sys.stderr, verbosity=0,
                                    resultclass=lambda *a, **kw: ProgressTestResult(*a, total=total, **kw))
    result = runner.run(suite)
    summary = dict(total=total, completed=result.testsRun, **result.counts,
                   elapsed_seconds=round(time.monotonic() - started, 1), successful=result.wasSuccessful() and total > 0)
    progress(f'Tests complete: {result.testsRun}/{total} | passed {summary["passed"]}, '
             f'failed {summary["failed"]}, errors {summary["errors"]}, skipped {summary["skipped"]} '
             f'({summary["elapsed_seconds"]}s)')
    if not total:
        progress('FAILED: no tests were discovered; refusing to validate an untested release.')
    if summary_path:
        Path(summary_path).write_text(json.dumps(summary, indent=2) + '\n')
    return 0 if summary['successful'] else 1


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
    progress(f'Running regression tests with counts and results. Log: {release/"tests.log"}')
    run_logged([str(release/'venv/bin/python'),'-u',str(release/'validation/deploy.py'),'test',
                '--summary',str(release/'test-summary.json')],
               release/'tests.log',cwd=release/'validation',env=env,live=True,step='Regression tests')
    progress('Regression tests PASSED. Recording dependency versions.')
    with (release/'dependencies.txt').open('wb') as log:
        subprocess.run([str(release/'venv/bin/python'),'-m','pip','freeze'],stdout=log,check=True,env=env)


def git_state(source):
    # Read-only provenance; a missing git or non-checkout source records unknown.
    def git(*args):
        result=subprocess.run(['git','-C',str(source),*args],capture_output=True,text=True)
        if result.returncode:
            raise OSError(result.stderr.strip())
        return result.stdout.strip()
    try:
        return git('rev-parse','HEAD'),bool(git('status','--porcelain'))
    except OSError:
        return None,None


def release_info(release):
    """Version metadata for a release; legacy releases fall back to their packaged source."""
    path=release/'RELEASE.json'
    if path.is_file():
        return json.loads(path.read_text())
    try:
        return {'version':versioning.text(versioning.parse((release/'app/ai_bridge/__init__.py').read_text())),'legacy':True}
    except (OSError,ValueError):
        return {'version':None,'legacy':True}


def stage(root, source, python=sys.executable, builder=build_environment, tester=test_release):
    root,source=Path(root).absolute(),Path(source).resolve()
    version=versioning.check(source)
    commit,dirty=git_state(source)
    progress(f'Preparing release {version} from commit {commit or "unknown"}; waiting for deployment lock if another stage is running.')
    if dirty:
        progress('WARNING: source has uncommitted changes; the release records git_dirty=true and cannot be traced to a commit.')
    with deployment_lock(root):
        releases=root/'releases'
        releases.mkdir(mode=0o700,exist_ok=True)
        staged=datetime.datetime.now(datetime.timezone.utc)
        release=releases/(staged.strftime('%Y%m%dT%H%M%SZ')+'-v'+version+'-'+secrets.token_hex(4))
        release.mkdir(mode=0o700)
        progress(f'Copying source and test fixtures into {release}')
        # Build at its FINAL path; venv scripts contain absolute paths.
        # Incomplete releases are retained for diagnosis and never selectable.
        shutil.copytree(source/'ai_bridge',release/'app/ai_bridge',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        validation=release/'validation'
        validation.mkdir()
        shutil.copytree(source/'tests',validation/'tests',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        (validation/'ai_bridge').symlink_to(release/'app/ai_bridge', target_is_directory=True)
        for name in ('install.sh', 'installer.py', 'bootstrap.py', 'uninstall.sh', 'uninstall.py', 'merge_env.py','requirements.txt','requirements.lock','deploy.py','release.py','CHANGELOG.md'):
            shutil.copy2(source/name,validation/name)
        builder(release,source,python)
        tester(release,source)
        progress('Recording release metadata, application and environment checksums.')
        (release/'RELEASE.json').write_text(json.dumps({'version':version,'git_commit':commit,'git_dirty':dirty,
                                                         'staged_at':staged.isoformat(timespec='seconds')},indent=2)+'\n')
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


def app_checksums(release):
    return {k:v for k,v in json.loads((release/'SHA256.json').read_text()).items() if k.startswith('app/')}


def check_upgrade(candidate, active):
    """Forward activation must raise the version, or reselect identical application code."""
    new,old=release_info(candidate)['version'],release_info(active)['version']
    if new is None or old is None:
        return
    new_key,old_key=versioning.parse(f'__version__ = "{new}"'),versioning.parse(f'__version__ = "{old}"')
    if new_key<old_key:
        raise ValueError(f'Release {new} is older than active {old}; use --rollback to select an older release.')
    if new_key==old_key and app_checksums(candidate)!=app_checksums(active):
        raise ValueError(f'Release {new} changes application code but keeps active version {old}; '
                         'bump it with release.py and stage again.')


def activate(root, release, rollback=False):
    root=Path(root).absolute()
    with deployment_lock(root):
        release=verify(root,release)
        current=root/'current'
        if current.exists() and not current.is_symlink():
            raise ValueError('current must be absent or a release symlink.')
        old=os.readlink(current) if current.is_symlink() else None
        if old and not rollback and (root/'current').resolve().is_dir():
            check_upgrade(release,(root/'current').resolve())
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
    parser.add_argument('action',choices=('stage','verify','activate','rollback','check-port','test'))
    parser.add_argument('release',nargs='?')
    parser.add_argument('--root',type=Path,default=Path.home()/'.local/share/ai-bridge')
    parser.add_argument('--source',type=Path,default=Path(__file__).resolve().parent)
    parser.add_argument('--python',default=sys.executable)
    parser.add_argument('--port',type=int,default=8765)
    parser.add_argument('--warn-only',action='store_true',help='Report port conflicts without failing installation.')
    parser.add_argument('--tests',default='tests',help='Test discovery directory for the test action.')
    parser.add_argument('--pattern',default='test*.py',help='Test discovery pattern for the test action.')
    parser.add_argument('--summary',type=Path,help='Write test totals as JSON.')
    args=parser.parse_intermixed_args()
    if args.action == 'test':
        raise SystemExit(run_tests(args.tests, args.pattern, args.summary))
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
        result=verify(args.root,release) if args.action=='verify' else activate(args.root,release,rollback=args.action=='rollback')
    print(result)


if __name__=='__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print('ERROR: '+str(error), file=sys.stderr, flush=True)
        raise SystemExit(1)

"""Durable MCP evidence; native execution mode alone is never evidence.

The public read-only format is documented in docs/agent-management.md.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import time

UUID = re.compile(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}')
JOB_ID = re.compile(r'[a-z0-9-]{4,40}')
AGENTS = {'claude', 'codex', 'cursor'}
MAX_JOBS = 64
MAX_BYTES = 16384


def valid_session(agent, sid):
    return isinstance(agent, str) and agent in AGENTS and isinstance(sid, str) and UUID.fullmatch(sid) is not None


def filename(agent, sid):
    if not valid_session(agent, sid):
        raise ValueError('Use an exact agent and session UUID.')
    return hashlib.sha256(f'{agent}:{sid}'.encode('utf-8')).hexdigest() + '.json'


def _private(info, directory=False):
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if (not kind(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or
            (not directory and info.st_nlink != 1)):
        raise ValueError('Unsafe provenance ownership, permissions or file type.')


def _migrate_management(fd):
    """Tighten only the exact legacy management directory, pinned without links."""
    info = os.fstat(fd)
    if stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o755:
        attributes = os.listxattr(fd)
        if {'system.posix_acl_access', 'system.posix_acl_default'}.intersection(attributes):
            raise ValueError('Legacy management directory has extended ACLs.')
        current = os.fstat(fd)
        if (current.st_uid == info.st_uid and current.st_ino == info.st_ino and
                current.st_dev == info.st_dev and stat.S_IMODE(current.st_mode) == 0o700):
            return  # Another caller completed the same tightening.
        if (current.st_uid, current.st_mode, current.st_ino, current.st_dev) != (
                info.st_uid, info.st_mode, info.st_ino, info.st_dev):
            raise ValueError('Legacy management directory changed during migration.')
        os.fchmod(fd, 0o700)
        os.fsync(fd)


def prepare_management(cfg):
    with _directory(cfg.state_dir, True, subdirs=('management',)):
        pass
    return Path(cfg.state_dir).expanduser().absolute() / 'management'


def observed_session(agent, line):
    """Recognized top-level session events only; tool text is never evidence."""
    try:
        event = json.loads(line)
    except (ValueError, UnicodeError, RecursionError):
        return None
    if not isinstance(event, dict):
        return None
    if agent == 'codex' and event.get('type') == 'thread.started':
        sid = event.get('thread_id')
    elif agent in ('claude', 'cursor') and (
            (event.get('type') == 'system' and event.get('subtype') == 'init') or
            event.get('type') == 'result'):
        if (event.get('session_id') and event.get('chatId') and
                event['session_id'] != event['chatId']):
            return None
        sid = event.get('session_id') or event.get('chatId')
    else:
        return None
    return sid if valid_session(agent, sid) else None


@contextmanager
def _directory(state_dir, create, subdirs=('management', 'provenance')):
    # Pin every component with O_NOFOLLOW, including ancestors. Never resolve
    # an untrusted link or use record contents as a filesystem path.
    path = Path(state_dir).expanduser().absolute()
    if '..' in path.parts:
        raise ValueError('Provenance paths must not contain parent traversal.')
    parts = (*path.parts[1:], *subdirs)
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for index, part in enumerate(parts):
            if create and index >= len(path.parts) - 2:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
            if index == len(path.parts) - 1 and part == 'management':
                _migrate_management(fd)
            if index >= len(path.parts) - 2:
                _private(os.fstat(fd), directory=True)
        yield fd
    finally:
        os.close(fd)


def _load(fd, name):
    try:
        file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    except FileNotFoundError:
        return None
    with os.fdopen(file_fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        _private(info)
        if info.st_size > MAX_BYTES:
            raise ValueError('Provenance record exceeds size limit.')
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError('Provenance record exceeds size limit.')
    try:
        value = json.loads(data)
    except RecursionError as error:
        raise ValueError('Invalid provenance metadata nesting.') from error
    if not isinstance(value, dict):
        raise ValueError('Invalid provenance metadata.')
    return value


def _read(fd, name, agent, sid):
    row = _load(fd, name)
    if row is None:
        return None
    if (not isinstance(row, dict) or type(row.get('schema')) is not int or row['schema'] != 1 or
            row.get('agent') != agent or row.get('session_id') != sid or row.get('origin') != 'mcp' or
            type(row.get('first_seen')) not in (int, float) or
            not 0 <= row['first_seen'] <= 1e12 or not math.isfinite(row['first_seen']) or
            not isinstance(row.get('job_ids'), list) or
            not 1 <= len(row['job_ids']) <= MAX_JOBS or
            any(not isinstance(j, str) or not JOB_ID.fullmatch(j) for j in row['job_ids']) or
            len(set(row['job_ids'])) != len(row['job_ids'])):
        raise ValueError('Invalid provenance record.')
    return row


def _write(fd, name, row):
    data = json.dumps(row, ensure_ascii=True).encode('utf-8')
    if len(data) > MAX_BYTES:
        raise ValueError('Provenance record exceeds size limit.')
    temporary = '.provenance-' + secrets.token_hex(16)
    out = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                  0o600, dir_fd=fd)
    try:
        with os.fdopen(out, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=fd)
        except FileNotFoundError:
            pass


class Provenance:
    def __init__(self, cfg):
        self.state_dir = cfg.state_dir

    def read(self, agent, sid):
        name = filename(agent, sid)
        try:
            with _directory(self.state_dir, False) as fd:
                return _read(fd, name, agent, sid)
        except FileNotFoundError:
            return None

    def observe(self, job, parsed):
        """Only explicit MCP invocation + an adapter-observed UUID authorizes a write.

        Only a new MCP session can establish origin. Matching resumes may append
        job IDs to existing MCP provenance, but cannot classify a direct chat.
        Requested IDs and native source/mode fields are not observations.
        """
        if job.get('kind') != 'agent' or job.get('invocation_origin') != 'mcp':
            return False
        if not isinstance(parsed, dict):
            raise ValueError('Invalid parsed provenance evidence.')
        agent, sid, jid = job.get('agent'), parsed.get('session_id'), job.get('id')
        if (not valid_session(agent, sid) or not isinstance(jid, str) or not JOB_ID.fullmatch(jid) or
                job.get('requested_session_id') not in (None, sid)):
            return False
        name = filename(agent, sid)
        with _directory(self.state_dir, True) as fd:
            lock = os.open('.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                           0o600, dir_fd=fd)
            try:
                _private(os.fstat(lock))
                fcntl.flock(lock, fcntl.LOCK_EX)
                row = _read(fd, name, agent, sid)
                if row is None and job.get('requested_session_id') is not None:
                    return False
                # Durable per-job binding also protects backfill/restarts from a
                # later output snapshot claiming a different valid UUID.
                binding_name = '.job-' + jid + '.json'
                binding = _load(fd, binding_name)
                if binding is not None:
                    if (not isinstance(binding, dict) or type(binding.get('schema')) is not int or
                            binding['schema'] != 1 or
                            binding.get('job_id') != jid or binding.get('agent') != agent or
                            not valid_session(agent, binding.get('session_id'))):
                        raise ValueError('Invalid provenance job binding.')
                    if binding['session_id'] != sid:
                        return False
                else:
                    _write(fd, binding_name, {'schema': 1, 'job_id': jid,
                                             'agent': agent, 'session_id': sid})
                if row and jid in row['job_ids']:
                    return True
                if row is None:
                    row = {'schema': 1, 'agent': agent, 'session_id': sid, 'origin': 'mcp',
                           'first_seen': time.time(), 'job_ids': []}
                row['job_ids'] = (row['job_ids'] + [jid])[-MAX_JOBS:]
                _write(fd, name, row)
            finally:
                os.close(lock)
        return True

    def backfill(self, jobs, job):
        if job.get('kind') == 'agent' and job.get('invocation_origin') == 'mcp':
            from .job_paths import readable_sidecar
            from .management import read_json
            path = readable_sidecar(jobs.dir, job['id'], 'output')
            try:
                # Recorder snapshots include bounded progress/final text as well as IDs.
                output = read_json(path, max_bytes=4 * 1024 * 1024)
            except FileNotFoundError:
                return False
            if not isinstance(output, dict):
                raise ValueError('Invalid provenance output snapshot.')
            if output.get('provenance_error'):
                job['provenance_error'] = str(output['provenance_error'])[:300]
            if not output.get('parser_error'):
                sid = output.get('provenance_session_id')
                if 'provenance_session_id' not in output:
                    sid = self._legacy_session(jobs, job, output)
                return self.observe(job, {'session_id': sid})
        return False

    def _legacy_session(self, jobs, job, output):
        # Older snapshots kept only the last parsed ID. Recover the first
        # recognized event from a complete bounded log, never that last ID.
        if output.get('truncated') or output.get('records_dropped'):
            return None
        try:
            with _directory(self.state_dir, False, subdirs=('jobs',)) as directory:
                fd = os.open(job['id'] + '.log', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=directory)
                with os.fdopen(fd, 'rb') as stream:
                    _private(os.fstat(stream.fileno()))
                    data = stream.read(4 * 1024 * 1024 + 1)
        except FileNotFoundError:
            return None
        if len(data) > 4 * 1024 * 1024:
            return None
        for line in data.splitlines():
            sid = observed_session(job['agent'], line)
            if sid:
                return sid
        return None

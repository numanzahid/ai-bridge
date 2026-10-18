"""Shared task, session overlay and managed-agent operations."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import tempfile
import time

from .core import resolve_cwd
from .optional_agents import agents as ag

UNCERTAIN = {'running', 'cleanup_incomplete', 'lost'}


def uncertain(job):
    return job.get('status') in UNCERTAIN and not (job.get('status') == 'lost' and
                                                  job.get('identity_state') == 'previous_boot')


TASK_ID = re.compile(r'task-[0-9a-f]{24}')


def private_dir(path, create=True):
    path = Path(path).absolute()
    for parent in reversed((path, *path.parents)):
        if parent.is_symlink():
            raise ValueError('Management directories must not traverse symlinks.')
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError(f'Management directory must be owned by this user and private: {path}')
    return path


def read_json(path, max_bytes=65536):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('Unsafe management metadata permissions or ownership.')
        if info.st_size > max_bytes:
            raise ValueError('Management metadata exceeds size limit.')
        data = stream.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError('Management metadata exceeds size limit.')
        return json.loads(data)


def write_json(path, value):
    fd, name = tempfile.mkstemp(prefix='.management-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def management_lock(cfg):
    from .provenance import prepare_management
    directory = prepare_management(cfg)
    fd = os.open(directory / '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('Unsafe management lock.')
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


class Tasks:
    def __init__(self, cfg):
        self.cfg = cfg
        self.root = Path(cfg.tasks_dir).expanduser().absolute()

    def create(self):
        private_dir(self.root)
        task_id = 'task-' + secrets.token_hex(12)
        path = self.root / task_id
        path.mkdir(mode=0o700)
        (path / 'workspace').mkdir(mode=0o700)
        row = {'task_id': task_id, 'schema': 1, 'owner_uid': os.getuid(),
               'created': time.time(), 'workspace': str(path / 'workspace')}
        write_json(path / '.task.json', row)
        if self.cfg.trust_tasks:
            # Empty bridge-created folder: trust it so agent CLIs never stop at a prompt nobody can answer.
            from .trust import set_trust
            row = dict(row, trust=set_trust(row['workspace'])['agents'])
        return row

    def inspect(self, task_id):
        if not TASK_ID.fullmatch(task_id):
            raise ValueError('Use an exact task ID.')
        private_dir(self.root)
        path = self.root / task_id
        if not path.exists():
            raise KeyError('Task not found.')
        private_dir(path, create=False)
        private_dir(path / 'workspace', create=False)
        row = read_json(path / '.task.json')
        if (row.get('task_id') != task_id or row.get('schema') != 1 or
                row.get('owner_uid') != os.getuid() or row.get('workspace') != str(path / 'workspace')):
            raise ValueError('Unrecognized task ownership metadata.')
        return row

    def list(self):
        if not self.root.exists() and not self.root.is_symlink():
            return []
        private_dir(self.root)
        return [self.inspect(p.name) for p in sorted(self.root.iterdir()) if TASK_ID.fullmatch(p.name)]

    def for_cwd(self, cwd):
        # Only adopt an exact workspace with a valid ownership marker.
        path = Path(cwd).absolute()
        if path.name == 'workspace' and path.parent.parent == self.root:
            return self.inspect(path.parent.name)
        return None

    def cleanup(self, task_id, jobs, dry_run=True, *, allow_uninspectable=False, confirmed=False):
        if allow_uninspectable and not dry_run and not confirmed:
            raise ValueError('Ignoring uninspectable processes requires --yes and --allow-uninspectable.')
        with management_lock(self.cfg):
            row = self.inspect(task_id)
            workspace = Path(row['workspace'])
            identities = [(p.stat().st_dev, p.stat().st_ino) for p in (workspace.parent, workspace)]
            blockers = []
            for job in jobs.list(limit=None):
                if uncertain(job) and (job.get('task_id') == task_id or
                        Path(job.get('cwd') or '/').resolve().is_relative_to(workspace)):
                    blockers.append({'job_id': job['id'], 'status': job['status']})
            observed = observed_task_processes(workspace)
            ignored = [entry for entry in observed
                       if allow_uninspectable and entry.get('status') == 'uninspectable_process']
            blockers.extend(entry for entry in observed
                            if not (allow_uninspectable and entry.get('status') == 'uninspectable_process'))
            blockers.extend(task_mounts(workspace.parent))
            result = {'task_id': task_id, 'path': str(workspace.parent), 'dry_run': dry_run,
                      'blockers': blockers, 'native_history_deleted': False}
            result['cleanup_allowed'] = not blockers
            if allow_uninspectable:
                result.update(allow_uninspectable=True,
                              ignored_uninspectable_pids=sorted({entry['pid'] for entry in ignored}),
                              risk='HIGH RISK: uninspectable processes may still use this workspace; '
                                   'deletion can remove files they need. No process will be stopped.')
            if blockers and not dry_run:
                detail = json.dumps(result) if allow_uninspectable else json.dumps(blockers)
                raise ValueError('Task cleanup refused: active or uncertain processes/jobs: ' + detail)
            if not dry_run:
                if (self.inspect(task_id) != row or
                        [(p.stat().st_dev, p.stat().st_ino) for p in (workspace.parent, workspace)] != identities):
                    raise ValueError('Task state changed during cleanup; rerun dry-run.')
                if not shutil.rmtree.avoids_symlink_attacks:
                    raise ValueError('Safe directory removal is unavailable on this platform.')
                if self.cfg.trust_tasks:
                    from .trust import set_trust
                    result['trust_removed'] = set_trust(str(workspace), trusted=False)['agents']
                # Pin the parent; removal never follows workspace symlinks.
                fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    shutil.rmtree(task_id, dir_fd=fd)
                finally:
                    os.close(fd)
                result['removed'] = True
            return result


def task_mounts(path, mountinfo=None):
    if mountinfo is None:
        mountinfo = Path('/proc/self/mountinfo').read_text()
    blockers = []
    for line in mountinfo.splitlines():
        columns = line.split(' - ', 1)[0].split()
        if len(columns) < 5:
            raise ValueError('Cannot verify task mount boundaries.')
        decoded = re.sub(r'\\([0-7]{3})', lambda match: chr(int(match[1], 8)), columns[4])
        mount = Path(decoded)
        if mount.is_relative_to(path):
            blockers.append({'path': str(mount), 'status': 'mounted_subtree'})
    return blockers


def observed_task_processes(workspace):
    """Uninspectable same-user processes keep cleanup conservative."""
    blockers = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != os.getuid():
                continue
            cwd = (entry / 'cwd').resolve(strict=True)
            if cwd.is_relative_to(workspace):
                blockers.append({'pid': int(entry.name), 'status': 'observed_native_process'})
        except FileNotFoundError:
            continue
        except PermissionError:
            blockers.append({'pid': int(entry.name), 'status': 'uninspectable_process'})
    return blockers


def start_agent(cfg, jobs, agent, prompt, cwd=None, session_id=None, permission=None,
                model=None, task_id=None, timeout=None, *, invocation_origin=None,
                mode=None, remote=False, name=None, trust=False):
    if agent not in ag.AGENTS or (permission or cfg.agent_default_permission) not in ag.PERMISSIONS:
        raise ValueError('Unknown agent or permission.')
    # Interactive (normal native chat) is the default where the CLI supports it.
    mode = mode or ('interactive' if agent in ag.INTERACTIVE_AGENTS else 'exec')
    if mode not in ('interactive', 'exec'):
        raise ValueError('mode must be interactive or exec.')
    if mode == 'interactive' and agent not in ag.INTERACTIVE_AGENTS:
        raise ValueError(f'{agent} supports mode=exec only.')
    if remote and mode != 'interactive':
        raise ValueError('remote needs mode=interactive (exec runs cannot be remote controlled).')
    if name is not None and (not isinstance(name, str) or not 1 <= len(name) <= 100 or
                             any(ord(c) < 32 for c in name)):
        raise ValueError('name must contain 1..100 printable characters.')
    if timeout is not None and (type(timeout) is not int or not 1 <= timeout <= 604800):
        raise ValueError('timeout must be 1..604800 seconds, or omitted.')
    binary = ag.find_binary(agent)
    if not binary:
        raise ValueError('Agent CLI is unavailable.')
    perm = permission or cfg.agent_default_permission
    with management_lock(cfg):
        tasks = Tasks(cfg)
        task = tasks.inspect(task_id) if task_id is not None else None
        if session_id is not None:
            workdir = resolve_cwd(cfg, ag.resume_cwd(agent, session_id, cwd))
            if task and task['workspace'] != workdir:
                raise ValueError('Task conflicts with the saved session workspace.')
            busy = jobs.running_for_session(agent, session_id)
            if busy:
                raise ValueError(f"Session is busy with job {busy['id']}.")
            task = task or tasks.for_cwd(workdir)
        elif task:
            workdir = task['workspace']
            if cwd and os.path.realpath(os.path.expanduser(cwd)) != workdir:
                raise ValueError('cwd conflicts with task workspace.')
        elif cwd is not None:
            workdir = resolve_cwd(cfg, cwd)
            task = tasks.for_cwd(workdir)
        else:
            task = tasks.create()
            workdir = task['workspace']
        trusted = None
        if trust:
            from .trust import set_trust
            trusted = set_trust(workdir, [agent])
        peers = [m['id'] for m in jobs.list(limit=None, kind='agent')
                 if uncertain(m) and m.get('cwd') == workdir]
        if mode == 'interactive':
            argv = ag.build_interactive_argv(agent, binary, prompt, session_id, perm, model, name, remote)
        else:
            argv = ag.build_argv(agent, binary, prompt, session_id, perm, model)
        label = f'resume {session_id}' if session_id else 'new turn'
        return jobs.start(argv, workdir, kind='agent',
                          display=f'{agent}: {label} ({mode}{", remote" if remote else ""})',
                          extra={'agent': agent, 'session_id': session_id, 'requested_session_id': session_id,
                                 'mode': mode, 'remote': remote, 'name': name, 'trust': trusted,
                                 'invocation_origin': invocation_origin,
                                 'permission': perm, 'model': model, 'task_id': task['task_id'] if task else None,
                                 'workspace_type': 'task' if task else 'project', 'timeout': timeout,
                                 'workspace_peers': peers,
                                 'workspace_note': 'Shared files: coordinate edits with peers or create separate tasks.' if peers else None})


def runtime(jobs, job_id=None):
    from .execution import identity_matches
    from .job_paths import readable_sidecar
    rows = [jobs.load(job_id)] if job_id else jobs.list(limit=None, kind='agent')
    out = []
    for row in rows:
        if row.get('kind') != 'agent':
            raise ValueError('Not a managed agent job; use job_status/kill_job for commands.')
        verified = identity_matches(row.get('pid'), row.get('identity'))
        view = {k: row.get(k) for k in ('id', 'agent', 'session_id', 'status', 'task_id', 'cwd',
                                      'timeout', 'cleanup', 'identity_state', 'workspace_peers', 'workspace_note')}
        view.update(job_id=row['id'], managed=True, runner_pid=row.get('pid'), runner_identity_verified=verified,
                    observation_scope='Bridge jobs only; saved sessions and unrelated native services are not processes managed here.')
        process_path = readable_sidecar(jobs.dir, row['id'], 'process')
        if process_path.exists():
            process = read_json(process_path)
            target_pid, target_identity = process.get('pid'), process.get('identity')
            target_verified = bool(target_pid and identity_matches(target_pid, target_identity))
            view.update(target_pid=target_pid, target_identity_verified=target_verified,
                        observed_descendants=[{'pid': int(pid), 'identity_verified': identity_matches(int(pid), identity)}
                                              for pid, identity in process.get('descendants', {}).items()],
                        cleanup_scope='Original group and sampled owned lineage; detached/privileged escape is not certifiable.')
        if verified:
            try:
                root = Path('/proc') / str(row['pid'])
                fields = (root / 'stat').read_text().rsplit(') ', 1)[1].split()
                view['runner_resources'] = {'cpu_ticks': int(fields[11]) + int(fields[12]),
                                            'rss_bytes': int(fields[21]) * os.sysconf('SC_PAGE_SIZE'),
                                            'cgroup': (root / 'cgroup').read_text().strip()}
            except (OSError, ValueError, IndexError):
                view['runner_resources'] = None
        out.append(view)
    return out[0] if job_id else out


def stop_agent(jobs, job_id):
    from .execution import BOOT_ID, Descendants
    from .job_paths import readable_sidecar
    row = jobs.load(job_id)
    if row.get('kind') != 'agent':
        raise ValueError('Not a managed agent job.')
    if row['status'] == 'running' and not row.get('runner'):
        raise ValueError('No managed runner for safe agent cancellation; inspect legacy state locally.')
    row = jobs.kill(job_id)
    # A completed runner may have left observed children. Only its private,
    # same-boot lineage identities can authorize further cancellation.
    if row['status'] in ('cleanup_incomplete', 'lost'):
        if not row.get('runner') or not row.get('identity') or row['identity'].get('boot_id') != BOOT_ID:
            raise ValueError('No verified current-boot runner lineage; inspect locally.')
        path = readable_sidecar(jobs.dir, job_id, 'process')
        if not path.exists():
            raise ValueError('No recorded lineage; inspect locally.')
        state = read_json(path)
        if state.get('descendants'):
            tracker = Descendants(-1)
            tracker.cgroup = state.get('target_cgroup')
            tracker.known = {int(pid): identity for pid, identity in state['descendants'].items()}
            result = tracker.cancel()
            row['cleanup'] = dict(row.get('cleanup') or {}, **result, complete=False,
                                  note='Recorded lineage checked; whole-tree cleanup remains uncertified.')
            jobs.save(row)
    return dict(runtime(jobs, job_id), cancel_requested=row.get('cancel_requested', False))


class Sessions:
    def __init__(self, cfg, jobs):
        self.cfg, self.jobs = cfg, jobs

    def overlay_path(self, agent, sid):
        if agent not in ag.AGENTS or not re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', sid):
            raise ValueError('Use an exact agent and UUID.')
        from .provenance import prepare_management
        prepare_management(self.cfg)
        root = private_dir(self.cfg.state_dir / 'management' / 'sessions')
        return root / (hashlib.sha256(f'{agent}:{sid}'.encode()).hexdigest() + '.json')

    def overlay(self, agent, sid, **changes):
        with management_lock(self.cfg):
            info = ag.session_info(agent, sid)
            path = self.overlay_path(agent, sid)
            value = read_json(path) if path.exists() or path.is_symlink() else {}
            if changes:
                if 'alias' in changes and changes['alias'] is None:
                    # Clearing restores the native title.
                    value.pop('alias', None)
                    changes = {k: v for k, v in changes.items() if k != 'alias'}
                if 'alias' in changes and (not isinstance(changes['alias'], str) or
                        not 1 <= len(changes['alias']) <= 200 or any(ord(c) < 32 for c in changes['alias'])):
                    raise ValueError('Display alias must contain 1..200 printable characters.')
                value.update(changes)
                write_json(path, value)
            return dict(info, bridge_metadata=value)

    def enrich(self, agent, rows, include_archived=False):
        from .provenance import Provenance, valid_session, prepare_management
        prepare_management(self.cfg)
        provenance = Provenance(self.cfg)
        jobs = self.jobs.list(limit=None, kind='agent')
        indexed = {}
        for job in jobs:
            if job.get('agent') != agent:
                continue
            observed = job.get('session_id')
            if not observed:
                observed = (self.jobs.output_info(job['id']).get('parsed') or {}).get('session_id')
            if observed:
                indexed.setdefault(observed, []).append(job)
        out = []
        for original in rows:
            row = dict(original)
            row['bridge_origin'] = None
            sid = row.get('session_id', '')
            if not valid_session(agent, sid):
                out.append(row)
                continue
            try:
                row['bridge_origin'] = 'mcp' if provenance.read(agent, sid) else None
            except (OSError, ValueError) as error:
                row['bridge_provenance_error'] = str(error)[:300]
            path = self.overlay_path(agent, sid)
            overlay = read_json(path) if path.exists() or path.is_symlink() else {}
            row['archived'] = overlay.get('archived', False)
            if row['archived'] and not include_archived:
                continue
            if overlay.get('alias'):
                row['native_title'] = row.get('title')
                row['title'] = overlay['alias']
                row['title_source'] = 'bridge_alias'
            matches = indexed.get(sid, [])
            busy = [j for j in matches if uncertain(j)]
            row.update(status='active_job' if any(j['status'] == 'running' for j in busy) else
                       'uncertain' if busy else 'completed' if matches else 'idle',
                       status_scope='bridge_jobs', active_job_ids=[j['id'] for j in busy],
                       task_id=next((j.get('task_id') for j in matches if j.get('task_id')), None))
            if not row['task_id'] and row.get('cwd'):
                task = Tasks(self.cfg).for_cwd(row['cwd'])
                row['task_id'] = task['task_id'] if task else None
            row['workspace_type'] = 'task' if row['task_id'] else 'project'
            out.append(row)
        return out

    def interactive(self, agent, sid, action='open'):
        from .execution import probe
        info = ag.session_info(agent, sid)
        cwd = ag.resume_cwd(agent, sid, None)
        storage = Path(info['storage_path'])
        if storage.is_symlink():
            raise ValueError('Native session storage symlink requires manual inspection.')
        from .permissions import native_session_storage_safe
        if not native_session_storage_safe(storage, os.getuid()):
            raise ValueError('Native session storage ownership or permissions are unsafe.')
        binary = ag.find_binary(agent)
        if not binary:
            raise ValueError('Native CLI is unavailable.')
        # Inspect installed help only; preflight never starts a turn or writes history.
        help_args = [binary, 'resume', '--help'] if agent == 'codex' else [binary, '--help']
        help_result = probe(help_args)
        help_text = help_result['stdout'] + help_result['stderr']
        if help_result['exit_code'] != 0 or help_result['timed_out'] or help_result['stdout_truncated'] or help_result.get('stderr_truncated'):
            raise ValueError('Cannot establish installed native resume capability.')
        if action == 'fork':
            if agent != 'codex':
                raise ValueError('Native fork is unsupported by this adapter; the original is preserved.')
            check = probe([binary, 'fork', '--help'])
            if (check['exit_code'] != 0 or check['timed_out'] or check.get('stdout_truncated') or
                    check.get('stderr_truncated') or 'SESSION_ID' not in check['stdout']):
                raise ValueError('Native fork capability is unverified.')
            if info.get('mode') == 'noninteractive' and '--include-non-interactive' not in check['stdout']:
                raise ValueError('Native fork of noninteractive state is not advertised; use open instead.')
            argv = [binary, 'fork'] + (['--include-non-interactive'] if info.get('mode') == 'noninteractive' else []) + [sid]
        elif agent == 'codex':
            if '--include-non-interactive' not in help_text:
                raise ValueError('Installed CLI does not advertise noninteractive resume; conversion unsupported.')
            argv = [binary, 'resume', '--include-non-interactive', sid]
        else:
            if '--resume' not in help_text:
                raise ValueError('Native resume capability is unverified.')
            argv = [binary, '--resume', sid]
        return {'agent': agent, 'session_id': sid, 'cwd': cwd, 'argv': argv,
                'action': action, 'method': 'native_cli', 'history_rewritten': False,
                'verified_execution': False, 'storage_path': info.get('storage_path'),
                'note': 'Preflight only; native resume may still reject state. Promotion does not rewrite picker provenance.'}

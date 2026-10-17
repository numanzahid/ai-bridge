"""Host-terminal removal planner. Standard library only; never kills job processes."""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile


class Refusal(ValueError):
    pass


class Layout:
    def __init__(self, root=Path('/')):
        self.root = Path(root)
        self.unit = self.root/'etc/systemd/system/ai-bridge.service'
        self.etc = self.root/'etc/ai-bridge'
        self.registry = self.root/'var/lib/ai-bridge/installation.json'
        self.pool = self.root/'var/lib/ai-bridge-bootstrap'


def exists(path):
    return path.exists() or path.is_symlink()


def safe_path(path):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts or str(path) == '/':
        raise Refusal(f'Unsafe path: {path}')
    for component in (path, *path.parents):
        if component.is_symlink():
            raise Refusal(f'Symlink traversal refused: {component}')
    return path


def trusted(path):
    safe_path(path)
    value = path.stat()
    if not (stat.S_ISREG(value.st_mode) or stat.S_ISDIR(value.st_mode)):
        raise Refusal(f'Special system artifact refused: {path}')
    if value.st_uid != 0 or value.st_mode & 0o022:
        raise Refusal(f'Expected root-owned, non-writable system artifact: {path}')


def settings(path):
    if not exists(path):
        return {}
    trusted(path)
    if not path.is_file():
        raise Refusal(f'Expected a regular configuration file: {path}')
    result = {}
    for line in path.read_text().splitlines():
        key, sep, value = line.partition('=')
        if sep and key.strip() in ('AI_BRIDGE_CONFIG_DIR', 'AI_BRIDGE_STATE_DIR', 'XDG_CONFIG_HOME', 'HOME'):
            value = value.strip()
            if value.startswith(('"', "'")):
                words = shlex.split(value)
                if len(words) != 1:
                    raise Refusal(f'Unsupported directory value in {path}')
                value = words[0]
            if any(char in value for char in ('$','%', '\\', '\n')):
                raise Refusal(f'Use literal absolute directory values in {path}')
            result[key.strip()] = value
    return result


def unit_identity(path, layout, lookup):
    trusted(path)
    text = path.read_text()
    values = {}
    section = ''
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(('#', ';')):
            continue
        if line.startswith('['):
            section = line
            continue
        key, sep, value = line.partition('=')
        if sep and ((section == '[Service]' and key.startswith('Exec') and key != 'ExecStart') or
                    (section == '[Install]' and key not in ('WantedBy',)) or
                    (section == '[Unit]' and key not in ('Description', 'After', 'Wants'))):
            raise Refusal(f'Custom unit actions/dependencies need manual review: {path}')
        if section == '[Service]' and sep:
            values.setdefault(key, []).append(value)
    if values.get('KillMode') != ['process']:
        raise Refusal('Service must use KillMode=process; review background-process safety manually.')
    users = values.get('User', ['root'])
    if len(users) != 1:
        raise Refusal(f'Ambiguous service identity: {path}')
    try:
        account = lookup(users[0])
    except KeyError as error:
        raise Refusal('Service account is missing; restore its identity or review removal manually.') from error
    home = safe_path(Path(account.pw_dir))
    app = home/'.local/share/ai-bridge'
    starts = values.get('ExecStart', [])
    if len(starts) != 1:
        raise Refusal(f'Unrecognized service executable: {path}')
    argv = shlex.split(starts[0])
    executable = Path(argv[0]) if argv else Path('/')
    if len(argv) != 3 or argv[1:] != ['-m', 'ai_bridge'] or executable.name != 'python':
        raise Refusal(f'Unit is not an identifiable bridge service: {path}')
    if executable != app/'venv/bin/python' and not (executable.is_relative_to(app/'releases') and executable.parts[-3:] == ('venv','bin','python')):
        raise Refusal('Service executable is outside the recognized install root; manual review required.')
    if values.get('EnvironmentFile') != [str(layout.etc/'ai-bridge.env')]:
        raise Refusal('Unexpected service environment file; manual review required.')
    for entry in values.get('Environment', []):
        if any(item.partition('=')[0] not in ('PYTHONPATH', 'PYTHONUNBUFFERED') for item in shlex.split(entry)):
            raise Refusal('Custom unit environment needs manual path/ownership review.')
    return account


def configured_paths(home, env):
    if env.get('HOME', str(home)) != str(home):
        raise Refusal('Configured HOME differs from passwd home; manual review required.')
    return {'config': safe_path(Path(env.get('AI_BRIDGE_CONFIG_DIR') or str(Path(env.get('XDG_CONFIG_HOME') or home/'.config')/'ai-bridge'))),
            'state': safe_path(Path(env.get('AI_BRIDGE_STATE_DIR') or home/'.local/state/ai-bridge'))}


def fingerprint(path):
    value = path.lstat()
    return [value.st_dev, value.st_ino, value.st_uid, stat.S_IMODE(value.st_mode), stat.S_IFMT(value.st_mode),
            value.st_mtime_ns if stat.S_ISREG(value.st_mode) else None, value.st_size if stat.S_ISREG(value.st_mode) else None]


def record_install(layout, account):
    """Register only dedicated bridge directories, never broad shared locations."""
    home = safe_path(Path(account.pw_dir))
    paths = configured_paths(home, settings(layout.etc/'ai-bridge.env'))
    records = {}
    if exists(layout.registry):
        trusted(layout.registry)
        prior = json.loads(layout.registry.read_text())
        if (prior.get('user'), prior.get('uid'), prior.get('home')) != (account.pw_name, account.pw_uid, str(home)):
            raise Refusal('Existing ownership record belongs to another identity.')
    for kind, path in paths.items():
        if path.name != 'ai-bridge' or path == home or path in home.parents or any(path.is_relative_to(home/name) for name in ('.codex', '.claude', '.cursor')) or exists(path/'.git') or (path/'install.sh').is_file():
            print(f'WARNING: unregistered custom {kind} directory; future removal needs manual ownership review: {path}', file=sys.stderr)
            continue
        if exists(path):
            if not path.is_dir() or path.stat().st_uid != account.pw_uid:
                raise Refusal(f'Directory ownership mismatch: {path}')
            records[kind] = {'path': str(path), 'identity': fingerprint(path)}
    parent = layout.registry.parent
    safe_path(parent)
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    trusted(parent)
    fd, temporary = tempfile.mkstemp(prefix='.installation-', dir=parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump({'version': 1, 'user': account.pw_name, 'uid': account.pw_uid, 'home': str(home), 'directories': records}, stream)
        os.replace(temporary, layout.registry)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Plan:
    def __init__(self, layout=None, lookup=pwd.getpwnam, systemctl=None):
        self.layout = layout or Layout()
        self.lookup = lookup
        self.systemctl = systemctl or self._systemctl
        self.targets = []
        self.blockers = []
        self.notes = []
        self.account = None
        self.had_unit = False
        self.states = []
        self.links = []

    @staticmethod
    def _systemctl(*args):
        return subprocess.run(['systemctl', *args], check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()

    def add(self, path, reason, system=False):
        if not exists(path):
            return
        safe_path(path)
        if system:
            trusted(path)
        if path.is_dir() and exists(path/'.git'):
            raise Refusal(f'Source repository retained; manual removal required: {path}')
        if not path.is_dir() and not path.is_file():
            raise Refusal(f'Special file refused: {path}')
        if any(path == Path(row['path']) for row in self.targets):
            return
        self.targets.append({'path': str(path), 'reason': reason, 'identity': fingerprint(path)})

    def build(self, user=None):
        layout = self.layout
        registry = None
        if exists(layout.registry):
            trusted(layout.registry)
            registry = json.loads(layout.registry.read_text())
        if exists(layout.unit):
            self.account = unit_identity(layout.unit, layout, self.lookup)
            self.had_unit = True
            if user and user != self.account.pw_name:
                raise Refusal('--user does not match the installed service identity.')
        elif registry:
            self.account = self.lookup(registry['user'])
        elif user:
            self.account = self.lookup(user)
            self.notes.append('No unit/registry: only recognizable legacy artifacts for the explicit account will be considered.')
        else:
            if exists(layout.etc) or exists(layout.unit.with_name('ai-bridge.service.before-versioned')):
                raise Refusal('Missing unit and ownership registry. Specify --user after reviewing the original service identity.')
            self.notes.append('No installation identity found; nothing selected. Use --user for reviewed legacy remnants.')
            return self
        account = self.account
        if user and user != account.pw_name:
            raise Refusal('--user does not match the installed ownership identity.')
        home = safe_path(Path(account.pw_dir))
        if home == Path('/') or (exists(home) and home.stat().st_uid != account.pw_uid):
            raise Refusal('Service home ownership mismatch.')
        if registry and (registry.get('user'), registry.get('uid'), registry.get('home')) != (account.pw_name, account.pw_uid, str(home)):
            raise Refusal('Ownership registry disagrees with service/passwd identity.')
        env = settings(layout.etc/'ai-bridge.env')
        paths = configured_paths(home, env)
        app = home/'.local/share/ai-bridge'
        if exists(app):
            safe_path(app)
            recognizable = (app/'app/ai_bridge/core.py').is_file() or (app/'releases').is_dir()
            if not recognizable or app.stat().st_uid != account.pw_uid:
                raise Refusal(f'Unmanaged application directory: {app}')
            self.add(app, 'installed releases, environments and legacy application')
        candidates = list(paths.items())
        for kind, suffix in (('config', '.config/ai-bridge'), ('state', '.local/state/ai-bridge')):
            default = home/suffix
            if default != paths[kind]:
                candidates.append((kind, default))
        for kind, path in candidates:
            defaults = home/('.config/ai-bridge' if kind == 'config' else '.local/state/ai-bridge')
            if not exists(path):
                continue
            if path.stat().st_uid != account.pw_uid or not path.is_dir():
                raise Refusal(f'Unmanaged {kind} directory: {path}')
            if path != defaults:
                record = (registry or {}).get('directories', {}).get(kind, {})
                if record.get('path') != str(path) or record.get('identity') != fingerprint(path) or path.name != 'ai-bridge':
                    raise Refusal(f'Alternate {kind} directory has no matching trusted ownership record: {path}. Retain and clean it manually after review.')
            elif kind == 'state' and not (path/'jobs').is_dir() and not (path/'audit.log').is_file():
                raise Refusal(f'Unrecognized bridge state directory: {path}')
            elif kind == 'config' and not any((path/name).exists() for name in ('server-instructions.md', 'tools.json', 'instructions', 'resources', 'prompts')):
                raise Refusal(f'Unrecognized bridge configuration directory: {path}')
            if path == home or path in home.parents or app.is_relative_to(path) or any(path.is_relative_to(home/name) for name in ('.codex', '.claude', '.cursor')):
                raise Refusal(f'Protected/shared directory: {path}')
            self.add(path, f'{kind}: all bridge instructions, resources, prompts, jobs or logs')
            if kind == 'state':
                self.states.append(path)
        if self.had_unit:
            self.add(layout.unit, 'system service unit', True)
            for system in (layout.unit.parent, layout.root/'run/systemd/system'):
                if not system.exists():
                    continue
                for link in system.rglob('ai-bridge.service'):
                    if link.is_symlink():
                        safe_path(link.parent)
                        trusted(link.parent)
                        if link.resolve() != layout.unit or link.lstat().st_uid != 0:
                            raise Refusal(f'Unrecognized activation link: {link}')
                        self.links.append({'path': str(link), 'identity': fingerprint(link), 'target': str(layout.unit)})
            # Inspect the effective identity; do not silently ignore injected drop-ins.
            if self.systemctl('show', 'ai-bridge.service', '--property=KillMode', '--value') != 'process':
                raise Refusal('Effective service KillMode is unsafe for detached jobs; manual review required.')
            for action in ('ExecStop', 'ExecStopPost'):
                if self.systemctl('show', 'ai-bridge.service', '--property='+action, '--value'):
                    raise Refusal('Effective service has custom stop actions; manual review required.')
            effective_user = self.systemctl('show', 'ai-bridge.service', '--property=User', '--value') or 'root'
            if effective_user != account.pw_name:
                raise Refusal('Effective service identity differs from unit/passwd identity.')
            fragment = self.systemctl('show', 'ai-bridge.service', '--property=FragmentPath', '--value')
            if fragment and fragment != str(layout.unit):
                raise Refusal('Effective unit fragment differs from the planned unit.')
            drops = layout.unit.with_name('ai-bridge.service.d')
            if exists(drops):
                trusted(drops)
                for entry in drops.iterdir():
                    trusted(entry)
                    if not entry.is_file() or not entry.read_text().startswith('# AI-Bridge-owned drop-in\n'):
                        raise Refusal(f'Unmanaged service drop-in: {entry}; review/remove it manually first.')
                for entry in drops.iterdir():
                    for line in entry.read_text().splitlines():
                        key, sep, value = line.partition('=')
                        if sep and key.strip() not in ('RestartSec', 'TimeoutStartSec', 'TimeoutStopSec', 'MemoryMax', 'CPUQuota'):
                            raise Refusal('Drop-in changes identity, paths or lifecycle; manual review required.')
                self.add(drops, 'exclusively marked bridge drop-ins', True)
            effective = self.systemctl('show', 'ai-bridge.service', '--property=DropInPaths', '--value')
            if effective:
                expected = {str(p) for p in drops.glob('*.conf')} if exists(drops) else set()
                # Escaped system paths require manual inspection instead of guesswork.
                if set(effective.split()) != expected:
                    raise Refusal('Unrecognized effective service drop-ins; manual review required.')
        backup = layout.unit.with_name('ai-bridge.service.before-versioned')
        if exists(backup):
            old_account = unit_identity(backup, layout, self.lookup)
            if old_account.pw_uid != account.pw_uid:
                raise Refusal('Backup unit belongs to another identity.')
            self.add(backup, 'original bridge unit backup', True)
        if exists(layout.etc):
            trusted(layout.etc)
            if not (layout.etc/'ai-bridge.env').is_file():
                raise Refusal('Config directory without identifiable environment file; manual review required.')
            self.add(layout.etc, 'bridge environment and root-only sudo credential', True)
        policy = layout.root/'etc/sudoers.d'/('ai-bridge-'+account.pw_name)
        if exists(policy):
            trusted(policy)
            if policy.read_text() != f'{account.pw_name} ALL=(root) PASSWD: ALL\n':
                raise Refusal('Sudo policy differs from installer policy; manual review required.')
            self.add(policy, 'exact installer sudo policy', True)
        if exists(layout.pool):
            trusted(layout.pool)
            for copy in layout.pool.iterdir():
                if not copy.name.startswith(account.pw_name+'-'):
                    continue
                safe_path(copy)
                if copy.is_dir() and copy.stat().st_uid == account.pw_uid and (copy/'install.sh').is_file() and (copy/'ai_bridge/core.py').is_file():
                    self.add(copy, 'installer bootstrap source copy for service account')
                else:
                    raise Refusal(f'Unrecognized bootstrap copy: {copy}')
        if registry:
            self.add(layout.registry, 'trusted installation ownership record', True)
        self.notes.extend(['Account and home retained; no securely recorded installer-created account deletion is offered.',
                           'Unrelated source clones, native agent data, package dependencies and shared tunnels are retained.',
                           'System journal/archive copies outside the owned paths are retained; apply host retention policy manually.'])
        self.check_jobs()
        self.check_trees()
        return self

    def check_jobs(self):
        self.blockers = []
        inventory = process_inventory(self.account.pw_uid) if self.account else {'runners': [], 'groups': set()}
        for state in self.states:
            jobs = safe_path(state/'jobs')
            if not exists(jobs):
                continue
            for path in jobs.glob('*.json'):
                if path.name.endswith(('.process.json', '.output.json', '.outcome.json')):
                    continue
                safe_path(path)
                try:
                    if not path.is_file() or path.stat().st_size > 1048576:
                        raise ValueError('Invalid metadata file type or size')
                    meta = json.loads(path.read_text())
                    if not isinstance(meta, dict):
                        raise ValueError('Invalid job metadata shape')
                    if meta.get('status') not in ('done', 'failed', 'killed'):
                        self.blockers.append(f'Unresolved job {path.name}: {meta.get("status", "unknown")}; inspect/reconcile it before removal.')
                    process = jobs/'.sidecars'/(path.stem+'.process.json')
                    if not exists(process):
                        process = jobs/(path.stem+'.process.json')
                    if exists(process):
                        safe_path(process)
                        identity = json.loads(process.read_text()).get('identity') or {}
                        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
                        if identity.get('boot_id') == boot and identity.get('pgid') in inventory['groups']:
                            self.blockers.append(f'Job process group remains active for {path.name}; no processes will be killed.')
                except (OSError, ValueError, TypeError, AttributeError):
                    self.blockers.append(f'Unreadable job metadata: {path}')
        if self.account:
            for pid in inventory['runners']:
                self.blockers.append(f'Active job runner PID {pid}; finish or explicitly manage its application job first.')

    def check_trees(self):
        mounts = []
        for line in Path('/proc/self/mountinfo').read_text().splitlines():
            mounts.append(Path(re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), line.split()[4])))
        for target in self.targets:
            path = Path(target['path'])
            if any(mount == path or mount.is_relative_to(path) for mount in mounts):
                raise Refusal(f'Mounted subtree in removal target: {path}')
            if path.is_dir():
                for directory, dirs, files in os.walk(path, followlinks=False):
                    for name in dirs + files:
                        child = Path(directory)/name
                        if not child.is_symlink() and child.lstat().st_dev != target['identity'][0]:
                            raise Refusal(f'Cross-device subtree refused: {child}')

    def manifest(self):
        return {'user': self.account.pw_name if self.account else None,
                'uid': self.account.pw_uid if self.account else None,
                'home_retained': self.account.pw_dir if self.account else None,
                'targets': sorted(self.targets, key=lambda row: row['path']),
                'activation_links': sorted(self.links, key=lambda row: row['path']),
                'remove_if_empty': [str(self.layout.pool), str(self.layout.registry.parent)],
                'blockers': self.blockers, 'notes': self.notes,
                'service_action': 'stop and disable ai-bridge.service' if self.had_unit else 'none'}

    def digest(self):
        return hashlib.sha256(json.dumps(self.manifest(), sort_keys=True).encode()).hexdigest()

    def execute(self):
        if self.blockers:
            raise Refusal('Unresolved jobs block removal. No force-kill override is provided.')
        if not shutil.rmtree.avoids_symlink_attacks:
            raise Refusal('This platform lacks symlink-resistant directory removal.')
        fresh = Plan(self.layout, self.lookup, self.systemctl).build(self.account.pw_name if self.account else None)
        if fresh.digest() != self.digest():
            raise Refusal('Installation changed after preview; rerun dry-run.')
        self.check_jobs()
        self.check_trees()
        if self.blockers:
            raise Refusal('Job state changed; removal refused.')
        # Verify every root before stopping the service or deleting anything.
        for row in self.targets + self.links:
            with parent_fd(Path(row['path'])) as fd:
                if fingerprint_at(fd, Path(row['path']).name) != row['identity']:
                    raise Refusal('Removal target changed after preview; rerun dry-run.')
        if self.had_unit:
            try:
                self.systemctl('stop', 'ai-bridge.service')
                if self.systemctl('show', 'ai-bridge.service', '--property=MainPID', '--value') not in ('', '0'):
                    raise Refusal('Service is still running.')
                self.check_jobs()
                if self.blockers:
                    raise Refusal('Job appeared while stopping; inspect locally.')
                self.systemctl('disable', 'ai-bridge.service')
                for row in self.links:
                    if exists(Path(row['path'])):
                        raise Refusal('Activation link remains after disable; inspect locally.')
            except (OSError, subprocess.CalledProcessError, Refusal) as error:
                raise Refusal(f'Service transition incomplete; files retained, service may be stopped: {error}') from error
        removed = []
        try:
            for row in sorted(self.targets, key=lambda item: (item['path'] == str(self.layout.registry),
                                                              item['path'] == str(self.layout.unit),
                                                              item['path'] == str(self.layout.etc),
                                                              -len(Path(item['path']).parts))):
                path = Path(row['path'])
                with parent_fd(path) as fd:
                    if fingerprint_at(fd, path.name) != row['identity']:
                        raise Refusal(f'Target changed after preview: {path}')
                    if stat.S_ISDIR(row['identity'][4]):
                        shutil.rmtree(path.name, dir_fd=fd)
                    else:
                        os.unlink(path.name, dir_fd=fd)
                removed.append(str(path))
                print(f'Removed: {path}')
            if self.had_unit:
                self.systemctl('daemon-reload')
        except (OSError, subprocess.CalledProcessError, Refusal) as error:
            raise Refusal(f'PARTIAL REMOVAL: {len(removed)} targets removed. Service may be stopped. Rerun dry-run and inspect locally. {error}') from error
        # Only empty managed containers are removed; other users' copies stay intact.
        for parent in (self.layout.pool, self.layout.registry.parent):
            if parent.exists():
                try:
                    parent.rmdir()
                except OSError:
                    pass


@contextmanager
def parent_fd(path):
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = following
        yield fd
    finally:
        os.close(fd)


def fingerprint_at(fd, name):
    value = os.stat(name, dir_fd=fd, follow_symlinks=False)
    return [value.st_dev, value.st_ino, value.st_uid, stat.S_IMODE(value.st_mode), stat.S_IFMT(value.st_mode),
            value.st_mtime_ns if stat.S_ISREG(value.st_mode) else None, value.st_size if stat.S_ISREG(value.st_mode) else None]


def process_inventory(uid):
    # Inspect identity/group data once. No PID is ever used as a kill target.
    result = {'runners': [], 'groups': set()}
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            matches_user = proc.stat().st_uid == uid
            fields = (proc/'stat').read_text().rsplit(') ', 1)[1].split()
            if fields[0] == 'Z':
                continue
            result['groups'].add(int(fields[2]))
            if matches_user and b'ai_bridge.job_runner' in (proc/'cmdline').read_bytes().split(b'\0'):
                result['runners'].append(proc.name)
        except (FileNotFoundError, ProcessLookupError):
            pass
        except (OSError, ValueError, IndexError) as error:
            raise Refusal(f'Cannot safely inspect process {proc.name}; no removal attempted.') from error
    return result


def inside_bridge():
    return 'ai-bridge.service' in Path('/proc/self/cgroup').read_text() or any(key.startswith('AI_BRIDGE_') for key in os.environ)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Full AI Bridge removal from a trusted host terminal. Preview first; account/home and shared tunnels are retained.')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--purge', action='store_true', help='Deprecated alias: full removal always includes state/config.')
    parser.add_argument('--user', help='Explicit installed account, required for legacy remnants without a unit/registry')
    parser.add_argument('--yes', action='store_true')
    parser.add_argument('--confirm')
    parser.add_argument('--manifest-sha256')
    parser.add_argument('--record-install', metavar='USER', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        raise Refusal('Root is required to inspect and remove system-owned artifacts; use bash uninstall.sh.')
    layout = Layout()
    if args.record_install:
        record_install(layout, pwd.getpwnam(args.record_install))
        return 0
    plan = Plan(layout).build(args.user)
    print(json.dumps(plan.manifest(), indent=2))
    print('Manifest SHA256: '+plan.digest())
    print('WARNING: stopping the bridge disconnects all MCP clients. Use a trusted host terminal, never a bridge tool.')
    print('Remote tunnel cleanup is manual; remove only the bridge route, never reset shared tunnel configuration.')
    if args.dry_run:
        return 2 if plan.blockers else 0
    if not plan.targets:
        return 0
    if inside_bridge():
        raise Refusal('Refusing removal from the bridge service/environment. Use a trusted host terminal.')
    phrase = 'UNINSTALL AI-BRIDGE for '+plan.account.pw_name
    if args.yes:
        if args.confirm != phrase or args.manifest_sha256 != plan.digest():
            raise Refusal('Automation requires --yes, exact --confirm phrase and current --manifest-sha256 from dry-run.')
    else:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise Refusal('Noninteractive removal refused. Preview from a trusted host terminal; deliberate automation needs all confirmation flags.')
        if input(f'Type {phrase!r} to permanently delete these targets: ') != phrase:
            raise Refusal('Confirmation did not match; nothing removed.')
    plan.execute()
    print('AI Bridge removal complete. Retained account/home, native agents, unrelated source clones and shared tunnels.')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, OSError, KeyError, EOFError, KeyboardInterrupt, subprocess.CalledProcessError) as error:
        print(f'ERROR: {error}', file=sys.stderr)
        sys.exit(1)

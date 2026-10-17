"""Small SSH adapter: offline aliases, bounded commands, exclusive file commits."""
from contextlib import contextmanager
import fnmatch
import glob
import ipaddress
import itertools
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import stat
import tempfile
import time

from .execution import execute

ALIAS = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z')
OPTIONS = ['BatchMode=yes', 'StrictHostKeyChecking=yes', 'ForwardAgent=no',
           'ForwardX11=no', 'ClearAllForwardings=yes', 'PermitLocalCommand=no',
           'RequestTTY=no', 'CanonicalizeHostname=no', 'UpdateHostKeys=no',
           'CheckHostIP=no', 'ControlMaster=no', 'ControlPath=none',
           'PasswordAuthentication=no', 'KbdInteractiveAuthentication=no',
           'ConnectTimeout=10', 'ConnectionAttempts=1', 'LogLevel=ERROR']
STATIC = set('hostname user port identityfile identityagent identitiesonly proxyjump '
             'userknownhostsfile globalknownhostsfile hostkeyalias certificatefile '
             'hostkeyalgorithms pubkeyacceptedalgorithms preferredauthentications '
             'compression addressfamily bindaddress serveraliveinterval serveralivecountmax'.split())


def clean(value, label):
    if not isinstance(value, str) or not value or len(value) > 4096 or any(ord(c) < 32 or ord(c) == 127 or 0xd800 <= ord(c) <= 0xdfff for c in value):
        raise ValueError(f'{label} must be nonempty text without control/undecodable characters.')
    return value


def alias_name(value):
    clean(value, 'Host alias')
    if not ALIAS.fullmatch(value):
        raise ValueError('Use a literal configured SSH alias, not a destination or option.')
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return value
    raise ValueError('Use a named configured alias rather than an IP address.')


class Configuration:
    def __init__(self, home=None, system=None):
        self.home = Path.home() if home is None else Path(home)
        self.system = Path('/etc/ssh/ssh_config') if system is None else Path(system)
        self.aliases = {}
        self.lines = []
        self.private = []
        self.skipped_match = False
        self.total = 0
        self.files = 0
        self.match = False
        self.patterns = ['*']
        self.unsupported = []
        for path, origin, base in ((self.home/'.ssh/config', 'user', self.home/'.ssh'),
                                   (self.system, 'system', self.system.parent)):
            self.match, self.patterns = False, ['*']
            self.lines.append('Host *')
            self.read(path, origin, base, ())

    def read(self, path, origin, base, stack):
        if not path.exists():
            return
        resolved = path.resolve()
        if resolved in stack or len(stack) >= 8 or self.files >= 64:
            raise ValueError('SSH Include cycle or discovery limit; review configuration locally.')
        if not resolved.is_file() or resolved.stat().st_size > 262144:
            raise ValueError('SSH config must be a bounded regular text file.')
        fd = os.open(resolved, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd) as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.geteuid()) or info.st_mode & 0o022:
                raise ValueError('SSH config ownership/mode is unsafe; review it locally.')
            text = stream.read(262145)
        if len(text) > 262144:
            raise ValueError('SSH config exceeded its per-file budget.')
        self.total += len(text)
        self.files += 1
        if self.total > 2097152:
            raise ValueError('SSH configuration discovery exceeded its byte budget.')
        self.private.extend((str(path), str(resolved)))
        for line in text.splitlines():
            lexer = shlex.shlex(line, posix=True); lexer.whitespace_split = True; lexer.commenters = '#'
            words = list(lexer)
            if not words:
                continue
            key = words.pop(0)
            if '=' in key:
                key, value = key.split('=', 1); words.insert(0, value)
            elif words and words[0] == '=':
                words.pop(0)
            key = key.lower()
            if key == 'match':
                self.match = True; self.skipped_match = True
                patterns = words[1].split(',') if len(words) == 2 and words[0].lower() == 'host' else ['*']
                self.unsupported.append(patterns)
                continue
            if key == 'host':
                self.match = False; self.patterns = words
                self.lines.append('Host '+ ' '.join(words))
                for word in words:
                    try:
                        alias_name(word)
                    except ValueError:
                        continue
                    self.aliases.setdefault(word, origin)
                    if len(self.aliases) > 2000:
                        raise ValueError('SSH alias discovery exceeded its record budget.')
            elif self.match:
                continue
            elif key == 'include':
                if self.patterns != ['*']:
                    self.skipped_match = True
                    self.unsupported.append(self.patterns.copy())
                    continue
                for word in words:
                    if '$' in word or '%' in word:
                        raise ValueError('Use static Include paths; environment/token expansion is unsupported.')
                    expanded = Path(word).expanduser()
                    if not expanded.is_absolute():
                        expanded = base/expanded
                    for included in sorted(itertools.islice(glob.iglob(str(expanded)), 65)):
                        self.read(Path(included), origin, base, stack+(resolved,))
            elif key in STATIC:
                # Preserve original quoting; never include executable Match predicates.
                self.lines.append(line)
            elif key in ('proxycommand', 'localcommand', 'remotecommand'):
                self.unsupported.append(self.patterns.copy())

    def require(self, host):
        alias_name(host)
        if host not in self.aliases:
            raise ValueError('Host is not a discovered literal SSH alias.')
        for patterns in self.unsupported:
            positives = [p for p in patterns if not p.startswith('!')]
            if any(fnmatch.fnmatchcase(host, p) for p in positives) and not any(fnmatch.fnmatchcase(host, p[1:]) for p in patterns if p.startswith('!')):
                raise ValueError('Alias uses unsupported executable/conditional config directives; use static settings/ProxyJump or native SSH deliberately.')

    @contextmanager
    def snapshot(self):
        with tempfile.TemporaryDirectory(prefix='aib-ssh-') as directory:
            path = Path(directory)/'config'
            # First-value semantics also apply to any native jump connections.
            path.write_text('Host *\n'+''.join('  '+v.replace('=', ' ', 1)+'\n' for v in OPTIONS)+'\n'+'\n'.join(self.lines)+'\n')
            path.chmod(0o600)
            yield path


def binaries():
    return {name: shutil.which(name) for name in ('ssh', 'rsync', 'sftp', 'scp')}


def hosts(limit=30, offset=0, configuration=None):
    config = configuration or Configuration()
    rows = [{'alias': key, 'source': source} for key, source in sorted(config.aliases.items())]
    limit, offset = max(1, min(int(limit), 100)), max(0, int(offset))
    return {'hosts': rows[offset:offset+limit], 'total': len(rows), 'offset': offset,
            'next_offset': offset+limit if offset+limit < len(rows) else None,
            'connection_attempted': False, 'match_blocks_skipped': config.skipped_match,
            'local_clients': {key: bool(value) for key,value in binaries().items()},
            'note': 'Static aliases only. No addresses, users, identity paths or config bodies are returned; Match-only/wildcard destinations are not discovered.'}


class Session:
    def __init__(self, cfg, host, timeout, configuration=None):
        self.cfg, self.host = cfg, alias_name(host)
        self.config = configuration or Configuration()
        self.config.require(host)
        self.clients = binaries()
        if not self.clients['ssh']:
            raise ValueError('SSH client is unavailable locally.')
        self.deadline = time.monotonic()+max(1, min(int(timeout), 600))
        self.private = list(self.config.private)

    def argv(self, path):
        return [self.clients['ssh'], '-F', str(path)]+[item for value in OPTIONS for item in ('-o', value)]

    def call(self, argv, stdin=b'', on_output=None):
        remaining = self.deadline-time.monotonic()
        if remaining <= 0:
            raise TimeoutError('SSH operation exceeded its total timeout.')
        result = execute(self.cfg, argv, str(Path.home()), remaining, stdin_data=stdin, on_output=on_output)
        # Local transport identity is separate from remote privilege.
        result.pop('privilege', None); result.pop('privilege_message', None)
        return result

    def inspect(self, path):
        result = self.call(self.argv(path)+['-G', self.host])
        if result['exit_code'] != 0 or result['stdout_truncated'] or result['timed_out']:
            raise ValueError('Offline SSH option inspection failed; review config locally.')
        for line in result['stdout'].splitlines():
            key, _, value = line.partition(' ')
            if key == 'proxyjump' and value != 'none' and self.config.unsupported:
                raise ValueError('Jump routing may depend on unsupported conditional/executable config; review natively.')
            if key in ('hostname','user','identityfile','identityagent','certificatefile','proxyjump','hostkeyalias','userknownhostsfile','globalknownhostsfile'):
                values = [v for v in value.split() if v and v != self.host and v not in ('none','~')]
                self.private.extend(values)
                if key in ('identityfile','certificatefile','userknownhostsfile','globalknownhostsfile'):
                    self.private.append(str(Path(value).expanduser()))
        return result

    def remote(self, path, command, on_output=None):
        return self.call(self.argv(path)+['-T', self.host, command], on_output=on_output)

    def scrub(self, result):
        def redact(value):
            if isinstance(value, str):
                for secret in sorted(set(self.private), key=len, reverse=True):
                    value = value.replace(secret, '[ssh-private]')
                value = re.sub(r'(?<![A-Za-z0-9])(?:[A-Fa-f0-9]{0,4}:){2,}[A-Fa-f0-9:]+', '[address]', value)
                value = re.sub(r'(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])', '[address]', value)
                return value
            if isinstance(value, dict):
                return {key:redact(item) for key,item in value.items()}
            if isinstance(value, list):
                return [redact(item) for item in value]
            return value
        # Do not redact an explicitly requested alias or pretend a local sudo was remote.
        result = redact(result)
        result.update(host=self.host, local_sudo_used=False, local_service_euid=os.geteuid())
        return result


def run(cfg, host, command, timeout=120, remote_sudo=False, configuration=None):
    if not isinstance(command, str) or not command or len(command) > 65536 or '\0' in command or any(0xd800 <= ord(c) <= 0xdfff for c in command):
        raise ValueError('Command must be valid nonempty text, at most 65536 characters.')
    session = Session(cfg, host, timeout, configuration)
    nonce = secrets.token_hex(20)
    markers = {'attempt': 'AIB_'+nonce+'_ATTEMPT', 'root': 'AIB_'+nonce+'_ROOT'}
    seen = set(); pending = bytearray()
    def observe(name, data):
        if name != 'stderr':
            return
        pending.extend(data)
        for key, value in markers.items():
            if value.encode() in pending:
                seen.add(key)
        del pending[:-256]
    actual = 'sh -c '+shlex.quote(command)
    if remote_sudo:
        inner = 'if [ "$(/usr/bin/id -u)" = 0 ]; then printf "%s\\n" '+shlex.quote(markers['root'])+' >&2; fi; exec '+actual
        actual = 'printf "%s\\n" '+shlex.quote(markers['attempt'])+' >&2; exec sudo -n -- sh -c '+shlex.quote(inner)
    with session.config.snapshot() as path:
        session.inspect(path)
        result = session.remote(path, actual, observe)
    for value in markers.values():
        result['stderr'] = result['stderr'].replace(value+'\n', '').replace(value, '')
    outcome = 'timed_out' if result['timed_out'] else 'succeeded' if result['exit_code'] == 0 else 'failed'
    result.update(method='ssh', status=outcome,
                  remote_privilege={'sudo_requested':remote_sudo, 'sudo_attempted': (True if 'attempt' in seen else None) if remote_sudo else False,
                                    'sudo_confirmed': 'root' in seen, 'confirmation': 'remote root-shell receipt' if 'root' in seen else 'unconfirmed',
                                    'outcome':outcome},
                  remote_privilege_message=('Remote root shell confirmed through sudo; inspect command outcome separately.' if 'root' in seen else
                                            'Remote sudo requested, but root execution was not confirmed.' if remote_sudo else 'No remote sudo requested.'),
                  note='Local SSH transport only; no bridge sudo password is forwarded. Remote identity receipts trust the authenticated host.')
    return session.scrub(result)


def remote_path(value):
    clean(value, 'Remote path')
    if any(c in value for c in '*?[]{}'):
        raise ValueError('Wildcard/bracket path syntax is unsupported; use a literal regular filename.')
    if not value.startswith('/') or '//' in value or value.endswith('/') or any(part in ('.','..') for part in value.split('/')[1:]) or value == '/':
        raise ValueError('Use an exact absolute remote file path, without trailing slash or dot components.')
    return value


def sftp_quote(value):
    return '"'+value.replace('\\', '\\\\').replace('"', '\\"')+'"'


def transfer(cfg, host, direction, local_path, remote_file, method='auto', timeout=120, dry_run=False, configuration=None):
    if direction not in ('upload', 'download') or method not in ('auto','rsync','sftp','scp'):
        raise ValueError('Use upload/download and auto, rsync, sftp or scp.')
    clean(local_path, 'Local path'); remote_path(remote_file)
    if any(c in local_path for c in '*?[]{}'):
        raise ValueError('Wildcard/bracket local paths are unsupported.')
    local = Path(local_path).expanduser()
    if not local.is_absolute():
        raise ValueError('Local paths must be absolute.')
    if any(part in ('.','..') for part in local_path.split('/')) or any(path.is_symlink() for path in (local, *local.parents)):
        raise ValueError('Local path must not traverse symlinks or dot components.')
    if direction == 'upload':
        if not stat.S_ISREG(local.lstat().st_mode):
            raise ValueError('Uploads require a regular file, not a symlink/directory.')
    elif local.exists() or local.is_symlink() or not local.parent.is_dir():
        raise ValueError('Download needs an absent exact destination and an existing parent directory.')
    session = Session(cfg, host, timeout, configuration)
    candidates = [name for name in ('rsync','sftp','scp') if session.clients[name]]
    if method != 'auto' and method not in candidates:
        raise ValueError('Requested transfer client is unavailable locally.')
    if not candidates:
        raise ValueError('No supported transfer client is installed.')
    base = {'host':host, 'direction':direction, 'requested_method':method, 'method':None,
            'remote_sudo_requested':False, 'local_sudo_used':False, 'overwrite':False,
            'bytes':None, 'checksum':None, 'committed':False}
    if dry_run:
        return dict(base, status='dry_run', connection_attempted=False, candidates=candidates,
                    note='No connection or transfer. Auto method and remote capability remain unverified; only regular files are supported.')
    with session.config.snapshot() as path:
        session.inspect(path)
        chosen = method
        if method == 'auto':
            chosen = next((name for name in candidates if name != 'rsync'), None)
            if 'rsync' in candidates:
                probe = session.remote(path, 'command -v rsync >/dev/null 2>&1')
                if probe['exit_code'] == 0:
                    chosen = 'rsync'
                elif probe['exit_code'] != 1 or probe['timed_out']:
                    return session.scrub(dict(base, **probe, status='failed', phase='capability_probe'))
            if chosen is None:
                raise ValueError('Remote rsync is unavailable and no local SFTP/SCP fallback exists.')
        base['method'] = chosen
        response = None
        with tempfile.TemporaryDirectory(prefix='.aib-transfer-', dir=local.parent if direction == 'download' else None) as temporary:
            payload = Path(temporary)/'payload'
            stage = remote_file+'.aib-'+secrets.token_hex(12)
            target = stage+'/payload' if direction == 'upload' else remote_file
            if direction == 'download':
                checked = session.remote(path, 'test -f '+shlex.quote(remote_file)+' && test ! -L '+shlex.quote(remote_file))
                if checked['exit_code'] != 0 or checked['timed_out']:
                    return session.scrub(dict(base, **checked, status='failed', phase='remote_file_check'))
            if direction == 'upload':
                prepared = session.remote(path, 'umask 077; mkdir -- '+shlex.quote(stage))
                if prepared['exit_code'] != 0:
                    return session.scrub(dict(base, **prepared, status='failed', phase='prepare', staging_cleanup='unconfirmed', staging_path=stage))
            try:
                source = str(local) if direction == 'upload' else target
                dest = target if direction == 'upload' else str(payload)
                transport = shlex.join(session.argv(path))
                if chosen == 'rsync':
                    operand = host+':'+(dest if direction == 'upload' else source)
                    argv = [session.clients['rsync'], '--protect-args', '--', source, operand] if direction == 'upload' else [session.clients['rsync'], '--protect-args', '--', operand, dest]
                    argv[1:1] = ['-e', transport]
                    result = session.call(argv)
                elif chosen == 'sftp':
                    batch = ('put ' if direction == 'upload' else 'get ')+sftp_quote(source)+' '+sftp_quote(dest)+'\n'
                    result = session.call([session.clients['sftp'], '-S', session.clients['ssh'], '-F', str(path), '-b', '-', host], batch.encode())
                else:
                    # Legacy SCP paths are shell-quoted; modern SFTP handles raw operands.
                    # Force legacy explicitly so path interpretation is deterministic.
                    operand = host+':'+shlex.quote(dest if direction == 'upload' else source)
                    argv = [session.clients['scp'], '-O', '-S', session.clients['ssh'], '-F', str(path), '--']
                    argv += [source, operand] if direction == 'upload' else [operand, dest]
                    result = session.call(argv)
                if result['exit_code'] != 0 or result['timed_out']:
                    response = session.scrub(dict(base, **result, status='failed', phase='transfer', note='No automatic retry after transfer begins.'))
                    return response
                if direction == 'upload':
                    result = session.remote(path, 'test -f '+shlex.quote(target)+' && test ! -L '+shlex.quote(target)+' && ln -T -- '+shlex.quote(target)+' '+shlex.quote(remote_file))
                    committed = True if result['exit_code'] == 0 and not result['timed_out'] else None
                else:
                    if not stat.S_ISREG(payload.lstat().st_mode):
                        raise ValueError('Downloaded payload is not a regular file.')
                    os.link(payload, local, follow_symlinks=False)
                    committed = True
                response = session.scrub(dict(base, **result, status='succeeded' if committed else 'unconfirmed', committed=committed,
                                          note='Exclusive regular-file commit; no existing destination is replaced. A lost commit reply is ambiguous: inspect before retrying. Bytes/checksum are not independently verified.'))
                return response
            except (OSError, ValueError) as error:
                response = session.scrub(dict(base, status='timed_out' if isinstance(error, TimeoutError) else 'failed',
                    error='Transfer/commit could not complete; inspect destination and staging before retrying.',
                    error_type=type(error).__name__, stdout='', stderr='', exit_code=None,
                    timed_out=isinstance(error, TimeoutError), phase='transfer_or_commit',
                    committed=None if direction == 'upload' else False))
                return response
            finally:
                if direction == 'upload':
                    # Clean only this operation's private staging payload; never the destination.
                    try:
                        cleanup = session.remote(path, 'rm -f -- '+shlex.quote(stage+'/payload')+'; rmdir -- '+shlex.quote(stage))
                        cleaned = 'completed' if cleanup['exit_code'] == 0 and not cleanup['timed_out'] else 'unconfirmed'
                    except (OSError, ValueError):
                        cleaned = 'unconfirmed'
                    if response is not None:
                        response['staging_cleanup'] = cleaned
                        if cleaned != 'completed':
                            response['staging_path'] = stage

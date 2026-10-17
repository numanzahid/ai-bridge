"""Explicit per-account launcher installation and conservative ownership validation."""
import grp
import hashlib
import json
import os
from pathlib import Path
import pwd
import stat
import sys
import tempfile


def private_primary_group(uid, gid):
    """Accept only an enumerated primary group with no other account members."""
    try:
        account = pwd.getpwuid(uid)
        group = grp.getgrgid(gid)
        accounts, groups = pwd.getpwall(), grp.getgrall()
        if account.pw_uid != uid or account.pw_gid != gid or group.gr_gid != gid:
            return False
        if uid == os.geteuid() and (os.getgid() != gid or os.getegid() != gid):
            return False
        if not any(row.pw_name == account.pw_name and row.pw_uid == uid and row.pw_gid == gid for row in accounts):
            return False
        matching = [row for row in groups if row.gr_gid == gid]
        if not matching or group not in matching:
            return False
        return (all(row.pw_name == account.pw_name and row.pw_uid == uid
                    for row in accounts if row.pw_gid == gid) and
                all(name == account.pw_name for row in [group, *matching] for name in row.gr_mem))
    except (KeyError, OSError, RuntimeError):
        return False


def checked(path, uid, directory=False, allow_private_group=False):
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError(f'Symlinked launcher installation path refused: {part}')
    info = path.stat()
    writable = info.st_mode & 0o022
    if (allow_private_group and directory and info.st_uid == uid and
            stat.S_IMODE(info.st_mode) == 0o775 and private_primary_group(uid, info.st_gid) and
            not {'system.posix_acl_access', 'system.posix_acl_default'}.intersection(
                os.listxattr(path, follow_symlinks=False))):
        writable = False
    if (info.st_uid != uid or writable or
            not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))):
        raise ValueError(f'Unsafe launcher ownership, permissions or file type: {path}')
    if not directory and info.st_nlink != 1:
        raise ValueError('Hard-linked launcher metadata/file refused.')
    return info


def layout(home):
    home = Path(home).absolute()
    if '..' in home.parts or home == Path('/'):
        raise ValueError('Unsafe account home.')
    return home / '.local/bin/ai-bridge', home / '.local/share/ai-bridge/.cli-launcher.json'


def registered_launcher(home, uid):
    target, record = layout(home)
    if not record.exists() and not record.is_symlink():
        return None
    checked(record, uid)
    if record.stat().st_mode & 0o077 or record.stat().st_size > 8192:
        raise ValueError('Launcher ownership record must be private and bounded.')
    value = json.loads(record.read_text())
    if (value.get('schema'), value.get('uid'), value.get('path')) != (1, uid, str(target)):
        raise ValueError('Launcher ownership record does not match this account/path.')
    if not target.exists() and not target.is_symlink():
        return None
    checked(target.parent, uid, True, allow_private_group=True)
    checked(target, uid)
    if not target.stat().st_mode & 0o100:
        raise ValueError('Registered launcher is no longer executable; inspect locally.')
    if target.stat().st_size > 16384 or hashlib.sha256(target.read_bytes()).hexdigest() != value.get('sha256'):
        raise ValueError('Installed launcher differs from its ownership record; manual review required.')
    return target


def install(home, uid, source):
    if uid != os.geteuid():
        raise ValueError('Run --install-cli as the installed service account, not on its behalf.')
    home = Path(home).absolute()
    target, record = layout(home)
    checked(home, uid, True, allow_private_group=True)
    root = record.parent
    for path in (home / '.local', home / '.local/share', root):
        checked(path, uid, True, allow_private_group=True)
    checked(root / 'releases', uid, True)
    current = root / 'current'
    if not current.is_symlink() or current.lstat().st_uid not in (uid, 0):
        raise ValueError('Select a verified versioned release before installing the CLI.')
    release = current.resolve(strict=True)
    if release.parent != root / 'releases':
        raise ValueError('Current release is outside the owned release directory.')
    for path in (release, release / 'app', release / 'app/ai_bridge', release / 'venv', release / 'venv/bin'):
        checked(path, uid, True)
    checked(release / 'READY', uid)
    checked(release / 'app/ai_bridge/cli.py', uid)
    if not os.access(release / 'venv/bin/python', os.X_OK):
        raise ValueError('Selected release has no executable environment.')
    source = Path(source)
    if source.is_symlink() or not source.is_file() or source.stat().st_size > 16384:
        raise ValueError('Launcher source must be a bounded regular file.')
    data = source.read_bytes()
    old = registered_launcher(home, uid)
    if old:
        if old.read_bytes() != data:
            raise ValueError('Owned launcher differs from this source. No update performed; review migration/rollback manually.')
        return old
    if target.exists() or target.is_symlink():
        raise ValueError('Refusing to overwrite a foreign ai-bridge executable.')
    if record.exists() or record.is_symlink():
        raise ValueError('An ownership record already exists without its launcher; inspect before reinstalling.')
    if not target.parent.exists():
        target.parent.mkdir(mode=0o755)
        target.parent.chmod(0o755)
    checked(target.parent, uid, True, allow_private_group=True)
    # Exclusive publication never replaces an existing command or ownership file.
    fd, temporary = tempfile.mkstemp(prefix='.ai-bridge-cli-', dir=target.parent)
    linked = False
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fchmod(stream.fileno(), 0o755)
            os.fsync(stream.fileno())
        os.link(temporary, target, follow_symlinks=False)
        linked = True
        payload = {'schema': 1, 'uid': uid, 'path': str(target), 'sha256': hashlib.sha256(data).hexdigest()}
        record_fd = os.open(record, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(record_fd, 'w') as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(payload, stream)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        if linked and target.lstat().st_ino == os.stat(temporary).st_ino:
            target.unlink()
        raise
    finally:
        os.unlink(temporary)
    return target


def main():
    if len(sys.argv) != 1:
        raise ValueError('--install-cli accepts no extra arguments.')
    account = pwd.getpwuid(os.geteuid())
    if Path.home() != Path(account.pw_dir):
        raise ValueError('HOME differs from passwd home. Run as the service account with its normal HOME.')
    target = install(Path(account.pw_dir), account.pw_uid, Path(__file__).with_name('ai-bridge'))
    print(f'CLI installed/verified: {target}\nEnsure {target.parent} is on PATH. No release/service was changed.')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError) as error:
        print(f'ERROR: {error}', file=sys.stderr)
        raise SystemExit(1)

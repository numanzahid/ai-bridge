"""Root-only interactive bootstrap. Never imported by the running bridge."""
import argparse
import getpass
import os
from pathlib import Path
import pwd
import re
import shutil
import subprocess
import sys
import tempfile


def validate_user(account):
    if account.pw_uid < 1000 or account.pw_name == 'root':
        raise ValueError('Refusing to reuse a privileged or system account.')
    home = Path(account.pw_dir)
    if not home.is_absolute() or home == Path('/') or not home.is_dir():
        raise ValueError('The service account needs an existing absolute home directory.')
    for path in (home, *home.parents):
        if path.is_symlink():
            raise ValueError('Symlinked home paths are not supported by bootstrap.')
    if home.stat().st_uid != account.pw_uid:
        raise ValueError('The service account must own its home directory.')


def installation_matches(home, user, etc=Path('/etc/ai-bridge'), unit=Path('/etc/systemd/system/ai-bridge.service')):
    installed = (home/'.local/share/ai-bridge/app').is_dir() or (home/'.local/share/ai-bridge/current').is_symlink()
    if etc.exists() or unit.exists():
        if not installed or not unit.is_file() or f'User={user}' not in unit.read_text().splitlines():
            raise ValueError('An existing installation belongs to another identity or is incomplete. Use its owner and explicit stage/activate workflow; bootstrap will not overwrite it.')
    return installed


def copy_source(source, parent, account):
    # Copy only public source inputs; never copy credentials, native history, or VCS state.
    allowed = ('ai_bridge', 'tests', 'docs', 'requirements.txt', 'requirements.lock',
               'install.sh', 'bootstrap.py', 'deploy.py', 'merge_env.py', 'README.md', 'SECURITY.md', 'LICENSE')
    if any(path.is_symlink() for path in (parent, *parent.parents)):
        raise ValueError('Symlinked bootstrap copy parent is not supported.')
    parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    if parent.is_symlink() or parent.stat().st_uid != 0 or parent.stat().st_mode & 0o022:
        raise ValueError('Bootstrap copy parent must be root-owned and not writable by other users.')
    # A restrictive root umask must not prevent the selected user traversing the pool.
    parent.chmod(0o755)
    destination = Path(tempfile.mkdtemp(prefix=account.pw_name+'-', dir=parent))
    def reject_links(directory, entries):
        for entry in entries:
            if (Path(directory)/entry).is_symlink():
                raise ValueError('Source symlinks are not accepted during root bootstrap.')
        return [entry for entry in entries if entry in ('__pycache__', '.pytest_cache') or entry.endswith('.pyc')]
    for name in allowed:
        src = source/name
        if src.is_symlink():
            raise ValueError('Source symlinks are not accepted during root bootstrap.')
        if src.is_dir():
            shutil.copytree(src, destination/name, ignore=reject_links)
        elif src.is_file():
            shutil.copyfile(src, destination/name)
    # Ownership changes are confined to this newly created, initially root-private copy.
    for path in sorted(destination.rglob('*'), reverse=True):
        os.chown(path, account.pw_uid, account.pw_gid, follow_symlinks=False)
    os.chown(destination, account.pw_uid, account.pw_gid)
    return destination


def configure_sudo(account, created, policy_root=Path('/etc/sudoers.d')):
    for command in ('sudo', 'visudo', 'runuser'):
        if not shutil.which(command):
            raise ValueError(f'{command} is required for the dedicated-user path. Install it first; nothing will be run as root instead.')
    if created:
        password = getpass.getpass(f'New login/sudo password for {account.pw_name}: ')
        if not password or any(char in password for char in ('\n', '\r', '\0')) or password != getpass.getpass('Confirm password: '):
            raise ValueError('Passwords must be nonempty and match.')
        subprocess.run(['chpasswd'], input=f'{account.pw_name}:{password}\n', text=True, check=True)
        password = None
    print('Ordinary commands and agents run unprivileged. Password-authenticated sudo permits full root administration.')
    if input(f'Grant {account.pw_name} password-authenticated sudo for all commands? [y/N] ').strip().lower() != 'y':
        raise ValueError('Dedicated-user sudo setup declined. No installation was started.')
    target = policy_root/('ai-bridge-'+account.pw_name)
    policy = f'{account.pw_name} ALL=(root) PASSWD: ALL\n'
    if target.exists():
        if target.is_symlink() or target.read_text() != policy or target.stat().st_uid != 0 or target.stat().st_mode & 0o777 != 0o440:
            raise ValueError('Existing sudo policy differs or has unsafe ownership/mode; review it manually.')
        return
    fd, temporary = tempfile.mkstemp(prefix='.ai-bridge-', dir=target.parent)
    try:
        os.fchmod(fd, 0o440)
        with os.fdopen(fd, 'w') as stream:
            stream.write(policy)
        subprocess.run(['visudo', '-cf', temporary], check=True)
        # Never overwrite a policy created concurrently.
        os.link(temporary, target)
    finally:
        os.unlink(temporary)
    print('Policy installed. The installer will ask for this user password to authenticate and optionally store a root-only systemd credential.')


def prerequisites():
    missing_sudo = not shutil.which('sudo') or not shutil.which('visudo')
    if missing_sudo:
        manager = next((name for name in ('apt-get', 'dnf') if shutil.which(name)), None)
        if not manager or input('Password-authenticated administration needs sudo. Install the sudo package now? [y/N] ').strip().lower() != 'y':
            raise ValueError('Install sudo first; bootstrap will not fall back to root.')
        if manager == 'apt-get':
            subprocess.run([manager, 'update'], check=True)
        subprocess.run([manager, 'install', '-y', 'sudo'], check=True)
    for command in ('runuser', 'sudo', 'visudo', 'useradd', 'chpasswd'):
        if not shutil.which(command):
            raise ValueError(f'{command} is required. Install it first; bootstrap will not fall back to root.')


def dedicated(source, name):
    if not re.fullmatch(r'[a-z_][a-z0-9_-]{0,30}', name):
        raise ValueError('Use a simple local account name (letters, digits, underscore, hyphen).')
    prerequisites()
    try:
        account = pwd.getpwnam(name)
        created = False
    except KeyError:
        # Refuse conflicts before making a new account.
        installation_matches(Path('/home')/name, name)
        if (Path('/home')/name).exists():
            raise ValueError('The prospective home already exists; prepare and review the account manually.')
        subprocess.run(['useradd', '--create-home', '--user-group', '--shell', '/bin/bash', name], check=True)
        account, created = pwd.getpwnam(name), True
    validate_user(account)
    print(f'Service identity: {account.pw_name} (UID {account.pw_uid}), HOME {account.pw_dir}.')
    existing = installation_matches(Path(account.pw_dir), name)
    if not existing:
        configure_sudo(account, created)
    destination = copy_source(source, Path('/var/lib/ai-bridge-bootstrap'), account)
    print(f'Using source copy: {destination}', flush=True)
    env = {'HOME': account.pw_dir, 'USER': name, 'LOGNAME': name,
           'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'}
    for key in ('TERM', 'LANG', 'AI_BRIDGE_PORT'):
        if key in os.environ:
            env[key] = os.environ[key]
    os.chdir(destination)
    # Keep the terminal attached for sudo authentication; preserve the child exit status.
    os.execvpe('runuser', ['runuser', '-u', name, '--', 'env', '-i',
                         *[f'{key}={value}' for key, value in env.items()],
                         '/bin/bash', str(destination/'install.sh')], env)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--dedicated-user', default='ai-bridge')
    args = parser.parse_args()
    if os.geteuid() != 0 or not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ValueError('Dedicated-user bootstrap requires root and an interactive terminal. For explicit unattended root installation use --run-as-root; prepare dedicated accounts interactively.')
    dedicated(args.source.resolve(), args.dedicated_user)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, EOFError, KeyboardInterrupt, subprocess.CalledProcessError) as error:
        print(f'ERROR: {error}', file=sys.stderr)
        sys.exit(1)

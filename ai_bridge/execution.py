"""Bounded Linux process execution and a separate sudo authentication channel."""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
import json
import os
from pathlib import Path
import selectors
import shlex
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time


try:
    BOOT_ID = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
except OSError:
    BOOT_ID = None


def signal_verified(pid: int, identity: dict | None, sig):
    """Pin the process before the final identity check; never signal a reused PID."""
    if not identity or not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal'):
        raise ValueError('Safe cancellation requires verified identity and Linux pidfd support; inspect locally.')
    try:
        fd = os.pidfd_open(pid)
    except ProcessLookupError:
        return
    try:
        if not identity_matches(pid, identity):
            raise ValueError('Process identity changed; cancellation refused.')
        signal.pidfd_send_signal(fd, sig)
    finally:
        os.close(fd)


def process_identity(pid: int) -> dict | None:
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(') ', 1)[1].split()
        if fields[0] == 'Z':
            return None
        return {'boot_id': BOOT_ID,
                'start_ticks': fields[19], 'pgid': int(fields[2])}
    except (OSError, ValueError, IndexError):
        return None


def identity_matches(pid: int, identity: dict | None) -> bool:
    return bool(identity and process_identity(pid) == identity)


def group_alive(pgid: int) -> bool:
    # Check members rather than the leader; zombies do not retain output pipes.
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry/'stat').read_text().rsplit(') ',1)[1].split()
            if fields[0] != 'Z' and int(fields[2]) == pgid:
                return True
        except (OSError, ValueError, IndexError):
            continue
    return False


@contextmanager
def sudo_command(cfg, argv: list[str], env: dict):
    """Never put authentication bytes on target stdin, including NOPASSWD policy.

    sudo -A uses a private, same-UID UNIX socket via a tiny askpass helper. No
    password is stored on disk, in argv, or in the child's environment.
    """
    from .core import SudoUnavailable
    if os.geteuid() == 0:
        yield argv, env
        return
    sudo = shutil.which('sudo')
    if not sudo:
        raise SudoUnavailable('sudo is not installed on this machine.')
    if not cfg.sudo_password:
        yield [sudo, '-n', '--'] + argv, env
        return
    with tempfile.TemporaryDirectory(prefix='aib-auth-') as directory:
        os.chmod(directory, 0o700)
        sockpath = str(Path(directory)/'askpass.sock')
        helper = Path(directory)/'askpass'
        # The helper contains only a socket path, never credentials.
        helper.write_text('#!/bin/sh\nexec '+shlex.quote(sys.executable)+' -c '+shlex.quote(
            'import socket,sys; s=socket.socket(socket.AF_UNIX); s.connect(sys.argv[1]); '
            'b=b""\nwhile True:\n c=s.recv(4096)\n if not c: break\n b+=c\nsys.stdout.buffer.write(b)')+' '+shlex.quote(sockpath)+'\n')
        helper.chmod(0o700)
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(sockpath)
        os.chmod(sockpath, 0o600)
        listener.listen(4)
        listener.settimeout(.1)
        stop = threading.Event()
        def serve():
            while not stop.is_set():
                try:
                    conn, _ = listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    return
                with conn:
                    conn.settimeout(.5)
                    try:
                        _, uid, _ = struct.unpack('3i', conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                        if uid in (os.getuid(), 0):
                            conn.sendall((cfg.sudo_password+'\n').encode())
                    except OSError:
                        pass
        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            yield [sudo, '-k', '-A', '-p', '', '--'] + argv, dict(env, SUDO_ASKPASS=str(helper))
        finally:
            stop.set()
            listener.close()
            thread.join(timeout=1)


def terminate_group(pid: int, grace: float = .5) -> dict:
    """Bounded best-effort cleanup. Privilege failures are explicitly reported."""
    denied = False
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            break
        except PermissionError:
            denied = True
        deadline = time.monotonic()+grace
        while time.monotonic() < deadline:
            if not group_alive(pid):
                return {'complete': True, 'permission_denied': denied}
            time.sleep(.025)
    return {'complete': not group_alive(pid), 'permission_denied': denied,
            'scope': 'original process group; detached descendants are not guaranteed'}


class Capture:
    """Keep a bounded head and tail while continuing to drain a pipe."""
    def __init__(self, limit: int):
        self.limit = max(256, limit)
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0

    def add(self, data: bytes):
        self.total += len(data)
        count = min(len(data), self.limit//2-len(self.head))
        self.head.extend(data[:count])
        self.tail.extend(data[count:])
        if len(self.tail) > self.limit-len(self.head):
            del self.tail[:len(self.tail)-(self.limit-len(self.head))]

    def value(self) -> bytes:
        return bytes(self.head+self.tail)


def execute(cfg, argv: list[str], cwd: str, timeout: float | None, sudo=False,
            stdin_data=b'', on_output=None, cancel=None, on_start=None, on_privilege=None) -> dict:
    from .core import child_env
    started = time.monotonic()
    captures = {'stdout': Capture(cfg.capture_bytes), 'stderr': Capture(cfg.capture_bytes)}
    from .privilege import initial
    privilege = initial(sudo)
    disclose = sudo or os.geteuid() == 0
    needs_sudo = sudo and os.geteuid() != 0
    with (tempfile.TemporaryDirectory(prefix='aib-execution-') if needs_sudo else nullcontext(None)) as directory:
        receipt = Path(directory)/'receipt' if directory else None
        if receipt:
            receipt.touch(mode=0o600)
        wrapped = argv
        if sudo and os.geteuid() != 0:
            wrapped = [sys.executable, str(Path(__file__).with_name('privilege_exec.py')), str(receipt)] + argv
        manager = sudo_command(cfg, wrapped, child_env()) if sudo else _plain(argv, child_env())
        with manager as (command, env):
            return _execute(command, env, cwd, timeout, stdin_data, on_output, cancel,
                            on_start, on_privilege, privilege, disclose, receipt, started, captures)


def _execute(command, env, cwd, timeout, stdin_data, on_output, cancel,
             on_start, on_privilege, privilege, disclose, receipt, started, captures):
    from .privilege import fields
    cleanup = None
    timed_out = cancelled = False
    def report():
        if privilege['sudo_attempted']:
            try:
                privilege.update(json.loads(receipt.read_text()))
                privilege['sudo_used'] = bool(privilege['target_started'])
            except (OSError, ValueError):
                pass
        if disclose and on_privilege:
            on_privilege(privilege.copy())
    try:
        proc = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        privilege['sudo_attempted'] = privilege['method'] == 'sudo'
        if not privilege['sudo_attempted']:
            privilege.update(target_started=True, effective_uid=os.geteuid(), effective_root=os.geteuid() == 0)
        report()
        if on_start:
            on_start(proc.pid, process_identity(proc.pid))
        selector = selectors.DefaultSelector()
        for name in captures:
            stream = getattr(proc, name)
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, name)
        if stdin_data:
            os.set_blocking(proc.stdin.fileno(), False)
            selector.register(proc.stdin, selectors.EVENT_WRITE, 'stdin')
        else:
            proc.stdin.close()
        pending = memoryview(stdin_data)
        stop_deadline = None
        try:
            while selector.get_map():
                if not privilege['target_started']:
                    report()
                now = time.monotonic()
                if stop_deadline is None and ((timeout is not None and now-started >= timeout) or (cancel and cancel())):
                    timed_out = timeout is not None and now-started >= timeout
                    cancelled = not timed_out
                    cleanup = terminate_group(proc.pid)
                    stop_deadline = time.monotonic()+.3
                if stop_deadline is not None and now >= stop_deadline:
                    break
                for key, _ in selector.select(.05):
                    stream, name = key.fileobj, key.data
                    if name == 'stdin':
                        try:
                            count = os.write(stream.fileno(), pending[:65536])
                            pending = pending[count:]
                        except (BrokenPipeError, OSError):
                            pending = pending[:0]
                        if not pending:
                            selector.unregister(stream)
                            stream.close()
                        continue
                    try:
                        data = os.read(stream.fileno(), 65536)
                    except BlockingIOError:
                        continue
                    if not data:
                        selector.unregister(stream)
                        stream.close()
                    else:
                        captures[name].add(data)
                        if on_output:
                            on_output(name, data)
            # Pipes may close while the process keeps running.
            while proc.poll() is None and stop_deadline is None:
                if not privilege['target_started']:
                    report()
                if (timeout is not None and time.monotonic()-started >= timeout) or (cancel and cancel()):
                    timed_out = timeout is not None and time.monotonic()-started >= timeout
                    cancelled = not timed_out
                    cleanup = terminate_group(proc.pid)
                    break
                time.sleep(.025)
        finally:
            selector.close()
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if not stream.closed:
                    stream.close()
            try:
                proc.wait(timeout=.2)
            except subprocess.TimeoutExpired:
                pass
    except OSError as error:
        report()
        error.privilege = privilege.copy()
        raise
    report()
    result = {'exit_code': proc.poll(), 'timed_out': timed_out, 'cancelled': cancelled,
              'duration_s': round(time.monotonic()-started, 2)}
    for name, capture in captures.items():
        result[name] = capture.value().decode(errors='replace')
        result[name+'_bytes'] = capture.total
        result[name+'_truncated'] = capture.total > len(capture.value())
    if cleanup is not None:
        if privilege['sudo_attempted']:
            cleanup['original_group_complete'] = cleanup['complete']
            cleanup['complete'] = False
            cleanup['privileged_descendants'] = 'unverified; inspect locally before retrying'
        result['cleanup'] = cleanup
    if disclose:
        result.update(fields(privilege))
    return result


@contextmanager
def _plain(argv, env):
    yield argv, env


def probe(argv: list[str], timeout=15) -> dict:
    """Bounded, secret-filtered CLI discovery; never an agent/model turn."""
    from types import SimpleNamespace
    return execute(SimpleNamespace(capture_bytes=32768), argv, os.getcwd(), timeout)

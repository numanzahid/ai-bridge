"""Core helpers: config, secret redaction, process execution, background jobs."""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import tempfile
import time
import sys
import threading
import contextvars
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass
class Config:
    token: str
    host: str = "127.0.0.1"
    port: int = 8765
    allowed_hosts: list[str] = field(default_factory=list)
    allowed_origins: list[str] = field(default_factory=list)
    default_cwd: str = str(Path.home())
    state_dir: Path = Path.home() / ".local/state/ai-bridge"
    max_output_chars: int = 30000
    agent_default_permission: str = "edit"
    sudo_password: str | None = None
    capture_bytes: int = 262144
    job_log_bytes: int = 4194304
    retention_days: int = 0
    audit_bytes: int = 2097152
    tasks_dir: Path = Path.home() / ".ai-bridge-tasks"

    @classmethod
    def load(cls) -> "Config":
        token = _env("AI_BRIDGE_TOKEN")
        if len(token) < 32:
            raise SystemExit(
                "AI_BRIDGE_TOKEN is missing or shorter than 32 chars. "
                "Generate one with: python3 -c 'import secrets;print(secrets.token_hex(32))'"
            )
        state_dir = Path(_env("AI_BRIDGE_STATE_DIR") or Path.home() / ".local/state/ai-bridge")
        state_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(state_dir, 0o700)
        (state_dir / "jobs").mkdir(mode=0o700, exist_ok=True)
        os.chmod(state_dir / "jobs", 0o700)

        return cls(
            token=token,
            host=_env("AI_BRIDGE_HOST", "127.0.0.1"),
            port=int(_env("AI_BRIDGE_PORT", "8765")),
            allowed_hosts=[h.strip() for h in _env("AI_BRIDGE_ALLOWED_HOSTS").split(",") if h.strip()],
            allowed_origins=[h.strip() for h in _env("AI_BRIDGE_ALLOWED_ORIGINS").split(",") if h.strip()],
            tasks_dir=Path(_env("AI_BRIDGE_TASKS_DIR") or Path.home() / ".ai-bridge-tasks"),
            default_cwd=_env("AI_BRIDGE_DEFAULT_CWD") or str(Path.home()),
            state_dir=state_dir,
            max_output_chars=int(_env("AI_BRIDGE_MAX_OUTPUT_CHARS", "30000")),
            agent_default_permission=_env("AI_BRIDGE_AGENT_DEFAULT_PERMISSION", "edit"),
            sudo_password=_load_sudo_password(),
            capture_bytes=max(4096, int(_env("AI_BRIDGE_CAPTURE_BYTES", "262144"))),
            job_log_bytes=max(8192, int(_env("AI_BRIDGE_JOB_LOG_BYTES", "4194304"))),
            retention_days=max(0, int(_env("AI_BRIDGE_RETENTION_DAYS", "0"))),
            audit_bytes=max(4096, int(_env("AI_BRIDGE_AUDIT_BYTES", "2097152"))),
        )


def _load_sudo_password() -> str | None:
    """Sudo password comes from a file, never from a tool argument.

    Order: AI_BRIDGE_SUDO_PASSWORD_FILE, then the systemd credential
    'sudo_pass' (LoadCredential=), then nothing (sudo must be NOPASSWD or
    sudo tools will fail cleanly).
    """
    candidates = []
    if _env("AI_BRIDGE_SUDO_PASSWORD_FILE"):
        candidates.append(Path(_env("AI_BRIDGE_SUDO_PASSWORD_FILE")))
    if _env("CREDENTIALS_DIRECTORY"):
        candidates.append(Path(_env("CREDENTIALS_DIRECTORY")) / "sudo_pass")
    for p in candidates:
        try:
            pw = p.read_text().rstrip("\r\n")
            if pw:
                return pw
        except OSError:
            continue
    return None


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\r(?!\n)")


class Redactor:
    """Removes secrets (and common encodings of them) from any text we return."""

    def __init__(self, secrets_: list[str]):
        forms: set[str] = set()
        for s in secrets_:
            if not s or len(s) < 4:
                continue
            b = s.encode()
            forms.update(
                {
                    s,
                    base64.b64encode(b).decode(),
                    base64.b64encode(b).decode().rstrip("="),
                    base64.urlsafe_b64encode(b).decode().rstrip("="),
                    binascii.hexlify(b).decode(),
                    binascii.hexlify(b).decode().upper(),
                    urllib.parse.quote(s, safe=""),
                    s[::-1],
                }
            )
        # Tool results and audit arguments are often serialized before redaction.
        # Passwords containing quotes, backslashes, or Unicode need escaped forms too.
        for value in list(forms):
            forms.add(json.dumps(value, ensure_ascii=True)[1:-1])
            forms.add(json.dumps(value, ensure_ascii=False)[1:-1])
        forms = {f for f in forms if len(f) >= 4}
        self._re = (
            re.compile("|".join(re.escape(f) for f in sorted(forms, key=len, reverse=True)))
            if forms
            else None
        )

    def __call__(self, text: str) -> str:
        if not text:
            return text
        text = ANSI_RE.sub("", text)
        if self._re is not None:
            text = self._re.sub("[REDACTED]", text)
        return text


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = limit // 3
    tail = limit - head
    cut = len(text) - limit
    return f"{text[:head]}\n\n... [{cut} chars truncated] ...\n\n{text[-tail:]}"


# ---------------------------------------------------------------------------
# Process execution
# ---------------------------------------------------------------------------


def child_env() -> dict[str, str]:
    """Environment for spawned processes, minus our own secrets."""
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("AI_BRIDGE_") and k not in ("CREDENTIALS_DIRECTORY",)
    }
    env.setdefault("HOME", str(Path.home()))
    env["TERM"] = "dumb"
    env["NO_COLOR"] = "1"
    env["DEBIAN_FRONTEND"] = "noninteractive"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["PAGER"] = "cat"
    env["SYSTEMD_PAGER"] = ""
    return env


class SudoUnavailable(RuntimeError):
    pass


def resolve_cwd(cfg: Config, cwd: str | None) -> str:
    path = Path(os.path.expanduser(cwd or cfg.default_cwd))
    if not path.is_dir():
        raise FileNotFoundError(f"Working directory does not exist: {path}")
    return str(path)


def kill_group(pid: int, grace: float = 4.0) -> dict:
    from .execution import terminate_group
    return terminate_group(pid, min(grace, 1.0))


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # Reap if it is our zombie child
    try:
        done, _ = os.waitpid(pid, os.WNOHANG)
        if done == pid:
            return False
    except ChildProcessError:
        pass
    try:
        with open(f"/proc/{pid}/stat") as f:
            if f.read().split(") ", 1)[1].startswith("Z"):
                return False
    except OSError:
        pass
    return True


def run_sync(
    cfg: Config,
    command: str,
    cwd: str | None,
    timeout: int,
    sudo: bool,
    stdin_data: bytes = b"",
) -> dict:
    from .execution import execute
    return execute(cfg, ["bash", "-lc", command], resolve_cwd(cfg, cwd), timeout, sudo, stdin_data)


# ---------------------------------------------------------------------------
# Background jobs (survive MCP request timeouts and server restarts)
# ---------------------------------------------------------------------------

class Jobs:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.dir = cfg.state_dir / "jobs"
        self._children = {}

    def _meta_path(self, job_id: str) -> Path:
        if not re.fullmatch(r"[a-z0-9-]{4,40}", job_id):
            raise ValueError(f"Bad job id: {job_id}")
        return self.dir / f"{job_id}.json"

    def log_path(self, job_id: str) -> Path:
        return self.dir / f"{job_id}.log"

    def exit_path(self, job_id: str) -> Path:
        return self.dir / f"{job_id}.exit"

    def save(self, meta: dict) -> None:
        path = self._meta_path(meta["id"])
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".tmp-")
        with os.fdopen(fd, "w") as f:
            json.dump(meta, f, indent=1)
        os.replace(tmp, path)

    def load(self, job_id: str) -> dict:
        path = self._meta_path(job_id)
        if not path.exists():
            raise KeyError(f"No such job: {job_id}")
        meta = json.loads(path.read_text())
        return self._refresh(meta)

    def _refresh(self, meta: dict) -> dict:
        from .execution import identity_matches
        child = self._children.get(meta.get('id'))
        if child and child.poll() is not None:
            self._children.pop(meta['id'], None)
        from .job_paths import readable_sidecar
        process = readable_sidecar(self.dir, meta['id'], 'process')
        if (meta.get('status') == 'running' or 'privilege' not in meta) and process.exists():
            try:
                state = json.loads(process.read_text())
                if 'privilege' in state:
                    meta['privilege'] = state['privilege']
            except (OSError, ValueError):
                pass
        if meta.get("status") != "running":
            return meta
        outcome = readable_sidecar(self.dir, meta['id'], 'outcome')
        ef = self.exit_path(meta['id'])
        if outcome.exists():
            result = json.loads(outcome.read_text())
            meta['exit_code'] = result.get('exit_code')
            meta['ended'] = result.get('ended')
            meta['cleanup'] = result.get('cleanup')
            meta['timed_out'] = result.get('timed_out', False)
            if 'privilege' in result:
                meta['privilege'] = result['privilege']
            if result.get('cleanup', {}).get('complete') is False:
                meta['status'] = 'cleanup_incomplete'
            elif result.get('cancelled'):
                meta['status'] = 'killed'
            else:
                meta['status'] = 'done' if meta['exit_code'] == 0 else 'failed'
            self.save(meta)
        elif ef.exists():
            try:
                meta['exit_code'] = int(ef.read_text().strip() or '-1')
            except ValueError:
                meta['exit_code'] = -1
            meta.update(status='done' if meta['exit_code'] == 0 else 'failed', ended=ef.stat().st_mtime)
            self.save(meta)
        elif meta.get('identity'):
            if not identity_matches(meta['pid'], meta['identity']):
                # Completion markers are written before the runner exits. A dead
                # runner without one is interrupted, never automatically replayed.
                from .execution import BOOT_ID
                state = 'previous_boot' if meta['identity'].get('boot_id') != BOOT_ID else 'not_running_or_changed'
                meta.update(status='lost', ended=time.time(), identity_state=state)
                self.save(meta)
        else:
            # Old records are readable but cannot safely be signalled or presumed
            # completed based on a possibly reused PID. Keep them busy conservatively.
            meta['identity_state'] = 'unverified_legacy'
        return meta

    def start(self, argv: list[str], cwd: str, sudo: bool = False,
              kind: str = 'command', display: str = '', extra: dict | None = None) -> dict:
        from .execution import process_identity
        from .job_paths import sidecar_path
        self.prune()
        job_id = time.strftime('%m%d-%H%M%S-')+secrets.token_hex(6)
        ready_path = sidecar_path(self.dir, job_id, 'process', create=True)
        outcome_path = sidecar_path(self.dir, job_id, 'outcome')
        # The detached runner owns pipe draining and credential lifetime, so a
        # bridge restart does not interrupt output handling or authentication.
        env = child_env()
        env['PYTHONPATH'] = str(Path(__file__).resolve().parents[1])
        proc = subprocess.Popen([sys.executable, '-m', 'ai_bridge.job_runner'], cwd=cwd, env=env,
                                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True)
        meta = {'id': job_id, 'kind': kind, 'display': display[:200], 'cwd': cwd,
                'sudo': sudo, 'pid': proc.pid, 'identity': process_identity(proc.pid),
                'runner': True, 'status': 'running', 'started': time.time(),
                'ended': None, 'exit_code': None}
        meta.update({k: v for k, v in (extra or {}).items() if k != 'prompt'})
        self.save(meta)
        payload = {'id': job_id, 'root': str(self.dir), 'argv': argv, 'cwd': cwd,
                   'sudo': sudo, 'password': self.cfg.sudo_password if sudo else None,
                   'log_bytes': self.cfg.job_log_bytes, 'agent': meta.get('agent'),
                   'timeout': meta.get('timeout')}
        import selectors
        pending = memoryview(json.dumps(payload).encode())
        os.set_blocking(proc.stdin.fileno(), False)
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(proc.stdin, selectors.EVENT_WRITE)
                deadline = time.monotonic()+5
                while pending:
                    if time.monotonic() > deadline:
                        raise TimeoutError('Job runner did not accept its startup request.')
                    if selector.select(.1):
                        count = os.write(proc.stdin.fileno(), pending[:65536])
                        pending = pending[count:]
            proc.stdin.close()
            self._children[job_id] = proc
            deadline = time.monotonic()+5
            while not ready_path.exists() and not outcome_path.exists():
                if proc.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError('Job runner failed to become ready.')
                time.sleep(.01)
        except BaseException:
            proc.terminate()
            proc.wait(timeout=2)
            meta.update(status='failed', ended=time.time(), exit_code=1)
            self.save(meta)
            raise
        return self.load(job_id)

    def wait(self, job_id: str, wait_seconds: float) -> dict:
        deadline = time.time() + max(0.0, wait_seconds)
        meta = self.load(job_id)
        while meta["status"] == "running" and time.time() < deadline:
            time.sleep(1.0)
            meta = self.load(job_id)
        return meta

    def tail(self, job_id: str, lines: int = 80) -> str:
        path = self.log_path(job_id)
        if not path.exists():
            return ""
        size = path.stat().st_size
        with open(path, "rb") as f:
            f.seek(max(0, size - 400_000))
            data = f.read().decode(errors="replace")
        return "\n".join(data.splitlines()[-lines:])

    def output_info(self, job_id: str) -> dict:
        self._meta_path(job_id)
        from .job_paths import readable_sidecar
        path = readable_sidecar(self.dir, job_id, 'output')
        if path.exists():
            return json.loads(path.read_text())
        legacy = self.log_path(job_id)
        if legacy.exists():
            size = legacy.stat().st_size
            return {'total_bytes': size, 'retained_bytes': min(size,self.cfg.job_log_bytes),
                    'truncated': size>self.cfg.job_log_bytes, 'legacy': True}
        return {}

    def read_log(self, job_id: str) -> str:
        self._meta_path(job_id)
        path = self.log_path(job_id)
        if not path.exists():
            return ''
        with path.open('rb') as stream:
            size = path.stat().st_size
            half = self.cfg.job_log_bytes//2
            head = stream.read(half)
            stream.seek(max(len(head), size-half))
            tail = stream.read(half)
        return (head+tail).decode(errors='replace')

    def log_chunk(self, job_id: str, offset: int = 0, max_bytes: int = 12000,
                  generation: str | None = None) -> dict:
        self._meta_path(job_id)
        info = self.output_info(job_id)
        path = self.log_path(job_id)
        offset, max_bytes = max(0, offset), max(1, min(max_bytes, 65536))
        with path.open('rb') as stream:
            snapshot = os.fstat(stream.fileno())
            current = f'{snapshot.st_ino}:{snapshot.st_mtime_ns}:{snapshot.st_size}'
            if generation is not None and current != generation:
                return {'error': 'Log snapshot changed; restart at offset 0.', 'generation': current}
            size = snapshot.st_size
            stream.seek(offset)
            data = stream.read(max_bytes)
        return {'job_id': job_id, 'offset_bytes': offset, 'next_offset_bytes': offset+len(data) if offset+len(data) < size else None,
                'text': data.decode(errors='replace'), 'generation': current,
                'truncated': info.get('truncated', False), 'retained_bytes': size,
                'note': 'Offsets refer to retained head/tail bytes; the omitted middle is unavailable.'}

    def kill(self, job_id: str) -> dict:
        from .execution import identity_matches, signal_verified
        meta = self.load(job_id)
        if meta['status'] == 'running':
            if not identity_matches(meta['pid'], meta.get('identity')):
                raise ValueError('Cannot safely cancel: process identity is missing or changed. Inspect locally.')
            if meta.get('runner'):
                signal_verified(meta['pid'], meta['identity'], signal.SIGTERM)
                meta = self.wait(job_id, 3)
                if meta['status'] == 'running':
                    meta['cancel_requested'] = True
            else:
                meta['cleanup'] = kill_group(meta['pid'])
                meta['status'] = 'killed' if meta['cleanup']['complete'] else 'cleanup_incomplete'
                meta['ended'] = time.time()
                self.save(meta)
        return meta

    def list(self, limit: int | None = 30, kind: str | None = None) -> list[dict]:
        metas = []
        candidates = []
        for path in self.dir.glob('*.json'):
            try:
                candidates.append((path.stat().st_mtime, path))
            except OSError:
                continue
        for _, p in sorted(candidates, reverse=True):
            if p.stem.endswith(('.output', '.outcome', '.process')):
                continue
            try:
                m = self._refresh(json.loads(p.read_text()))
            except (OSError, ValueError, KeyError):
                continue
            if kind and m.get('kind') != kind:
                continue
            metas.append(m)
            if limit is not None and len(metas) >= limit:
                break
        return metas

    def running_for_session(self, agent: str, session_id: str) -> dict | None:
        for m in self.list(limit=None, kind='agent'):
            uncertain = (m['status'] in ('running', 'cleanup_incomplete') or
                         (m['status'] == 'lost' and m.get('identity_state') != 'previous_boot'))
            if not uncertain or m.get('agent') != agent:
                continue
            observed = m.get('session_id')
            if observed is None:
                observed = (self.output_info(m['id']).get('parsed') or {}).get('session_id')
            if observed == session_id:
                return m
        return None

    def prune(self):
        # 0 disables retention. Never delete running, legacy uncertain, or
        # incomplete-cleanup records. Only exact job artifact names are removed.
        if not self.cfg.retention_days:
            return
        cutoff = time.time()-self.cfg.retention_days*86400
        for meta in self.list(limit=None):
            if meta.get('status') not in ('done', 'failed', 'killed') or not meta.get('ended') or meta['ended'] >= cutoff:
                continue
            from .job_paths import KINDS, sidecar_path
            for kind in KINDS:
                sidecar_path(self.dir, meta['id'], kind).unlink(missing_ok=True)
            # Include the superseded candidate layout only for this finalized job.
            for suffix in ('.json', '.log', '.exit', '.output.json', '.outcome.json', '.process.json'):
                path = self.dir/(meta['id']+suffix)
                self._meta_path(meta['id'])
                path.unlink(missing_ok=True)


OPERATION_ID = contextvars.ContextVar('ai_bridge_operation', default=None)
AUDIT_LOCK = threading.Lock()


def audit(cfg: Config, redact: Redactor, tool: str, args: dict, result: str = '', phase='result', **details) -> None:
    try:
        from .responses import render
        # Redact before limiting fields, including audit args: never truncate a
        # configured credential first and leave its partial prefix behind.
        line = render({'t': time.strftime('%Y-%m-%dT%H:%M:%S'), 'tool': tool,
                       'operation_id': OPERATION_ID.get(), 'phase': phase,
                       'args': args, 'result': redact(result)[:300], **details}, min(8192,cfg.audit_bytes//5), redact)
        line = json.dumps(json.loads(line), ensure_ascii=False)+'\n'
        with AUDIT_LOCK:
            path = cfg.state_dir/'audit.log'
            if path.exists() and path.stat().st_size+len(line.encode()) > cfg.audit_bytes:
                if path.is_symlink():
                    raise OSError('Audit log must not be a symlink.')
                os.chmod(path,0o600)
                for number in range(2,0,-1):
                    old = cfg.state_dir/('audit.log.'+str(number))
                    if old.exists():
                        os.replace(old, cfg.state_dir/('audit.log.'+str(number+1)))
                os.replace(path, cfg.state_dir/'audit.log.1')
            descriptor = os.open(path, os.O_WRONLY|os.O_CREAT|os.O_APPEND|os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, 'a', encoding='utf-8') as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(line)
    except OSError:
        # Execution availability remains independent of audit storage. A later
        # status check reports audit writability; do not leak exception contents.
        pass

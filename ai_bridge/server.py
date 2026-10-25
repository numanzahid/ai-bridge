"""AI Bridge: a remote MCP server that lets Claude or ChatGPT operate this machine.

Tools: shell (with optional sudo), background jobs, file read/write, and a
bridge to local coding agents (Claude Code, Codex, Cursor CLI).
"""

from __future__ import annotations

import hmac
import json
import os
import platform
import shutil
import socket
import time
import threading
import sys
import functools
import secrets
from pathlib import Path
from typing import Literal

import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from . import __author__, __description__, __title__, __url__, __version__
from .optional_agents import agents as ag
from .agent_failures import describe_failure
from . import catalog
from .host_config import HostConfig
from .core import (
    Config,
    Jobs,
    Redactor,
    SudoUnavailable,
    audit,
    resolve_cwd,
    run_sync,
    OPERATION_ID,
)

from .approvals import Approvals
from .privilege import (CLIENT_INSTRUCTIONS, HOST_TARGET_INSTRUCTIONS,
                        fields as privilege_fields, initial as privilege_initial, job_fields)

CFG = Config.load()
REDACT = Redactor([CFG.token, CFG.sudo_totp_secret, CFG.sudo_totp_secret.upper()] +
                  ([CFG.sudo_password] if CFG.sudo_password else []))
JOBS = Jobs(CFG)
APPROVALS = Approvals(CFG.state_dir, CFG.approval_dir,
                      log=lambda event, **fields: audit(CFG, REDACT, event, {}, phase='approval', **fields),
                      totp_secret=CFG.sudo_totp_secret)
AGENT_START_LOCK = threading.Lock()
WAIT_SLOTS = threading.BoundedSemaphore(8)

HOST_CONFIG = HostConfig()
INSTRUCTIONS = REDACT(HOST_TARGET_INSTRUCTIONS + "\n" + HOST_CONFIG.instructions + "\n" + CLIENT_INSTRUCTIONS)

def _icons():
    """Bundled icons as data URIs, so clients need no public, token-free URL to show them."""
    import base64
    from mcp.types import Icon
    icons = []
    for size in (128, 48):
        path = Path(__file__).with_name('assets') / f'icon-{size}.png'
        if path.is_file():
            data = base64.b64encode(path.read_bytes()).decode()
            icons.append(Icon(src=f'data:image/png;base64,{data}', mime_type='image/png', sizes=[f'{size}x{size}']))
    return icons or None


mcp = MCPServer(name="ai-bridge", title=f"{__title__} ({socket.gethostname()})", version=__version__,
                description=f"{__description__} By {__author__}.", website_url=__url__, icons=_icons(),
                instructions=INSTRUCTIONS)

RO = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
RW = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True)
MANAGE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)


def bridge_tool(**options):
    def register(fn):
        @functools.wraps(fn)
        def traced(*args, **kwargs):
            operation = OPERATION_ID.get() or secrets.token_hex(12)
            token = OPERATION_ID.set(operation)
            started = time.monotonic()
            # Do not duplicate prompts, file contents, credentials or argv here.
            audit(CFG, REDACT, fn.__name__, {}, phase='start')
            try:
                result = fn(*args, **kwargs)
                try:
                    data = json.loads(result)
                except (ValueError, TypeError):
                    data = {}
                summary = {k: data[k] for k in ('job_id','exit_code','status','outcome') if isinstance(data,dict) and k in data}
                audit(CFG, REDACT, fn.__name__, {}, phase='finish', **summary,
                      duration_s=round(time.monotonic()-started,3))
                return result
            except (OSError, ValueError, KeyError) as error:
                payload = {'error': str(error), 'outcome': 'unavailable', 'automatic_retry': False}
                if fn.__name__ in ('run_command', 'start_job', 'read_file', 'write_file'):
                    import inspect
                    requested = inspect.signature(fn).bind_partial(*args, **kwargs).arguments.get('sudo', False)
                    if requested or os.geteuid() == 0:
                        payload.update(privilege_fields(getattr(error, 'privilege', privilege_initial(requested))))
                result = _out(fn.__name__, {}, payload)
                audit(CFG, REDACT, fn.__name__, {}, phase='finish', exception=True,
                      duration_s=round(time.monotonic()-started,3))
                return result
            except Exception:
                audit(CFG, REDACT, fn.__name__, {}, phase='finish', exception=True,
                      duration_s=round(time.monotonic()-started,3))
                raise
            finally:
                OPERATION_ID.reset(token)
        return HOST_CONFIG.tool(mcp, REDACT, **options)(traced)
    return register


def _out(tool: str, args: dict, payload: dict | str) -> str:
    from .responses import render
    text = render(payload, CFG.max_output_chars, REDACT)
    audit(CFG, REDACT, tool, args, text)
    return text


def _approval(tool: str, args: dict, requested: bool) -> dict | None:
    """The response that replaces a privileged call until verified TOTP or host approval.

    A root service is already root, so there is nothing for an approval to guard.
    Errors fail closed: the call does not run.
    """
    if not requested or not CFG.sudo_approval or os.geteuid() == 0:
        return None
    try:
        return APPROVALS.gate(tool, args)
    except OSError as error:
        return {'status': 'approval_unavailable', 'automatic_retry': False,
                'error': f'Approval storage is unavailable, so the privileged call did not run: {error}'}


@bridge_tool(annotations=MANAGE)
def approve_privileged_request(request_id: str, code: str) -> str:
    """Verify a user-supplied authenticator code for one pending administrator operation.

    Use after approval_required when the user chooses the authenticator option by
    sending a current code for the displayed host, operation and request_id. Keep
    all six digits as a string. Never generate, guess or echo codes. This records
    authorization only; it executes no command. After status approved, repeat the
    exact original tool call. Host CLI approval is an equal alternative: if the user
    says they approved there, repeat the original call directly without this tool
    or another code. This verifier grants once only, not a task window.
    """
    if not CFG.sudo_approval or os.geteuid() == 0:
        payload = {'status': 'approval_not_required', 'automatic_retry': False}
    else:
        payload = APPROVALS.approve_remote(request_id, code)
    # Codes must never enter audit arguments, tool results, or error messages.
    return _out('approve_privileged_request', {}, payload)


def _fmt_time(ts: float | None) -> str | None:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else None


def _wait_job(job_id: str, seconds: int):
    # The SDK already dispatches sync tools to workers. Do not let polling
    # consume every worker needed for shell/status/cancellation requests.
    acquired = WAIT_SLOTS.acquire(blocking=False) if seconds > 0 else False
    try:
        if seconds > 0 and not acquired:
            return dict(JOBS.load(job_id), wait_deferred=True)
        return JOBS.wait(job_id, seconds)
    finally:
        if acquired:
            WAIT_SLOTS.release()


def _job_view(meta: dict, tail_lines: int = 60) -> dict:
    view = {
        "task_id": meta.get("task_id"),
        "workspace_type": meta.get("workspace_type"),
        "job_id": meta["id"],
        "status": meta["status"],
        "exit_code": meta.get("exit_code"),
        "command": meta.get("display"),
        "cwd": meta.get("cwd"),
        "sudo": meta.get("sudo"),
        "cleanup": meta.get("cleanup"),
        "identity_state": meta.get("identity_state"),
        "wait_deferred": meta.get("wait_deferred", False),
        "started": _fmt_time(meta.get("started")),
        "ended": _fmt_time(meta.get("ended")),
        "runtime_s": round((meta.get("ended") or time.time()) - meta["started"], 1),
    }
    view.update(job_fields(meta))
    if tail_lines:
        view["output_tail"] = JOBS.tail(meta["id"], tail_lines)
    return view


# ---------------------------------------------------------------------------
# Shell and files
# ---------------------------------------------------------------------------


def _catalog_page(tool: str, roots: list[Path], key: str, limit: int, offset: int, skills: bool = False) -> str:
    limit, offset = max(1, min(limit, 100)), max(0, offset)
    rows = catalog.discover(roots, skills=skills)
    page = rows[offset:offset + limit]
    payload = {"directories": [str(p) for p in roots], key: page, "total": len(rows),
               "offset": offset, "next_offset": offset + len(page) if offset + len(page) < len(rows) else None}
    while len(json.dumps(payload, ensure_ascii=False, indent=1)) > CFG.max_output_chars - 500 and len(page) > 1:
        page.pop()
        payload["next_offset"] = offset + len(page)
    return _out(tool, {"limit": limit, "offset": offset}, payload)


@bridge_tool(annotations=RO)
def list_instructions(kind: Literal['instructions', 'skills'] = 'instructions', limit: int = 30, offset: int = 0) -> str:
    """Discover guidance on the AI Bridge host: names, descriptions and paths.

    Read chosen host files with read_file.
    kind=instructions: user docs in ~/.config/ai-bridge/instructions/.
    kind=skills: local SKILL.md files (Codex, Claude, Cursor, .agents or AI_BRIDGE_SKILL_DIRS).
    Discovery never installs or runs skills. Follow next_offset for more entries.
    """
    if kind == 'skills':
        return _catalog_page("list_instructions", catalog.skill_dirs(), "skills", limit, offset, skills=True)
    return _catalog_page("list_instructions", [catalog.instructions_dir()], "instructions", limit, offset)


def _ssh_result(tool, args, operation):
    from . import ssh_admin
    try:
        result = operation(ssh_admin)
    except (OSError, ValueError) as error:
        result = {'status': 'timed_out' if isinstance(error, TimeoutError) else 'unavailable',
                  'stdout': '', 'stderr': '', 'exit_code': None, 'timed_out': isinstance(error, TimeoutError),
                  'method': 'ssh' if tool == 'ssh_run' else (args.get('method') if args.get('method') != 'auto' else None),
                  'error': str(error) if isinstance(error, ValueError) else 'SSH operation could not complete; inspect configuration/client availability locally.',
                  'error_type': type(error).__name__, 'local_sudo_used': False,
                  'remote_privilege': {'sudo_requested': args.get('remote_sudo', False),
                                       'sudo_attempted': None, 'sudo_confirmed': False}}
        if ssh_admin.ALIAS.fullmatch(args.get('host', '')):
            result['host'] = args['host']
    return _out(tool, args, result)


@bridge_tool(annotations=RO)
def ssh_hosts(limit: int = 30, offset: int = 0) -> str:
    """Discover static configured SSH aliases offline; no addresses, users or key paths are returned.

    Includes are bounded; Match-only and wildcard destinations are excluded. No host is contacted.
    """
    return _ssh_result('ssh_hosts', {'limit': limit, 'offset': offset},
                       lambda adapter: adapter.hosts(limit, offset))


@bridge_tool(annotations=RW)
def ssh_run(host: str, command: str, timeout: int = 120, remote_sudo: bool = False) -> str:
    """Run a bounded noninteractive command on a configured SSH alias with pinned existing host keys.

    remote_sudo uses remote sudo -n only, never bridge/local sudo or forwarded passwords.
    Announce remote task/target and report confirmation separately from command outcome.
    Set remote_sudo=true when the authorized task needs remote administrator access.
    approval_required means nothing ran: offer the returned authenticator and host CLI
    methods together. After either is verified, repeat this exact call; either suffices.
    """
    args = {'host': host, 'timeout': timeout, 'remote_sudo': remote_sudo}
    blocked = _approval('ssh_run', {'host': host, 'command': command}, remote_sudo)
    if blocked:
        return _out('ssh_run', args, dict(blocked, remote_privilege={
            'sudo_requested': True, 'sudo_attempted': False, 'sudo_confirmed': False}))
    return _ssh_result('ssh_run', args, lambda adapter: adapter.run(CFG, host, command, timeout, remote_sudo))


@bridge_tool(annotations=RW)
def ssh_transfer(host: str, direction: Literal['upload', 'download'], local_path: str,
                 remote_path: str, method: Literal['auto', 'rsync', 'sftp', 'scp'] = 'auto',
                 timeout: int = 120, dry_run: bool = False) -> str:
    """Transfer one regular file with exclusive destination creation, no overwrites or extra sudo.

    Exact absolute paths, no directories, wildcards or recursive sync. Auto prefers available
    remote/local rsync then local SFTP/SCP. Dry-run is offline; capability remains unverified.
    Uploads use private staging and remote GNU ln -T; downloads commit with a local hard link.
    Failed commits can be ambiguous; inspect before retrying. Existing files are never replaced.
    """
    args = {'host': host, 'direction': direction, 'method': method, 'dry_run': dry_run}
    return _ssh_result('ssh_transfer', args, lambda adapter: adapter.transfer(
        CFG, host, direction, local_path, remote_path, method, timeout, dry_run))

@bridge_tool(annotations=RO)
def system_info() -> str:
    """Facts about the AI Bridge host: OS, user, disk, memory, sudo setup and agent CLIs.

    This describes the bridge host, not a cloud client's shell or sandbox.
    """
    prefix = Path(sys.prefix)
    info = {
        "bridge_version": __version__,
        # Staged releases run from <root>/releases/<name>/venv; other layouts report None.
        "bridge_release": prefix.parent.name if prefix.name == 'venv' and prefix.parent.parent.name == 'releases' else None,
        "hostname": socket.gethostname(),
        "user": os.environ.get("USER") or Path.home().name,
        "home": str(Path.home()),
        "default_cwd": CFG.default_cwd,
        "os": platform.platform(),
        "python": platform.python_version(),
        "architecture": platform.machine(),
        "kernel": platform.release(),
        "package_managers": [name for name in ('apt-get','dnf','yum','pacman','zypper','apk') if shutil.which(name)],
        "sudo_verified": False,
        "sudo_note": 'Configuration only; no elevation probe was run.',
        "sudo_approval": ('not applicable: service runs as root' if os.geteuid() == 0 else
                          'required: host approval or authenticator code' if CFG.sudo_approval and CFG.sudo_totp_secret else
                          'required: the user approves each privileged call on the host' if CFG.sudo_approval else 'off'),
        "sudo_authenticator": (APPROVALS.remote_status() if CFG.sudo_approval and os.geteuid() != 0 else 'not applicable'),
        "limits": {'capture_bytes': CFG.capture_bytes, 'job_log_bytes': CFG.job_log_bytes,
                   'retention_days': CFG.retention_days, 'response_chars': CFG.max_output_chars},
        "service_euid": os.geteuid(),
        "effective_root": os.geteuid() == 0,
        "sudo": "already root; sudo unnecessary" if os.geteuid() == 0 else ("password configured" if CFG.sudo_password else ("NOPASSWD only" if shutil.which("sudo") else "not installed")),
    }
    try:
        release = platform.freedesktop_os_release()
        info['distribution'] = release.get('PRETTY_NAME') or release.get('ID')
    except OSError:
        info['distribution'] = None
    for label, cmd in (("uptime", "uptime -p"), ("disk", "df -h / | tail -1"), ("memory", "free -h | sed -n 2p")):
        r = run_sync(CFG, cmd, None, 10, False)
        info[label] = r["stdout"].strip() if r['exit_code'] == 0 else 'unavailable'
        if r['exit_code'] != 0:
            info[label+'_error'] = {'exit_code': r['exit_code'], 'timed_out': r['timed_out']}
    info['agents'] = {}
    for agent in ag.AGENTS:
        try:
            info['agents'][agent] = ag.find_binary(agent) or 'not installed'
        except Exception as error:
            info['agents'][agent] = f'unavailable: {error}'
    return _out("system_info", {}, info)


@bridge_tool(annotations=RW)
def run_command(command: str, cwd: str | None = None, timeout: int = 120, sudo: bool = False) -> str:
    """Run a remote administration or diagnostic command on the AI Bridge host.

    Returns stdout, stderr and exit code. Use for the user's requested maintenance,
    service checks or configuration tasks. Ordinary calls run as the service user.

    command: bash command line (runs as a login shell, no TTY).
    cwd: working directory (default: the configured default dir).
    timeout: seconds, 1-600. Use start_job for longer work.
    sudo: request administrator (root) execution when the authorized task needs it;
      the user need not name sudo. Use this parameter, not sudo inside command.
      The server returns approval_required BEFORE execution. Present every returned
      approval method in the first prompt: a user-supplied authenticator code OR
      approval through the host CLI. Either is sufficient. After verified code
      approval or confirmed CLI approval, repeat this exact call; the server checks
      authorization before launch. Never supply a Linux password. Client approval
      controls still apply. Inspect privilege metadata and exit code before claiming success.
    """
    args = {"command": command, "cwd": cwd, "timeout": timeout, "sudo": sudo}
    timeout = max(1, min(int(timeout), 600))
    try:
        blocked = _approval('run_command', {'command': command, 'cwd': resolve_cwd(CFG, cwd), 'timeout': timeout}, sudo)
        if blocked:
            return _out("run_command", args, {**blocked, **privilege_fields(privilege_initial(True))})
        r = run_sync(CFG, command, cwd, timeout, sudo)
    except (FileNotFoundError, SudoUnavailable) as e:
        return _out("run_command", args, {"error": str(e), **privilege_fields(getattr(e, "privilege", privilege_initial(sudo)))})
    if sudo and r["exit_code"] != 0 and "password" in r["stderr"].lower() and not CFG.sudo_password:
        r["hint"] = "No sudo password is configured and sudo needs one. See README: Sudo setup."
    if r["timed_out"]:
        r["hint"] = f"Timed out after {timeout}s. Inspect cleanup for termination status. Use start_job for long tasks."
    return _out("run_command", args, r)


@bridge_tool(annotations=RW)
def start_job(command: str, cwd: str | None = None, sudo: bool = False) -> str:
    """Start a long-running bash command in the background. Returns a job_id.

    Commands execute on the bridge host.
    Watch it with job_status. Jobs keep running if this chat ends or the server restarts.
    For an authorized task needing root, set sudo=true instead of embedding sudo in
    command. approval_required means no job started: offer the returned TOTP and host
    CLI methods together. After either is verified, repeat this exact call once.
    """
    args = {"command": command, "cwd": cwd, "sudo": sudo}
    try:
        blocked = _approval('start_job', {'command': command, 'cwd': resolve_cwd(CFG, cwd)}, sudo)
        if blocked:
            return _out("start_job", args, {**blocked, **privilege_fields(privilege_initial(True))})
        meta = JOBS.start(["bash", "-lc", command], resolve_cwd(CFG, cwd), sudo=sudo, display=command)
    except (FileNotFoundError, SudoUnavailable) as e:
        return _out("start_job", args, {"error": str(e), **privilege_fields(getattr(e, "privilege", privilege_initial(sudo)))})
    return _out("start_job", args, _job_view(meta, tail_lines=0))


@bridge_tool(annotations=RO)
def job_status(job_id: str, wait_seconds: int = 0, tail_lines: int = 60, log_offset: int | None = None,
               log_bytes: int = 12000, generation: str | None = None) -> str:
    """Status and output of any background job, including coding-agent turns from agent_send.

    wait_seconds: block up to this long (max 110) for the job to finish; poll again while running.
    tail_lines: recent output lines (max 400). For agent jobs this is the turn's recent activity,
      plus the final answer, outcome, session_id and verified runner resources.
    log_offset: page the full saved output instead (start at 0, follow next_offset_bytes and pass
      the returned generation). Truncated logs keep the head and tail only.
    """
    args = {"job_id": job_id, "wait_seconds": wait_seconds, "log_offset": log_offset}
    try:
        meta = _wait_job(job_id, min(max(wait_seconds, 0), 110))
    except (KeyError, ValueError, OSError) as e:
        return _out("job_status", args, {"error": str(e)})
    tail_lines = min(max(tail_lines, 0), 400)
    if meta.get("kind") == "agent":
        view = _agent_view(meta, tail_lines if log_offset is None else 0)
    else:
        view = _job_view(meta, tail_lines if log_offset is None else 0)
    if log_offset is not None:
        view["log"] = JOBS.log_chunk(job_id, log_offset, log_bytes, generation)
    return _out("job_status", args, view)


@bridge_tool(annotations=RO)
def list_jobs(limit: int = 20, kind: Literal['command', 'agent'] | None = None) -> str:
    """List recent background jobs, newest first: commands and coding-agent turns (kind filters)."""
    rows = [
        {
            "job_id": m["id"],
            "kind": m.get("kind"),
            "status": m["status"],
            "agent": m.get("agent"),
            "session_id": m.get("session_id"),
            "task_id": m.get("task_id"),
            "workspace_type": m.get("workspace_type"),
            "command": (m.get("display") or "")[:120],
            "started": _fmt_time(m.get("started")),
            **job_fields(m),
        }
        for m in JOBS.list(limit=min(limit, 100), kind=kind)
    ]
    return _out("list_jobs", {"limit": limit, "kind": kind}, rows)


@bridge_tool(annotations=RW)
def kill_job(job_id: str) -> str:
    """Stop one exact background job or coding-agent turn by job_id. Inspect cleanup in the result.

    Commands get SIGTERM then SIGKILL. Agent turns also stop their verified child processes; detached
    or privileged descendants may survive. Never stops processes by name or saved session ID.
    """
    try:
        if JOBS.load(job_id).get("kind") == "agent":
            from .management import stop_agent
            return _out("kill_job", {"job_id": job_id}, stop_agent(JOBS, job_id))
        meta = JOBS.kill(job_id)
    except (KeyError, ValueError, OSError) as e:
        return _out("kill_job", {"job_id": job_id}, {"error": str(e)})
    return _out("kill_job", {"job_id": job_id}, _job_view(meta, tail_lines=20))


def _file_operation(request: dict, sudo: bool):
    from .execution import execute
    result = execute(CFG, [sys.executable, str(Path(__file__).with_name('file_ops.py'))],
                     str(Path.home()), 30, sudo, json.dumps(request).encode())
    if result['timed_out'] or result['stdout_truncated'] or result['exit_code'] != 0:
        return {'error': 'File operation failed, timed out, or exceeded the result capture limit.',
                'exit_code': result['exit_code'], 'stderr': result['stderr'], 'cleanup': result.get('cleanup'),
                **privilege_fields(result.get('privilege'))}
    try:
        value = json.loads(result['stdout'])
    except ValueError:
        return {'error': 'File helper did not return a valid result.', 'stderr': result['stderr'],
                **privilege_fields(result.get('privilege'))}
    value = value.get('result', value)
    if 'privilege' in result:
        if not isinstance(value, dict):
            value = {'content': value}
        value.update(privilege_fields(result['privilege']))
    return value


@bridge_tool(annotations=RO)
def read_file(path: str, start_line: int = 1, max_lines: int = 400, sudo: bool = False,
              include_hash: bool = False) -> str:
    """Read a regular text file on the AI Bridge host, with line numbers and bounded bytes.

    sudo=true requests approved root access for protected files on that host.

    include_hash optionally scans the entire file for a SHA256 to use with an atomic write.
    Large files report that only a prefix was captured; use shell tools for arbitrary ranges.
    For an authorized read requiring administrator access, set sudo=true. On
    approval_required, present both returned methods (authenticator code or host CLI).
    After either is verified, repeat the exact call; do not ask for both approvals.
    """
    args = {'path': path, 'start_line': start_line, 'max_lines': max_lines, 'sudo': sudo}
    blocked = _approval('read_file', {'path': os.path.expanduser(path)}, sudo)
    if blocked:
        return _out('read_file', args, {**blocked, **privilege_fields(privilege_initial(True))})
    try:
        result = _file_operation({'operation': 'read', 'path': os.path.expanduser(path),
            'start_line': start_line, 'max_lines': max_lines,
            'max_bytes': max(4096, CFG.capture_bytes//4), 'include_hash': include_hash}, sudo)
    except (OSError, SudoUnavailable, ValueError) as error:
        result = {'error': str(error), **privilege_fields(getattr(error, 'privilege', privilege_initial(sudo)))}
    return _out('read_file', args, result)


@bridge_tool(annotations=RW)
def write_file(path: str, content: str, append: bool = False, sudo: bool = False,
               make_dirs: bool = True, atomic: bool = False, expected_sha256: str | None = None,
               backup: bool = False, mode: int | None = None) -> str:
    """Write text on the AI Bridge host, preserving direct-write behavior by default.

    sudo=true requests approved root access for protected files on that host.

    atomic=true replaces supported regular files; optional expected_sha256 detects stale content,
    backup creates a private local copy, and mode sets integer permission bits (384 is 0600).
    Atomic writes reject symlinks, hard links, ACLs/xattrs and special files. Hash checking is
    optimistic, not a lock against unrelated editors. append cannot be combined with atomic.
    For an authorized write requiring administrator access, set sudo=true. On
    approval_required, present both returned methods (authenticator code or host CLI).
    Either authorizes the bound path, content and options; repeat the exact call after
    verified approval. Changing content or options requires a new approval request.
    """
    args = {'path': path, 'append': append, 'sudo': sudo, 'bytes': len(content.encode()), 'atomic': atomic}
    blocked = _approval('write_file', {'path': os.path.expanduser(path), 'content': content, 'append': append,
                                       'make_dirs': make_dirs, 'atomic': atomic, 'expected_sha256': expected_sha256,
                                       'backup': backup, 'mode': mode}, sudo)
    if blocked:
        return _out('write_file', args, {**blocked, **privilege_fields(privilege_initial(True))})
    try:
        result = _file_operation({'operation': 'write', 'path': os.path.expanduser(path),
            'content': content, 'append': append, 'make_dirs': make_dirs, 'atomic': atomic,
            'expected_sha256': expected_sha256, 'backup': backup, 'mode': mode}, sudo)
    except (OSError, SudoUnavailable, ValueError) as error:
        result = {'error': str(error), **privilege_fields(getattr(error, 'privilege', privilege_initial(sudo)))}
    return _out('write_file', args, result)


# ---------------------------------------------------------------------------
# Agent bridge
# ---------------------------------------------------------------------------

AgentName = Literal["claude", "codex", "cursor"]
Permission = Literal["full", "edit", "readonly"]


@bridge_tool(annotations=RO)
def agent_list(show: Literal['agents', 'models', 'tasks'] = 'agents', agent: AgentName | None = None,
               limit: int = 30, offset: int = 0) -> str:
    """Optional coding agents on this machine (claude, codex, cursor).

    show=agents: installed CLIs, versions, running turns and default permission.
    show=models: model choices for one agent (agent required), with source and freshness. Cached
      entries do not prove account access. Pass a chosen ID to agent_send(model=...).
    show=tasks: private task workspaces; pass task_id to agent_send to share one.
    Follow next_offset for more models or tasks. Manage agent turns with job_status/list_jobs/kill_job.
    """
    args = {"show": show, "agent": agent, "limit": limit, "offset": offset}
    limit, offset = max(1, min(limit, 100)), max(0, offset)
    if show == 'models':
        return _out("agent_list", args, _model_page(agent, limit, offset))
    if show == 'tasks':
        from .management import Tasks
        return _out("agent_list", args, _page({}, 'tasks', Tasks(CFG).list(), limit, offset))
    rows = {}
    for a in ag.AGENTS:
        try:
            b = ag.find_binary(a)
            rows[a] = {"installed": bool(b), "path": b, "version": ag.version_of(b) if b else None}
        except Exception as error:
            rows[a] = {"installed": None, "path": None, "version": None, "error": str(error)}
    running = [
        {"job_id": m["id"], "agent": m.get("agent"), "session_id": m.get("session_id")}
        for m in JOBS.list(limit=50, kind="agent")
        if m["status"] == "running"
    ]
    return _out("agent_list", args, {"agents": rows, "running_turns": running, "default_permission": CFG.agent_default_permission})


def _page(payload: dict, key: str, rows: list, limit: int, offset: int) -> dict:
    """Add one size-bounded page of rows whose next_offset never skips entries."""
    page = rows[offset:offset + limit]
    payload.update({key: page, "total": len(rows), "offset": offset,
                    "next_offset": offset + len(page) if offset + len(page) < len(rows) else None})
    while len(json.dumps(payload, ensure_ascii=False, indent=1, default=str)) > CFG.max_output_chars - 500 and len(page) > 1:
        page.pop()
        payload["next_offset"] = offset + len(page)
    return payload


def _model_page(agent: str | None, limit: int, offset: int) -> dict:
    if agent not in ag.AGENTS:
        return {"error": f"show=models needs agent, one of {ag.AGENTS}"}
    try:
        payload = ag.model_catalog(agent)
    except Exception as error:
        payload = {"agent": agent, "status": "unavailable", "source": None, "models": [],
                   "fetched_at": None, "age_seconds": None, "note": f"Model discovery unavailable: {error}"}
    return _page(payload, "models", payload.pop("models"), limit, offset)


@bridge_tool(annotations=RW)
def agent_send(
    agent: AgentName,
    prompt: str,
    cwd: str | None = None,
    session_id: str | None = None,
    permission: Permission | None = None,
    model: str | None = None,
    wait_seconds: int = 30,
    task_id: str | None = None,
    timeout: int | None = None,
    mode: Literal['interactive', 'exec'] | None = None,
    remote: bool = False,
    name: str | None = None,
) -> str:
    """Send a prompt to a coding agent on the AI Bridge host and start a turn.

    Use for user-requested delegation or another bridge-managed agent session.
    Returns job_id (and the result if it finishes fast).

    agent: claude (Claude Code), codex (OpenAI Codex CLI), cursor (Cursor CLI).
    prompt: what the agent should do.
    cwd: project directory; on resume it is inferred from the saved session. A conflicting cwd is rejected.
    task_id: reuse a managed workspace. Without cwd/session/task, a private task is created.
    timeout: optional turn deadline in seconds (1..604800); omitted means unlimited.
    session_id: continue an existing session (from a previous result or agent_sessions). Omit for a new session.
    permission: full = run anything without asking; edit = edit files, limited commands;
                readonly = analyse/plan only. Default comes from server config.
    model: optional model name for that CLI.
    mode: interactive (default for claude/codex) runs the turn in a normal chat the user can also open in
      the terminal (claude attach/--resume, codex resume); exec is a one-shot headless run (cursor: exec only).
    remote: enable Remote Control so the chat can be continued from the Claude app / claude.ai/code or the
      ChatGPT Codex remote. Needs interactive mode. For codex this turns Remote Control on for the shared
      daemon (it restarts once). The result's native.remote_url / native.remote_control reports it.
    name: display name for the chat (shown in the CLI's resume list and remote session list).
    wait_seconds: wait up to this long (max 110) before returning. Then poll job_status with the job_id;
      stop with kill_job.
    """
    from .management import start_agent
    args = {"agent": agent, "cwd": cwd, "session_id": session_id, "task_id": task_id, "mode": mode, "remote": remote}
    try:
        with AGENT_START_LOCK:
            meta = start_agent(CFG, JOBS, agent, prompt, cwd, session_id, permission, model, task_id, timeout,
                               invocation_origin="mcp", mode=mode, remote=remote, name=name)
    except Exception as error:
        return _out('agent_send', args, {'error': str(error)})
    meta = _wait_job(meta["id"], min(max(wait_seconds, 0), 110))
    return _out('agent_send', args, _agent_view(meta, 15))


def _agent_view(meta: dict, progress_lines: int) -> dict:
    """Agent-turn result: status is the job lifecycle, outcome the agent result. Limit/provider
    failures are reported with failure.scope='agent', never retried automatically."""
    job_id = meta["id"]
    try:
        output = JOBS.output_info(job_id)
        if output.get('parser_error'):
            raise ValueError(output['parser_error'])
        parsed = output.get('parsed') or ag.parse_output(meta["agent"], JOBS.read_log(job_id))
    except Exception as error:
        return {'job_id': job_id, 'kind': 'agent', 'agent': meta.get('agent'),
                    'status': meta['status'], 'session_id': meta.get('session_id'),
                    'exit_code': meta.get('exit_code'), 'outcome': 'unavailable',
                    'failure': {'scope': 'adapter', 'code': 'parser_unavailable', 'automatic_retry': False},
                    'error': f'Agent output parser unavailable: {error}',
                    'next': 'Page the raw output with job_status(log_offset=0). No new chat was started.'}
    mismatch = bool(meta.get("requested_session_id") and parsed["session_id"]
                    and parsed["session_id"] != meta["requested_session_id"])
    if parsed["session_id"] and parsed["session_id"] != meta.get("session_id") and not mismatch:
        meta["session_id"] = parsed["session_id"]
        JOBS.save(meta)

    view = {
        "job_id": job_id,
        "kind": "agent",
        "task_id": meta.get("task_id"),
        "workspace_type": meta.get("workspace_type"),
        "workspace_peers": meta.get("workspace_peers", []),
        "workspace_note": meta.get("workspace_note"),
        "timeout": meta.get("timeout"),
        "timed_out": meta.get("timed_out", False),
        "agent": meta["agent"],
        "mode": meta.get("mode") or "exec",
        "remote": meta.get("remote", False),
        "native": parsed.get("native") or None,
        "status": meta["status"],
        "session_id": meta.get("session_id"),
        "requested_model": meta.get("model"),
        "reported_model": parsed.get("reported_model"),
        "continuity": ('mismatch' if mismatch else 'confirmed' if meta.get('requested_session_id') and parsed['session_id'] else 'unverified' if meta.get('requested_session_id') else 'new_session'),
        "output_truncated": output.get('truncated', False),
        "cleanup": meta.get('cleanup'),
        "records_dropped": output.get('records_dropped', 0),
        "permission": meta.get('permission'),
        "permission_note": ('Cursor edit/full both map to --force; native modes are not bridge-enforced host permissions.' if meta['agent'] == 'cursor' else 'Permission maps to native CLI flags; it does not restrict bridge shell/file/sudo tools.'),
        "cwd": meta.get("cwd"),
        "runtime_s": round((meta.get("ended") or time.time()) - meta["started"], 1),
        "wait_deferred": meta.get("wait_deferred", False),
        "recent_activity": parsed["progress"][-progress_lines:] if progress_lines else [],
    }
    view.update(job_fields(meta))
    view.update(_runner_facts(job_id))
    if mismatch:
        view.update(is_error=True, outcome='failed', requested_session_id=meta["requested_session_id"],
                    returned_session_id=parsed["session_id"],
                    error="CLI returned a different session ID. Continuity was not preserved; do not continue automatically.")
        return view
    if meta["status"] == "running":
        view['outcome'] = 'running'
        view["next"] = "Still working. Call job_status again with the same job_id and wait_seconds."
    else:
        view["exit_code"] = meta.get("exit_code")
        view["result"] = parsed["final"]
        view["is_error"] = parsed["is_error"] or meta["status"] != "done"
        if parsed["stats"]:
            view["stats"] = parsed["stats"]
        if view["is_error"] or not parsed["final"]:
            view["errors_or_raw_output"] = "\n".join(parsed["other"][-40:]) or JOBS.tail(job_id, 40)
        view["next"] = "To continue this conversation, call agent_send with this session_id."
        if view['is_error']:
            diagnostics = '\n'.join(str(item) for item in parsed['other'][-40:])
            if parsed['is_error'] and parsed['final']:
                diagnostics += '\n' + str(parsed['final'])
            view.update(describe_failure(diagnostics, meta['status']))
        elif (parsed.get('native') or {}).get('waiting_for'):
            view['outcome'] = 'waiting'
            view['next'] = (f"The chat is waiting for {parsed['native']['waiting_for']}. Continue it with agent_send "
                            "and this session_id, in the terminal, or remotely.")
        else:
            view['outcome'] = 'succeeded'
            if not view['session_id']:
                view['next'] = 'Turn completed without a reported session ID; do not assume a resumable chat.'
    return view


def _runner_facts(job_id: str) -> dict:
    """Verified runner identity/resources for an agent job; best effort, never fails the view."""
    from .management import runtime
    try:
        row = runtime(JOBS, job_id)[0]
    except Exception:
        return {}
    keys = ('runner_pid', 'runner_identity_verified', 'runner_resources', 'target_pid',
            'target_identity_verified', 'observed_descendants')
    return {k: row[k] for k in keys if k in row}


@bridge_tool(annotations=RO)
def agent_sessions(agent: AgentName, cwd: str | None = None, limit: int = 15, offset: int = 0,
                   include_archived: bool = False) -> str:
    """List past sessions of a coding agent on this machine (newest first), optionally only for one project dir.

    Includes sessions started directly in the terminal, not only through this bridge.
    Codex rows include parent_session_id when a parent thread is recorded; treat those as subagent sessions under that parent.
    last_used_model/model_source describe recognized saved metadata, not an active/next-turn model.
    Codex records the turn selection, Claude the reply model, and Cursor its lastUsedModel setting.
    Unknown or unsupported records return null; recent JSONL model lookup is bounded to the last 2 MB.
    offset: skip this many matching chats to page through older sessions.
    include_archived: also list chats archived with agent_manage.
    Cursor lists local CLI chats, not Cursor editor/cloud chats.
    """
    limit, offset = max(1, min(limit, 100)), max(0, offset)
    args = {"agent": agent, "cwd": cwd, "limit": limit, "offset": offset}
    try:
        from .management import Sessions
        rows = Sessions(CFG, JOBS).enrich(agent, ag.list_sessions(agent, cwd, None), include_archived)
    except Exception as e:  # noqa: BLE001
        return _out("agent_sessions", args, {"error": f"Could not read {agent} sessions: {e}"})
    page = rows[offset:offset + limit]
    payload = {"sessions": page, "total": len(rows), "offset": offset,
               "next_offset": offset + len(page) if offset + len(page) < len(rows) else None}
    if agent == "cursor":
        payload["note"] = "Local Cursor CLI chats only. Editor/cloud chats are not interchangeable with CLI sessions."
    # Keep pages valid JSON and ensure the next cursor never skips rows that
    # would otherwise be removed by the generic output truncator.
    while len(json.dumps(payload, indent=1, ensure_ascii=False, default=str)) > CFG.max_output_chars - 500 and len(page) > 1:
        page.pop()
        payload["next_offset"] = offset + len(page)
    return _out("agent_sessions", args, payload)


@bridge_tool(annotations=MANAGE)
def agent_manage(
    action: Literal['alias', 'archive', 'unarchive', 'cleanup_task'],
    agent: AgentName | None = None,
    session_id: str | None = None,
    alias: str | None = None,
    task_id: str | None = None,
    confirm: bool = False,
    allow_uninspectable: bool = False,
) -> str:
    """Organize agent chats and remove finished task workspaces. Native chat history is never changed.

    alias: set a display name for agent+session_id (omit alias to clear it).
    archive / unarchive: hide or restore a chat in agent_sessions (include_archived shows it).
    cleanup_task: delete a task workspace from agent_list(show=tasks). Without confirm it only previews
      blockers (running/uncertain jobs, processes using it, mounts). confirm=true deletes; this cannot be
      undone. allow_uninspectable also ignores processes that cannot be inspected (high risk).
    """
    from .management import Sessions, Tasks
    args = {"action": action, "agent": agent, "session_id": session_id, "task_id": task_id, "confirm": confirm}
    try:
        if action == 'cleanup_task':
            if not task_id:
                raise ValueError('cleanup_task needs task_id.')
            result = Tasks(CFG).cleanup(task_id, JOBS, not confirm,
                                        allow_uninspectable=allow_uninspectable, confirmed=confirm)
            if not confirm:
                result['next'] = ('Review blockers; call again with confirm=true to delete.' if result['cleanup_allowed']
                                  else 'Cleanup is blocked; resolve the blockers first.')
        else:
            if not agent or not session_id:
                raise ValueError(f'{action} needs agent and session_id.')
            changes = {'alias': alias} if action == 'alias' else {'archived': action == 'archive'}
            row = Sessions(CFG, JOBS).overlay(agent, session_id, **changes)
            result = {'session_id': session_id, 'agent': agent, 'action': action,
                      'bridge_metadata': row.get('bridge_metadata'), 'native_history_changed': False}
    except (KeyError, ValueError, OSError) as error:
        result = {'error': str(error)}
    return _out('agent_manage', args, result)


@bridge_tool(annotations=RO)
def agent_history(
    agent: AgentName,
    session_id: str,
    last_n: int = 30,
    include_tools: bool = True,
    max_chars_per_message: int = 2000,
    offset: int = 0,
) -> str:
    """Read the conversation of one agent session: user prompts, agent replies, and (optionally) tool actions.

    last_n: how many entries to return (max 300).
    offset: skip this many entries from the end to page backward through history.
    Reads stored history without starting or modifying the agent session.
    """
    args = {"agent": agent, "session_id": session_id, "last_n": last_n}
    try:
        info = ag.session_info(agent, session_id)
        msgs = ag.history(agent, session_id)
    except KeyError as e:
        return _out("agent_history", args, {"error": str(e)})
    except Exception as e:  # noqa: BLE001
        return _out("agent_history", args, {"error": f"Could not read history: {e}"})
    if not include_tools:
        msgs = [m for m in msgs if m["role"] != "tool"]
    total = len(msgs)
    offset = max(0, offset)
    end = max(0, total - offset)
    start = max(0, end - max(1, min(last_n, 300)))
    msgs = msgs[start:end]
    max_chars_per_message = max(100, min(max_chars_per_message, 10000))
    for m in msgs:
        if len(m["text"]) > max_chars_per_message:
            m["text"] = m["text"][:max_chars_per_message] + " ...[cut]"
    payload = {"session_id": session_id, "session": info, "total_entries": total,
               "showing": len(msgs), "offset": offset,
               "next_offset": offset + len(msgs) if start > 0 else None, "entries": msgs}
    while len(json.dumps(payload, indent=1, ensure_ascii=False, default=str)) > CFG.max_output_chars - 500 and len(msgs) > 1:
        msgs.pop(0)
        payload["showing"] = len(msgs)
        payload["next_offset"] = offset + len(msgs)
    return _out("agent_history", args, payload)


# ---------------------------------------------------------------------------
# HTTP app with token gate
# ---------------------------------------------------------------------------


HOST_CONFIG.register(mcp, REDACT)


class TokenGate:
    """Accepts requests on /<token>/mcp or with 'Authorization: Bearer <token>'. Everything else is 404."""

    def __init__(self, app, token: str):
        self.app = app
        self.prefix = "/" + token
        self.bearer = ("Bearer " + token).encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        if path == "/healthz":
            return await self._plain(send, 200, b"ok")

        allowed = False
        seg = path[: len(self.prefix)]
        if hmac.compare_digest(seg.encode(), self.prefix.encode()) and (
            len(path) == len(self.prefix) or path[len(self.prefix)] == "/"
        ):
            new_path = path[len(self.prefix) :] or "/"
            scope = dict(scope, path=new_path, raw_path=new_path.encode())
            allowed = True
        else:
            for k, v in scope.get("headers", []):
                if k == b"authorization" and hmac.compare_digest(v, self.bearer):
                    allowed = True
                    break
        if not allowed:
            return await self._plain(send, 404, b"not found")
        return await self.app(scope, receive, send)

    @staticmethod
    async def _plain(send, status: int, body: bytes):
        await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"text/plain")]})
        await send({"type": "http.response.body", "body": body})


def build_app():
    if CFG.allowed_hosts:
        security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=CFG.allowed_hosts + [f"127.0.0.1:{CFG.port}", f"localhost:{CFG.port}"],
            allowed_origins=[f"https://{h}" for h in CFG.allowed_hosts]
            + ["https://claude.ai", "https://claude.com", "https://chatgpt.com"] + CFG.allowed_origins,
        )
    else:
        security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    inner = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        transport_security=security,
        host=CFG.host,
    )
    return TokenGate(inner, CFG.token)


def main():
    app = build_app()
    print(f"ai-bridge listening on http://{CFG.host}:{CFG.port}  (MCP path: /<token>/mcp)", flush=True)
    print(f"sudo: {'password configured' if CFG.sudo_password else 'no password (NOPASSWD or none)'}", flush=True)
    # access_log=False: the URL contains the token, keep it out of logs.
    # Long-lived MCP connections must not hold a restart until systemd's 90s kill timeout.
    # Detached command/agent jobs have their own runners and survive this server shutdown.
    uvicorn.run(app, host=CFG.host, port=CFG.port, log_level="warning", access_log=False,
                proxy_headers=True, timeout_graceful_shutdown=10)


if __name__ == "__main__":
    main()

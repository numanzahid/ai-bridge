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

CFG = Config.load()
REDACT = Redactor([CFG.token] + ([CFG.sudo_password] if CFG.sudo_password else []))
JOBS = Jobs(CFG)
AGENT_START_LOCK = threading.Lock()
WAIT_SLOTS = threading.BoundedSemaphore(8)

HOST_CONFIG = HostConfig()
INSTRUCTIONS = REDACT(HOST_CONFIG.instructions)

mcp = MCPServer(name="ai-bridge", instructions=INSTRUCTIONS, version="1.0.0")

RO = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
RW = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True)


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
                result = _out(fn.__name__, {}, {'error': str(error), 'outcome': 'unavailable', 'automatic_retry': False})
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
def list_instructions(limit: int = 30, offset: int = 0) -> str:
    """Discover user-maintained guidance in ~/.config/ai-bridge/instructions/ (honors XDG_CONFIG_HOME).

    Call when first working on this machine. Lists Markdown/text document names, brief descriptions,
    and paths; use read_file to load only documents relevant to the task. Contents are not preloaded
    at connection. Follow next_offset for more entries. Changes appear on the next listing.
    """
    return _catalog_page("list_instructions", [catalog.instructions_dir()], "instructions", limit, offset)


@bridge_tool(annotations=RO)
def list_skills(limit: int = 30, offset: int = 0) -> str:
    """Discover local skills when relevant to the user's task; returns names, descriptions, and SKILL.md paths.

    Scans local Codex, Claude, Cursor, and .agents skill directories, or AI_BRIDGE_SKILL_DIRS.
    Use read_file to load a selected skill and its referenced files on demand; do not load all skills.
    This does not execute/install skills or supply client-specific tools they may require.
    Follow next_offset for more entries. Contents are not preloaded at connection.
    """
    return _catalog_page("list_skills", catalog.skill_dirs(), "skills", limit, offset, skills=True)


@bridge_tool(annotations=RO)
def system_info() -> str:
    """Basic facts about this machine: OS, user, uptime, disk, memory, sudo setup, installed agent CLIs."""
    info = {
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
        "limits": {'capture_bytes': CFG.capture_bytes, 'job_log_bytes': CFG.job_log_bytes,
                   'retention_days': CFG.retention_days, 'response_chars': CFG.max_output_chars},
        "sudo": "password configured" if CFG.sudo_password else ("NOPASSWD only" if shutil.which("sudo") else "not installed"),
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
    """Run a bash command and return stdout, stderr and exit code.

    command: bash command line (runs as a login shell, no TTY).
    cwd: working directory (default: the configured default dir).
    timeout: seconds, 1-600. Use start_job for longer work.
    sudo: run as root. The password is injected server side; never include it yourself.
    """
    args = {"command": command, "cwd": cwd, "timeout": timeout, "sudo": sudo}
    timeout = max(1, min(int(timeout), 600))
    try:
        r = run_sync(CFG, command, cwd, timeout, sudo)
    except (FileNotFoundError, SudoUnavailable) as e:
        return _out("run_command", args, {"error": str(e)})
    if sudo and r["exit_code"] != 0 and "password" in r["stderr"].lower() and not CFG.sudo_password:
        r["hint"] = "No sudo password is configured and sudo needs one. See README: Sudo setup."
    if r["timed_out"]:
        r["hint"] = f"Timed out after {timeout}s. Inspect cleanup for termination status. Use start_job for long tasks."
    return _out("run_command", args, r)


@bridge_tool(annotations=RW)
def start_job(command: str, cwd: str | None = None, sudo: bool = False) -> str:
    """Start a long-running bash command in the background. Returns a job_id.

    Use job_status to watch it. Jobs keep running even if this chat ends or the server restarts.
    """
    args = {"command": command, "cwd": cwd, "sudo": sudo}
    try:
        meta = JOBS.start(["bash", "-lc", command], resolve_cwd(CFG, cwd), sudo=sudo, display=command)
    except (FileNotFoundError, SudoUnavailable) as e:
        return _out("start_job", args, {"error": str(e)})
    return _out("start_job", args, _job_view(meta, tail_lines=0))


@bridge_tool(annotations=RO)
def job_status(job_id: str, wait_seconds: int = 0, tail_lines: int = 60) -> str:
    """Status and recent output of a background job.

    wait_seconds: block up to this long (max 110) for the job to finish before answering.
    tail_lines: how many output lines to include (max 400).
    """
    args = {"job_id": job_id, "wait_seconds": wait_seconds}
    try:
        meta = _wait_job(job_id, min(max(wait_seconds, 0), 110))
    except (KeyError, ValueError, OSError) as e:
        return _out("job_status", args, {"error": str(e)})
    return _out("job_status", args, _job_view(meta, min(max(tail_lines, 0), 400)))


@bridge_tool(annotations=RO)
def list_jobs(limit: int = 20) -> str:
    """List recent background jobs and agent runs, newest first."""
    rows = [
        {
            "job_id": m["id"],
            "kind": m.get("kind"),
            "status": m["status"],
            "agent": m.get("agent"),
            "session_id": m.get("session_id"),
            "command": (m.get("display") or "")[:120],
            "started": _fmt_time(m.get("started")),
        }
        for m in JOBS.list(limit=min(limit, 100))
    ]
    return _out("list_jobs", {"limit": limit}, rows)


@bridge_tool(annotations=RW)
def kill_job(job_id: str) -> str:
    """Stop a running background job or agent run (SIGTERM, then SIGKILL)."""
    try:
        meta = JOBS.kill(job_id)
    except (KeyError, ValueError, OSError) as e:
        return _out("kill_job", {"job_id": job_id}, {"error": str(e)})
    return _out("kill_job", {"job_id": job_id}, _job_view(meta, tail_lines=20))


@bridge_tool(annotations=RO)
def job_log(job_id: str, offset_bytes: int = 0, max_bytes: int = 12000,
            generation: str | None = None) -> str:
    """Read a bounded chunk of retained job output. Follow next_offset_bytes.

    Pass the returned generation on subsequent pages; a changed snapshot asks you to restart.
    Truncated logs retain the head and tail, not the omitted middle. No command is executed.
    """
    try:
        result = JOBS.log_chunk(job_id, offset_bytes, max_bytes, generation)
    except (OSError, ValueError, KeyError) as error:
        result = {'error': str(error)}
    return _out('job_log', {'job_id': job_id, 'offset_bytes': offset_bytes}, result)


def _file_operation(request: dict, sudo: bool):
    from .execution import execute
    result = execute(CFG, [sys.executable, str(Path(__file__).with_name('file_ops.py'))],
                     str(Path.home()), 30, sudo, json.dumps(request).encode())
    if result['timed_out'] or result['stdout_truncated'] or result['exit_code'] != 0:
        return {'error': 'File operation failed, timed out, or exceeded the result capture limit.',
                'exit_code': result['exit_code'], 'stderr': result['stderr'], 'cleanup': result.get('cleanup')}
    try:
        value = json.loads(result['stdout'])
    except ValueError:
        return {'error': 'File helper did not return a valid result.', 'stderr': result['stderr']}
    return value.get('result', value)


@bridge_tool(annotations=RO)
def read_file(path: str, start_line: int = 1, max_lines: int = 400, sudo: bool = False,
              include_hash: bool = False) -> str:
    """Read a regular text file with line numbers and bounded bytes. sudo=true for root files.

    include_hash optionally scans the entire file for a SHA256 to use with an atomic write.
    Large files report that only a prefix was captured; use shell tools for arbitrary ranges.
    """
    args = {'path': path, 'start_line': start_line, 'max_lines': max_lines, 'sudo': sudo}
    try:
        result = _file_operation({'operation': 'read', 'path': os.path.expanduser(path),
            'start_line': start_line, 'max_lines': max_lines,
            'max_bytes': max(4096, CFG.capture_bytes//4), 'include_hash': include_hash}, sudo)
    except (OSError, SudoUnavailable, ValueError) as error:
        result = {'error': str(error)}
    return _out('read_file', args, result)


@bridge_tool(annotations=RW)
def write_file(path: str, content: str, append: bool = False, sudo: bool = False,
               make_dirs: bool = True, atomic: bool = False, expected_sha256: str | None = None,
               backup: bool = False, mode: int | None = None) -> str:
    """Write text, preserving existing direct-write behavior by default. sudo=true for root files.

    atomic=true replaces supported regular files; optional expected_sha256 detects stale content,
    backup creates a private local copy, and mode sets integer permission bits (384 is 0600).
    Atomic writes reject symlinks, hard links, ACLs/xattrs and special files. Hash checking is
    optimistic, not a lock against unrelated editors. append cannot be combined with atomic.
    """
    args = {'path': path, 'append': append, 'sudo': sudo, 'bytes': len(content.encode()), 'atomic': atomic}
    try:
        result = _file_operation({'operation': 'write', 'path': os.path.expanduser(path),
            'content': content, 'append': append, 'make_dirs': make_dirs, 'atomic': atomic,
            'expected_sha256': expected_sha256, 'backup': backup, 'mode': mode}, sudo)
    except (OSError, SudoUnavailable, ValueError) as error:
        result = {'error': str(error)}
    return _out('write_file', args, result)


# ---------------------------------------------------------------------------
# Agent bridge
# ---------------------------------------------------------------------------

AgentName = Literal["claude", "codex", "cursor"]
Permission = Literal["full", "edit", "readonly"]


@bridge_tool(annotations=RO)
def agent_list() -> str:
    """Which coding agent CLIs are installed here (claude, codex, cursor), with versions."""
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
    return _out("agent_list", {}, {"agents": rows, "running_turns": running, "default_permission": CFG.agent_default_permission})


@bridge_tool(annotations=RO)
def agent_models(agent: AgentName, limit: int = 30, offset: int = 0) -> str:
    """Discover model choices on demand, with source and freshness; does not start a model turn.

    Cursor queries its account model-list CLI; Codex reads its timestamped local catalog cache.
    Unsupported interfaces/formats return unavailable, never guessed model IDs. Cached entries do not
    guarantee current account access. CLI current/default settings do not identify every chat's model.
    Follow next_offset to page. Pass a chosen ID to agent_send(model=...). Aliases are not resolved.
    """
    args = {"agent": agent, "limit": limit, "offset": offset}
    if agent not in ag.AGENTS:
        return _out("agent_models", args, {"error": f"agent must be one of {ag.AGENTS}"})
    limit, offset = max(1, min(limit, 100)), max(0, offset)
    try:
        payload = ag.model_catalog(agent)
    except Exception as error:
        payload = {"agent": agent, "status": "unavailable", "source": None, "models": [],
                   "fetched_at": None, "age_seconds": None, "note": f"Model discovery unavailable: {error}"}
    rows = payload.pop('models')
    page = rows[offset:offset + limit]
    payload.update(models=page, total=len(rows), offset=offset,
                   next_offset=offset + len(page) if offset + len(page) < len(rows) else None)
    while len(json.dumps(payload, ensure_ascii=False, indent=1)) > CFG.max_output_chars - 500 and len(page) > 1:
        page.pop()
        payload['next_offset'] = offset + len(page)
    return _out("agent_models", args, payload)


@bridge_tool(annotations=RW)
def agent_send(
    agent: AgentName,
    prompt: str,
    cwd: str | None = None,
    session_id: str | None = None,
    permission: Permission | None = None,
    model: str | None = None,
    wait_seconds: int = 30,
) -> str:
    """Send a prompt to a local coding agent and start a turn. Returns job_id (and the result if it finishes fast).

    agent: claude (Claude Code), codex (OpenAI Codex CLI), cursor (Cursor CLI).
    prompt: what the agent should do.
    cwd: project directory; on resume it is inferred from the saved session. A conflicting cwd is rejected.
    session_id: continue an existing session (from a previous result or agent_sessions). Omit for a new session.
    permission: full = run anything without asking; edit = edit files, limited commands;
                readonly = analyse/plan only. Default comes from server config.
    model: optional model name for that CLI.
    wait_seconds: wait up to this long (max 110) before returning. Poll with agent_wait after.
    """
    perm = permission or CFG.agent_default_permission
    args = {"agent": agent, "cwd": cwd, "session_id": session_id, "permission": perm, "model": model, "prompt_chars": len(prompt)}
    if agent not in ag.AGENTS:
        return _out("agent_send", args, {"error": f"agent must be one of {ag.AGENTS}"})
    if perm not in ag.PERMISSIONS:
        return _out("agent_send", args, {"error": f"permission must be one of {ag.PERMISSIONS}"})
    try:
        binary = ag.find_binary(agent)
        if not binary:
            return _out("agent_send", args, {"error": f"{agent} CLI is not installed or not on PATH. Check agent_list."})
        workdir = resolve_cwd(CFG, ag.resume_cwd(agent, session_id, cwd) if session_id is not None else cwd)
    except Exception as e:
        return _out("agent_send", args, {"error": str(e)})
    try:
        argv = ag.build_argv(agent, binary, prompt, session_id, perm, model)
    except Exception as e:
        return _out("agent_send", args, {"error": str(e)})
    # Serialize the busy check and process start; concurrent MCP requests must
    # not launch two bridge turns against the same session.
    with AGENT_START_LOCK:
        if session_id is not None:
            busy = JOBS.running_for_session(agent, session_id)
            if busy:
                return _out("agent_send", args, {"error": f"Session is busy with job {busy['id']}. Wait for it or kill it first."})
        try:
            meta = JOBS.start(
                argv,
                workdir,
                kind="agent",
                display=f"{agent}: resume {session_id}" if session_id else f"{agent}: new turn",
                extra={"agent": agent, "session_id": session_id, "requested_session_id": session_id,
                       "permission": perm, "model": model},
            )
        except Exception as error:
            return _out('agent_send', args, {'error': f'Could not start {agent}: {error}'})
    return agent_wait(meta["id"], wait_seconds)


@bridge_tool(annotations=RO)
def agent_wait(job_id: str, wait_seconds: int = 60, progress_lines: int = 15) -> str:
    """Wait for an agent turn started by agent_send. Returns status, progress, and the agent's final answer when done.

    Call repeatedly while status is 'running'. wait_seconds max 110.
    status is the job lifecycle; outcome describes the agent result. Limit/provider
    failures return failure.scope='agent' and a reason, not an MCP transport error.
    Failed turns are never automatically retried. Keep session_id for an explicit continuation.
    """
    args = {"job_id": job_id, "wait_seconds": wait_seconds}
    try:
        meta = _wait_job(job_id, min(max(wait_seconds, 0), 110))
    except (KeyError, ValueError, OSError) as e:
        return _out("agent_wait", args, {"error": str(e)})
    if meta.get("kind") != "agent":
        return _out("agent_wait", args, {"error": "Not an agent job. Use job_status."})

    try:
        output = JOBS.output_info(job_id)
        if output.get('parser_error'):
            raise ValueError(output['parser_error'])
        parsed = output.get('parsed') or ag.parse_output(meta["agent"], JOBS.read_log(job_id))
    except Exception as error:
        return _out('agent_wait', args, {'job_id': job_id, 'agent': meta.get('agent'),
                    'status': meta['status'], 'session_id': meta.get('session_id'),
                    'exit_code': meta.get('exit_code'), 'outcome': 'unavailable',
                    'failure': {'scope': 'adapter', 'code': 'parser_unavailable', 'automatic_retry': False},
                    'error': f'Agent output parser unavailable: {error}',
                    'next': 'Use job_status to inspect the underlying job. No new chat was started.'})
    mismatch = bool(meta.get("requested_session_id") and parsed["session_id"]
                    and parsed["session_id"] != meta["requested_session_id"])
    if parsed["session_id"] and parsed["session_id"] != meta.get("session_id") and not mismatch:
        meta["session_id"] = parsed["session_id"]
        JOBS.save(meta)

    view = {
        "job_id": job_id,
        "agent": meta["agent"],
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
        "recent_activity": parsed["progress"][-max(1, min(progress_lines, 100)) :],
    }
    if mismatch:
        view.update(is_error=True, outcome='failed', requested_session_id=meta["requested_session_id"],
                    returned_session_id=parsed["session_id"],
                    error="CLI returned a different session ID. Continuity was not preserved; do not continue automatically.")
        return _out("agent_wait", args, view)
    if meta["status"] == "running":
        view['outcome'] = 'running'
        view["next"] = "Still working. Call agent_wait again with the same job_id."
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
        else:
            view['outcome'] = 'succeeded'
            if not view['session_id']:
                view['next'] = 'Turn completed without a reported session ID; do not assume a resumable chat.'
    return _out("agent_wait", args, view)


@bridge_tool(annotations=RO)
def agent_sessions(agent: AgentName, cwd: str | None = None, limit: int = 15, offset: int = 0) -> str:
    """List past sessions of a coding agent on this machine (newest first), optionally only for one project dir.

    Includes sessions started directly in the terminal, not only through this bridge.
    Codex rows include parent_session_id when a parent thread is recorded; treat those as subagent sessions under that parent.
    last_used_model/model_source describe recognized saved metadata, not an active/next-turn model.
    Codex records the turn selection, Claude the reply model, and Cursor its lastUsedModel setting.
    Unknown or unsupported records return null; recent JSONL model lookup is bounded to the last 2 MB.
    offset: skip this many matching chats to page through older sessions.
    Cursor lists local CLI chats, not Cursor editor/cloud chats.
    """
    limit, offset = max(1, min(limit, 100)), max(0, offset)
    args = {"agent": agent, "cwd": cwd, "limit": limit, "offset": offset}
    try:
        rows = ag.list_sessions(agent, cwd, None)
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
    uvicorn.run(app, host=CFG.host, port=CFG.port, log_level="warning", access_log=False, proxy_headers=True)


if __name__ == "__main__":
    main()

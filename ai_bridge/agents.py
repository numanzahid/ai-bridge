"""Bridge to local coding agent CLIs: Claude Code, OpenAI Codex, Cursor CLI.

Every turn runs the CLI in headless mode as a background job, so a remote
assistant can send a prompt, poll for the result, and resume the same session
later. Session history is read straight from each CLI's own files.
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import re
import shlex
import subprocess
import time
from pathlib import Path

from .execution import probe

AGENTS = ("claude", "codex", "cursor")
PERMISSIONS = ("full", "edit", "readonly")

HOME = Path.home()


def _cursor_store(path: Path):
    from .cursor_store import Store
    return Store(path)


def _model_name(value):
    try:
        from .model_info import model_name
        return model_name(value)
    except Exception:
        return None


def _saved_model(path: Path, agent: str) -> dict:
    try:
        from .model_info import saved_model
        return saved_model(path, agent)
    except Exception:
        return {'last_used_model': None, 'model_source': None}


def _claude_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or HOME / ".claude")


def _codex_dir() -> Path:
    return Path(os.environ.get("CODEX_HOME") or HOME / ".codex")


def _cursor_chats_dir() -> Path:
    return Path(os.environ.get("AI_BRIDGE_CURSOR_HOME") or HOME / ".cursor") / "chats"


# ---------------------------------------------------------------------------
# Binary discovery
# ---------------------------------------------------------------------------


def find_binary(agent: str) -> str | None:
    override = os.environ.get(f"AI_BRIDGE_{agent.upper()}_BIN")
    if override:
        return override if shutil.which(override) or Path(override).exists() else None
    if agent == "claude":
        return shutil.which("claude")
    if agent == "codex":
        return shutil.which("codex")
    if agent == "cursor":
        for name in ("cursor-agent", "agent"):
            path = shutil.which(name)
            if not path:
                continue
            if name == "agent":
                # 'agent' is a generic name; make sure it is Cursor's.
                real = os.path.realpath(path)
                if "cursor" not in real.lower():
                    try:
                        response = probe([path, '--help'], timeout=10)
                        if response['timed_out'] or response['stdout_truncated']:
                            continue
                        out = response['stdout']
                    except (OSError, subprocess.SubprocessError):
                        continue
                    if "cursor" not in out.lower():
                        continue
            return path
    return None


def version_of(binary: str) -> str:
    try:
        r = probe([binary, '--version'], timeout=15)
        if r['timed_out'] or r['stdout_truncated'] or r['stderr_truncated']:
            return 'unknown'
        return (r['stdout'] or r['stderr']).strip().splitlines()[0][:120]
    except (OSError, subprocess.SubprocessError, IndexError):
        return "unknown"


def model_catalog(agent: str) -> dict:
    from .model_info import discover_models
    return discover_models(
        agent,
        codex_dir=_codex_dir() if agent == 'codex' else None,
        binary=find_binary('cursor') if agent == 'cursor' else None,
    )


# ---------------------------------------------------------------------------
# Command building
# ---------------------------------------------------------------------------


def _safe_prompt(prompt: str) -> str:
    # A prompt starting with '-' would be parsed as a flag.
    return " " + prompt if prompt.startswith("-") else prompt


def build_argv(
    agent: str,
    binary: str,
    prompt: str,
    session_id: str | None,
    permission: str,
    model: str | None,
) -> list[str]:
    p = _safe_prompt(prompt)
    extra = shlex.split(os.environ.get(f"AI_BRIDGE_{agent.upper()}_EXTRA_ARGS", ""))

    if agent == "claude":
        argv = [binary, "-p", "--output-format", "stream-json", "--verbose"]
        if session_id:
            argv += ["--resume", session_id]
        argv += {
            "full": ["--dangerously-skip-permissions"],
            "edit": ["--permission-mode", "acceptEdits"],
            "readonly": ["--permission-mode", "plan"],
        }[permission]
        if model:
            argv += ["--model", model]
        return argv + extra + [p]

    if agent == "codex":
        perm = {
            "full": ["--dangerously-bypass-approvals-and-sandbox"],
            "edit": ["--sandbox", "workspace-write", "-c", 'approval_policy="never"'],
            "readonly": ["--sandbox", "read-only", "-c", 'approval_policy="never"'],
        }[permission]
        common = ["--json", "--skip-git-repo-check"]
        if model:
            common += ["-m", model]
        if session_id:
            return [binary, "exec"] + perm + ["resume"] + common + extra + [session_id, p]
        return [binary, "exec"] + perm + common + extra + [p]

    if agent == "cursor":
        argv = [binary, "-p", "--output-format", "stream-json", "--trust"]
        if session_id:
            argv += ["--resume", session_id]
        argv += {"full": ["--force"], "edit": ["--force"], "readonly": ["--mode", "ask"]}[permission]
        if model:
            argv += ["--model", model]
        return argv + extra + ["--", prompt]

    raise ValueError(f"Unknown agent: {agent}")


# ---------------------------------------------------------------------------
# Output parsing (stream-json for claude/cursor, JSONL events for codex)
# ---------------------------------------------------------------------------


def _blocks_text(content) -> tuple[list[str], list[str]]:
    """Return (texts, tool_summaries) from a message content field."""
    texts, tools = [], []
    if isinstance(content, str):
        return [content], []
    for b in content or []:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text" and b.get("text"):
            texts.append(b["text"])
        elif t == "tool_use":
            tools.append(_tool_summary(b.get("name", "tool"), b.get("input")))
    return texts, tools


def _tool_summary(name: str, inp) -> str:
    detail = ""
    if isinstance(inp, dict):
        for key in ("command", "cmd", "file_path", "path", "pattern", "url", "query", "description"):
            if inp.get(key):
                detail = str(inp[key])
                break
        if not detail and inp:
            detail = json.dumps(inp)[:200]
    elif isinstance(inp, str):
        detail = inp
    detail = " ".join(detail.split())[:200]
    return f"[{name}] {detail}".strip()


def parse_output(agent: str, log: str) -> dict:
    """Extract session id, final answer, progress lines and errors from a job log."""
    session_id = None
    reported_model = None
    final = None
    is_error = False
    progress: list[str] = []
    messages: list[str] = []
    raw_other: list[str] = []
    stats: dict = {}

    for line in log.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            raw_other.append(line)
            continue
        if not isinstance(ev, dict):
            continue
        t = ev.get("type")

        if agent in ("claude", "cursor"):
            session_id = ev.get("session_id") or ev.get("chatId") or session_id
            if t == "assistant":
                candidate = _model_name((ev.get("message") or {}).get("model"))
                if candidate:
                    reported_model = candidate
                texts, tools = _blocks_text((ev.get("message") or {}).get("content"))
                for x in texts:
                    messages.append(x)
                    progress.append("[text] " + " ".join(x.split())[:200])
                progress.extend(tools)
            elif t == "tool_call":
                if ev.get("subtype") == "started":
                    tc = ev.get("tool_call") or {}
                    name = next(iter(tc), "tool") if isinstance(tc, dict) else "tool"
                    args = (tc.get(name) or {}).get("args") if isinstance(tc, dict) else None
                    progress.append(_tool_summary(name.replace("ToolCall", ""), args))
            elif t == "result":
                final = ev.get("result")
                is_error = is_error or bool(ev.get("is_error")) or ev.get("subtype") not in (None, "success")
                if ev.get('is_error') or ev.get('subtype') not in (None, 'success'):
                    # Some CLIs report provider errors in result.errors with exit 0.
                    for key in ('errors', 'error', 'message'):
                        if ev.get(key):
                            raw_other.append(json.dumps(ev[key], ensure_ascii=False))
                for k in ("num_turns", "duration_ms", "total_cost_usd"):
                    if k in ev:
                        stats[k] = ev[k]
            elif t == "error":
                is_error = True
                raw_other.append(json.dumps(ev)[:500])

        elif agent == "codex":
            if t == "thread.started":
                session_id = ev.get("thread_id") or session_id
            elif t in ("item.completed", "item.started"):
                item = ev.get("item") or {}
                it = item.get("type") or item.get("item_type")
                if t == "item.completed" and it in ("agent_message", "assistant_message"):
                    text = item.get("text") or ""
                    messages.append(text)
                    progress.append("[text] " + " ".join(text.split())[:200])
                elif t == "item.started" and it == "command_execution":
                    progress.append(_tool_summary("shell", item.get("command")))
                elif t == "item.completed" and it == "file_change":
                    changes = item.get("changes") or []
                    paths = ", ".join(c.get("path", "?") for c in changes if isinstance(c, dict))
                    progress.append(f"[edit] {paths}"[:220])
                elif t == "item.started" and it in ("mcp_tool_call", "web_search"):
                    progress.append(f"[{it}] {item.get('tool') or item.get('query') or ''}"[:220])
                elif it == "error":
                    raw_other.append(item.get("message", ""))
            elif t == "turn.completed":
                stats["usage"] = ev.get("usage")
            elif t in ("turn.failed", "error"):
                is_error = True
                err = ev.get("error") or ev.get("message") or ev
                raw_other.append(json.dumps(err)[:500] if not isinstance(err, str) else err)

    if final is None and messages:
        final = messages[-1]
    return {
        "session_id": session_id,
        "reported_model": reported_model,
        "final": final,
        "is_error": is_error,
        "progress": progress,
        "other": raw_other,
        "stats": stats,
    }


# ---------------------------------------------------------------------------
# Session listing and history
# ---------------------------------------------------------------------------


def _iter_jsonl(path: Path, max_lines: int | None = None):
    try:
        with open(path, errors="replace") as f:
            for i, line in enumerate(f):
                if max_lines is not None and i >= max_lines:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        yield obj
                except ValueError:
                    continue
    except OSError:
        return


def _short(text: str, n: int = 160) -> str:
    return " ".join((text or "").split())[:n]


def _is_noise_user_text(text: str) -> bool:
    t = text.lstrip()
    return t.startswith(
        (
            "# AGENTS.md instructions",
            "<user_info>",
            "<environment_context>",
            "<user_instructions>",
            "<command-",
            "<local-command",
            "Caveat:",
            "<system-reminder>",
        )
    )


# ---- Claude Code ----------------------------------------------------------


def _claude_user_text(entry: dict) -> str | None:
    msg = entry.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        return None if _is_noise_user_text(content) else content
    texts = [b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text"]
    text = "\n".join(t for t in texts if t and not _is_noise_user_text(t))
    return text or None


def claude_sessions(cwd: str | None, limit: int | None) -> list[dict]:
    files = glob.glob(str(_claude_dir() / "projects" / "*" / "*.jsonl"))
    files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    out = []
    for f in files:
        sid = Path(f).stem
        first, scwd, summary, n = None, None, None, 0
        mode, native_source = "unknown", None
        for e in _iter_jsonl(Path(f), max_lines=400):
            if e.get("type") in ("summary", "custom-title"):
                summary = e.get("customTitle") or e.get("summary") or summary
            if e.get("isSidechain"):
                continue
            if e.get('entrypoint') == 'sdk-cli' or e.get('promptSource') == 'sdk':
                mode, native_source = 'noninteractive', 'sdk-cli'
            scwd = scwd or e.get("cwd")
            if e.get("type") == "user" and first is None and not e.get("isMeta"):
                first = _claude_user_text(e)
            if e.get("type") in ("user", "assistant"):
                n += 1
        if cwd and (not scwd or os.path.realpath(scwd) != os.path.realpath(os.path.expanduser(cwd))):
            continue
        out.append(
            {
                "session_id": sid,
                "cwd": scwd,
                "updated": time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(f))),
                "title": _short(summary or first or "(no prompt)"),
                "messages_seen": n,
                "source": "claude_local",
                "mode": mode, "native_source": native_source,
                "mode_scope": "sdk_marker_in_scanned_transcript_prefix",
                "session_type": "unknown", "provenance": "native_transcript",
                **_saved_model(Path(f), 'claude'),
                "resumable": bool(scwd and Path(scwd).is_dir()),
                "storage_path": f,
                "owner_uid": os.stat(f).st_uid,
            }
        )
        if limit is not None and len(out) >= limit:
            break
    return out


def claude_history(session_id: str) -> list[dict]:
    files = glob.glob(str(_claude_dir() / "projects" / "*" / f"{glob.escape(session_id)}.jsonl"))
    if not files:
        raise KeyError(f"Claude Code session not found: {session_id}")
    msgs = []
    for e in _iter_jsonl(Path(files[0])):
        if e.get("isSidechain") or e.get("isMeta"):
            continue
        if e.get("type") == "user":
            text = _claude_user_text(e)
            if text:
                msgs.append({"role": "user", "text": text, "time": e.get("timestamp")})
        elif e.get("type") == "assistant":
            texts, tools = _blocks_text((e.get("message") or {}).get("content"))
            if texts:
                msgs.append({"role": "assistant", "text": "\n".join(texts), "time": e.get("timestamp")})
            for t in tools:
                msgs.append({"role": "tool", "text": t, "time": e.get("timestamp")})
    return msgs


# ---- Codex ----------------------------------------------------------------


def _codex_files() -> list[str]:
    files = glob.glob(str(_codex_dir() / "sessions" / "**" / "rollout-*.jsonl"), recursive=True)
    files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return files


def _codex_entry_messages(e: dict, have_events: bool = False):
    """Decode response items and legacy message events (callers select one source)."""
    p = e.get("payload") or {}
    pt = p.get("type")
    if e.get("type") == "event_msg":
        if pt == "user_message" and p.get("message") and not _is_noise_user_text(p["message"]):
            yield "user", p["message"]
        elif pt == "agent_message" and p.get("message"):
            yield "assistant", p["message"]
        elif pt == "item_completed":
            item = p.get("item") or {}
            role = {"UserMessage": "user", "AgentMessage": "assistant"}.get(item.get("type"))
            if role:
                text = "\n".join(b.get("text", "") for b in item.get("content", []) if isinstance(b, dict))
                if text and not (role == "user" and _is_noise_user_text(text)):
                    yield role, text
    elif e.get("type") == "response_item":
        if pt in ("function_call", "custom_tool_call", "local_shell_call"):
            args = p.get("arguments") or p.get("input") or p.get("action")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    pass
            if isinstance(args, dict) and isinstance(args.get("command"), list):
                args = {"command": " ".join(map(str, args["command"]))}
            yield "tool", _tool_summary(p.get("name") or pt, args)
        elif pt == "message":
            texts = [c.get("text", "") for c in p.get("content") or []
                     if isinstance(c, dict) and c.get("type") in ("input_text", "output_text")]
            text = "\n".join(t for t in texts if t and not _is_noise_user_text(t))
            if text and p.get("role") in ("user", "assistant"):
                yield p["role"], text


def _codex_messages(entries):
    # Choose the canonical response items per turn and role. Unrelated events
    # (token counts, task_started) must not suppress actual messages.
    def decode(turn):
        response_roles = {role for e in turn if e.get("type") == "response_item"
                          for role, _ in _codex_entry_messages(e)}
        for e in turn:
            for role, text in _codex_entry_messages(e):
                if e.get("type") == "event_msg" and role in response_roles:
                    continue
                yield {"role": role, "text": text, "time": e.get("timestamp")}
    turn = []
    for e in entries:
        if e.get("type") == "event_msg" and (e.get("payload") or {}).get("type") == "task_started":
            yield from decode(turn)
            turn = []
        turn.append(e)
    yield from decode(turn)


def codex_sessions(cwd: str | None, limit: int | None) -> list[dict]:
    out = []
    titles = {e.get("id"): e.get("thread_name") for e in _iter_jsonl(_codex_dir() / "session_index.jsonl")}
    for f in _codex_files():
        sid, scwd, first, parent_session_id = None, None, None, None
        native_source, thread_source = None, None
        for e in _iter_jsonl(Path(f), max_lines=300):
            p = e.get("payload") or {}
            if e.get("type") == "session_meta":
                native_source = p.get("source")
                thread_source = p.get("thread_source")
                sid = p.get("id") or sid
                scwd = p.get("cwd") or scwd
                parent_session_id = p.get("parent_thread_id") or parent_session_id
                if not parent_session_id and isinstance(native_source, dict):
                    subagent = native_source.get('subagent')
                    spawn = subagent.get('thread_spawn') if isinstance(subagent, dict) else None
                    if isinstance(spawn, dict):
                        parent_session_id = spawn.get('parent_thread_id')
            if first is None:
                for role, text in _codex_entry_messages(e, have_events=True):
                    if role == "user":
                        first = text
                        break
            if sid and first:
                break
        if not sid:
            sid = Path(f).stem[-36:]
        if cwd and (not scwd or os.path.realpath(scwd) != os.path.realpath(os.path.expanduser(cwd))):
            continue
        out.append(
            {
                "session_id": sid,
                "parent_session_id": parent_session_id,
                "cwd": scwd,
                "updated": time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(f))),
                "title": _short(titles.get(sid) or first or "(no prompt)"),
                "source": "codex_local",
                "native_source": native_source, "thread_source": thread_source,
                "mode_scope": "native_session_origin",
                "mode": ('noninteractive' if native_source == 'exec' else
                         'interactive' if native_source in ('cli', 'vscode') else 'unknown'),
                "session_type": 'subagent' if parent_session_id or (isinstance(native_source, dict) and 'subagent' in native_source) else
                                'main' if thread_source == 'user' else 'unknown',
                "provenance": "native_session_meta",
                **_saved_model(Path(f), 'codex'),
                "resumable": bool(scwd and Path(scwd).is_dir()),
                "storage_path": f,
                "owner_uid": os.stat(f).st_uid,
            }
        )
        if limit is not None and len(out) >= limit:
            break
    return out


def codex_history(session_id: str) -> list[dict]:
    info = session_info("codex", session_id)
    return list(_codex_messages(_iter_jsonl(Path(info["storage_path"]))))


# ---- Cursor CLI -----------------------------------------------------------


def cursor_sessions(cwd: str | None, limit: int | None) -> list[dict]:
    dbs = list(_cursor_chats_dir().glob("*/*/store.db"))
    dbs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    out = []
    for db in dbs:
        row = {
            "session_id": db.parent.name,
            "cwd": None,
            "updated": time.strftime("%Y-%m-%d %H:%M", time.localtime(db.stat().st_mtime)),
            "title": "(unreadable chat)",
            "source": "cursor_cli",
            "mode": "unknown", "mode_scope": "unverifiable",
            "session_type": "main", "provenance": "native_cli_store",
            "last_used_model": None,
            "model_source": None,
            "resumable": False,
            "storage_path": str(db),
            "owner_uid": db.stat().st_uid,
        }
        try:
            with _cursor_store(db) as store:
                if store.meta.get("subagentInfo"):
                    continue
                row.update(cwd=store.workspace(), title=_short(store.meta.get("name") or "(untitled chat)"))
                model = _model_name(store.meta.get('lastUsedModel'))
                row.update(last_used_model=model, model_source='cursor.meta.lastUsedModel' if model else None)
                row["resumable"] = store.can_resume(row["cwd"])
                if not row["resumable"]:
                    row["resume_note"] = "Project path does not match this CLI store, or the chat is empty. No automatic migration is attempted."
                row["history_scope"] = "active structured turns and any saved compaction summary"
                if store.root.get(6) or store.root.get(13):
                    row["history_note"] = "Cursor compacted this chat; older turns may only remain as summaries."
        except Exception as e:  # Optional reader: schema/import failures must stay local.
            row["error"] = str(e)
        if cwd and (not row["cwd"] or os.path.realpath(row["cwd"]) != os.path.realpath(os.path.expanduser(cwd))):
            continue
        out.append(row)
        if limit is not None and len(out) >= limit:
            break
    return out


def cursor_history(session_id: str) -> list[dict]:
    info = session_info("cursor", session_id)
    with _cursor_store(Path(info["storage_path"])) as store:
        return store.messages()


def list_sessions(agent: str, cwd: str | None, limit: int | None) -> list[dict]:
    return {"claude": claude_sessions, "codex": codex_sessions, "cursor": cursor_sessions}[agent](cwd, limit)


def session_info(agent: str, session_id: str) -> dict:
    # Exact IDs only: CLI fuzzy names / --last can silently choose another chat.
    if not re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", session_id):
        raise ValueError("Use the complete session_id returned by agent_sessions.")
    matches = [row for row in list_sessions(agent, None, None) if row["session_id"] == session_id]
    if not matches:
        raise KeyError(f"{agent} session not found locally: {session_id}. No new chat was started.")
    if len(matches) != 1:
        raise ValueError(f"Ambiguous {agent} session ID: {session_id}")
    return matches[0]


def resume_cwd(agent: str, session_id: str, cwd: str | None) -> str:
    info = session_info(agent, session_id)
    if not info.get("resumable"):
        raise ValueError(info.get("resume_note") or "This chat cannot be resumed safely: its CLI state or project path is unavailable.")
    project = info["cwd"]
    if cwd and os.path.realpath(os.path.expanduser(cwd)) != os.path.realpath(project):
        raise ValueError(f"Session belongs to {project}; refusing to resume it in a different project.")
    if not Path(project).is_dir():
        raise ValueError(f"Session project directory no longer exists: {project}")
    return project


def history(agent: str, session_id: str) -> list[dict]:
    session_info(agent, session_id)
    return {"claude": claude_history, "codex": codex_history, "cursor": cursor_history}[agent](session_id)

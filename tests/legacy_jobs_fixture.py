"""Regression fixture: original pre-hardening Jobs listing/storage methods.

Copied without algorithm changes from the verified 2026-10-16 recovery baseline.
No CLI launch/cancellation methods: this fixture only reads temporary test state.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import tempfile
import time

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


class LegacyJobs:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.dir = cfg.state_dir / "jobs"


    def _meta_path(self, job_id: str) -> Path:
        if not re.fullmatch(r"[a-z0-9-]{4,40}", job_id):
            raise ValueError(f"Bad job id: {job_id}")
        return self.dir / f"{job_id}.json"


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
        if meta.get("status") == "running":
            ef = self.exit_path(meta["id"])
            if ef.exists():
                try:
                    meta["exit_code"] = int(ef.read_text().strip() or "-1")
                except ValueError:
                    meta["exit_code"] = -1
                meta["status"] = "done" if meta["exit_code"] == 0 else "failed"
                meta["ended"] = meta.get("ended") or ef.stat().st_mtime
                self.save(meta)
            elif not pid_alive(meta["pid"]):
                meta["status"] = "lost"
                meta["ended"] = time.time()
                self.save(meta)
        return meta


    def list(self, limit: int = 30, kind: str | None = None) -> list[dict]:
        metas = []
        for p in sorted(self.dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                m = self._refresh(json.loads(p.read_text()))
            except (OSError, ValueError, KeyError):
                continue
            if kind and m.get("kind") != kind:
                continue
            metas.append(m)
            if len(metas) >= limit:
                break
        return metas

"""Keep auxiliary records out of legacy Jobs.list()'s root *.json scan."""
import os
from pathlib import Path
import re
import stat


KINDS = ('process', 'output', 'outcome')


def sidecar_path(root: Path, job_id: str, kind: str, *, create=False) -> Path:
    if not re.fullmatch(r'[a-z0-9-]{4,40}', job_id) or kind not in KINDS:
        raise ValueError('Invalid job sidecar identifier or kind.')
    directory = root/'.sidecars'
    if create:
        directory.mkdir(mode=0o700, exist_ok=True)
    if directory.exists() or directory.is_symlink():
        if not stat.S_ISDIR(directory.lstat().st_mode):
            raise ValueError('Job sidecar directory must be a real directory, not a symlink.')
        if create:
            os.chmod(directory, 0o700)
    return directory/(job_id+'.'+kind+'.json')


def readable_sidecar(root: Path, job_id: str, kind: str) -> Path:
    """Read-only fallback for the superseded, never-deployed candidate layout.

    Never move live files or adopt an old PID. Root-layout files need coordinated
    quarantine before rolling back to an original bridge that scans *.json.
    """
    path = sidecar_path(root, job_id, kind)
    return path if path.exists() else root/(job_id+'.'+kind+'.json')

"""Small privileged/unprivileged file helper. JSON input; never shell interpolation."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile


def digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(65536), b''):
            h.update(block)
    return h.hexdigest()


def read_text(path, start_line=1, max_lines=400, max_bytes=262144, include_hash=False):
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    start = max(1, start_line)
    chunks = []
    truncated = False
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError('read_file supports regular files; use shell tools for devices/pipes.')
        skipped = 0
        # Skip lines without accumulating an arbitrarily long line in memory.
        while skipped < start-1:
            part = stream.readline(65536)
            if not part:
                break
            if part.endswith(b'\n'):
                skipped += 1
        budget = max_bytes
        for _ in range(max(1, min(max_lines, 3000))):
            if budget <= 0:
                truncated = True
                break
            part = stream.readline(budget+1)
            if not part:
                break
            if len(part)>budget:
                part = part[:budget]
                truncated = True
            chunks.append(part)
            budget -= len(part)
            if truncated:
                break
        more = stream.tell() < before.st_size
        sha = None
        if include_hash:
            stream.seek(0)
            h = hashlib.sha256()
            for block in iter(lambda: stream.read(65536), b''):
                h.update(block)
            after = os.fstat(stream.fileno())
            if (before.st_size,before.st_mtime_ns,before.st_ctime_ns) != (after.st_size,after.st_mtime_ns,after.st_ctime_ns):
                raise ValueError('File changed while reading/hash computation; read it again.')
            sha = h.hexdigest()
    data = b''.join(chunks)
    if b'\0' in data[:8000]:
        raise ValueError('File looks binary. Use shell tools to inspect it.')
    lines = data.decode(errors='replace').splitlines()
    body = '\n'.join(f'{i:>6}\t{line}' for i,line in enumerate(lines,start))
    footer = f'\n\n[lines {start}-{start+len(lines)-1}; '+('captured prefix; file byte limit reached' if truncated else 'more lines available' if more else 'end of file')+']'
    if sha:
        footer += '\n[sha256: '+sha+']'
    return body+footer


def write_text(path, content, append=False, make_dirs=True, atomic=False,
               expected_sha256=None, backup=False, mode=None):
    path = Path(path)
    if mode is not None and (not isinstance(mode, int) or not 0 <= mode <= 0o777):
        raise ValueError('mode must be an integer between 0 and 511 (0777).')
    if (expected_sha256 is not None or backup) and not atomic:
        raise ValueError('expected_sha256 and backup require atomic=true.')
    if append and atomic:
        raise ValueError('Atomic replacement cannot be combined with append.')
    if make_dirs:
        path.parent.mkdir(parents=True, exist_ok=True)
    data = content.encode('utf-8')
    if not atomic:
        if mode is not None and path.exists():
            raise ValueError('mode for an existing file requires atomic=true; default writes preserve permissions.')
        flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else os.O_TRUNC)
        fd = os.open(path, flags, mode if mode is not None else 0o666)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
        return {'ok': True, 'path': str(path), 'bytes_written': len(data)}
    if path.parent.resolve() != path.parent.absolute():
        raise ValueError('Atomic writes reject symlinked parent paths.')
    dirfd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary = None
    backup_path = None
    try:
        # Serializes bridge writers in this directory, not unrelated editors.
        fcntl.flock(dirfd, fcntl.LOCK_EX)
        original = path.lstat() if path.exists() or path.is_symlink() else None
        if original and (not stat.S_ISREG(original.st_mode) or original.st_nlink != 1):
            raise ValueError('Atomic writes require a regular, non-symlink, single-link file.')
        if original and os.listxattr(path, follow_symlinks=False):
            raise ValueError('Atomic replacement of files with ACLs/xattrs is unsupported; use native tools.')
        if expected_sha256 is not None:
            if not original or digest(path) != expected_sha256:
                raise ValueError('Content hash mismatch; file was not changed.')
        if backup and original:
            fd, name = tempfile.mkstemp(prefix='.'+path.name+'.ai-bridge-backup-', dir=path.parent)
            backup_path = name
            with os.fdopen(fd, 'wb') as target, path.open('rb') as source:
                for block in iter(lambda: source.read(65536), b''):
                    target.write(block)
                target.flush()
                os.fsync(target.fileno())
        fd, temporary = tempfile.mkstemp(prefix='.'+path.name+'.ai-bridge-', dir=path.parent)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            if original:
                os.fchown(stream.fileno(), original.st_uid, original.st_gid)
            os.fchmod(stream.fileno(), mode if mode is not None else (stat.S_IMODE(original.st_mode) if original else 0o600))
            stream.flush()
            os.fsync(stream.fileno())
        current = path.lstat() if path.exists() or path.is_symlink() else None
        signature = lambda st: (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns) if st else None
        if signature(current) != signature(original) or (expected_sha256 is not None and digest(path) != expected_sha256):
            raise ValueError('File changed during preparation; replacement cancelled.')
        os.replace(temporary, path)
        temporary = None
        os.fsync(dirfd)
        return {'ok': True, 'path': str(path), 'bytes_written': len(data),
                'sha256': hashlib.sha256(data).hexdigest(), 'backup_path': backup_path,
                'atomic': True, 'concurrency': 'optimistic check; unrelated external writers do not share the lock'}
    finally:
        if temporary:
            os.unlink(temporary)
        os.close(dirfd)


def main():
    try:
        request = json.load(sys.stdin)
        operation = request.pop('operation')
        result = read_text(**request) if operation == 'read' else write_text(**request)
        print(json.dumps({'result': result}, ensure_ascii=False))
    except Exception as error:
        print(json.dumps({'error': str(error)}, ensure_ascii=False))


if __name__ == '__main__':
    main()

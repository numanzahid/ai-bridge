"""Read-only ownership checks for native session preflight."""
import grp
import os
from pathlib import Path
import pwd
import stat


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



def native_session_storage_safe(path, uid):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        return False
    try:
        metadata = path.lstat()
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != uid or
                metadata.st_mode & 0o002):
            return False
        if metadata.st_mode & 0o020:
            if not private_primary_group(uid, metadata.st_gid):
                return False
            attributes = os.listxattr(path, follow_symlinks=False)
            if {'system.posix_acl_access', 'system.posix_acl_default'}.intersection(attributes):
                return False
        return True
    except (OSError, ValueError):
        return False

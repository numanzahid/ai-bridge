"""Receipt launcher: confirm target creation and effective identity after authentication."""
import json
import os
import subprocess
import sys


def main():
    receipt, argv = sys.argv[1], sys.argv[2:]
    try:
        process = subprocess.Popen(argv)
    except OSError as error:
        print(str(error), file=sys.stderr)
        return 127
    # The invoking user owns the private receipt directory and precreated file.
    # Writing no secrets here lets the parent distinguish denial from target failure.
    fd = os.open(receipt, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'w') as stream:
        json.dump({'target_started': True, 'effective_uid': os.geteuid(),
                   'effective_root': os.geteuid() == 0}, stream)
    return process.wait()


if __name__ == '__main__':
    sys.exit(main())

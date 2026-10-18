#!/usr/bin/env python3
"""Bump the AI Bridge version and roll CHANGELOG.md's Unreleased notes into it. Never runs git."""
import argparse
import datetime
from pathlib import Path
import re
import sys

VERSION_LINE = re.compile(r'^__version__ = "(\d+)\.(\d+)\.(\d+)"$', re.M)
UNRELEASED = '## [Unreleased]'


def parse(text):
    """Return the version tuple declared in ai_bridge/__init__.py source text."""
    match = VERSION_LINE.search(text)
    if not match:
        raise ValueError('ai_bridge/__init__.py must declare __version__ = "MAJOR.MINOR.PATCH".')
    return tuple(int(part) for part in match.groups())


def text(version):
    return '.'.join(str(part) for part in version)


def bumped(version, part):
    major, minor, patch = version
    return {'major': (major+1, 0, 0), 'minor': (major, minor+1, 0), 'patch': (major, minor, patch+1)}[part]


def unreleased_notes(changelog):
    if UNRELEASED not in changelog:
        raise ValueError(f'CHANGELOG.md must contain a "{UNRELEASED}" section.')
    body = changelog.split(UNRELEASED, 1)[1].split('\n## [', 1)[0]
    return body.strip()


def check(source):
    """Validate that the declared version has a dated CHANGELOG entry."""
    version = text(parse((source/'ai_bridge/__init__.py').read_text()))
    changelog = (source/'CHANGELOG.md').read_text()
    if not re.search(r'^## \['+re.escape(version)+r'\] - \d{4}-\d{2}-\d{2}$', changelog, re.M):
        raise ValueError(f'CHANGELOG.md has no dated "## [{version}] - YYYY-MM-DD" entry.')
    unreleased_notes(changelog)
    return version


def bump(source, part, today=None):
    init, log = source/'ai_bridge/__init__.py', source/'CHANGELOG.md'
    init_text, changelog = init.read_text(), log.read_text()
    if not unreleased_notes(changelog):
        raise ValueError('Describe the changes under "## [Unreleased]" in CHANGELOG.md before bumping.')
    new = text(bumped(parse(init_text), part))
    day = (today or datetime.date.today()).isoformat()
    log.write_text(changelog.replace(UNRELEASED, f'{UNRELEASED}\n\n## [{new}] - {day}', 1))
    init.write_text(VERSION_LINE.sub(f'__version__ = "{new}"', init_text, count=1))
    return new


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('show', help='Print the declared version.')
    commands.add_parser('check', help='Validate the version and its CHANGELOG entry.')
    step = commands.add_parser('bump', help='Increase the version and date the Unreleased notes.')
    step.add_argument('part', choices=('major', 'minor', 'patch'))
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args(argv)
    if args.command == 'show':
        print(text(parse((args.source/'ai_bridge/__init__.py').read_text())))
    elif args.command == 'check':
        print(check(args.source))
    else:
        version = bump(args.source, args.part)
        print(version)
        print(f'Next: review the diff, commit "Release {version}", tag v{version}, then stage.', file=sys.stderr)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError) as error:
        print('ERROR: '+str(error), file=sys.stderr)
        raise SystemExit(1)

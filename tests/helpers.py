"""Shared test fixtures."""
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

SID = '11111111-2222-4333-8444-555555555555'
OTHER = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'

# Importing server constructs its config; keep all test state temporary.
STATE = tempfile.TemporaryDirectory(prefix='ai-bridge-unit-')
with patch.dict(os.environ, {'AI_BRIDGE_TOKEN': 'unit-test-token-' * 4, 'AI_BRIDGE_STATE_DIR': STATE.name,
                            'AI_BRIDGE_CONFIG_DIR': str(Path(STATE.name) / 'config')}):
    from ai_bridge import server


def install_section(start, end):
    """Return the install.sh text between two marker strings, failing clearly if either moved."""
    source = Path('install.sh').read_text()
    if start not in source:
        raise AssertionError(f'install.sh marker not found: {start!r}')
    tail = source.split(start, 1)[1]
    if end not in tail:
        raise AssertionError(f'install.sh marker not found after {start!r}: {end!r}')
    return tail.split(end, 1)[0]


def collect_pages(test, fetch, key, *, newest_first=False):
    """Follow next_offset until exhausted, asserting each page makes progress."""
    found, offset = [], 0
    while offset is not None:
        page = fetch(offset)
        test.assertTrue(page[key])
        found = page[key] + found if newest_first else found + page[key]
        if page['next_offset'] is not None:
            test.assertGreater(page['next_offset'], offset)
        offset = page['next_offset']
    return found

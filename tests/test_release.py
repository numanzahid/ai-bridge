import datetime
from pathlib import Path
import tempfile
import unittest

import release

import ai_bridge
from helpers import server


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.source = Path(self.tmp.name)
        (self.source/'ai_bridge').mkdir()
        (self.source/'ai_bridge/__init__.py').write_text('"""Package."""\n\n__version__ = "1.4.9"\n')
        self.changelog = self.source/'CHANGELOG.md'
        self.changelog.write_text('# Changelog\n\n## [Unreleased]\n\n### Fixed\n- A fix.\n\n## [1.4.9] - 2026-01-01\n\nOld notes.\n')

    def test_bump_parts_date_unreleased_notes_and_update_version(self):
        for part, expected in (('patch', '1.4.10'), ('minor', '1.5.0'), ('major', '2.0.0')):
            with self.subTest(part=part):
                self.assertEqual(release.text(release.bumped((1, 4, 9), part)), expected)
        self.assertEqual(release.bump(self.source, 'minor', datetime.date(2026, 2, 3)), '1.5.0')
        self.assertIn('__version__ = "1.5.0"', (self.source/'ai_bridge/__init__.py').read_text())
        text = self.changelog.read_text()
        self.assertIn('## [Unreleased]\n\n## [1.5.0] - 2026-02-03\n\n### Fixed\n- A fix.', text)
        self.assertIn('## [1.4.9] - 2026-01-01', text)
        self.assertEqual(release.check(self.source), '1.5.0')

    def test_bump_refuses_empty_notes_and_check_requires_dated_entry(self):
        self.changelog.write_text('# Changelog\n\n## [Unreleased]\n\n## [1.4.9] - 2026-01-01\n')
        before = (self.source/'ai_bridge/__init__.py').read_text()
        with self.assertRaisesRegex(ValueError, 'Unreleased'):
            release.bump(self.source, 'patch')
        self.assertEqual((self.source/'ai_bridge/__init__.py').read_text(), before)
        self.changelog.write_text('# Changelog\n\n## [Unreleased]\n')
        with self.assertRaisesRegex(ValueError, 'no dated'):
            release.check(self.source)
        (self.source/'ai_bridge/__init__.py').write_text('__version__ = "1.4"\n')
        with self.assertRaisesRegex(ValueError, 'MAJOR.MINOR.PATCH'):
            release.check(self.source)

    def test_checkout_version_has_changelog_entry_and_is_served(self):
        # The checkout, or a staged release's validation copy, which includes CHANGELOG.md.
        root = Path(__file__).resolve().parents[1]
        self.assertEqual(release.check(root), ai_bridge.__version__)
        self.assertEqual(server.mcp.version, ai_bridge.__version__)


if __name__ == '__main__':
    unittest.main()

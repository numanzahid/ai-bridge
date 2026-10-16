import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_bridge import catalog


class CatalogTests(unittest.TestCase):
    def test_documents_are_metadata_only_and_edits_are_discovered(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'projects.md').write_text('# Projects\n\nBODY MUST NOT BE RETURNED\n')
            (root / 'credentials.json').write_text('{}')
            rows = catalog.discover([root])
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['name'], 'Projects')
            self.assertEqual(rows[0]['description'], '')
            self.assertNotIn('BODY MUST NOT BE RETURNED', str(rows))
            (root / 'documents.txt').write_text('New document')
            self.assertEqual(len(catalog.discover([root])), 2)

    def test_skill_frontmatter_and_linked_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skills, actual = root / 'skills', root / 'actual'
            skills.mkdir()
            actual.mkdir()
            (actual / 'SKILL.md').write_text('---\nname: useful\ndescription: >-\n  First line\n  second line.\n---\n\nFULL WORKFLOW BODY\n')
            (actual / 'reference.md').write_text('not a skill entry')
            (skills / 'linked').symlink_to(actual, target_is_directory=True)
            (actual / 'cycle').symlink_to(skills, target_is_directory=True)
            rows = catalog.discover([skills, actual], skills=True)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['name'], 'useful')
            self.assertEqual(rows[0]['description'], 'First line second line.')
            self.assertNotIn('FULL WORKFLOW BODY', str(rows))

    def test_quoted_description_and_missing_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'guide.md').write_text('---\ntitle: "Folder guide"\ndescription: "Use for \\"documents\\"."\n---\n')
            rows = catalog.discover([root, root / 'missing'])
            self.assertEqual(rows[0]['description'], 'Use for "documents".')
            self.assertEqual(rows[0]['name'], 'Folder guide')

    def test_custom_directories(self):
        with patch.dict(os.environ, {'XDG_CONFIG_HOME': '/tmp/bridge-config', 'AI_BRIDGE_CONFIG_DIR': '', 'AI_BRIDGE_SKILL_DIRS': '/tmp/a:/tmp/b'}):
            self.assertEqual(catalog.instructions_dir(), Path('/tmp/bridge-config/ai-bridge/instructions'))
            self.assertEqual(catalog.skill_dirs(), [Path('/tmp/a'), Path('/tmp/b')])
            with patch.dict(os.environ, {'AI_BRIDGE_CONFIG_DIR': '/tmp/explicit-bridge-config'}):
                self.assertEqual(catalog.instructions_dir(), Path('/tmp/explicit-bridge-config/instructions'))

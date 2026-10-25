"""Local-only guidance preserves existing files and survives repeated installs."""
from pathlib import Path
import stat
import tempfile
import unittest

from ai_bridge import local_instructions as local


class LocalInstructionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)

    def test_preserves_existing_guidance_and_private_backup_without_duplicates(self):
        path = self.home / 'AGENTS.md'
        original = 'Personal instructions.\nKeep all of this.\n'
        path.write_text(original)
        path.chmod(0o640)
        result = local.update_file(path)
        self.assertTrue(path.read_text().startswith(original))
        self.assertEqual(Path(result['backup']).read_text(), original)
        self.assertEqual(stat.S_IMODE(Path(result['backup']).stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)
        first = path.read_bytes()
        self.assertEqual(local.update_file(path)['status'], 'unchanged')
        self.assertEqual(path.read_bytes(), first)
        self.assertEqual(len(list(path.parent.glob('AGENTS.md.before-ai-bridge-*'))), 1)

    def test_replaces_only_marked_section_and_preserves_surrounding_content(self):
        path = self.home / 'CLAUDE.md'
        path.write_text(f'Before\n{local.START}\nOld preference\n{local.END}\nAfter\n')
        local.update_file(path)
        self.assertEqual(path.read_text(), f'Before\n{local.BLOCK.rstrip()}\nAfter\n')

    def test_installs_both_agents_and_updates_active_codex_override(self):
        override = self.home / '.codex/AGENTS.override.md'
        override.parent.mkdir()
        override.write_text('Override guidance\n')
        results = local.install(self.home, {})
        self.assertEqual(len(results), 3)
        self.assertIn('Override guidance', override.read_text())
        for path in (self.home / '.codex/AGENTS.md', override, self.home / '.claude/CLAUDE.md'):
            self.assertEqual(path.read_text().count(local.START), 1)
            self.assertIn('does not tell chatgpt.com or claude.ai to avoid MCP', path.read_text())

    def test_honors_custom_agent_homes(self):
        codex, claude = self.home / 'codex-custom', self.home / 'claude-custom'
        local.install(self.home, {'CODEX_HOME': str(codex), 'CLAUDE_CONFIG_DIR': str(claude)})
        self.assertTrue((codex / 'AGENTS.md').is_file())
        self.assertTrue((claude / 'CLAUDE.md').is_file())
        self.assertFalse((self.home / '.codex').exists())

    def test_rejects_symlinks_and_invalid_markers_without_modifying_files(self):
        target = self.home / 'target'
        target.write_text('Untouched')
        path = self.home / 'AGENTS.md'
        path.symlink_to(target)
        with self.assertRaisesRegex(ValueError, 'non-regular'):
            local.update_file(path)
        self.assertEqual(target.read_text(), 'Untouched')
        for content in (local.START, local.END, local.END + local.START,
                        local.START + local.END + local.START + local.END):
            with self.subTest(content=content):
                target.write_text(content)
                with self.assertRaisesRegex(ValueError, 'markers'):
                    local.update_file(target)
                self.assertEqual(target.read_text(), content)

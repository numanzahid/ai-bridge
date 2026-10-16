from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ai_bridge import agents, model_info


class ModelInfoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def records(self, entries):
        path = self.root / 'session.jsonl'
        path.write_text('\n'.join(json.dumps(entry) for entry in entries))
        return path

    def test_codex_uses_latest_context_not_first_model_or_provider(self):
        path = self.records([
            {'type': 'session_meta', 'payload': {'model_provider': 'provider-is-not-a-model'}},
            {'type': 'turn_context', 'payload': {'model': 'old-model'}},
            *[{'type': 'unrelated'} for _ in range(400)],
            {'type': 'turn_context', 'payload': {'model': 'new-model'}},
        ])
        self.assertEqual(model_info.saved_model(path, 'codex')['last_used_model'], 'new-model')
        with path.open('a') as stream:
            stream.write('\n'+json.dumps({'type': 'turn_context', 'payload': {'new_schema': True}}))
        self.assertIsNone(model_info.saved_model(path, 'codex')['last_used_model'])

    def test_claude_ignores_synthetic_and_sidechain_models(self):
        path = self.records([
            {'type': 'assistant', 'message': {'model': 'actual-model'}},
            {'type': 'assistant', 'message': {'model': '<synthetic>'}},
            {'type': 'assistant', 'isSidechain': True, 'message': {'model': 'child-model'}},
        ])
        self.assertEqual(model_info.saved_model(path, 'claude')['last_used_model'], 'actual-model')

    def test_lookup_is_bounded_and_future_shapes_remain_unknown(self):
        path = self.records([
            {'type': 'turn_context', 'payload': {'model': 'outside-window'}},
            {'type': 'large_record', 'value': 'x' * 300},
        ])
        with patch.object(model_info, 'TAIL_BYTES', 100):
            self.assertIsNone(model_info.saved_model(path, 'codex')['last_used_model'])
        for value in ({'id': 'not-a-string'}, 1, '', '<synthetic>'):
            self.assertIsNone(model_info.model_name(value))
        self.assertEqual(model_info.model_name('auto'), 'auto')
        self.assertIsNone(model_info.saved_model(self.root/'missing', 'claude')['last_used_model'])

    def test_runtime_reported_model_is_not_inferred_from_selection(self):
        output = '\n'.join(json.dumps(entry) for entry in [
            {'type': 'assistant', 'message': {'model': 'reported-backend', 'content': []}},
            {'type': 'assistant', 'message': {'model': '<synthetic>', 'content': []}},
        ])
        self.assertEqual(agents.parse_output('claude', output)['reported_model'], 'reported-backend')
        self.assertIsNone(agents.parse_output('codex', '{"type":"thread.started","model":"selected"}')['reported_model'])

    def test_codex_catalog_has_source_and_age_but_no_access_claim(self):
        stamp = datetime.now(timezone.utc).isoformat()
        (self.root/'models_cache.json').write_text(json.dumps({'fetched_at': stamp, 'models': [
            {'slug': 'new-id', 'display_name': 'New name', 'visibility': 'list', 'future_field': True},
            {'future_schema': 'unknown'},
        ]}))
        result = model_info.discover_models('codex', codex_dir=self.root)
        self.assertEqual(result['status'], 'cached')
        self.assertEqual(result['source'], 'codex.models_cache.json')
        self.assertEqual(result['fetched_at'], stamp)
        self.assertIsNotNone(result['age_seconds'])
        self.assertEqual(result['models'][0]['id'], 'new-id')
        self.assertNotIn('available', result['models'][0])

    def test_missing_or_changed_cache_returns_unavailable(self):
        for text in ('not json', '[]', '{"models":[{"new_id":"future-schema"}]}'):
            (self.root/'models_cache.json').write_text(text)
            result = model_info.discover_models('codex', codex_dir=self.root)
            self.assertEqual(result['status'], 'unavailable')
            self.assertEqual(result['models'], [])

    def test_cursor_parser_requires_known_structure_and_preserves_alias(self):
        rows = model_info.parse_cursor_models('Available models\n\nauto - Auto (default)\nnew-id - New model (current)\n\nTip: select an ID\n')
        self.assertEqual(rows[0], {'id': 'auto', 'name': 'Auto', 'is_default': True})
        self.assertTrue(rows[1]['is_cli_current'])
        for text in ('future format', 'Available models\nnew format row', 'Available models\na - A\na - A'):
            with self.assertRaises(ValueError):
                model_info.parse_cursor_models(text)

    def test_cursor_discovery_is_explicit_and_failures_do_not_escape(self):
        response = {'exit_code':0,'timed_out':False,'stdout_truncated':False,'stdout':'Available models\nauto - Auto (default)\n'}
        with patch.object(model_info, 'probe', return_value=response) as run:
            result = model_info.discover_models('cursor', binary='/test/agent')
            self.assertEqual(result['status'], 'live')
            self.assertEqual(run.call_args.args[0], ['/test/agent', 'models'])
            self.assertEqual(run.call_args.kwargs['timeout'], 20)
        with patch.object(model_info, 'probe', side_effect=subprocess.TimeoutExpired('models', 20)):
            self.assertEqual(model_info.discover_models('cursor', binary='/test/agent')['status'], 'unavailable')
        with patch.object(model_info.subprocess, 'run') as run:
            self.assertEqual(model_info.discover_models('claude')['models'], [])
            self.assertEqual(model_info.discover_models('cursor')['status'], 'unavailable')
            run.assert_not_called()

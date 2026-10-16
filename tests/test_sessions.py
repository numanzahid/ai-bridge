import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from ai_bridge import agents
from ai_bridge.cursor_store import Store, fields

SID = '11111111-2222-4333-8444-555555555555'
OTHER = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'


def varint(value):
    data = bytearray()
    while value > 127:
        data.append((value & 127) | 128)
        value >>= 7
    return bytes(data + bytes([value]))


def pb(number, value):
    if isinstance(value, str):
        value = value.encode()
    return varint(number * 8 + 2) + varint(len(value)) + value


def cursor_fixture(root, project):
    db = root / hashlib.md5(str(project).encode()).hexdigest() / SID / 'store.db'
    db.parent.mkdir(parents=True)
    con = sqlite3.connect(db)
    con.executescript('CREATE TABLE blobs(id TEXT PRIMARY KEY, data BLOB); CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);')
    def blob(data):
        ref = hashlib.sha256(data).digest()
        con.execute('insert or ignore into blobs values (?,?)', (ref.hex(), data))
        return ref
    # An abandoned message and model-cache duplicate must not enter history.
    blob(json.dumps({'role': 'assistant', 'content': 'ABANDONED'}).encode())
    cached = blob(json.dumps({'role': 'user', 'content': 'hello'}).encode())
    user = blob(pb(1, 'hello'))
    answer = blob(pb(1, pb(1, 'remembered')))
    tool = blob(pb(2, pb(1, pb(1, pb(1, 'printf safe'))) + pb(57, 'tool-call-id')))
    turn = blob(pb(1, pb(1, user) + pb(2, tool) + pb(2, answer)))
    state = blob(pb(1, cached) + pb(8, turn) + pb(9, project.as_uri()))
    meta = {'agentId': SID, 'latestRootBlobId': state.hex(), 'name': 'Fixture chat'}
    con.execute('insert into meta values (?,?)', ('0', json.dumps(meta).encode().hex()))
    con.commit()
    con.close()
    return db


class CursorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / 'project with spaces'
        self.project.mkdir()
        self.db = cursor_fixture(self.root / 'chats', self.project)

    def test_ordered_graph_excludes_cache_and_abandoned_blobs(self):
        with Store(self.db) as store:
            self.assertEqual(store.workspace(), str(self.project))
            self.assertTrue(store.can_resume(store.workspace()))
            self.assertEqual(store.messages(), [
                {'role': 'user', 'text': 'hello'},
                {'role': 'tool', 'text': '[shell] printf safe'},
                {'role': 'assistant', 'text': 'remembered'},
            ])

    def test_missing_blob_is_an_error_not_empty_history(self):
        with sqlite3.connect(self.db) as con:
            con.execute('delete from blobs')
        with self.assertRaisesRegex(ValueError, 'missing blob'):
            Store(self.db)

    def test_truncated_protobuf_is_rejected(self):
        with self.assertRaises(ValueError):
            fields(b'\x0a\x08abc')

    def test_project_filter_and_resume_inferred_from_storage(self):
        with patch.object(agents, '_cursor_chats_dir', return_value=self.root / 'chats'):
            self.assertEqual(len(agents.cursor_sessions(str(self.project), 20)), 1)
            self.assertEqual(agents.cursor_sessions(str(self.root / 'other'), 20), [])
            self.assertEqual(agents.resume_cwd('cursor', SID, None), str(self.project))
            with self.assertRaisesRegex(ValueError, 'different project'):
                agents.resume_cwd('cursor', SID, str(self.root))

    def test_cursor_model_uses_only_explicit_string_metadata(self):
        with sqlite3.connect(self.db) as con:
            raw = con.execute("select value from meta where key='0'").fetchone()[0]
            meta = json.loads(bytes.fromhex(raw).decode())
            meta['lastUsedModel'] = 'saved-selection'
            con.execute("update meta set value=? where key='0'", (json.dumps(meta).encode().hex(),))
        with patch.object(agents, '_cursor_chats_dir', return_value=self.root / 'chats'):
            row = agents.cursor_sessions(None, 20)[0]
            self.assertEqual(row['last_used_model'], 'saved-selection')
            self.assertEqual(row['model_source'], 'cursor.meta.lastUsedModel')

    def test_missing_project_blocks_resume(self):
        self.project.rmdir()
        with patch.object(agents, '_cursor_chats_dir', return_value=self.root / 'chats'):
            with self.assertRaisesRegex(ValueError, 'no longer exists'):
                agents.resume_cwd('cursor', SID, None)

    def test_relocated_store_remains_visible_but_not_resumable(self):
        other = self.db.parent.parent.with_name('0' * 32)
        self.db.parent.parent.rename(other)
        with patch.object(agents, '_cursor_chats_dir', return_value=self.root / 'chats'):
            row = agents.cursor_sessions(None, 20)[0]
            self.assertEqual(row['cwd'], str(self.project))
            self.assertFalse(row['resumable'])
            with self.assertRaises(ValueError):
                agents.resume_cwd('cursor', SID, None)


class CodexTests(unittest.TestCase):
    def event(self, kind, **kwargs):
        return {'type': 'event_msg', 'payload': {'type': kind, **kwargs}}

    def message(self, role, text):
        return {'type': 'response_item', 'payload': {'type': 'message', 'role': role,
                'content': [{'type': 'input_text' if role == 'user' else 'output_text', 'text': text}]}}

    def test_modern_rollout_does_not_disappear_or_duplicate(self):
        rows = [self.event('task_started'), self.message('user', 'hello'),
                self.event('item_completed', item={'type': 'UserMessage', 'content': [{'text': 'hello'}]}),
                self.event('item_completed', item={'type': 'AgentMessage', 'content': [{'text': 'hi'}]}),
                self.message('assistant', 'hi'), self.event('token_count')]
        self.assertEqual([(m['role'], m['text']) for m in agents._codex_messages(rows)], [('user', 'hello'), ('assistant', 'hi')])

    def test_legacy_and_modern_turns_can_coexist(self):
        rows = [self.event('task_started'), self.event('user_message', message='old'),
                self.event('agent_message', message='old reply'), self.event('task_started'),
                self.message('user', 'new'), self.message('assistant', 'new reply')]
        self.assertEqual([m['text'] for m in agents._codex_messages(rows)], ['old', 'old reply', 'new', 'new reply'])

    def test_instruction_wrappers_are_not_user_prompts(self):
        rows = [self.message('user', '# AGENTS.md instructions\nsecret scaffold'), self.message('user', 'actual prompt')]
        self.assertEqual([m['text'] for m in agents._codex_messages(rows)], ['actual prompt'])

    def test_session_match_is_exact(self):
        with patch.object(agents, 'list_sessions', return_value=[{'session_id': SID}]):
            with self.assertRaises(ValueError):
                agents.session_info('codex', SID[:8])
            with self.assertRaises(KeyError):
                agents.session_info('codex', OTHER)

    def test_current_rollout_discovery_and_history(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {'CODEX_HOME': td}):
            root = Path(td)
            folder = root / 'sessions' / '2026'
            folder.mkdir(parents=True)
            rows = [{'type': 'session_meta', 'payload': {'id': SID, 'cwd': td, 'parent_thread_id': OTHER}},
                    self.event('task_started'), self.message('user', 'actual title'), self.message('assistant', 'reply')]
            (folder / f'rollout-date-{SID}.jsonl').write_text('\n'.join(map(json.dumps, rows)))
            (root / 'session_index.jsonl').write_text(json.dumps({'id': SID, 'thread_name': 'Renamed chat'}))
            sessions = agents.codex_sessions(td, 10)
            self.assertEqual(sessions[0]['title'], 'Renamed chat')
            self.assertEqual(sessions[0]['parent_session_id'], OTHER)
            self.assertEqual(agents.resume_cwd('codex', SID, None), td)
            self.assertEqual([m['text'] for m in agents.codex_history(SID)], ['actual title', 'reply'])


if __name__ == '__main__':
    unittest.main()

import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

# Importing server constructs its config; keep all test state temporary.
_state = tempfile.TemporaryDirectory(prefix='ai-bridge-unit-')
with patch.dict(os.environ, {'AI_BRIDGE_TOKEN': 'unit-test-token-' * 4, 'AI_BRIDGE_STATE_DIR': _state.name,
                            'AI_BRIDGE_CONFIG_DIR': str(Path(_state.name) / 'config')}):
    from ai_bridge import server

SID = '11111111-2222-4333-8444-555555555555'
OTHER = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'


class ServerSessions(unittest.TestCase):
    def agent_result(self, log, status='failed', exit_code=1):
        meta = {'id': 'test-job', 'kind': 'agent', 'agent': 'cursor', 'status': status,
                'session_id': SID, 'requested_session_id': SID, 'cwd': _state.name,
                'started': time.time(), 'ended': time.time(), 'exit_code': exit_code}
        with patch.object(server.JOBS, 'wait', return_value=meta), \
             patch.object(server.JOBS, 'read_log', return_value=log), \
             patch.object(server.JOBS, 'tail', return_value=log):
            return json.loads(server.agent_wait('test-job', 0))

    def test_result_event_limit_with_zero_exit_is_still_reported(self):
        log = json.dumps({'type': 'result', 'is_error': True, 'errors': ['Usage limit reached']})
        result = self.agent_result(log, status='done', exit_code=0)
        self.assertEqual(result['outcome'], 'blocked')
        self.assertEqual(result['failure']['code'], 'usage_limit')
        self.assertEqual(result['exit_code'], 0)
        self.assertEqual(result['session_id'], SID)

    def test_successful_reply_about_limits_is_not_a_provider_failure(self):
        result = self.agent_result(json.dumps({'type': 'result', 'subtype': 'success',
                                              'result': 'Implemented rate limit handling for service unavailable.'}),
                                   status='done', exit_code=0)
        self.assertEqual(result['outcome'], 'succeeded')
        self.assertNotIn('failure', result)

    def test_unknown_error_is_not_guessed(self):
        result = self.agent_result('Unrecognized CLI failure')
        self.assertEqual(result['failure']['code'], 'execution_failed')
        self.assertFalse(result['failure']['automatic_retry'])

    def test_interrupted_job_is_not_a_usage_limit(self):
        result = self.agent_result("You're out of usage", status='killed', exit_code=None)
        self.assertEqual(result['outcome'], 'interrupted')
        self.assertEqual(result['failure']['code'], 'interrupted')

    def test_failure_categories(self):
        for text, code in [('Too many requests (429)', 'rate_limit'),
                           ('Authentication required', 'authentication_required'),
                           ('Service unavailable (503)', 'service_unavailable')]:
            with self.subTest(code=code):
                self.assertEqual(self.agent_result(text)['failure']['code'], code)

    def test_model_catalog_pagination_preserves_rows(self):
        rows = [{'id': str(i), 'name': 'x' * 200} for i in range(20)]
        def result(agent):
            return {'agent': agent, 'status': 'cached', 'source': 'fixture', 'models': rows}
        with patch.object(server.ag, 'model_catalog', side_effect=result), patch.object(server.CFG, 'max_output_chars', 1800):
            found, offset = [], 0
            while offset is not None:
                page = json.loads(server.agent_models('codex', limit=20, offset=offset))
                found.extend(page['models'])
                offset = page['next_offset']
            self.assertEqual(found, rows)

    def test_requested_model_is_not_reported_as_observed(self):
        meta = {'id': 'test-job', 'kind': 'agent', 'agent': 'codex', 'status': 'running',
                'session_id': SID, 'model': 'requested-alias', 'cwd': _state.name, 'started': time.time()}
        with patch.object(server.JOBS, 'wait', return_value=meta), patch.object(server.JOBS, 'read_log', return_value=''):
            view = json.loads(server.agent_wait('test-job', 0))
            self.assertEqual(view['requested_model'], 'requested-alias')
            self.assertIsNone(view['reported_model'])

    def test_catalog_is_not_loaded_when_building_the_connection(self):
        with patch.object(server.catalog, 'discover') as discover:
            server.build_app()
            discover.assert_not_called()

    def test_catalog_pagination_does_not_skip_entries_when_size_limited(self):
        rows = [{'name': str(i), 'path': '/tmp/' + 'x' * 200, 'description': 'd' * 200} for i in range(20)]
        with patch.object(server.catalog, 'discover', return_value=rows), patch.object(server.CFG, 'max_output_chars', 2000):
            found, offset = [], 0
            while offset is not None:
                page = json.loads(server.list_skills(limit=20, offset=offset))
                found.extend(page['skills'])
                offset = page['next_offset']
            self.assertEqual(found, rows)

    def test_resume_uses_saved_project(self):
        with patch.object(server.ag, 'find_binary', return_value='codex'), \
             patch.object(server.ag, 'resume_cwd', return_value=_state.name) as resolve, \
             patch.object(server.JOBS, 'running_for_session', return_value=None), \
             patch.object(server.JOBS, 'start', return_value={'id': 'test-job'}) as start, \
             patch.object(server, 'agent_wait', return_value='{}'):
            server.agent_send('codex', 'continue', session_id=SID, permission='readonly')
            resolve.assert_called_once_with('codex', SID, None)
            self.assertEqual(start.call_args.args[1], _state.name)
            self.assertEqual(start.call_args.kwargs['extra']['requested_session_id'], SID)

    def test_unknown_session_never_launches_a_cli(self):
        with patch.object(server.ag, 'find_binary', return_value='codex'), \
             patch.object(server.ag, 'resume_cwd', side_effect=KeyError('not found')), \
             patch.object(server.JOBS, 'start') as start:
            response = json.loads(server.agent_send('codex', 'continue', session_id=SID))
            self.assertIn('error', response)
            start.assert_not_called()

    def test_empty_session_id_does_not_start_a_new_chat(self):
        with patch.object(server.ag, 'find_binary', return_value='codex'), \
             patch.object(server.JOBS, 'start') as start:
            response = json.loads(server.agent_send('codex', 'continue', session_id=''))
            self.assertIn('error', response)
            start.assert_not_called()

    def test_busy_session_never_launches_a_second_turn(self):
        with patch.object(server.ag, 'find_binary', return_value='codex'), \
             patch.object(server.ag, 'resume_cwd', return_value=_state.name), \
             patch.object(server.JOBS, 'running_for_session', return_value={'id': 'busy-job'}), \
             patch.object(server.JOBS, 'start') as start:
            response = json.loads(server.agent_send('codex', 'continue', session_id=SID))
            self.assertIn('busy', response['error'])
            start.assert_not_called()

    def test_different_returned_session_is_not_silently_accepted(self):
        meta = {'id': 'test-job', 'kind': 'agent', 'agent': 'codex', 'status': 'done',
                'session_id': SID, 'requested_session_id': SID, 'cwd': _state.name,
                'started': time.time(), 'ended': time.time(), 'exit_code': 0}
        log = json.dumps({'type': 'thread.started', 'thread_id': OTHER})
        with patch.object(server.JOBS, 'wait', return_value=meta), \
             patch.object(server.JOBS, 'read_log', return_value=log), \
             patch.object(server.JOBS, 'save') as save:
            result = json.loads(server.agent_wait('test-job', 0))
            self.assertTrue(result['is_error'])
            self.assertEqual(result['session_id'], SID)
            self.assertEqual(result['returned_session_id'], OTHER)
            save.assert_not_called()

    def test_session_pagination_does_not_skip_rows_when_size_limited(self):
        rows = [{'session_id': str(i), 'title': 'x' * 200} for i in range(30)]
        with patch.object(server.ag, 'list_sessions', return_value=rows), patch.object(server.CFG, 'max_output_chars', 1800):
            found = []
            offset = 0
            while offset is not None:
                page = json.loads(server.agent_sessions('codex', limit=30, offset=offset))
                found.extend(page['sessions'])
                offset = page['next_offset']
            self.assertEqual(found, rows)

    def test_history_pages_back_without_skipping_or_repeating(self):
        messages = [{'role': 'user', 'text': str(i) + 'x' * 200} for i in range(30)]
        with patch.object(server.ag, 'session_info', return_value={'session_id': SID}), \
             patch.object(server.ag, 'history', side_effect=lambda *args: [dict(m) for m in messages]), \
             patch.object(server.CFG, 'max_output_chars', 1800):
            found = []
            offset = 0
            while offset is not None:
                page = json.loads(server.agent_history('codex', SID, last_n=30, offset=offset))
                found = page['entries'] + found
                offset = page['next_offset']
            self.assertEqual(found, messages)


if __name__ == '__main__':
    unittest.main()

class AuditAndContinuityTests(unittest.TestCase):
    def test_operation_audit_has_start_finish_and_linked_exit(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(server.CFG,'state_dir',Path(tmp)):
            result=json.loads(server.run_command('printf trace-ok'))
            self.assertEqual(result['exit_code'],0)
            rows=[json.loads(line) for line in (Path(tmp)/'audit.log').read_text().splitlines()]
            self.assertEqual([r['phase'] for r in rows],['start','result','finish'])
            self.assertEqual(len({r['operation_id'] for r in rows}),1)
            self.assertEqual(rows[-1]['exit_code'],0)

    def test_missing_returned_id_is_unverified_not_confirmed(self):
        meta={'id':'test-job','kind':'agent','agent':'cursor','status':'done',
              'session_id':SID,'requested_session_id':SID,'cwd':_state.name,
              'started':time.time(),'exit_code':0}
        with patch.object(server.JOBS,'wait',return_value=meta), \
             patch.object(server.JOBS,'output_info',return_value={}), \
             patch.object(server.JOBS,'read_log',return_value=json.dumps({'type':'result','subtype':'success','result':'done'})):
            result=json.loads(server.agent_wait('test-job',0))
            self.assertEqual(result['continuity'],'unverified')
            self.assertEqual(result['session_id'],SID)

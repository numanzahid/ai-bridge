import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from helpers import OTHER, SID, STATE, collect_pages, server


class ServerSessions(unittest.TestCase):
    def agent_result(self, log, status='failed', exit_code=1):
        meta = {'id': 'test-job', 'kind': 'agent', 'agent': 'cursor', 'status': status,
                'session_id': SID, 'requested_session_id': SID, 'cwd': STATE.name,
                'started': time.time(), 'ended': time.time(), 'exit_code': exit_code}
        with patch.object(server.JOBS, 'wait', return_value=meta), \
             patch.object(server.JOBS, 'read_log', return_value=log), \
             patch.object(server.JOBS, 'tail', return_value=log):
            return json.loads(server.job_status('test-job', 0))

    def test_failure_classification(self):
        limit_event = json.dumps({'type': 'result', 'is_error': True, 'errors': ['Usage limit reached']})
        for log, status, exit_code, outcome, code in (
                # A result-event limit with a zero exit is still reported.
                (limit_event, 'done', 0, 'blocked', 'usage_limit'),
                # Interrupted jobs are not usage limits, even if the text mentions usage.
                ("You're out of usage", 'killed', None, 'interrupted', 'interrupted'),
                # Unknown errors are not guessed.
                ('Unrecognized CLI failure', 'failed', 1, None, 'execution_failed'),
                ('Too many requests (429)', 'failed', 1, None, 'rate_limit'),
                ('Authentication required', 'failed', 1, None, 'authentication_required'),
                ('Service unavailable (503)', 'failed', 1, None, 'service_unavailable')):
            with self.subTest(code=code):
                result = self.agent_result(log, status=status, exit_code=exit_code)
                self.assertEqual(result['failure']['code'], code)
                self.assertFalse(result['failure']['automatic_retry'])
                self.assertEqual(result['session_id'], SID)
                self.assertEqual(result['exit_code'], exit_code)
                if outcome:
                    self.assertEqual(result['outcome'], outcome)

    def test_successful_reply_about_limits_is_not_a_provider_failure(self):
        result = self.agent_result(json.dumps({'type': 'result', 'subtype': 'success',
                                              'result': 'Implemented rate limit handling for service unavailable.'}),
                                   status='done', exit_code=0)
        self.assertEqual(result['outcome'], 'succeeded')
        self.assertNotIn('failure', result)

    def test_model_catalog_pagination_preserves_rows(self):
        rows = [{'id': str(i), 'name': 'x' * 200} for i in range(20)]
        def result(agent):
            return {'agent': agent, 'status': 'cached', 'source': 'fixture', 'models': rows}
        with patch.object(server.ag, 'model_catalog', side_effect=result), patch.object(server.CFG, 'max_output_chars', 1800):
            found = collect_pages(self, lambda offset: json.loads(server.agent_list('models', 'codex', limit=20, offset=offset)), 'models')
        self.assertEqual(found, rows)

    def test_requested_model_is_not_reported_as_observed(self):
        meta = {'id': 'test-job', 'kind': 'agent', 'agent': 'codex', 'status': 'running',
                'session_id': SID, 'model': 'requested-alias', 'cwd': STATE.name, 'started': time.time()}
        with patch.object(server.JOBS, 'wait', return_value=meta), patch.object(server.JOBS, 'read_log', return_value=''):
            view = json.loads(server.job_status('test-job', 0))
            self.assertEqual(view['requested_model'], 'requested-alias')
            self.assertIsNone(view['reported_model'])

    def test_catalog_is_not_loaded_when_building_the_connection(self):
        with patch.object(server.catalog, 'discover') as discover:
            server.build_app()
            discover.assert_not_called()

    def test_catalog_pagination_does_not_skip_entries_when_size_limited(self):
        rows = [{'name': str(i), 'path': '/tmp/' + 'x' * 200, 'description': 'd' * 200} for i in range(20)]
        with patch.object(server.catalog, 'discover', return_value=rows), patch.object(server.CFG, 'max_output_chars', 2000):
            found = collect_pages(self, lambda offset: json.loads(server.list_instructions('skills', limit=20, offset=offset)), 'skills')
        self.assertEqual(found, rows)

    def test_resume_uses_saved_project(self):
        with patch.object(server.ag, 'find_binary', return_value='codex'), \
             patch.object(server.ag, 'resume_cwd', return_value=STATE.name) as resolve, \
             patch.object(server.JOBS, 'running_for_session', return_value=None), \
             patch.object(server.JOBS, 'start', return_value={'id': 'test-job'}) as start, \
             patch.object(server, '_wait_job', return_value={'id': 'test-job'}), \
             patch.object(server, '_agent_view', return_value={}):
            server.agent_send('codex', 'continue', session_id=SID, permission='readonly')
            resolve.assert_called_once_with('codex', SID, None)
            self.assertEqual(start.call_args.args[1], STATE.name)
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
             patch.object(server.ag, 'resume_cwd', return_value=STATE.name), \
             patch.object(server.JOBS, 'running_for_session', return_value={'id': 'busy-job'}), \
             patch.object(server.JOBS, 'start') as start:
            response = json.loads(server.agent_send('codex', 'continue', session_id=SID))
            self.assertIn('busy', response['error'])
            start.assert_not_called()

    def test_different_returned_session_is_not_silently_accepted(self):
        meta = {'id': 'test-job', 'kind': 'agent', 'agent': 'codex', 'status': 'done',
                'session_id': SID, 'requested_session_id': SID, 'cwd': STATE.name,
                'started': time.time(), 'ended': time.time(), 'exit_code': 0}
        log = json.dumps({'type': 'thread.started', 'thread_id': OTHER})
        with patch.object(server.JOBS, 'wait', return_value=meta), \
             patch.object(server.JOBS, 'read_log', return_value=log), \
             patch.object(server.JOBS, 'save') as save:
            result = json.loads(server.job_status('test-job', 0))
            self.assertTrue(result['is_error'])
            self.assertEqual(result['session_id'], SID)
            self.assertEqual(result['returned_session_id'], OTHER)
            save.assert_not_called()

    def test_session_pagination_does_not_skip_rows_when_size_limited(self):
        from uuid import UUID
        from ai_bridge.management import Sessions
        rows = [{'session_id': str(UUID(int=i + 1)), 'title': 'x' * 200} for i in range(30)]
        expected = Sessions(server.CFG, server.JOBS).enrich('codex', rows)
        with patch.object(server.ag, 'list_sessions', return_value=rows), patch.object(server.CFG, 'max_output_chars', 1800):
            found = collect_pages(self, lambda offset: json.loads(server.agent_sessions('codex', limit=30, offset=offset)), 'sessions')
        self.assertEqual(found, expected)
        self.assertEqual(len({row['session_id'] for row in found}), len(rows))

    def test_history_pages_back_without_skipping_or_repeating(self):
        messages = [{'role': 'user', 'text': str(i) + 'x' * 200} for i in range(30)]
        with patch.object(server.ag, 'session_info', return_value={'session_id': SID}), \
             patch.object(server.ag, 'history', side_effect=lambda *args: [dict(m) for m in messages]), \
             patch.object(server.CFG, 'max_output_chars', 1800):
            found = collect_pages(self, lambda offset: json.loads(server.agent_history('codex', SID, last_n=30, offset=offset)),
                                  'entries', newest_first=True)
        self.assertEqual(found, messages)


class AuditAndContinuityTests(unittest.TestCase):
    def test_operation_audit_has_start_finish_and_linked_exit(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(server.CFG, 'state_dir', Path(tmp)):
            result = json.loads(server.run_command('printf trace-ok'))
            self.assertEqual(result['exit_code'], 0)
            rows = [json.loads(line) for line in (Path(tmp)/'audit.log').read_text().splitlines()]
            self.assertEqual([r['phase'] for r in rows], ['start', 'result', 'finish'])
            self.assertEqual(len({r['operation_id'] for r in rows}), 1)
            self.assertEqual(rows[-1]['exit_code'], 0)

    def test_missing_returned_id_is_unverified_not_confirmed(self):
        meta = {'id': 'test-job', 'kind': 'agent', 'agent': 'cursor', 'status': 'done',
                'session_id': SID, 'requested_session_id': SID, 'cwd': STATE.name,
                'started': time.time(), 'exit_code': 0}
        with patch.object(server.JOBS, 'wait', return_value=meta), \
             patch.object(server.JOBS, 'output_info', return_value={}), \
             patch.object(server.JOBS, 'read_log', return_value=json.dumps({'type': 'result', 'subtype': 'success', 'result': 'done'})):
            result = json.loads(server.job_status('test-job', 0))
            self.assertEqual(result['continuity'], 'unverified')
            self.assertEqual(result['session_id'], SID)



class AgentManageTests(unittest.TestCase):
    def manage(self, **kwargs):
        return json.loads(server.agent_manage(**kwargs))

    def test_alias_and_archive_change_only_bridge_metadata(self):
        info = {'session_id': SID, 'title': 'native title', 'cwd': STATE.name}
        with patch.object(server.ag, 'session_info', return_value=info), \
             patch.object(server.ag, 'list_sessions', return_value=[dict(info)]):
            result = self.manage(action='alias', agent='codex', session_id=SID, alias='Display name')
            self.assertEqual(result['bridge_metadata']['alias'], 'Display name')
            self.assertFalse(result['native_history_changed'])
            row = json.loads(server.agent_sessions('codex'))['sessions'][0]
            self.assertEqual((row['title'], row['native_title']), ('Display name', 'native title'))
            self.manage(action='archive', agent='codex', session_id=SID)
            self.assertEqual(json.loads(server.agent_sessions('codex'))['sessions'], [])
            self.assertTrue(json.loads(server.agent_sessions('codex', include_archived=True))['sessions'][0]['archived'])
            self.manage(action='unarchive', agent='codex', session_id=SID)
            self.manage(action='alias', agent='codex', session_id=SID)  # no alias clears it
            self.assertEqual(json.loads(server.agent_sessions('codex'))['sessions'][0]['title'], 'native title')
        self.assertIn('error', self.manage(action='archive', agent='codex'))

    def test_task_cleanup_previews_until_confirmed(self):
        from ai_bridge.management import Tasks
        with tempfile.TemporaryDirectory() as tmp, patch.object(server.CFG, 'tasks_dir', Path(tmp) / 'tasks'), \
             patch('ai_bridge.management.observed_task_processes', return_value=[]):
            task = Tasks(server.CFG).create()
            listed = json.loads(server.agent_list('tasks'))['tasks']
            self.assertEqual([row['task_id'] for row in listed], [task['task_id']])
            preview = self.manage(action='cleanup_task', task_id=task['task_id'])
            self.assertTrue(preview['dry_run'])
            self.assertIn('confirm=true', preview['next'])
            self.assertTrue(Path(task['workspace']).exists())
            removed = self.manage(action='cleanup_task', task_id=task['task_id'], confirm=True)
            self.assertTrue(removed['removed'])
            self.assertFalse(Path(task['workspace']).exists())
        self.assertIn('error', self.manage(action='cleanup_task'))

if __name__ == '__main__':
    unittest.main()

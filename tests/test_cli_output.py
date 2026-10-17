"""Terminal presentation regression tests without accessing native session stores."""
import copy
from datetime import datetime, timezone
import errno
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ai_bridge import cli, cli_output as output

SID = '12345678-1234-1234-1234-123456789abc'
ROW = dict(session_id=SID, agent='codex', title='Review 日本語 café', cwd='/tmp/项目/café',
           status='active_job', mode='noninteractive', session_type='main', updated='2026-01-01 00:00')
PAGE = dict(sessions=[ROW], total=3, offset=0, next_offset=1,
            unavailable_agents={'cursor': 'Cannot read native store'})
NOW = datetime(2026, 1, 1, 2, tzinfo=timezone.utc).timestamp()


class Stream(io.StringIO):
    def __init__(self, tty=False):
        super().__init__()
        self.tty = tty

    def isatty(self):
        return self.tty


class OutputTests(unittest.TestCase):
    def run_cli(self, argv, result=PAGE, tty=False, error=None, env=None):
        stdout, stderr = Stream(tty), Stream(tty)
        with patch('sys.stdout', stdout), patch('sys.stderr', stderr), \
                patch.object(cli, 'config'), patch.object(cli, 'Jobs'), \
                patch.object(cli, 'dispatch', return_value=result, side_effect=error) as dispatch, \
                patch.dict(os.environ, env or {}, clear=True), \
                patch.object(cli.shutil, 'get_terminal_size', return_value=os.terminal_size((100, 24))):
            try:
                code = cli.main(argv)
            except SystemExit as exc:
                code = exc.code
        return code, stdout.getvalue(), stderr.getvalue(), dispatch

    def test_default_json_is_unchanged_and_table_does_not_mutate(self):
        before = copy.deepcopy(PAGE)
        code, stdout, stderr, _ = self.run_cli(['sessions', 'list'])
        self.assertEqual((code, stderr), (0, ''))
        self.assertEqual(stdout, json.dumps(PAGE, ensure_ascii=False, indent=2) + '\n')
        code, stdout, stderr, _ = self.run_cli(['sessions', 'list'], tty=True)
        for text in ('ID', '12345678', 'Sessions: 1 shown, 3 total', '--offset 1',
                     'cursor unavailable: Cannot read native store', 'bridge jobs only'):
            self.assertIn(text, stdout)
        self.assertEqual((code, stderr, PAGE), (0, '', before))

    def test_flags_on_root_group_and_every_leaf(self):
        parser = cli.parser()
        for group_action in parser._actions:
            if not isinstance(group_action, cli.argparse._SubParsersAction):
                continue
            for group, sub in group_action.choices.items():
                actions = next(a for a in sub._actions if isinstance(a, cli.argparse._SubParsersAction))
                for action, leaf in actions.choices.items():
                    options = leaf._option_string_actions
                    self.assertIn('--json', options)
                    self.assertIn('--table', options)
        for argv in (['--json', 'sessions', 'list'], ['sessions', '--json', 'list'],
                     ['sessions', 'list', '--json']):
            self.assertEqual(json.loads(self.run_cli(argv, tty=True)[1]), PAGE)
        for argv in (['--table', 'sessions', 'list'], ['sessions', '--table', 'list'],
                     ['sessions', 'list', '--table']):
            stdout = self.run_cli(argv)[1]
            self.assertIn('Sessions: 1 shown', stdout)
            self.assertNotIn('\x1b', stdout)

    def test_conflicting_formats_and_parse_errors_are_json_with_exit_two(self):
        for argv in (['sessions', 'list', '--json', '--table'],
                     ['--json', 'sessions', 'list', '--table'],
                     ['sessions', '--table', 'list', '--json'],
                     ['sessions', 'list', '--limit', 'bad'], ['nonsense']):
            code, stdout, stderr, dispatch = self.run_cli(argv)
            self.assertEqual((code, stdout), (2, ''))
            self.assertFalse(json.loads(stderr)['automatic_retry'])
            dispatch.assert_not_called()

    def test_errors_remain_json_and_exit_one_even_for_table(self):
        for tty in (False, True):
            for flag in ('--json', '--table'):
                code, stdout, stderr, _ = self.run_cli(['tasks', 'list', flag], tty=tty,
                                                      error=ValueError('Permission denied'))
                self.assertEqual((code, stdout), (1, ''))
                self.assertEqual(json.loads(stderr), {'error': 'Permission denied', 'automatic_retry': False})

    def test_colors_only_for_tty_and_obey_no_color(self):
        args = ['sessions', 'list', '--table']
        self.assertIn('\x1b[32m', self.run_cli(args, tty=True)[1])
        for tty, env in ((False, {}), (True, {'NO_COLOR': ''}), (True, {'TERM': 'dumb'})):
            self.assertNotIn('\x1b', self.run_cli(args, tty=tty, env=env)[1])
        self.assertNotIn('\x1b', self.run_cli(['sessions', 'list', '--json'], tty=True)[1])

    def test_render_failure_after_mutation_returns_json_success_without_retry(self):
        for error in (KeyError('missing field'), TypeError('bad format')):
            for argv, tty in ((['tasks', 'create'], True),
                              (['tasks', 'create', '--table'], False)):
                with self.subTest(error=type(error).__name__, argv=argv):
                    effects = []
                    result = {'task_id': 'task-created', 'workspace': '/tmp/任务'}

                    def mutate(*args):
                        effects.append(result['task_id'])
                        return result

                    def fail_render(*args, **kwargs):
                        self.assertEqual(effects, ['task-created'])
                        raise error

                    with patch.object(output, 'render', side_effect=fail_render) as render:
                        code, stdout, stderr, dispatch = self.run_cli(argv, tty=tty, error=mutate)
                    self.assertEqual((code, stderr), (0, ''))
                    self.assertEqual(json.loads(stdout), result)
                    self.assertEqual(effects, ['task-created'])
                    dispatch.assert_called_once()
                    render.assert_called_once()

    def test_full_inspect_selector_and_all_fields_survive(self):
        row = dict(ROW, nested={'warning': 'uncertain', 'ids': [SID]}, future_field=True)
        code, stdout, _, _ = self.run_cli(['sessions', 'inspect', 'codex:' + SID], row, tty=True)
        self.assertEqual(code, 0)
        self.assertIn('selector: codex:' + SID, stdout)
        self.assertIn('session_id: ' + SID, stdout)
        self.assertIn('future_field: true', stdout)
        self.assertIn('nested:', stdout)
        self.assertEqual(json.loads(self.run_cli(['sessions', 'inspect', SID, '--agent', 'codex'], row)[1]), row)

    def test_lists_empty_pages_and_full_job_details(self):
        for group, action, data, expected in (
                ('agents', 'status', [], 'Agent jobs: 0 total.'),
                ('tasks', 'list', [], 'Tasks: 0 total.'),
                ('tasks', 'list', [{'task_id': 'task-' + 'a' * 24, 'created': 0,
                                    'workspace': '/tmp/任务'}], 'task-' + 'a' * 24),
                ('agents', 'status', [{'job_id': 'job-123', 'status': 'lost', 'agent': 'codex'}], 'lost')):
            self.assertIn(expected, self.run_cli([group, action], data, tty=True)[1])
            self.assertEqual(json.loads(self.run_cli([group, action], data)[1]), data)
        empty = dict(PAGE, sessions=[], offset=99, next_offset=None)
        text = self.run_cli(['sessions', 'list'], empty, tty=True)[1]
        self.assertIn('(none)', text)
        self.assertIn('0 shown, 3 total (offset 99)', text)
        self.assertNotIn('Next page:', text)
        data = {'job_id': 'abc', 'cleanup': {'complete': False}, 'observation_scope': 'limited'}
        self.assertIn('"complete": false', self.run_cli(['agents', 'inspect', 'abc'], data, tty=True)[1])

    def test_unicode_width_and_terminal_injection(self):
        args = cli.parser().parse_args(['sessions', 'list'])
        page = copy.deepcopy(PAGE)
        page['sessions'][0].update(title='A\x1b[31m\n\t\u202eevil' + '项目e\u0301' * 50,
                                   cwd='/tmp/' + '目录/' * 60)
        for width in (10, 20, 69, 70, 80, 100, 200):
            text = output.render(args, page, width=width, now=NOW)
            self.assertNotIn('\x1b', text)
            self.assertIn('Warning:', text)
            self.assertTrue(all(output.cell_width(line) <= width for line in text.splitlines()), text)
        self.assertEqual(output.cell_width('项目e\u0301'), 5)
        self.assertIn('\\u001b', output.safe('\x1b'))

    def test_unreadable_session_warning_is_not_truncated(self):
        page = copy.deepcopy(PAGE)
        page['sessions'][0]['error'] = 'Unreadable SQLite schema: ' + 'details ' * 30
        args = cli.parser().parse_args(['sessions', 'list'])
        text = output.render(args, page, width=80)
        self.assertIn('Warning: codex:' + SID, text)
        self.assertIn('Unreadable SQLite schema:', text.replace('\n', ''))
        self.assertEqual(text.replace('\n', '').count('details'), 30)

    @unittest.skipUnless(os.name == 'posix', 'requires a POSIX pseudo terminal')
    @unittest.skipUnless(hasattr(os, 'openpty'), 'os.openpty is unavailable')
    def test_real_tty_and_pipe_with_isolated_task_store(self):
        with tempfile.TemporaryDirectory(prefix='aib-output-') as root:
            env = dict(os.environ, AI_BRIDGE_STATE_DIR=root + '/state',
                       AI_BRIDGE_TASKS_DIR=root + '/tasks', NO_COLOR='', COLUMNS='80')
            command = [sys.executable, '-m', 'ai_bridge', 'tasks', 'list']
            piped = subprocess.run(command, capture_output=True, text=True, env=env, timeout=10)
            self.assertEqual(piped.returncode, 0, piped.stderr)
            self.assertEqual(json.loads(piped.stdout), [])
            try:
                master, slave = os.openpty()
            except NotImplementedError:
                self.skipTest('os.openpty is not implemented')
            except OSError as error:
                if error.errno in (errno.ENOSYS, errno.ENODEV, errno.ENOENT, errno.ENXIO):
                    self.skipTest(f'PTY support is unavailable: {error}')
                raise
            try:
                terminal = subprocess.run(command, stdout=slave, stderr=subprocess.PIPE,
                                          env=env, timeout=10)
                self.assertEqual(terminal.returncode, 0, terminal.stderr)
                text = os.read(master, 4096).decode()
                self.assertIn('Tasks: 0 total.', text)
                self.assertNotIn('\x1b', text)
            finally:
                os.close(slave)
                os.close(master)
            # No task records or native metadata are created by listing.
            self.assertFalse(Path(root, 'tasks').exists())

    @unittest.skipUnless(os.name == 'posix' and hasattr(os, 'openpty'), 'requires os.openpty')
    def test_pty_unavailable_skips_but_other_failures_propagate(self):
        piped = subprocess.CompletedProcess([], 0, stdout='[]', stderr='')
        for error in (NotImplementedError(),
                      *(OSError(code, 'PTY unavailable') for code in
                        (errno.ENOSYS, errno.ENODEV, errno.ENOENT, errno.ENXIO))):
            with self.subTest(error=repr(error)), \
                    patch.object(subprocess, 'run', return_value=piped), \
                    patch.object(os, 'openpty', side_effect=error):
                with self.assertRaises(unittest.SkipTest):
                    self.test_real_tty_and_pipe_with_isolated_task_store()
        for code in (errno.EIO, errno.EMFILE, errno.EACCES):
            with self.subTest(errno=code), \
                    patch.object(subprocess, 'run', return_value=piped), \
                    patch.object(os, 'openpty', side_effect=OSError(code, 'real failure')):
                with self.assertRaises(OSError) as raised:
                    self.test_real_tty_and_pipe_with_isolated_task_store()
                self.assertEqual(raised.exception.errno, code)

    def test_table_flag_does_not_bypass_interactive_open_guard(self):
        args = cli.parser().parse_args(['sessions', 'open', 'codex:' + SID, '--table'])
        with patch('sys.stdin', Stream(False)), patch('sys.stdout', Stream(False)):
            with self.assertRaisesRegex(ValueError, 'real host terminal'):
                cli.dispatch(args, None, None)

    def test_relative_timestamp_variants_and_bad_values(self):
        for value, expected in ((NOW - 120, '2m ago'), (NOW + 3600, 'in 1h'),
                                ('2026-01-01T01:00:00Z', '1h ago'), (None, '-'),
                                ('unknown', 'unknown'), (float('inf'), 'inf'), (NOW, 'just now')):
            self.assertEqual(output.relative(value, NOW), expected)
        self.assertEqual(output.relative('2026-01-01 00:00',
                         datetime(2026, 1, 1, 2).timestamp()), '2h ago')


if __name__ == '__main__':
    unittest.main()

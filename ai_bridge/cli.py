"""Host-terminal management, independent of HTTP authentication and service uptime."""
import argparse
import json
import os
import re
import shutil
from pathlib import Path
import sys

from .core import Config, Jobs
from .management import Sessions, Tasks, management_lock, private_dir, runtime, start_agent, stop_agent
from .optional_agents import agents as ag


def config():
    cfg = Config(token='', state_dir=Path(os.environ.get('AI_BRIDGE_STATE_DIR') or
                                         Path.home() / '.local/state/ai-bridge'),
                 tasks_dir=Path(os.environ.get('AI_BRIDGE_TASKS_DIR') or Path.home() / '.ai-bridge-tasks'),
                 agent_default_permission=os.environ.get('AI_BRIDGE_AGENT_DEFAULT_PERMISSION', 'edit'))
    private_dir(cfg.state_dir)
    private_dir(cfg.state_dir / 'jobs')
    return cfg


def target(value, agent):
    if ':' in value:
        specified, value = value.split(':', 1)
        if agent and specified != agent:
            raise ValueError('Conflicting agent selectors.')
        agent = specified
    if agent not in ag.AGENTS:
        raise ValueError('Use agent:UUID or --agent with an exact UUID.')
    # Validate identity even for noninteractive continuations.
    if not re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', value):
        raise ValueError('Use the complete session UUID.')
    return agent, value


class CLIParser(argparse.ArgumentParser):
    def error(self, message):
        # Keep argparse's exit code while making piped failures machine-readable.
        if not sys.stdout.isatty():
            print(json.dumps({'error': message, 'automatic_retry': False}), file=sys.stderr)
            self.exit(2)
        super().error(message)


def output_arguments(p, depth):
    formats = p.add_mutually_exclusive_group()
    for flag in ('json', 'table'):
        formats.add_argument('--' + flag, dest=f'output_formats_{depth}', action='append_const',
                             const=flag, default=argparse.SUPPRESS,
                             help=f'Use {flag} output (default: table on TTY, JSON otherwise).')


def parser():
    p = CLIParser(prog='ai-bridge' if Path(sys.argv[0]).name == 'ai-bridge' else 'python -m ai_bridge',
                                description=__doc__)
    groups = p.add_subparsers(dest='group', required=True)
    agents = groups.add_parser('agents').add_subparsers(dest='action', required=True)
    agents.add_parser('status')
    for action in ('inspect', 'stop'):
        agents.add_parser(action).add_argument('job_id')
    tasks = groups.add_parser('tasks').add_subparsers(dest='action', required=True)
    for action in ('list', 'create'):
        tasks.add_parser(action)
    tasks.add_parser('inspect').add_argument('task_id')
    clean = tasks.add_parser('cleanup')
    clean.add_argument('task_id')
    clean.add_argument('--dry-run', action='store_true')
    clean.add_argument('--allow-uninspectable', action='store_true',
                       help='HIGH RISK: ignore only unreadable process cwd observations; deletion also requires --yes.')
    clean.add_argument('--yes', action='store_true', help='Explicitly approve removal of this exact managed task workspace.')
    sessions = groups.add_parser('sessions').add_subparsers(dest='action', required=True)
    listing = sessions.add_parser('list')
    listing.add_argument('--agent', choices=ag.AGENTS)
    listing.add_argument('--all', action='store_true', help='Include bridge-archived records; all modes are listed by default.')
    listing.add_argument('--mode', choices=('interactive', 'noninteractive', 'unknown'))
    listing.add_argument('--type', choices=('main', 'subagent', 'unknown'))
    listing.add_argument('--status', choices=('active_job', 'completed', 'idle', 'uncertain'))
    listing.add_argument('--project', help='Exact workspace directory.')
    listing.add_argument('--task-id')
    listing.add_argument('--limit', type=int, default=30)
    listing.add_argument('--offset', type=int, default=0)
    for action in ('inspect', 'continue', 'open', 'promote', 'fork', 'rename', 'archive', 'unarchive'):
        command = sessions.add_parser(action)
        command.add_argument('session')
        command.add_argument('--agent', choices=ag.AGENTS)
        if action in ('open', 'promote', 'fork'):
            command.add_argument('--dry-run', action='store_true')
        if action == 'rename':
            command.add_argument('name', help='Bridge display alias; native transcript is unchanged.')
        if action == 'continue':
            turn_arguments(command)
    start = sessions.add_parser('start')
    start.add_argument('--agent', choices=ag.AGENTS, required=True)
    turn_arguments(start)
    # Suppressed defaults let a global choice survive subparser parsing.
    def add_formats(command, depth=0):
        output_arguments(command, depth)
        for action in command._actions:
            if isinstance(action, argparse._SubParsersAction):
                for child in action.choices.values():
                    add_formats(child, depth + 1)
    add_formats(p)
    return p


def turn_arguments(p):
    p.add_argument('prompt')
    p.add_argument('--cwd')
    p.add_argument('--task-id')
    p.add_argument('--permission', choices=ag.PERMISSIONS)
    p.add_argument('--model')
    p.add_argument('--timeout', type=int)


def dispatch(args, cfg, jobs):
    if args.group == 'agents':
        if args.action == 'status':
            return runtime(jobs)
        return stop_agent(jobs, args.job_id) if args.action == 'stop' else runtime(jobs, args.job_id)
    if args.group == 'tasks':
        tasks = Tasks(cfg)
        if args.action == 'list':
            return tasks.list()
        if args.action == 'create':
            with management_lock(cfg):
                return tasks.create()
        if args.action == 'inspect':
            return tasks.inspect(args.task_id)
        if not args.dry_run and not args.yes:
            raise ValueError('Preview with --dry-run; removal requires --yes and an exact task ID.')
        return tasks.cleanup(args.task_id, jobs, args.dry_run,
                             allow_uninspectable=args.allow_uninspectable, confirmed=args.yes)
    sessions = Sessions(cfg, jobs)
    if args.action == 'list':
        rows, unavailable = [], {}
        for agent in (ag.AGENTS if not args.agent else (args.agent,)):
            try:
                rows += [dict(row, agent=agent) for row in sessions.enrich(
                    agent, ag.list_sessions(agent, args.project, None), args.all)]
            except (OSError, ValueError, RuntimeError) as error:
                if args.agent:
                    raise
                unavailable[agent] = str(error)
        for field, wanted in (('mode', args.mode), ('session_type', args.type),
                              ('status', args.status), ('task_id', args.task_id)):
            if wanted:
                rows = [row for row in rows if row.get(field, 'unknown') == wanted]
        offset, limit = max(0, args.offset), max(1, min(args.limit, 100))
        page = rows[offset:offset + limit]
        return {'sessions': page, 'total': len(rows), 'offset': offset, 'unavailable_agents': unavailable,
                'next_offset': offset + len(page) if offset + len(page) < len(rows) else None}
    agent, sid = (args.agent, None) if args.action == 'start' else target(args.session, args.agent)
    if args.action in ('start', 'continue'):
        return start_agent(cfg, jobs, agent, args.prompt, args.cwd, sid, args.permission,
                           args.model, args.task_id, args.timeout)
    if args.action == 'inspect':
        return sessions.enrich(agent, [ag.session_info(agent, sid)], True)[0]
    if args.action in ('rename', 'archive', 'unarchive'):
        return sessions.overlay(agent, sid, **({'alias': args.name} if args.action == 'rename' else
                                             {'archived': args.action == 'archive'}))
    if not args.dry_run and not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise ValueError('Interactive opening requires a real host terminal; use --dry-run for a plan.')
    with management_lock(cfg):
        if jobs.running_for_session(agent, sid):
            raise ValueError('Session has an active or uncertain bridge job; interactive opening refused.')
        plan = sessions.interactive(agent, sid, args.action)
    if args.dry_run:
        return plan
    os.chdir(plan['cwd'])
    from .core import child_env
    os.execvpe(plan['argv'][0], plan['argv'], child_env())


def main(argv=None):
    command = parser()
    args = command.parse_args(argv)
    formats = [value for key, values in vars(args).items()
               if key.startswith('output_formats_') for value in values]
    if len(set(formats)) > 1:
        command.error('--json and --table cannot be combined.')
    table_output = formats[-1] == 'table' if formats else sys.stdout.isatty()
    try:
        cfg = config()
        result = dispatch(args, cfg, Jobs(cfg))
    except (OSError, ValueError, KeyError, RuntimeError) as error:
        print(json.dumps({'error': str(error), 'automatic_retry': False}), file=sys.stderr)
        return 1

    # Presentation must not turn a completed mutation into a failed command.
    # Finish rendering before writing anything so fallback is a single JSON result.
    rendered = None
    if table_output:
        try:
            from .cli_output import render
            rendered = render(args, result, width=shutil.get_terminal_size(fallback=(100, 24)).columns,
                              color=sys.stdout.isatty() and 'NO_COLOR' not in os.environ and
                              os.environ.get('TERM') != 'dumb')
        except Exception:
            # Optional formatting can fail independently of the action's success.
            pass
    print(rendered if rendered is not None else json.dumps(result, ensure_ascii=False, indent=2))
    return 0

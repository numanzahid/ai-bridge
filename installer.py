#!/usr/bin/env python3
"""Terminal update/setup flow with numbered steps and verified completion."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request

import deploy
from ai_bridge.local_instructions import install as install_local_instructions


def say(message):
    print(message, flush=True)


def run_command(argv, label, *, interactive=False):
    if interactive:
        subprocess.run(argv, check=True)
        return
    started = time.monotonic()
    with subprocess.Popen(argv) as process:
        while True:
            try:
                code = process.wait(timeout=5)
                break
            except subprocess.TimeoutExpired:
                say(f'    {label}: still working ({int(time.monotonic() - started)}s elapsed).')
            except KeyboardInterrupt:
                process.terminate()
                raise
    if code:
        raise RuntimeError(f'{label} failed (exit {code}).')


def read_settings(path):
    settings = {}
    for line in Path(path).read_text().splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        key, separator, value = line.partition('=')
        if separator:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            settings[key.strip()] = value
    return settings


def service_state():
    result = subprocess.run(['systemctl', 'show', 'ai-bridge', '--no-pager',
                             '--property=ActiveState', '--property=SubState', '--property=MainPID',
                             '--property=ExecStart'], capture_output=True, text=True, check=True, timeout=5)
    return dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)


def live_info(settings):
    # Keep credentials in memory/headers. Never print settings, request headers or response bodies.
    port = int(settings.get('AI_BRIDGE_PORT') or '8765')
    base = f'http://127.0.0.1:{port}'
    with urllib.request.urlopen(base + '/healthz', timeout=2) as response:
        if response.read(16) != b'ok':
            raise ValueError('Health check did not return ok.')
    headers = {'Authorization': 'Bearer ' + settings['AI_BRIDGE_TOKEN'],
               'Accept': 'application/json, text/event-stream', 'Content-Type': 'application/json'}
    allowed_host = settings.get('AI_BRIDGE_ALLOWED_HOSTS', '').split(',')[0].strip()
    if allowed_host:
        headers['Host'] = allowed_host
    body = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
            'params': {'name': 'system_info', 'arguments': {}}}
    request = urllib.request.Request(base + '/mcp', data=json.dumps(body).encode(), headers=headers)
    with urllib.request.urlopen(request, timeout=3) as response:
        payload = json.load(response)
    return json.loads(payload['result']['content'][0]['text'])


class Installer:
    def __init__(self, source=None, root=None, env_file=Path('/etc/ai-bridge/ai-bridge.env')):
        self.source = Path(source or Path(__file__).resolve().parent)
        self.root = Path(root or Path.home() / '.local/share/ai-bridge')
        self.env_file = Path(env_file)
        self.current_step = 'preparation'

    def step(self, number, total, text):
        self.current_step = text
        say(f'\n[{number}/{total}] {text}')

    def sudo(self, *args):
        return ([] if os.geteuid() == 0 else ['sudo']) + list(args)

    def authenticate(self):
        if not (self.root / 'current').is_dir():
            raise ValueError('No installation found for this account. Run bash install.sh first.')
        say('    Your Linux password may be requested. It stays hidden; authenticator codes are visible.')
        if os.geteuid() != 0:
            run_command(['sudo', '-v'], 'Checking installation privileges', interactive=True)

    def enroll(self, *, offer=False):
        settings = read_settings(self.env_file)
        python = self.root / 'current/venv/bin/python'
        script = self.root / 'current/app/ai_bridge/approvals.py'
        args = [str(python), '-I', str(script), '--env-file', str(self.env_file)]
        if settings.get('AI_BRIDGE_STATE_DIR'):
            args += ['--state-dir', settings['AI_BRIDGE_STATE_DIR']]
        result = subprocess.run(args + ['totp-status'], capture_output=True, text=True, check=True, timeout=5)
        enrolled = json.loads(result.stdout).get('authenticator') in ('ready', 'locked')
        if enrolled and settings.get('AI_BRIDGE_SUDO_TOTP_SECRET'):
            say('    Authenticator already enrolled. Keeping your existing setup; no new code needed.')
            return
        if offer and input('    Set up phone authenticator approval now? [Y/n] ').strip().lower() in ('n', 'no'):
            say('    Skipped. Host approval remains available. Set up later: bash install.sh --setup-authenticator')
            return
        say('    Opening authenticator setup. Scan the QR and enter the six-digit code shown by your app.')
        run_command(args + ['totp-rotate' if enrolled else 'totp-setup'], 'Authenticator setup', interactive=True)

    def restart(self, *, timeout=180):
        expected = deploy.release_info((self.root / 'current').resolve())['version']
        expected_executable = str((self.root / 'current').resolve() / 'venv/bin/python')
        before = service_state().get('MainPID', '0')
        settings = read_settings(self.env_file)
        say('    Requesting restart. Waiting for the old server to stop and the new server to respond...')
        run_command(self.sudo('systemctl', '--no-block', 'restart', 'ai-bridge'), 'Requesting service restart')
        started = time.monotonic()
        while time.monotonic() - started < timeout:
            state = service_state()
            active, substate = state.get('ActiveState', 'unknown'), state.get('SubState', 'unknown')
            say(f'    Service: {active}/{substate} | {int(time.monotonic() - started)}s elapsed')
            if active == 'failed':
                raise RuntimeError('Service failed to start. Check: sudo journalctl -u ai-bridge -n 50')
            if active == 'active' and substate == 'running' and state.get('MainPID', '0') not in ('0', before) \
                    and expected_executable in state.get('ExecStart', ''):
                try:
                    info = live_info(settings)
                    if info.get('bridge_version') == expected:
                        return info
                except (OSError, ValueError, KeyError, TypeError):
                    say('    Waiting for the new version and authenticated health check...')
            time.sleep(2)
        raise RuntimeError('Restart timed out. Check: systemctl status ai-bridge --no-pager')

    def complete(self, info, release=None):
        say('\nCOMPLETE — AI Bridge is running and the authenticated health check passed.')
        say(f'    Version: {info["bridge_version"]}')
        say(f'    Authenticator: {info.get("sudo_authenticator", "unknown")}')
        if release and (release / 'test-summary.json').is_file():
            summary = json.loads((release / 'test-summary.json').read_text())
            say(f'    Tests: {summary["passed"]} passed, {summary["failed"]} failed, '
                f'{summary["errors"]} errors, {summary["skipped"]} skipped.')
        if info.get('sudo_authenticator') == 'unavailable':
            say('    Authenticator needs attention. Run: bash install.sh --setup-authenticator')
        say('    Reconnect your MCP client. You can close this terminal; no more input is needed.')

    def local_agent_instructions(self):
        say('    Updating local Codex and Claude Code tool preferences (web MCP guidance stays separate).')
        for result in install_local_instructions():
            say(f'    {result["status"]}: {result["path"]}')
        say('    Start new local agent sessions to load these preferences.')

    def update(self):
        self.step(1, 5, 'Checking installation and privileges')
        self.authenticate()
        current = deploy.release_info((self.root / 'current').resolve())['version']
        target = deploy.versioning.check(self.source)
        say(f'    Current: {current} | New: {target}')
        self.step(2, 5, 'Building the new release and running tests')
        release = deploy.stage(self.root, self.source)
        self.step(3, 5, 'Activating the tested release')
        run_command(['bash', str(self.source / 'install.sh'), '--activate', str(release)], 'Activating release')
        self.local_agent_instructions()
        self.step(4, 5, 'Checking authenticator setup')
        self.enroll(offer=True)
        self.step(5, 5, 'Restarting and verifying the service')
        info = self.restart()
        self.complete(info, release)

    def setup(self):
        self.step(1, 3, 'Checking installation and privileges')
        self.authenticate()
        self.step(2, 3, 'Setting up your phone authenticator')
        self.enroll()
        self.step(3, 3, 'Restarting and verifying the service')
        self.complete(self.restart())

    def restart_only(self):
        self.step(1, 2, 'Checking installation and privileges')
        self.authenticate()
        self.step(2, 2, 'Restarting and verifying the service')
        self.complete(self.restart())

    def finish_install(self):
        self.local_agent_instructions()
        self.step(1, 2, 'Checking authenticator setup')
        if os.geteuid() == 0:
            say('    Root service: privileged approval does not apply.')
        else:
            self.enroll(offer=True)
        self.step(2, 2, 'Starting and verifying the service')
        self.complete(self.restart())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('update', 'setup-authenticator', 'restart', 'finish-install'))
    args = parser.parse_args(argv)
    if os.environ.get('AI_BRIDGE_CHILD') or not (sys.stdin.isatty() and sys.stdout.isatty()):
        print('Run this command in a host terminal. Use --stage for unattended staging.', file=sys.stderr)
        return 2
    installer = Installer()
    try:
        {'update': installer.update, 'setup-authenticator': installer.setup,
         'restart': installer.restart_only, 'finish-install': installer.finish_install}[args.action]()
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError,
            subprocess.TimeoutExpired, EOFError, KeyboardInterrupt) as error:
        # Avoid dumping command arguments or HTTP response bodies into terminal/logs.
        say(f'\nFAILED or cancelled during: {installer.current_step}.')
        say(f'    {str(error) or "Cancelled by user."}')
        say('    For service details: systemctl status ai-bridge --no-pager')
        say('    To retry a restart with progress: bash install.sh --restart')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

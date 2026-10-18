"""install.sh behaviour: whole-script runs with fake system commands, and isolated script sections."""
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest

from helpers import install_section


class InstallScriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root/'home'
        self.home.mkdir()
        self.bin = self.root/'bin'
        self.bin.mkdir()

    def command(self, name, body):
        path = self.bin/name
        path.write_text(body)
        path.chmod(0o700)

    def shell(self, *args, uid=0, script='install.sh', home=None):
        self.command('id', '#!/bin/sh\ncase "$1" in -u) echo '+str(uid)+';; -un) echo test-user;; *) echo test-group;; esac\n')
        env = dict(os.environ, HOME=home or str(self.home), PATH=str(self.bin)+':'+os.environ['PATH'])
        return subprocess.run(['bash', script, *args], env=env, input='', text=True, capture_output=True, timeout=10)

    # Whole-script runs

    def test_simple_commands_dispatch_to_progress_flow(self):
        self.command('python3', '#!/bin/sh\nprintf "%s\\n" "$@"\n')
        for flag, action in (('--update', 'update'), ('--setup-authenticator', 'setup-authenticator'), ('--restart', 'restart')):
            result = self.shell(flag, uid=1000)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('installer.py\n' + action, result.stdout)

    def test_root_noninteractive_refuses_before_staging(self):
        result = self.shell()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('explicit --run-as-root', result.stderr)
        self.assertFalse((self.home/'.local/share/ai-bridge').exists())

    def test_nonroot_rejects_root_flags_and_unknown_options(self):
        for option in ('--run-as-root', '--dedicated-user', '--typo'):
            with self.subTest(option=option):
                self.assertNotEqual(self.shell(option, uid=1000).returncode, 0)
        self.assertIn('Usage:', self.shell('--typo', uid=1000).stderr)

    def test_nonroot_staging_never_calls_sudo_or_changes_active_app(self):
        self.command('python3', '#!/bin/sh\nprintf "staged-fixture\\n"\n')
        self.command('sudo', '#!/bin/sh\necho unexpected-sudo >&2\nexit 99\n')
        # Explicit --stage, then the no-flag path taken when an installation already exists.
        for name, args in (('explicit --stage', ('--stage',)), ('existing installation', ())):
            with self.subTest(name):
                if not args:
                    (self.home/'.local/share/ai-bridge/app').mkdir(parents=True)
                result = self.shell(*args, uid=1000)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('staged-fixture', result.stdout)
                self.assertNotIn('unexpected-sudo', result.stderr)
                self.assertFalse((self.home/'.local/share/ai-bridge/current').exists())
                if not args:
                    self.assertIn('staging only', result.stderr)

    def test_nonroot_uninstall_dispatches_before_staging_and_forwards_arguments(self):
        source = self.root/'source'; source.mkdir()
        (source/'install.sh').write_text(Path('install.sh').read_text())
        (source/'uninstall.sh').write_text(Path('uninstall.sh').read_text())
        self.command('sudo', '#!/bin/sh\nprintf "%s\\n" "$@"\n')
        self.command('python3', '#!/bin/sh\necho UNEXPECTED-PYTHON; exit 99\n')
        result = self.shell('--uninstall', '--dry-run', '--user', 'service-user', uid=1000,
                            script=str(source/'install.sh'), home='/unrelated')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('-n\n--\npython3\n-I\n', result.stdout)
        self.assertIn('--dry-run\n--user\nservice-user', result.stdout)
        self.assertNotIn('UNEXPECTED', result.stdout)

    def test_fresh_root_install_without_sudo_uses_root_unit_and_no_credential(self):
        source = self.root/'source'; source.mkdir()
        etc, unit = self.root/'etc', self.root/'service.unit'
        release = self.root/'fixture-release'; (release/'venv/bin').mkdir(parents=True)
        interpreter = release/'venv/bin/python'
        interpreter.write_text('#!/bin/sh\nexit 0\n'); interpreter.chmod(0o700)
        installer = source/'install.sh'
        text = Path('install.sh').read_text().replace('ETC_DIR="/etc/ai-bridge"', 'ETC_DIR='+shlex.quote(str(etc)))
        text = text.replace('UNIT_FILE="/etc/systemd/system/ai-bridge.service"', 'UNIT_FILE='+shlex.quote(str(unit)))
        installer.write_text(text)
        commands = {
            'id': '#!/bin/sh\ncase "$1" in -u) echo 0;; -un|-gn) echo root;; *) exit 1;; esac\n',
            'sudo': '#!/bin/sh\necho UNEXPECTED-SUDO >&2; exit 99\n',
            'systemctl': '#!/bin/sh\nprintf "%s\\n" "$*" >> '+shlex.quote(str(self.root/'systemctl.log'))+'\n',
            'systemd-run': '#!/bin/sh\nprintf "%s\\n" "$*" >> '+shlex.quote(str(self.root/'scheduled.log'))+'\n',
            'install': '#!'+sys.executable+'\nimport shutil,sys,os\na=sys.argv[1:]; shutil.copyfile(a[-2],a[-1]); os.chmod(a[-1],int(a[a.index("-m")+1],8))\n',
            'python3': '#!/bin/sh\nif [ "$2" = "check-port" ]; then echo available; exit 0; fi\n'
                       'if [ "$2" = "stage" ]; then echo '+shlex.quote(str(release))+'; exit 0; fi\n'
                       'if [ "$2" = "activate" ]; then exit 0; fi\n'
                       'if [ "$2" = "--record-install" ]; then exit 0; fi\n'
                       'if [ "$2" = "import ensurepip" ]; then exit 0; fi\n'
                       'if [ "$1" = "-m" ]; then exit 0; fi\n'
                       'exec '+shlex.quote(sys.executable)+' "$@"\n',
        }
        for name, body in commands.items():
            self.command(name, body)
        env = dict(os.environ, HOME=str(self.home), PATH=str(self.bin)+':'+os.environ['PATH'])
        result = subprocess.run(['/bin/bash', str(installer), '--run-as-root'], env=env, input='', capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('User=root\n', unit.read_text())
        self.assertIn('# LoadCredential=', unit.read_text())
        self.assertFalse((etc/'sudo.pass').exists())
        self.assertIn('no sudo password will be stored', result.stdout)
        self.assertIn('--on-active=3s', (self.root/'scheduled.log').read_text())
        self.assertEqual((etc/'ai-bridge.env').stat().st_mode & 0o777, 0o640)
        # A different/incomplete existing installation is refused before any write.
        previous = unit.read_bytes()
        again = subprocess.run(['/bin/bash', str(installer), '--run-as-root'], env=env, input='', capture_output=True, text=True, timeout=10)
        self.assertNotEqual(again.returncode, 0)
        self.assertIn('refusing to overwrite', again.stderr)
        self.assertEqual(unit.read_bytes(), previous)

    # Isolated script sections

    def test_root_wrapper_requires_no_sudo_binary(self):
        wrapper = install_section('# Root can manage', '# Staging needs')
        result = subprocess.run(['bash', '-c', 'id() { echo 0; }; '+wrapper+'\nsudo -v; sudo printf root-ok'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, 'root-ok')

    def test_sudo_probe_ignores_timestamp_and_tests_shell(self):
        lines = [line for line in Path('install.sh').read_text().splitlines() if line.startswith('elif sudo -k -n')]
        self.assertEqual(len(lines), 1, 'install.sh sudo probe line not found')
        probe = lines[0]
        self.assertIn('-- bash -c true', probe)
        # Simulate cached auth that succeeds only when -k is absent.
        script = 'sudo() { [[ " $* " != *" -k "* ]]; }; ' + probe.replace('elif ', 'if ', 1).split(' && ')[0] + '; then exit 1; else exit 0; fi'
        self.assertEqual(subprocess.run(['bash', '-c', script]).returncode, 0)

    def test_root_prompt_shell_dispatches_both_choices(self):
        branch = install_section('if [ "$(id -u)" -eq 0 ] && [ "$MODE" != --run-as-root ]; then', '\nif [ "$(id -u)" -ne 0 ]; then')
        # Only the decision branch runs; no installer, account, or service action.
        import pty
        self.command('python3', '#!/bin/sh\nprintf "BOOTSTRAP %s\\n" "$*"\n')
        for choice, expected in ((b'1\n', 'BOOTSTRAP /source/bootstrap.py --source /source'), (b'2\n', 'MODE=--run-as-root'), (b'bad\n', 'Invalid choice')):
            master, slave = pty.openpty()
            harness = 'set -e; die() { echo "$*"; exit 1; }; MODE=; SRC_DIR=/source; if true; then '+branch+'\necho "MODE=$MODE"'
            process = subprocess.Popen(['bash', '-c', harness], stdin=slave, stdout=slave, stderr=slave,
                                       env=dict(os.environ, PATH=str(self.bin)+':'+os.environ['PATH']))
            os.close(slave)
            try:
                os.write(master, choice)
                process.wait(timeout=3)
                output = b''
                while True:
                    try:
                        output += os.read(master, 4096)
                    except OSError:
                        break
                self.assertIn(expected, output.decode())
                self.assertEqual(process.returncode, 1 if choice == b'bad\n' else 0)
            finally:
                os.close(master)
                if process.poll() is None:
                    process.kill(); process.wait()

    def test_credential_writer_creates_private_file_without_newline(self):
        code = install_section("printf '%s' \"$PW\" | sudo -n python3 -c '", "\n' \"$PASS_FILE\"")
        target = self.root/'synthetic-credential'
        result = subprocess.run([sys.executable, '-c', code, str(target)], input=b'test-only-password', capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(target.read_bytes(), b'test-only-password')
        self.assertEqual(result.stdout, b'')

    def test_ownership_registration_failure_does_not_break_install(self):
        block = install_section('say "Recording private uninstall ownership metadata"\n', '\nsay "Sudo password for AI commands"')
        result = subprocess.run(['bash', '-c', 'set -e; sudo() { return 1; }; SRC_DIR=/fixture; APP_USER=test; '+block+'\necho continued'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn('continued', result.stdout)
        self.assertIn('manual uninstall review', result.stderr)

    def test_unit_migration_preserves_settings_and_original_rollback(self):
        script = install_section("<<'UNITPY'\n", '\nUNITPY')
        unit = self.root/'ai-bridge.service'
        original = '[Service]\nEnvironmentFile=/private/config.env\nLoadCredential=sudo_pass:/private/sudo.pass\nKillMode=process\nEnvironment=PYTHONPATH=/old/app\nExecStart=/old/venv/bin/python -m ai_bridge\n'
        unit.write_text(original)
        selected = self.root/'release'
        subprocess.run([sys.executable, '-', str(unit), str(selected)], input=script, text=True, check=True)
        self.assertIn('ExecStart='+str(selected/'venv/bin/python'), unit.read_text())
        self.assertIn('LoadCredential=sudo_pass:/private/sudo.pass', unit.read_text())
        self.assertIn('EnvironmentFile=/private/config.env', unit.read_text())
        backup = unit.with_name('ai-bridge.service.before-versioned')
        self.assertEqual(backup.read_text(), original)
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
        subprocess.run([sys.executable, '-', str(unit), str(selected)], input=script, text=True, check=True)
        self.assertEqual(backup.read_text(), original)

    def test_busy_port_finishes_setup_without_scheduling_restart_loop(self):
        startup = install_section('sudo systemctl enable ai-bridge >/dev/null\n', '\ncat <<EOF')
        harness = 'set -e; say() { printf "%s\\n" "$*"; }; sudo() { echo "INVOKED $*"; }; '+startup
        for state in ('unavailable', 'available'):
            with self.subTest(state=state):
                result = subprocess.run(['bash', '-c', harness], env=dict(os.environ, PORT_STATE=state, PORT='8765', ENV_FILE='/fixture/ai-bridge.env'), capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                if state == 'unavailable':
                    self.assertNotIn('INVOKED', result.stdout)
                    self.assertIn('Installation complete. Startup pending', result.stderr)
                    self.assertIn('AI_BRIDGE_PORT', result.stderr)
                    self.assertIn('sudo systemctl restart ai-bridge', result.stderr)
                    self.assertIn('tunnel backend', result.stderr)
                else:
                    self.assertIn('INVOKED systemd-run', result.stdout)
                    self.assertIn('Installation complete.', result.stdout)


if __name__ == '__main__':
    unittest.main()

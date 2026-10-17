"""One detached process per job: bounded logs and completion across bridge restarts.

Not a scheduler: starts one supplied command once and never retries it.
"""
import json
import os
from pathlib import Path
import signal
import sys
import threading
import time

from .core import Config
from .execution import Capture, execute
from .job_paths import sidecar_path


def private_json(path: Path, value):
    import tempfile
    fd, name = tempfile.mkstemp(prefix='.job-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, ensure_ascii=False)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class Recorder:
    def __init__(self, root: Path, job_id: str, limit: int, agent=None):
        self.root, self.id = root, job_id
        sidecar_path(root, job_id, 'output', create=True)
        self.capture = Capture(limit)
        self.agent = agent
        self.pending = {"stdout": bytearray(), "stderr": bytearray()}
        self.dropping = {"stdout": False, "stderr": False}
        self.record_limit = max(262144, min(limit, 1048576))
        self.parsed = {'session_id': None, 'reported_model': None, 'final': None,
                       'is_error': False, 'progress': [], 'other': [], 'stats': {}}
        self.parser_error = None
        self.records_dropped = 0
        self.last_flush = 0
        self.version = 0

    def record(self, line):
        if not self.agent:
            return
        try:
            from .agents import parse_output
            parsed = parse_output(self.agent, line.decode(errors='replace'))
            for key in ('session_id', 'reported_model', 'final'):
                if parsed.get(key) is not None:
                    value = parsed[key]
                    if key == 'session_id' and (not isinstance(value,str) or len(value)>512):
                        self.records_dropped += 1
                        continue
                    if not isinstance(value,str):
                        value = json.dumps(value,ensure_ascii=False)
                    if len(value)>65536:
                        self.records_dropped += 1
                    self.parsed[key] = value[-65536:]
            self.parsed['is_error'] |= parsed['is_error']
            for key, count in (('progress', 100), ('other', 40)):
                self.parsed[key] = (self.parsed[key]+[str(v)[-4000:] for v in parsed[key]])[-count:]
            from .responses import render
            self.parsed['stats'].update(parsed['stats'])
            self.parsed['stats'] = json.loads(render(self.parsed['stats'],8192,lambda text:text))
        except Exception as error:
            self.parser_error = str(error)[:1000]

    def add(self, name, data):
        self.capture.add(data)
        if self.agent:
            # stderr may contain plain provider diagnostics. Preserve complete lines
            # without allowing a single unterminated JSON record to grow forever.
            for part in data.splitlines(keepends=True):
                ended = part.endswith(b'\n')
                if not self.dropping[name]:
                    self.pending[name].extend(part)
                    if len(self.pending[name]) > self.record_limit:
                        self.pending[name].clear()
                        self.dropping[name] = True
                        self.records_dropped += 1
                if ended:
                    if not self.dropping[name]:
                        self.record(self.pending[name])
                    self.pending[name].clear()
                    self.dropping[name] = False
        if time.monotonic()-self.last_flush >= 1:
            self.flush()

    def flush(self, final=False):
        if final:
            for name in self.pending:
                if self.pending[name] and not self.dropping[name]:
                    self.record(self.pending[name])
                    self.pending[name].clear()
        # Atomic snapshots: bounded head+tail, same bytes used for chunk reads.
        import tempfile
        path = self.root/(self.id+'.log')
        fd, temporary = tempfile.mkstemp(prefix='.log-', dir=self.root)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(self.capture.value())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        self.version += 1
        private_json(sidecar_path(self.root, self.id, 'output'), {
            'total_bytes': self.capture.total, 'retained_bytes': len(self.capture.value()),
            'truncated': self.capture.total > len(self.capture.value()),
            'records_dropped': self.records_dropped,
            'generation': self.version, 'parsed': self.parsed if self.agent else None,
            'parser_error': self.parser_error})
        self.last_flush = time.monotonic()


def main():
    # Only the trusted runner consumes this pipe. Target stdin is always empty.
    payload = json.load(sys.stdin)
    sys.stdin.close()
    root, job_id = Path(payload['root']), payload['id']
    cfg = Config(token='', sudo_password=payload.pop('password', None),
                 capture_bytes=8192, state_dir=root.parent)
    recorder = Recorder(root, job_id, payload['log_bytes'], payload.get('agent'))
    cancelled = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: cancelled.set())
    signal.signal(signal.SIGINT, lambda *_: cancelled.set())
    process_state = {}
    def privilege_changed(value):
        if process_state.get('privilege') != value:
            process_state['privilege'] = value
            private_json(sidecar_path(root, job_id, 'process'), process_state)
    def started(pid, identity):
        process_state.update(pid=pid, identity=identity)
        private_json(sidecar_path(root, job_id, 'process'), process_state)
    try:
        result = execute(cfg, payload['argv'], payload['cwd'], None, payload['sudo'],
                         on_output=recorder.add, cancel=cancelled.is_set, on_start=started, on_privilege=privilege_changed)
    except Exception as error:
        recorder.add('stderr', ('Job startup/execution failed: '+str(error)+'\n').encode())
        from .privilege import initial, fields
        result = {'exit_code': 1, 'error': str(error), 'cancelled': cancelled.is_set(),
                  **fields(getattr(error, 'privilege', initial(payload['sudo'])))}
    finally:
        cfg.sudo_password = None
    recorder.flush(final=True)
    result['ended'] = time.time()
    private_json(sidecar_path(root, job_id, 'outcome'), result)
    # Keep the legacy marker for rollback readers; never manufacture success.
    fd = os.open(root/(job_id+'.exit'),os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w') as stream:
        stream.write(str(result.get('exit_code') if result.get('exit_code') is not None else -1))


if __name__ == '__main__':
    main()

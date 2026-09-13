"""Durable, recoverable SQL execution. Optional SDK/data libraries load on use."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
from numbers import Integral, Real
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from pyodps3_runtime import redact


class ExecutionError(Exception):
    def __init__(self, message, exit_code, meta):
        self.exit_code = exit_code
        self.meta = dict(meta)
        super().__init__(redact(message))


def _utc():
    return datetime.now(timezone.utc).isoformat()


def _atomic_text(path, text):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='',
                                         dir=path.parent, prefix='.' + path.name + '-',
                                         suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class _Run:
    def __init__(self, odps, sql, instance_id, run_root, output):
        self.start = time.monotonic()
        root = Path(run_root) if run_root is not None else Path.cwd() / '.maxcompute-query-runs'
        self.directory = root.resolve() / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ-') + uuid.uuid4().hex)
        self.directory.mkdir(parents=True)
        self.path = self.directory / 'state.json'
        self.meta = dict(instance_id=instance_id, project=_name(odps.project),
                         endpoint=str(odps.endpoint), state='created', run_dir=str(self.directory),
                         sql_sha256=None, started_at=_utc(), updated_at=_utc(), elapsed_s=0.0,
                         logview=None, total_rows=None, downloaded_rows=0, displayed_rows=0,
                         truncated=None, restricted=False, output_path=str(output) if output else None,
                         scanned=None)
        if sql is not None:
            self.sql(sql)
        self.update()

    def sql(self, sql):
        _atomic_text(self.directory / 'query.sql', sql)
        self.meta['sql_sha256'] = hashlib.sha256(sql.encode('utf-8')).hexdigest()

    def update(self, **changes):
        self.meta.update(changes)
        self.meta.update(updated_at=_utc(), elapsed_s=round(time.monotonic() - self.start, 3))
        _atomic_text(self.path, json.dumps(self.meta, ensure_ascii=False, indent=2) + '\n')

    def announce(self, stderr):
        print(redact(f"instance_id={self.meta['instance_id']} project={self.meta['project']} run_record={self.path}"),
              file=stderr, flush=True)

    def error(self, exc, code, state):
        message = redact(str(exc))
        try:
            self.update(state=state, error=message)
        except Exception as persist_error:
            self.meta.update(state=state, error=message)
            message += '\nRun record update failed: ' + redact(persist_error)
        message += '\nrun_record=' + str(self.path)
        if self.meta['instance_id']:
            command = [str(Path(sys.executable).resolve()), '-X', 'utf8', '-B',
                       str(Path(__file__).with_name('mc_query.py').resolve()), 'sql',
                       '--instance-id', str(self.meta['instance_id']), '--project', str(self.meta['project'])]
            message += '\n恢复（从原实例重新下载）: ' + subprocess.list2cmdline(command)
        else:
            message += '\n提交状态未知；请先核实服务端实例，不能自动重新提交。'
        return ExecutionError(message, code, self.meta)


def _name(value):
    if isinstance(value, str):
        return value
    name = getattr(value, 'name', None)
    return name if isinstance(name, str) else None


def _classify(sql):
    from sql_utils import assert_readonly, tokenize
    assert_readonly(sql)
    tokens = tokenize(sql)
    return tokens[0].word in {'SELECT', 'WITH', 'READ'}


@contextmanager
def _interrupt_guard():
    """PyODPS 0.12.6 swallows SIGINT in its wait loop; remember it locally."""
    seen = [False]
    main = threading.current_thread() is threading.main_thread()
    previous = signal.getsignal(signal.SIGINT) if main else None
    def handler(signum, frame):
        seen[0] = True
        raise KeyboardInterrupt()
    if main:
        signal.signal(signal.SIGINT, handler)
    try:
        yield seen
    finally:
        if main:
            signal.signal(signal.SIGINT, previous)


def _remote_failed(instance):
    """An SDK error alone is not evidence that the remote task failed."""
    try:
        return instance.is_terminated(retry=False) is True and instance.is_successful(retry=False) is False
    except Exception:
        return False


def _validate(max_rows, wait_timeout, batch_size, sql, instance_id, save):
    for name, value in [('max_rows', max_rows), ('batch_size', batch_size)]:
        if isinstance(value, bool) or not isinstance(value, Integral) or value <= 0:
            raise ValueError(f'{name} must be a positive integer')
    if isinstance(wait_timeout, bool) or not isinstance(wait_timeout, Real) or not math.isfinite(wait_timeout) or wait_timeout <= 0:
        raise ValueError('wait_timeout must be positive and finite')
    if (sql is None) == (instance_id is None):
        raise ValueError('Provide exactly one of sql or instance_id')
    if instance_id is not None and (not isinstance(instance_id, str) or not instance_id.strip()):
        raise ValueError('instance_id must be nonempty')
    table = _classify(sql) if sql is not None else None
    output = Path(save).expanduser().resolve() if save is not None else None
    if output is not None:
        if output.suffix.lower() not in {'.csv', '.xlsx'}:
            raise ValueError('save must end in .csv or .xlsx')
        if table is False:
            raise ValueError('DESC/DESCRIBE/SHOW/EXPLAIN return raw text; export requires tabular SQL')
        if not output.parent.is_dir() or output.is_dir():
            raise ValueError('Output parent must exist and output must be a file')
    return table, output


def execute_query(odps, *, sql=None, instance_id=None, max_rows=200,
                  wait_timeout=600, save=None, batch_size=10000, run_root=None, stderr=None):
    """Submit once or reconnect; return (preview DataFrame/raw text/None, metadata)."""
    table, output = _validate(max_rows, wait_timeout, batch_size, sql, instance_id, save)
    stderr = sys.stderr if stderr is None else stderr
    from odps.errors import ODPSError, WaitTimeoutError
    try:
        run = _Run(odps, sql, instance_id, run_root, output)
    except Exception as exc:
        raise ExecutionError('Cannot create local run record: ' + str(exc), 6, {}) from exc
    instance = None
    stage = 'recovery' if instance_id is not None else 'submit'
    try:
        if instance_id is None:
            run.update(state='submitting')
            instance = odps.run_sql(sql)
            known_id = getattr(instance, 'id', None)
            if not isinstance(known_id, str) or not known_id:
                raise RuntimeError('Submission returned no instance ID; state is unknown')
            # Keep ID even if subsequent local disk writes fail.
            run.meta['instance_id'] = known_id
            run.update(state='submitted')
            run.announce(stderr)
        else:
            run.update(state='recovering')
            run.announce(stderr)
            instance = odps.get_instance(instance_id, project=run.meta['project'])
            returned_id = getattr(instance, 'id', None)
            returned_project = _name(getattr(instance, 'project', None))
            if isinstance(returned_id, str) and returned_id != instance_id:
                raise RuntimeError(f'Recovered instance ID mismatch: {returned_id}')
            if returned_project is not None and returned_project != run.meta['project']:
                raise RuntimeError(f'Recovered instance project mismatch: {returned_project}')
        stage = 'wait'
        try:
            logview = instance.get_logview_address()
            if isinstance(logview, str):
                run.update(logview=logview)
        except Exception:
            pass
        run.update(state='waiting')
        with _interrupt_guard() as interrupted:
            instance.wait_for_success(timeout=wait_timeout, blocking=False, on_exception=lambda exc: True)
            if interrupted[0]:
                raise KeyboardInterrupt()
        if instance.is_successful(retry=False) is not True:
            raise RuntimeError('Instance has not successfully completed; results were not read')
        stage = 'reader'
        run.update(state='reading')
        if sql is None:
            query_getter = getattr(instance, 'get_sql_query', None)
            recovered_sql = query_getter() if callable(query_getter) else None
            if isinstance(recovered_sql, str) and recovered_sql.strip():
                run.sql(recovered_sql)
                run.update()
                table = _classify(recovered_sql)
            else:
                table = True
        if not table:
            if output is not None:
                raise ValueError('Raw task results cannot be exported as CSV/XLSX')
            results = instance.get_task_results()
            data = '\n'.join(str(v) for v in results.values()) if isinstance(results, dict) else str(results)
        else:
            data = _read_result(instance, run, max_rows, output, batch_size)
        run.update(state='succeeded')
        return data, dict(run.meta)
    except KeyboardInterrupt:
        try:
            run.update(state='interrupted')
        except Exception:
            pass
        raise
    except Exception as exc:
        if stage == 'submit':
            state, code = 'submission_unknown', 4
        elif stage == 'recovery':
            state, code = 'recovery_failed', 4
        elif stage == 'reader':
            state, code = 'download_failed', 6
        elif isinstance(exc, WaitTimeoutError):
            state, code = 'wait_timeout', 5
        else:
            state = 'failed' if not isinstance(exc, (ConnectionError, TimeoutError)) and _remote_failed(instance) else 'wait_unknown'
            code = 4
        raise run.error(exc, code, state) from exc


def _read_result(instance, run, max_rows, output, batch_size):
    with instance.open_reader(tunnel=True, limit=False, arrow=False, reopen=True) as reader:
        total = reader.count
        frame = reader.to_pandas(count=min(max_rows, total), n_process=1)
        run.update(total_rows=total, downloaded_rows=len(frame), displayed_rows=len(frame),
                   truncated=total > len(frame))
        return frame

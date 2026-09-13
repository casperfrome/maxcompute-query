"""Durable, recoverable SQL execution. Optional SDK/data libraries load on use."""
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
import csv
import hashlib
import json
import logging
import math
from numbers import Integral, Real
import os
from pathlib import Path
import signal
import sys
import tempfile
import threading
import time
import uuid

from pyodps3_runtime import redact

XLSX_MAX_DATA_ROWS = 1048575  # One of Excel's 1,048,576 rows is the header.
XLSX_MAX_COLUMNS = 16384


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
        self.pending_output = None
        root = Path(run_root) if run_root is not None else Path.cwd() / '.maxcompute-query-runs'
        self.directory = root.resolve() / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ-') + uuid.uuid4().hex)
        self.directory.mkdir(parents=True)
        self.path = self.directory / 'state.json'
        self.meta = dict(instance_id=instance_id, project=_name(odps.project),
                         endpoint=str(odps.endpoint), state='created', run_dir=str(self.directory),
                         sql_sha256=None, started_at=_utc(), updated_at=_utc(), elapsed_s=0.0,
                         logview=None, total_rows=None, downloaded_rows=0, displayed_rows=0,
                         truncated=None, restricted=None, output_path=str(output) if output else None,
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
            quote = lambda value: "'" + str(value).replace("'", "''") + "'"
            command = (f"& {quote(Path(sys.executable).resolve())} -X utf8 -B "
                       f"{quote(Path(__file__).with_name('mc_query.py').resolve())} sql "
                       f"--instance-id {quote(self.meta['instance_id'])} --project {quote(self.meta['project'])}")
            message += '\n恢复（PowerShell，从原实例重新下载）: ' + command
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


@contextmanager
def _private_sdk_progress():
    """Do not let PyODPS INFO progress print signed LogView URLs for this run."""
    logger = logging.getLogger('odps.models.instance')
    current_thread = threading.get_ident()
    class Filter(logging.Filter):
        def filter(self, record):
            return record.thread != current_thread
    guard = Filter()
    logger.addFilter(guard)
    try:
        yield
    finally:
        logger.removeFilter(guard)


def _scanned(instance):
    try:
        names = instance.get_task_names()
        inputs = dict(instance.get_task_summary(names[0])).get('Inputs') or {}
        total = sum(v[1] for v in inputs.values() if isinstance(v, (list, tuple)) and len(v) > 1)
        if not total:
            return None
        value = float(total)
        for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
            if value < 1024 or unit == 'TB':
                return f'{value:.1f}{unit}'
            value /= 1024
    except Exception:
        return None


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
    from odps.errors import WaitTimeoutError
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
        with _interrupt_guard() as interrupted, _private_sdk_progress():
            try:
                instance.wait_for_success(timeout=wait_timeout, blocking=False, on_exception=lambda exc: True)
            except Exception:
                if interrupted[0]:
                    raise KeyboardInterrupt() from None
                raise
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
        scanned = _scanned(instance)
        if output is not None:
            # Reader context, writer handles, validation, and optional summary all
            # finish before this sole publication point.
            run.update(state='publishing', scanned=scanned)
            os.replace(run.pending_output, output)
            run.pending_output = None
            try:
                run.update(state='succeeded')
            except Exception as record_error:
                run.meta.update(state='succeeded', record_error=redact(record_error))
                print(redact(f'结果已完整保存到 {output}；最终运行记录更新失败: {record_error}'),
                      file=stderr, flush=True)
        else:
            run.update(state='succeeded', scanned=scanned)
        return data, dict(run.meta)
    except KeyboardInterrupt:
        try:
            state = 'submission_unknown' if stage == 'submit' and not run.meta['instance_id'] else 'interrupted'
            failure = run.error('本地已中断；远端实例不会被自动取消。', 130, state)
            print(str(failure), file=stderr, flush=True)
        except Exception:
            pass
        raise
    except Exception as exc:
        if stage == 'submit':
            known_id = getattr(exc, 'instance_id', None)
            if not run.meta['instance_id'] and isinstance(known_id, str) and known_id:
                run.meta['instance_id'] = known_id
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
    finally:
        if run.pending_output is not None:
            run.pending_output.unlink(missing_ok=True)


def _read_result(instance, run, max_rows, output, batch_size):
    from odps.errors import NoPermission
    from pandas import DataFrame
    try:
        opened = instance.open_reader(tunnel=True, limit=False, arrow=False, reopen=True)
    except NoPermission:
        if output is not None:
            raise
        run.update(restricted=True, total_rows=None, truncated=True)
        opened = instance.open_reader(tunnel=True, limit=True, arrow=False, reopen=True)
    with opened as reader:
        if run.meta['restricted'] is None:
            run.update(restricted=False)
        names = list(reader.schema.names)
        count = getattr(reader, 'count', None)
        valid_count = isinstance(count, Integral) and not isinstance(count, bool) and count >= 0
        total = int(count) if valid_count and not run.meta['restricted'] else None
        run.update(total_rows=total)
        if output is not None:
            if total is None:
                raise ValueError('Full export requires a verified nonnegative integer result count')
            _export(reader, run, names, total, output, batch_size)
            return None
        # count=0 means "all rows" in parts of PyODPS; never pass it to SDK.
        requested = min(max_rows, int(count)) if valid_count else max_rows
        if requested == 0:
            frame = DataFrame(columns=names)
        else:
            frame = reader.to_pandas(count=requested, n_process=1)
        run.update(downloaded_rows=len(frame))
        _check_frame(frame, names)
        if len(frame) > requested or (valid_count and len(frame) != requested):
            raise ValueError(f'Preview row count mismatch: requested {requested}, received {len(frame)}')
        run.update(displayed_rows=len(frame), truncated=(True if run.meta['restricted'] else
                   total > len(frame) if total is not None else None))
        return frame


def _check_frame(frame, names):
    from pandas import DataFrame
    if not isinstance(frame, DataFrame):
        raise ValueError('Result batch is not a pandas DataFrame')
    if list(frame.columns) != names:
        raise ValueError('Result batch schema or column order changed')


def _xlsx_value(value):
    """Preserve scalar values that Excel would otherwise truncate or round."""
    import pandas as pd
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, Decimal):
        return str(value) if not value.is_nan() else None
    if isinstance(value, pd.Timestamp):
        return value.isoformat() if value.nanosecond or value.tz is not None else value.to_pydatetime()
    if hasattr(value, 'item') and not isinstance(value, (str, bytes)):
        value = value.item()
    if isinstance(value, Integral) and not isinstance(value, bool) and len(str(abs(value))) > 15:
        return str(value)
    if isinstance(value, Real) and not isinstance(value, bool):
        if math.isnan(value):
            return None
        if not math.isfinite(value):
            return str(value)
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.isoformat()
    if isinstance(value, str) and len(value) > 32767:
        raise ValueError('Excel cell exceeds 32767 characters; use CSV to preserve the value')
    return value


def _xlsx_row(sheet, values):
    from openpyxl.cell import WriteOnlyCell
    result = []
    for value in values:
        value = _xlsx_value(value)
        cell = WriteOnlyCell(sheet, value=value)
        if isinstance(value, str):
            cell.data_type = 's'  # A SQL string beginning with '=' is still data.
        result.append(cell)
    return result


def _export(reader, run, names, total, output, batch_size):
    excel = output.suffix.lower() == '.xlsx'
    if excel and (total > XLSX_MAX_DATA_ROWS or len(names) > XLSX_MAX_COLUMNS):
        raise ValueError('Result exceeds Excel limits (1048575 data rows / 16384 columns); use CSV')
    temporary = None
    workbook = None
    stream = None
    sheet = None
    try:
        fd, filename = tempfile.mkstemp(dir=output.parent, prefix='.' + output.name + '-', suffix='.tmp')
        temporary = Path(filename)
        # The run owns cleanup from creation, including exceptions while closing.
        run.pending_output = temporary
        os.close(fd)
        if excel:
            from openpyxl import Workbook
            workbook = Workbook(write_only=True)
            sheet = workbook.create_sheet('Results')
            sheet.append(_xlsx_row(sheet, names))
        else:
            stream = temporary.open('w', encoding='utf-8-sig', newline='')
            csv.writer(stream).writerow(names)
        # One iterator, bounded DataFrames, no accumulated frames or concat.
        for frame in reader.iter_pandas(batch_size=batch_size, n_process=1):
            downloaded = run.meta['downloaded_rows'] + len(frame)
            run.update(downloaded_rows=downloaded)
            _check_frame(frame, names)
            if downloaded > total:
                raise ValueError(f'Export row count mismatch: expected {total}, received at least {downloaded}')
            if excel:
                for values in frame.itertuples(index=False, name=None):
                    sheet.append(_xlsx_row(sheet, values))
            else:
                frame.to_csv(stream, index=False, header=False)
        if run.meta['downloaded_rows'] != total:
            raise ValueError(f"Export row count mismatch: expected {total}, received {run.meta['downloaded_rows']}")
        if workbook is not None:
            workbook.save(temporary)
        else:
            stream.flush()
            os.fsync(stream.fileno())
            stream.close()
            stream = None
        run.update(displayed_rows=0, truncated=False)
    finally:
        if stream is not None:
            stream.close()
        if workbook is not None:
            # Close a partially written worksheet too (e.g. interruption/invalid cell).
            if sheet is not None and not sheet.closed:
                sheet.close()
            workbook.close()
            writer = getattr(sheet, '_writer', None)
            if writer is not None and Path(writer.out).exists():
                writer.cleanup()

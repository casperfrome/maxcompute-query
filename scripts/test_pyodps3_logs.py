"""Console log contracts; external DataWorks calls use a deterministic fake."""
import io
import json
from pathlib import Path

import pytest

import debug_pyodps3 as d
from test_debug_pyodps3 import FakeAPI


CHAIN = ('Traceback (most recent call last):\n'
         '  File "<pyodps_user_code>", line 12, in upload\n'
         '    raise ValueError("上传失败")\n'
         'ValueError: 上传失败\n'
         '详情：行数不一致\n\n'
         'The above exception was the direct cause of the following exception:\n\n'
         'Traceback (most recent call last):\n'
         '  File "/tmp/job.py", line 30, in main\n'
         '    upload()\n'
         'RuntimeError: 节点失败\n'
         'Exit code of the Shell command 1\n')


@pytest.mark.parametrize('prefix', ['', '2026-09-12 16:00:00 ERROR ',
                                  'WARNING:odps.pyodpswrapper:',
                                  '2026-09-12 16:00:00 WARNING:odps.pyodpswrapper:',
                                  '2026-09-12 16:00:00 ',
                                  '[2026-09-12 16:00:00] INFO WARNING:odps.pyodpswrapper:'])
def test_complete_prefixed_chain(prefix):
    raw = '\n'.join(prefix + line for line in CHAIN.splitlines())
    result = d.analyze_log(raw)
    assert [x['type'] for x in result['exceptions']] == ['ValueError', 'RuntimeError']
    assert '详情：行数不一致' in result['exceptions'][0]['message']
    assert result['user_code_lines'] == [12]
    block = result['tracebacks'][0]
    assert len(result['tracebacks']) == 1
    assert [x['line'] for x in block['frames']] == [12, 30]
    assert 'raise ValueError' in block['text'] and 'direct cause' in block['text']
    assert 'Exit code' not in block['text']


def test_separate_tracebacks_custom_exception_and_html():
    raw = (CHAIN + '\n打印阶段完成\nTraceback (most recent call last):\n'
           '  File "&lt;pyodps_user_code&gt;", line 44, in validate\n'
           'domain.ValidationFailure: invalid\n')
    result = d.analyze_log(raw)
    assert len(result['tracebacks']) == 2
    assert result['exceptions'][-1]['type'] == 'ValidationFailure'
    assert result['user_code_lines'] == [12, 44]


class LogsAPI:
    def __init__(self):
        self.calls = []
        self.current_run = 2
        self.raw = '开始处理\r\n' + CHAIN + 'password=FAKE_LOG_SECRET\n'
        self.log_error = None
        self.status_error = None
        self.current_status = 'Success'
        self.empty_count = 0

    def call(self, operation, payload, legacy=False):
        self.calls.append((operation, dict(payload)))
        if operation == 'GetTaskInstance':
            if self.status_error:
                raise self.status_error
            return {'TaskInstance': {'Id': 200, 'ProjectId': 334179,
                'ProjectEnv': 'Prod', 'TaskId': 10, 'TaskName': 'job', 'TaskType': 'PYODPS3',
                'Bizdate': 1789056000000, 'RunNumber': self.current_run,
                'Status': self.current_status, 'StartedTime': 1000, 'FinishedTime': 2000,
                'WorkflowInstanceId': 100}}
        if operation == 'ListTaskInstances':
            assert payload['Bizdate'] == 1789056000000
            if payload.get('Id') == 200:
                items = [{'Id': 200, 'RunNumber': 1, 'Status': 'Failure',
                          'StartedTime': 100, 'FinishedTime': 200},
                         {'Id': 200, 'RunNumber': 2, 'Status': 'Success'}]
                return {'PagingInfo': {'TotalCount': 2, 'TaskInstances': items}}
            page = payload['PageNumber']
            assert payload.get('TaskName') == 'job' or payload.get('TaskId') == 10
            items = [{'Id': page * 100, 'TaskId': 10, 'TaskName': 'job', 'RunNumber': 1,
                      'Status': 'Success', 'StartedTime': 1000, 'ProjectEnv': 'Prod',
                      'ProjectId': 334179, 'TaskType': 'PYODPS3'}]
            return {'PagingInfo': {'TotalCount': 2, 'PageSize': 1,
                                   'PageNumber': page, 'TaskInstances': items}}
        if operation == 'GetTaskInstanceLog':
            if self.log_error:
                raise self.log_error
            assert payload['Id'] == 200
            if self.empty_count:
                self.empty_count -= 1
                return {'TaskInstanceLog': ''}
            return {'TaskInstanceLog': self.raw, 'RequestId': 'log-request'}
        raise AssertionError('Unexpected / mutating operation: ' + operation)


def run_cli(monkeypatch, capsys, api, argv):
    monkeypatch.setattr(d, 'DataWorksAPI', lambda: api)
    code = d.main(argv)
    out = capsys.readouterr()
    return code, out


def test_instances_lists_all_pages_without_selecting(monkeypatch, capsys):
    api = LogsAPI()
    code, out = run_cli(monkeypatch, capsys, api,
        ['instances', '--name', 'job', '--bizdate', '20260911'])
    result = json.loads(out.out)
    assert code == 0
    assert [x['Id'] for x in result['instances']] == [100, 200]
    assert result['project_env'] == 'Prod'
    assert all(op == 'ListTaskInstances' for op, _ in api.calls)


def test_direct_instance_text_and_tail_keep_raw_artifact(monkeypatch, capsys, tmp_path):
    api = LogsAPI()
    code, out = run_cli(monkeypatch, capsys, api,
        ['logs', '--instance-id', '200', '--output-dir', str(tmp_path),
         '--format', 'text', '--tail', '2'])
    assert code == 0  # Remote latest run is Success, regardless of text in its log.
    assert 'password=[REDACTED]' in out.out and 'FAKE_LOG_SECRET' not in out.out + out.err
    assert 'raise ValueError' not in out.out
    bundle = next(tmp_path.iterdir())
    assert (bundle / 'run-2.log').read_bytes() == api.raw.encode('utf-8')
    manifest = d.load_manifest(bundle)
    assert manifest['run_number'] == 2 and manifest['log_state'] == 'available'
    assert [p for op, p in api.calls if op == 'GetTaskInstanceLog'][-1] == {'Id': 200, 'RunNumber': 2}
    assert not (bundle / 'snapshot.py').exists()


def test_historical_log_has_historical_status(monkeypatch, capsys, tmp_path):
    api = LogsAPI()
    code, out = run_cli(monkeypatch, capsys, api,
        ['logs', '--instance-id', '200', '--run-number', '1', '--output-dir', str(tmp_path)])
    result = json.loads(out.out)
    assert code == 1 and result['remote_status'] == 'Failure'
    assert result['run_number'] == 1 and result['started_time'] == 100
    assert [x['type'] for x in result['analysis']['exceptions']] == ['ValueError', 'RuntimeError']


def test_missing_history_does_not_borrow_latest_status(monkeypatch, capsys, tmp_path):
    api = LogsAPI()
    code, out = run_cli(monkeypatch, capsys, api,
        ['logs', '--instance-id', '200', '--run-number', '3', '--output-dir', str(tmp_path)])
    assert code == 3
    assert not any(op == 'GetTaskInstanceLog' for op, _ in api.calls)


@pytest.mark.parametrize('failure', ['permission', 'empty', 'status'])
def test_refresh_retains_cached_log(monkeypatch, capsys, tmp_path, failure):
    api = LogsAPI()
    run_cli(monkeypatch, capsys, api,
        ['logs', '--instance-id', '200', '--output-dir', str(tmp_path)])
    bundle = next(tmp_path.iterdir())
    if failure == 'permission':
        api.log_error = d.APIError('GetTaskInstanceLog', '403', 'Forbidden', 'denied')
    elif failure == 'status':
        api.status_error = d.APIError('GetTaskInstance', '403', 'Forbidden', 'denied')
    else:
        api.raw = ''
    code, out = run_cli(monkeypatch, capsys, api, ['logs', '--run-dir', str(bundle)])
    result = json.loads(out.out)
    assert result['log_available'] is True and result['log_cached'] is True
    assert result['log_state'] == ('pending' if failure == 'empty' else 'error')
    assert code == (0 if failure == 'empty' else 5)
    assert '上传失败' in (bundle / 'run-2.log').read_text(encoding='utf-8')


def test_existing_bundle_pins_attempt_after_remote_rerun(monkeypatch, capsys, tmp_path):
    api = LogsAPI()
    run_cli(monkeypatch, capsys, api,
        ['logs', '--instance-id', '200', '--output-dir', str(tmp_path)])
    bundle = next(tmp_path.iterdir())
    api.current_run = 3
    code, out = run_cli(monkeypatch, capsys, api, ['logs', '--run-dir', str(bundle)])
    result = json.loads(out.out)
    assert result['run_number'] == 2 and code == 0
    assert [p['RunNumber'] for op, p in api.calls if op == 'GetTaskInstanceLog'] == [2, 2]


def test_old_saved_bundle_still_reads_text(monkeypatch, capsys, tmp_path):
    api = FakeAPI()
    bundle = d.inspect_saved(api, file_id=10, bizdate='20260911', output_dir=tmp_path)
    d.submit_saved(api, bundle)
    code, out = run_cli(monkeypatch, capsys, api,
        ['logs', '--run-dir', str(bundle), '--format', 'traceback'])
    assert code == 1 and 'ValueError: failure' in out.out and 'line 2' in out.out
    assert len(api.submissions) == 1


def test_terminal_empty_log_waits_for_log_without_resubmitting(monkeypatch, capsys, tmp_path):
    api = LogsAPI()
    api.empty_count = 1
    monkeypatch.setattr(d.time, 'sleep', lambda _: None)
    code, out = run_cli(monkeypatch, capsys, api,
        ['logs', '--instance-id', '200', '--wait', '--timeout', '5', '--output-dir', str(tmp_path)])
    assert code == 0 and json.loads(out.out)['log_state'] == 'available'
    assert len([x for x in api.calls if x[0] == 'GetTaskInstanceLog']) == 2


def test_empty_log_is_pending_not_remote_failure(monkeypatch, capsys, tmp_path):
    api = LogsAPI()
    api.raw = ''
    code, out = run_cli(monkeypatch, capsys, api,
        ['logs', '--instance-id', '200', '--output-dir', str(tmp_path)])
    result = json.loads(out.out)
    assert code == 0 and result['log_state'] == 'pending'
    assert result['remote_status'] == 'Success' and not result['log_available']


def test_stdin_partial_plain_text_does_not_connect(monkeypatch, capsys, tmp_path):
    def forbidden():
        raise AssertionError('Offline parsing must not create a client')
    monkeypatch.setattr(d, 'DataWorksAPI', forbidden)
    monkeypatch.setattr(d.sys, 'stdin', io.StringIO('普通中文输出\npassword=FAKE_LOG_SECRET\n'))
    code = d.main(['analyze-log', '--stdin', '--partial', '--format', 'text',
                   '--output-dir', str(tmp_path)])
    out = capsys.readouterr()
    assert code == 0 and '普通中文输出' in out.out and 'FAKE_LOG_SECRET' not in out.out
    bundle = next(tmp_path.iterdir())
    result = json.loads((bundle / 'analysis.json').read_text(encoding='utf-8'))
    assert result['possibly_truncated'] is True and result['capture_partial'] is True
    assert not result['tracebacks']


def test_traceback_display_redacts_secret_in_source_line(monkeypatch, capsys, tmp_path):
    raw = 'Traceback (most recent call last):\n  File "x.py", line 1\n    token="PRIVATE_TOKEN"\nValueError: failed\n'
    path = tmp_path / 'input.log'
    path.write_bytes(raw.encode('utf-8'))
    assert d.main(['analyze-log', '--file', str(path), '--format', 'traceback']) == 0
    out = capsys.readouterr()
    assert 'PRIVATE_TOKEN' not in out.out and 'ValueError: failed' in out.out
    assert path.read_bytes() == raw.encode('utf-8')


def test_multiline_details_and_urls_are_not_new_exceptions():
    raw = ('Traceback (most recent call last):\n  File "x.py", line 1\n'
           'RuntimeError: validation failed\ndetails: rows mismatch\nhttps://example.invalid/context\n')
    result = d.analyze_log(raw)
    assert len(result['exceptions']) == 1
    assert 'details: rows mismatch\nhttps:' in result['exceptions'][0]['message']


@pytest.mark.parametrize('message', ['', 'first line\n\nlast line'])
def test_python_traceback_empty_and_paragraph_messages(message):
    import traceback
    try:
        raise ValueError(message) if message else ValueError()
    except ValueError:
        raw = traceback.format_exc()
    result = d.analyze_log(raw)
    assert result['exceptions'] == [{'type': 'ValueError', 'message': message}]
    assert result['tracebacks'][0]['text'] == raw.rstrip('\n')


def test_completed_saved_bundle_keeps_verification(monkeypatch, capsys, tmp_path):
    api = FakeAPI()
    api.status_value = 'Success'
    bundle = d.inspect_saved(api, file_id=10, bizdate='20260911', output_dir=tmp_path)
    d.submit_saved(api, bundle)
    code, out = run_cli(monkeypatch, capsys, api, ['logs', '--run-dir', str(bundle)])
    assert code == 0 and json.loads(out.out)['saved_execution_verified'] is True


def test_missing_run_dir_is_not_created_and_does_not_connect(monkeypatch, capsys, tmp_path):
    api = LogsAPI()
    code, out = run_cli(monkeypatch, capsys, api, ['logs', '--run-dir', str(tmp_path / 'missing')])
    assert code == 3 and not api.calls
    assert not (tmp_path / 'missing').exists()


def test_truncation_flag_and_terminal_pending_timeout(monkeypatch, capsys, tmp_path):
    assert d.analyze_log('输出\n[日志截断]')['possibly_truncated']
    assert d.analyze_log('a' * (4 * 1024 * 1024))['possibly_truncated']
    api = LogsAPI()
    api.raw = ''
    code, out = run_cli(monkeypatch, capsys, api,
        ['logs', '--instance-id', '200', '--wait', '--timeout', '0', '--output-dir', str(tmp_path)])
    result = json.loads(out.out)
    assert code == 7 and result['wait_timed_out'] is True
    assert result['remote_status'] == 'Success' and result['log_state'] == 'pending'


def test_cached_previous_attempt_never_leaks_into_new_attempt(monkeypatch, capsys, tmp_path):
    api = LogsAPI()
    run_cli(monkeypatch, capsys, api,
        ['logs', '--instance-id', '200', '--run-number', '1', '--output-dir', str(tmp_path)])
    api.raw = ''
    bundle = next(tmp_path.iterdir())
    code, out = run_cli(monkeypatch, capsys, api,
        ['logs', '--run-dir', str(bundle), '--run-number', '2'])
    result = json.loads(out.out)
    assert code == 0 and not result['log_available'] and not result['log_cached']
    assert not result.get('log_file') and 'analysis' not in result
    assert (bundle / 'run-1.log').is_file()


def test_explicit_attempt_change_survives_metadata_failure(monkeypatch, capsys, tmp_path):
    api = LogsAPI()
    run_cli(monkeypatch, capsys, api,
        ['logs', '--instance-id', '200', '--run-number', '1', '--output-dir', str(tmp_path)])
    bundle = next(tmp_path.iterdir())
    api.status_error = d.APIError('GetTaskInstance', '403', 'Forbidden')
    code, out = run_cli(monkeypatch, capsys, api,
        ['logs', '--run-dir', str(bundle), '--run-number', '2'])
    assert code == 5 and json.loads(out.out)['remote_status'] is None
    api.status_error = None
    code, out = run_cli(monkeypatch, capsys, api, ['logs', '--run-dir', str(bundle)])
    assert code == 0 and json.loads(out.out)['run_number'] == 2


def test_historical_status_unavailable_keeps_status_unknown(monkeypatch, capsys, tmp_path):
    api = LogsAPI()
    call = api.call
    def missing_history(operation, payload, legacy=False):
        if operation == 'ListTaskInstances':
            return {'PagingInfo': {'TotalCount': 0, 'TaskInstances': []}}
        return call(operation, payload, legacy)
    api.call = missing_history
    code, out = run_cli(monkeypatch, capsys, api,
        ['logs', '--instance-id', '200', '--run-number', '1', '--output-dir', str(tmp_path)])
    result = json.loads(out.out)
    assert code == 5 and result['remote_status'] is None
    assert result['log_available'] and result['status_error']


@pytest.mark.parametrize('argv', [
    ['logs', '--instance-id', '200', '--run-dir', 'x'],
    ['logs', '--instance-id', '200', '--run-number', '0'],
    ['logs', '--instance-id', '200', '--tail', '0'],
    ['instances', '--name', 'job'],
    ['analyze-log', '--stdin', '--file', 'x'],
])
def test_invalid_selectors_fail_before_connecting(argv, monkeypatch):
    monkeypatch.setattr(d, 'DataWorksAPI', lambda: pytest.fail('Invalid args connected'))
    with pytest.raises(SystemExit) as exc:
        d.main(argv)
    assert exc.value.code == 2


# Exact traceback/footer excerpt from the first harmless Dev acceptance run.
# Environment preamble and credentials are deliberately absent from this fixture.
REAL_WRAPPER_FOOTER_CHAIN = r'''Traceback (most recent call last):
  File "<pyodps_user_code>", line 23, in <module>
    result = validate_sample(-1)
  File "<pyodps_user_code>", line 17, in validate_sample
    raise ValueError("诊断输入必须为非负数\n输入值=" + str(value))
ValueError: 诊断输入必须为非负数
输入值=-1
The above exception was the direct cause of the following exception:
Traceback (most recent call last):
  File "<pyodps_user_code>", line 25, in <module>
    raise RuntimeError("可识别的闭环诊断异常\n请保留完整异常链") from exc
RuntimeError: 可识别的闭环诊断异常
请保留完整异常链
2026-09-12 18:40:46,280 WARNING:odps.pyodpswrapper:
错误信息中似乎不包含 PyODPS 相关的代码，请检查自己的代码逻辑是否正确。
2026-09-12 18:40:46 INFO =================================================================
2026-09-12 18:40:46 INFO Exit code of the Shell command 1
2026-09-12 18:40:46 INFO --- Invocation of Shell command completed ---
2026-09-12 18:40:46 ERROR Shell run failed!
2026-09-12 18:40:46 ERROR Current task status: ERROR
'''


def test_real_wrapper_footer_kept_out_of_exception_and_raw_log_unchanged(tmp_path, capsys):
    source = tmp_path / 'console.log'
    raw = REAL_WRAPPER_FOOTER_CHAIN.encode('utf-8')
    source.write_bytes(raw)
    output = tmp_path / 'parsed'
    assert d.main(['analyze-log', '--file', str(source), '--format', 'traceback',
                   '--output-dir', str(output)]) == 0
    displayed = capsys.readouterr().out
    bundle = next(output.iterdir())
    result = json.loads((bundle / 'analysis.json').read_text(encoding='utf-8'))
    assert result['exceptions'] == [
        {'type': 'ValueError', 'message': '诊断输入必须为非负数\n输入值=-1'},
        {'type': 'RuntimeError', 'message': '可识别的闭环诊断异常\n请保留完整异常链'},
    ]
    assert len(result['tracebacks']) == 1
    assert [frame['line'] for frame in result['tracebacks'][0]['frames']] == [23, 17, 25]
    assert 'direct cause' in displayed and '请保留完整异常链' in displayed
    assert '错误信息中似乎不包含' not in displayed and '====' not in displayed
    assert source.read_bytes() == raw and (bundle / 'input.log').read_bytes() == raw


def test_wrapper_words_and_separator_inside_exception_message_are_preserved():
    message = ('用户诊断文字\n'
               '错误信息中似乎不包含 PyODPS 相关的代码，请检查自己的代码逻辑是否正确。\n'
               '=================================================================\n'
               '这是用户消息的下一段\n\n请保留细节')
    raw = ('Traceback (most recent call last):\n  File "x.py", line 8\n'
           'RuntimeError: ' + message + '\nExit code of the Shell command 1\n')
    result = d.analyze_log(raw)
    assert result['exceptions'] == [{'type': 'RuntimeError', 'message': message}]
    assert message in result['tracebacks'][0]['text']

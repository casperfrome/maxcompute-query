"""Offline contracts for saved snapshot execution. No real DataWorks writes."""
import hashlib
import json
from pathlib import Path

import pytest

import debug_pyodps3 as d


CODE = "# 中文\r\nprint('hello')\r\n"


class FakeAPI:
    def __init__(self):
        self.submissions = []
        self.status_value = 'Failure'
        self.actual_code = CODE
        self.log_text = 'Traceback (most recent call last):\n  File "<pyodps_user_code>", line 2, in main\nValueError: failure\nExit code of the Shell command 1'
        self.submit_error = None
        self.resource_error = None
        self.log_calls = []
        self.recurrence = 'Normal'

    def saved_task(self, name=None, file_id=None):
        return dict(task_name='test_task', file_id=10, file_type=1221, node_id=None,
            source='saved', sql_text=CODE, code_sha256=hashlib.sha256(CODE.encode()).hexdigest(),
            owner='owner', connection_name='odps_first', last_edit_time=123,
            commit_status=0, node_configuration={'ResourceGroupId': 9, 'ParaValue': ''})

    def call(self, operation, payload, legacy=False):
        if operation == 'ListResourceGroups':
            if self.resource_error:
                raise self.resource_error
            return {'Success': True, 'Data': [{'Id': 9, 'Identifier': 'S_res_test', 'Status': 0}]}
        if operation == 'ExecuteAdhocWorkflowInstance':
            self.submissions.append(payload)
            if self.submit_error:
                raise self.submit_error
            return {'WorkflowInstanceId': 100, 'RequestId': 'request-1'}
        if operation == 'ListTaskInstances':
            assert payload['WorkflowInstanceId'] == 100
            return {'PagingInfo': {'TotalCount': 1, 'TaskInstances': [{'Id': 200, 'WorkflowInstanceId': 100}]}}
        if operation == 'GetTaskInstance':
            return {'TaskInstance': {'Id': 200, 'WorkflowInstanceId': 100,
                'ProjectId': 334179, 'ProjectEnv': 'Dev', 'TaskType': 'PYODPS3',
                'Status': self.status_value, 'RunNumber': 1, 'Script': {'Content': self.actual_code},
                'TriggerRecurrence': self.recurrence,
                'RuntimeResource': {'ResourceGroupId': 'S_res_test'}, 'Runtime': {'ProcessId': 'p1'}}}
        if operation == 'GetTaskInstanceLog':
            self.log_calls.append(payload)
            return {'TaskInstanceLog': self.log_text}
        raise AssertionError(operation)


def test_snapshot_original_request_and_failed_remote_are_separate(tmp_path):
    api = FakeAPI()
    bundle = d.inspect_saved(api, file_id=10, bizdate='20260911', output_dir=tmp_path)
    assert (bundle / 'snapshot.py').read_bytes() == CODE.encode()
    d.submit_saved(api, bundle)
    payload = api.submissions[0]
    assert payload['EnvType'] == 'Dev'
    assert payload['BizDate'] == 1789056000000
    task = payload['Tasks'][0]
    assert task['Type'] == 'PYODPS3'
    assert task['Script'] == {'Content': CODE, 'Parameters': 'bizdate=20260911'}
    assert task['RuntimeResource'] == {'ResourceGroupId': 'S_res_test'}
    assert task['DataSource'] == {'Name': 'odps_first'}
    assert 'Dependencies' not in task
    result = d.collect_run(api, bundle)
    assert result['submission_status'] == 'submitted'
    assert result['remote_status'] == 'Failure'
    assert result['execution_code_match'] is True
    assert result['business_validation'] == 'not_performed'
    assert api.log_calls[-1] == {'Id': 200, 'RunNumber': 1}
    assert (bundle / 'run-1.log').read_text(encoding='utf-8') == api.log_text
    with pytest.raises(ValueError, match='重复提交'):
        d.submit_saved(api, bundle)
    assert len(api.submissions) == 1


def test_resource_permission_stops_before_submission(tmp_path):
    api = FakeAPI()
    api.resource_error = d.APIError('ListResourceGroups', '403', 'not authorized', 'req')
    bundle = d.inspect_saved(api, file_id=10, bizdate='20260911', output_dir=tmp_path)
    with pytest.raises(d.APIError):
        d.submit_saved(api, bundle)
    assert not api.submissions
    assert d.load_manifest(bundle)['submission_status'] == 'blocked'


def test_unknown_submission_never_retries(tmp_path):
    api = FakeAPI()
    api.submit_error = d.APIError('ExecuteAdhocWorkflowInstance', None, 'connection lost')
    bundle = d.inspect_saved(api, file_id=10, bizdate='20260911', output_dir=tmp_path)
    with pytest.raises(d.APIError):
        d.submit_saved(api, bundle)
    assert d.load_manifest(bundle)['submission_status'] == 'unknown'
    with pytest.raises(ValueError, match='重复提交'):
        d.submit_saved(api, bundle)
    assert len(api.submissions) == 1


def test_changed_local_snapshot_never_submits(tmp_path):
    api = FakeAPI()
    bundle = d.inspect_saved(api, file_id=10, bizdate='20260911', output_dir=tmp_path)
    (bundle / 'snapshot.py').write_text('different', encoding='utf-8')
    with pytest.raises(ValueError, match='哈希'):
        d.submit_saved(api, bundle)
    assert not api.submissions


def test_actual_code_mismatch_is_not_verified_success(tmp_path):
    api = FakeAPI()
    bundle = d.inspect_saved(api, file_id=10, bizdate='20260911', output_dir=tmp_path)
    d.submit_saved(api, bundle)
    api.actual_code, api.status_value = 'other code', 'Success'
    result = d.collect_run(api, bundle)
    assert result['remote_status'] == 'Success'
    assert result['execution_code_match'] is False
    assert result['saved_execution_verified'] is False


def test_checking_timeout_is_not_failure_and_no_resubmit(tmp_path):
    api = FakeAPI()
    api.status_value = 'Checking'
    bundle = d.inspect_saved(api, file_id=10, bizdate='20260911', output_dir=tmp_path)
    d.submit_saved(api, bundle)
    result = d.collect_run(api, bundle, wait=True, timeout=0)
    assert result['wait_timed_out'] is True
    assert result['remote_status'] == 'Checking'
    assert len(api.submissions) == 1


def test_log_delay_can_be_collected_later(tmp_path):
    api = FakeAPI()
    bundle = d.inspect_saved(api, file_id=10, bizdate='20260911', output_dir=tmp_path)
    d.submit_saved(api, bundle)
    saved_log = api.log_text
    api.log_text = None
    assert d.collect_run(api, bundle)['log_available'] is False
    api.log_text = saved_log
    assert d.collect_run(api, bundle)['log_available'] is True


@pytest.mark.parametrize('value', ['20260230', '${bizdate}', '2026-09-11', ''])
def test_invalid_dates(value):
    with pytest.raises(ValueError):
        d.bizdate_millis(value)


def test_parameter_precedence_without_date_conflict():
    assert d.build_parameters('x=old bizdate=$bizdate', ['x=new'], '20260911') == 'x=new bizdate=20260911'
    with pytest.raises(ValueError):
        d.build_parameters('', ['bizdate=20260101'], '20260911')


def test_log_parser_keeps_exception_chain_and_real_environment(monkeypatch):
    monkeypatch.setenv('ALIYUN_ACCESS_KEY_SECRET', 'private-secret-123')
    raw = ('运行环境 Python=3.7.16 pandas=1.0.5 PyODPS=0.10.0\n'
           'SKYNET_PRIVATE=do-not-print\npassword=hidden\nprivate-secret-123\n'
           'Traceback (most recent call last):\n'
           '  File "&lt;pyodps_user_code&gt;", line 533, in check_runtime\n'
           "TypeError: groupby() got an unexpected keyword argument 'dropna'\n"
           'The above exception was the direct cause of the following exception:\n'
           'RuntimeError: environment check failed\n'
           'instance_id=202609120000abcdef rows=245090\nExit code of the Shell command 1')
    result = d.analyze_log(raw)
    assert result['versions']['python'] == '3.7.16'
    assert result['exceptions'][0]['type'] == 'TypeError'
    assert result['exceptions'][-1]['type'] == 'RuntimeError'
    assert result['user_code_lines'] == [533]
    assert result['exit_codes'] == [1]
    assert 'pandas_groupby_api' in result['diagnoses']
    assert result['sql_instance_ids'] == ['202609120000abcdef']
    rendered = json.dumps(result, ensure_ascii=False)
    assert all(s not in rendered for s in ['private-secret-123', 'do-not-print', 'hidden'])


@pytest.mark.parametrize('message,diagnosis', [
    ("ValueError: 平台连接项目必须为tst_mc_prod，当前为'tst_mc_prod_dev'", 'connection_project'),
    ("/home/tops/lib/python3.7/site-packages/odps/readers.py\n_calc_count\nTypeError: unsupported operand type(s) for /: 'float' and 'NoneType'", 'pyodps_reader_step'),
])
def test_known_errors(message, diagnosis):
    result = d.analyze_log(message)
    assert diagnosis in result['diagnoses']


def test_unknown_and_truncated_logs_do_not_claim_success():
    result = d.analyze_log('some output\n[日志截断]')
    assert result['exceptions'] == []
    assert result['versions']['python'] is None
    assert result['possibly_truncated'] is True
    assert result['business_validation'] == 'not_performed'


def test_effect_scan_does_not_call_dynamic_python():
    result = d.scan_effects("TARGET_TABLE='prod.real'\no.execute_sql(make_sql())\no.create_table('tmp')\n")
    assert result['table_constants']['TARGET_TABLE'] == 'prod.real'
    assert result['not_a_readonly_proof'] is True
    assert result['dynamic_sql_lines'] == [2]


def test_skipped_success_does_not_prove_execution(tmp_path):
    api = FakeAPI()
    bundle = d.inspect_saved(api, file_id=10, bizdate='20260911', output_dir=tmp_path)
    d.submit_saved(api, bundle)
    api.status_value, api.recurrence = 'Success', 'Skip'
    result = d.collect_run(api, bundle)
    assert result['remote_status'] == 'Success'
    assert result['saved_execution_verified'] is False


def test_permission_details_from_sdk_are_safe():
    from types import SimpleNamespace
    exc = SimpleNamespace(code='403030', message='No permission',
        data={'RequestId': 'req-permission', 'AccessDeniedDetail': {
            'AuthAction': 'dataworks:ListResourceGroups', 'EncodedDiagnosticMessage': 'sensitive-dump'}})
    result = d.DataWorksAPI.error('ListResourceGroups', exc)
    assert result.permission_action == 'dataworks:ListResourceGroups'
    assert result.request_id == 'req-permission'
    assert 'sensitive-dump' not in str(result)


def test_actual_sdk_request_disables_auto_retry_and_preserves_snapshot():
    from types import SimpleNamespace
    api = d.DataWorksAPI()
    calls = []
    def submit(request, options):
        calls.append((request.to_map(), options.autoretry))
        return SimpleNamespace(body=SimpleNamespace(to_map=lambda: {'WorkflowInstanceId': 123}))
    api._modern = SimpleNamespace(execute_adhoc_workflow_instance_with_options=submit)
    request = {'ProjectId': 334179, 'Name': 'debug', 'Owner': 'o', 'EnvType': 'Dev',
        'Tasks': [{'Name': 't', 'Type': 'PYODPS3', 'Owner': 'o', 'ClientUniqueCode': 'c',
            'RuntimeResource': {'ResourceGroupId': 'S_res_test'},
            'Script': {'Content': CODE, 'Parameters': 'bizdate=20260911'}}]}
    assert api.call('ExecuteAdhocWorkflowInstance', request)['WorkflowInstanceId'] == 123
    assert len(calls) == 1 and calls[0][1] is False
    sent = calls[0][0]
    assert sent['EnvType'] == 'Dev' and sent['ProjectId'] == 334179
    assert sent['Tasks'][0]['Script'] == {'Content': CODE, 'Parameters': 'bizdate=20260911'}
    assert not sent['Tasks'][0]['Dependencies']  # SDK from_map materializes an empty list.


def test_import_has_no_client_or_local_task_execution(monkeypatch):
    import importlib.util
    from alibabacloud_dataworks_public20200518.client import Client as OldClient
    from alibabacloud_dataworks_public20240518.client import Client as NewClient
    def forbidden(*args, **kwargs):
        raise AssertionError('Constructed client during import')
    monkeypatch.setattr(OldClient, '__init__', forbidden)
    monkeypatch.setattr(NewClient, '__init__', forbidden)
    spec = importlib.util.spec_from_file_location('debug_inert', d.__file__)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)


def test_signatures_and_context_are_redacted():
    raw = '开始 SQL https://example.com/log?Signature=private-signature&OSSAccessKeyId=private-ak&SecurityToken=private-token'
    output = json.dumps(d.analyze_log(raw), ensure_ascii=False)
    assert all(x not in output for x in ['private-signature', 'private-ak', 'private-token'])


def test_non_mapping_sdk_error_data_does_not_hide_error():
    from types import SimpleNamespace
    error = d.DataWorksAPI.error('GetTaskInstance', SimpleNamespace(data='upstream bad gateway', message='timeout'))
    assert error.operation == 'GetTaskInstance'
    assert error.code is None


def test_count_and_memory_outputs_are_evidence_not_business_validation():
    result = d.analyze_log('最终数据 245090 行，DataFrame内存 559.0 MiB\nSQL instance_id=abc123 rows=305314')
    assert result['row_counts'] == [245090, 305314]
    assert result['memory_observations'] == [{'value': 559.0, 'unit': 'MiB'}]
    assert result['business_validation'] == 'not_performed'


def test_deleted_saved_file_cannot_be_run(tmp_path):
    api = FakeAPI()
    original = api.saved_task
    def deleted(**kwargs):
        saved = original(**kwargs)
        saved['deleted_status'] = 'DELETED'
        return saved
    api.saved_task = deleted
    with pytest.raises(ValueError, match='删除'):
        d.inspect_saved(api, file_id=10, bizdate='20260911', output_dir=tmp_path)
    assert not api.submissions

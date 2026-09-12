"""Offline contracts for versioned console log readers and candidate evidence."""
import copy
import json

import pytest

import debug_pyodps3 as d
from test_debug_pyodps3 import FakeAPI, CODE
from test_pyodps3_logs import LogsAPI


class LegacyAPI:
    def __init__(self):
        self.calls = []
        self.error = None
        self.started = 1000
        self.after_started = None
        self.status = 'SUCCESS'
        self.log = '中文 print\nTraceback (most recent call last):\n  File "<pyodps_user_code>", line 2\nValueError: 完整错误\n'
        self.history = [{'InstanceId': 200, 'InstanceHistoryId': 42, 'Status': 'FAILURE',
                         'BeginRunningTime': 900, 'FinishTime': 950, 'NodeName': 'task'}]

    def call(self, operation, payload, legacy=False):
        assert legacy is True
        self.calls.append((operation, copy.deepcopy(payload)))
        if self.error:
            raise self.error
        if operation == 'GetInstance':
            return {'Success': True, 'Data': {'InstanceId': 200, 'Status': self.status,
                'BeginRunningTime': self.started, 'FinishTime': 1500,
                'NodeId': 10, 'NodeName': 'task', 'Bizdate': 1789056000000}}
        if operation == 'ListInstanceHistory':
            return {'Success': True, 'Instances': self.history}
        if operation == 'GetInstanceLog':
            if self.after_started is not None:
                self.started = self.after_started
            return {'Success': True, 'Data': self.log, 'RequestId': 'legacy-log'}
        raise AssertionError(operation)


def test_legacy_current_console_is_read_and_fingerprinted(tmp_path):
    api = LegacyAPI()
    result = d.read_instance_logs(api, tmp_path, 200, api_version='2020-05-18', project_env='Prod')
    assert result['remote_status'] == 'Success'
    assert result['api_version'] == '2020-05-18'
    assert result['selection_verified'] is True
    assert result['run_number'] is None
    assert result['instance_history_id'] is None
    assert (tmp_path / result['log_file']).read_bytes() == api.log.encode('utf-8')
    assert ('GetInstanceLog', {'InstanceId': 200, 'ProjectEnv': 'PROD'}) in api.calls


def test_legacy_history_uses_exact_history_status(tmp_path):
    api = LegacyAPI()
    result = d.read_instance_logs(api, tmp_path, 200, api_version='2020-05-18',
                                 project_env='Dev', instance_history_id=42)
    assert result['remote_status'] == 'Failure'
    assert result['instance_history_id'] == 42
    assert result['run_number'] is None
    assert result['started_time'] == 900
    assert ('GetInstanceLog', {'InstanceId': 200, 'ProjectEnv': 'DEV', 'InstanceHistoryId': 42}) in api.calls
    assert not any(name == 'GetInstance' for name, _ in api.calls)


def test_legacy_missing_history_does_not_borrow_current_status(tmp_path):
    api = LegacyAPI()
    api.history = []
    result = d.read_instance_logs(api, tmp_path, 200, api_version='2020-05-18',
                                 project_env='Prod', instance_history_id=42)
    assert result['remote_status'] is None
    assert result['status_error']
    assert result['log_available'] is True
    assert result['selection_verified'] is False


def test_legacy_continuation_uses_recorded_backend_and_env(tmp_path):
    api = LegacyAPI()
    d.read_instance_logs(api, tmp_path, 200, api_version='2020-05-18', project_env='Prod')
    result = d.read_instance_logs(api, tmp_path)
    assert result['api_version'] == '2020-05-18'
    assert result['log_cached'] is False


def test_legacy_changed_current_attempt_preserves_cache(tmp_path):
    api = LegacyAPI()
    initial = d.read_instance_logs(api, tmp_path, 200, api_version='2020-05-18', project_env='Prod')
    raw = (tmp_path / initial['log_file']).read_bytes()
    api.started = 2000
    api.log = 'new attempt must not replace old evidence'
    result = d.read_instance_logs(api, tmp_path)
    assert result['log_state'] == 'error'
    assert result['log_cached'] is True
    assert (tmp_path / initial['log_file']).read_bytes() == raw


def test_legacy_rerun_between_metadata_and_log_does_not_commit(tmp_path):
    api = LegacyAPI()
    api.after_started = 2000
    result = d.read_instance_logs(api, tmp_path, 200, api_version='2020-05-18', project_env='Prod')
    assert result['log_state'] == 'error'
    assert result['log_available'] is False
    assert not list(tmp_path.glob('*.log'))


def test_legacy_without_stable_start_is_unverified_and_cannot_wait(tmp_path):
    api = LegacyAPI()
    api.started = None
    result = d.read_instance_logs(api, tmp_path, 200, api_version='2020-05-18', project_env='Prod')
    assert result['selection_verified'] is False
    assert result['selection_unverified']
    with pytest.raises(ValueError, match='稳定|等待'):
        d.read_instance_logs(api, tmp_path, wait=True)


@pytest.mark.parametrize('kwargs', [
    {'api_version': '2020-05-18', 'run_number': 1, 'project_env': 'Prod'},
    {'api_version': '2024-05-18', 'instance_history_id': 42},
    {'api_version': '2020-05-18'},
])
def test_selector_mismatch_fails_before_api(tmp_path, kwargs):
    api = LegacyAPI()
    with pytest.raises(ValueError):
        d.read_instance_logs(api, tmp_path, 200, **kwargs)
    assert api.calls == []


def test_recorded_backend_cannot_be_changed_in_place(tmp_path):
    api = LegacyAPI()
    d.read_instance_logs(api, tmp_path, 200, api_version='2020-05-18', project_env='Prod')
    with pytest.raises(ValueError, match='版本|backend'):
        d.read_instance_logs(api, tmp_path, api_version='2024-05-18')


def test_legacy_permission_failure_preserves_same_selection_cache(tmp_path):
    api = LegacyAPI()
    initial = d.read_instance_logs(api, tmp_path, 200, api_version='2020-05-18', project_env='Prod', instance_history_id=42)
    api.error = d.APIError('GetInstance', '403', 'dataworks:GetInstance')
    result = d.read_instance_logs(api, tmp_path)
    assert result['log_cached'] is True
    assert result['log_file'] == initial['log_file']


def test_histories_lists_explicit_ids_without_running():
    api = LegacyAPI()
    result = d.list_instance_histories(api, 200, 'Prod')
    assert result['histories'][0]['InstanceHistoryId'] == 42
    assert result['api_version'] == '2020-05-18'
    assert api.calls == [('ListInstanceHistory', {'InstanceId': 200, 'ProjectEnv': 'PROD'})]


def test_instances_applies_workflow_type_and_returns_query():
    api = LogsAPI()
    result = d.list_instances(api, '20260911', task_id=10, workflow_instance_type='SmokeTest')
    assert result['query']['WorkflowInstanceType'] == 'SmokeTest'
    assert result['query']['TaskId'] == 10


def test_log_completeness_does_not_claim_full_without_evidence():
    result = d.analyze_log('hello\n')
    assert result['completeness'] == 'unknown'
    assert result['truncation_evidence'] == []
    result = d.analyze_log('日志已截断\n', partial=True)
    assert result['completeness'] == 'partial'
    assert len(result['truncation_evidence']) >= 2


def test_candidate_observation_saves_executed_code_and_configuration(tmp_path):
    api = FakeAPI()
    bundle = d.inspect_saved(api, file_id=10, bizdate='20260911', output_dir=tmp_path)
    d.submit_saved(api, bundle)
    manifest = d.load_manifest(bundle)
    manifest['source'] = 'candidate'
    d.save_manifest(bundle, manifest)
    api.status_value = 'Success'
    result = d.read_instance_logs(api, bundle)
    assert result['execution_code_match'] is True
    assert result['execution_verified'] is True
    assert (bundle / 'executed.py').read_bytes() == CODE.encode('utf-8')
    assert result['observed_configuration']['source'] == 'GetTaskInstance'
    assert result['observed_configuration']['runtime_resource'] == {'ResourceGroupId': 'S_res_test'}
    assert result.get('saved_execution_verified') is False


def test_candidate_wrong_workflow_is_rejected(tmp_path):
    d.save_manifest(tmp_path, {'source': 'candidate', 'project_id': 334179,
        'workflow_instance_id': 999, 'task_instance_id': 200, 'code_sha256': d.sha256(CODE)})
    with pytest.raises(ValueError, match='工作流'):
        d.read_instance_logs(FakeAPI(), tmp_path)


def test_collect_run_pins_attempt_and_keeps_terminal_log_wait(tmp_path, monkeypatch):
    api = FakeAPI()
    bundle = d.inspect_saved(api, file_id=10, bizdate='20260911', output_dir=tmp_path)
    d.submit_saved(api, bundle)
    api.status_value = 'Success'
    api.log_text = ''
    result = d.collect_run(api, bundle, wait=True, timeout=0)
    assert result['wait_timed_out'] is True
    assert result['log_run_number'] == 1
    assert result['log_state'] == 'pending'


def test_legacy_status_transition_is_not_a_different_attempt(tmp_path):
    api = LegacyAPI()
    call = api.call
    def transition(operation, payload, legacy=False):
        result = call(operation, payload, legacy)
        if operation == 'GetInstanceLog':
            api.status = 'SUCCESS'
        return result
    api.call = transition
    api.status = 'RUNNING'
    result = d.read_instance_logs(api, tmp_path, 200, api_version='2020-05-18', project_env='Dev')
    assert result['remote_status'] == 'Success'
    assert result['log_state'] == 'available'
    assert result['selection_verified'] is True


def test_legacy_empty_response_keeps_complete_cached_text(tmp_path):
    api = LegacyAPI()
    initial = d.read_instance_logs(api, tmp_path, 200, api_version='2020-05-18', project_env='Prod')
    api.log = ''
    result = d.read_instance_logs(api, tmp_path)
    assert result['log_cached'] is True
    assert result['log_state'] == 'pending'
    assert result['log_sha256'] == initial['log_sha256']


def test_switch_legacy_history_on_failure_does_not_reuse_previous_log(tmp_path):
    api = LegacyAPI()
    d.read_instance_logs(api, tmp_path, 200, api_version='2020-05-18', project_env='Prod', instance_history_id=42)
    api.error = d.APIError('GetInstanceLog', '403', 'denied')
    result = d.read_instance_logs(api, tmp_path, instance_history_id=43)
    assert result['instance_history_id'] == 43
    assert result['log_available'] is False
    assert result['remote_status'] is None
    assert result.get('log_sha256') is None


def test_legacy_cli_json_and_histories(monkeypatch, capsys, tmp_path):
    api = LegacyAPI()
    monkeypatch.setattr(d, 'DataWorksAPI', lambda: api)
    assert d.main(['histories', '--instance-id', '200', '--env', 'Dev']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['histories'][0]['InstanceHistoryId'] == 42
    assert d.main(['logs', '--instance-id', '200', '--api-version', '2020-05-18',
                   '--env', 'Prod', '--output-dir', str(tmp_path), '--format', 'traceback']) == 0
    output = capsys.readouterr()
    assert 'ValueError: 完整错误' in output.out
    assert '"api_version": "2020-05-18"' in output.err


def test_candidate_history_observation_never_records_latest_code(tmp_path):
    api = LogsAPI()
    call = api.call
    def with_code(operation, payload, legacy=False):
        result = call(operation, payload, legacy)
        if operation == 'GetTaskInstance':
            result['TaskInstance'].update(ProjectEnv='Dev', Script={'Content': 'new code'})
        if operation == 'ListTaskInstances':
            result['PagingInfo']['TaskInstances'][0]['Script'] = {'Content': CODE}
        return result
    api.call = with_code
    d.save_manifest(tmp_path, {'source': 'candidate', 'project_id': 334179,
        'workflow_instance_id': 100, 'task_instance_id': 200, 'code_sha256': d.sha256(CODE)})
    result = d.read_instance_logs(api, tmp_path, run_number=1)
    assert result['execution_code_match'] is True
    assert json.loads((tmp_path / 'observation.json').read_text(encoding='utf-8'))['RunNumber'] == 1
    assert (tmp_path / 'executed.py').read_bytes() == CODE.encode('utf-8')
    assert result['observed_configuration']['source'] == 'ListTaskInstances'


@pytest.mark.parametrize('raw', ['[日志截断]', '日志已被清理，无法查看', 'Log has expired and was deleted'])
def test_explicit_platform_loss_is_distinct_from_partial_capture(raw):
    result = d.analyze_log(raw)
    assert result['completeness'] == 'partial'
    assert result['log_recovery'] == 'unrecoverable_by_log_api'
    assert result['truncation_evidence']
    captured = d.analyze_log('只采集了可见内容', partial=True)
    assert captured['log_recovery'] == 'unknown'

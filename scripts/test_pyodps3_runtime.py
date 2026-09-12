"""Offline contracts for shared PyODPS3 API, persistence, and runtime configuration."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import pyodps3_runtime as r


class ResourceAPI:
    def __init__(self, legacy=None, modern=None, legacy_error=None, modern_error=None):
        self.legacy_groups = legacy or []
        self.modern_pages = modern or [[]]
        self.legacy_error, self.modern_error = legacy_error, modern_error
        self.calls = []

    def call(self, operation, payload, legacy=False):
        self.calls.append((operation, payload, legacy))
        assert operation == 'ListResourceGroups'
        if legacy:
            if self.legacy_error:
                raise self.legacy_error
            return {'Data': self.legacy_groups, 'RequestId': 'old'}
        if self.modern_error:
            raise self.modern_error
        page = payload['PageNumber']
        return {'PagingInfo': {'ResourceGroupList': self.modern_pages[page - 1],
                'TotalCount': sum(map(len, self.modern_pages))}, 'RequestId': 'new-' + str(page)}


def saved(resource=9):
    return {'project_id': 334179, 'owner': 'owner', 'connection_name': 'odps_first',
            'node_configuration': {'ResourceGroupId': resource, 'Cu': 0.5, 'ImageId': 'image1'}}


def test_legacy_exact_mapping_does_not_require_modern_permission():
    api = ResourceAPI(legacy=[{'Id': 9, 'Identifier': 'S_res_group_real', 'Status': 0}],
                      modern_error=r.APIError('ListResourceGroups', 'Forbidden'))
    result = r.resolve_runtime(api, saved())
    assert result['runtime_resource'] == {'ResourceGroupId': 'S_res_group_real', 'Cu': '0.5', 'Image': 'image1'}
    assert result['owner'] == 'owner'
    assert result['data_source'] == {'Name': 'odps_first'}
    assert all(legacy for _, _, legacy in api.calls)


def test_explicit_serverless_identifier_resolves_paginated_modern_groups():
    api = ResourceAPI(modern=[[{'Id': 'other', 'Status': 'Normal', 'ResourceGroupType': 'CommonV2'}],
                             [{'Id': 'Serverless_res_group_real', 'Status': 'Normal', 'ResourceGroupType': 'CommonV2'}]])
    overrides = {'resource_group_id': 'Serverless_res_group_real', 'data_source': 'odps_dev', 'cu': '2', 'image': 'custom'}
    result = r.resolve_runtime(api, saved(), overrides)
    assert result['runtime_resource'] == {'ResourceGroupId': 'Serverless_res_group_real', 'Cu': '2', 'Image': 'custom'}
    assert result['data_source'] == {'Name': 'odps_dev'}
    assert result['overrides'] == overrides
    new_calls = [p for _, p, legacy in api.calls if not legacy]
    assert [p['PageNumber'] for p in new_calls] == [1, 2]
    assert new_calls[0]['ProjectId'] == 334179
    assert new_calls[0]['ResourceGroupTypes'] == ['CommonV2', 'ExclusiveScheduler']


def test_numeric_identifier_is_not_inferred_from_serverless_suffix():
    api = ResourceAPI(modern=[[{'Id': 'Serverless_res_group_tenant_9', 'Status': 'Normal', 'ResourceGroupType': 'CommonV2'}]])
    with pytest.raises(ValueError, match='无法.*映射'):
        r.resolve_resource(api, saved())


def test_explicit_legacy_identifier_is_verified():
    api = ResourceAPI(legacy=[{'Id': 123, 'Identifier': 'S_actual', 'Status': 0}])
    assert r.resolve_runtime(api, saved(), {'resource_group_id': 'S_actual'})['runtime_resource']['ResourceGroupId'] == 'S_actual'


def test_runtime_records_explicit_selection_without_claiming_saved_id_mapping():
    api = ResourceAPI(legacy=[{'Id': 123, 'Identifier': 'S_actual', 'Status': 0}])
    result = r.resolve_runtime(api, saved(), {'resource_group_id': 'S_actual', 'data_source': 'odps_first', 'cu': '2'})
    evidence = result['resource_evidence']
    assert evidence['saved_id'] == 9
    assert evidence['selected_value'] == 'S_actual'
    assert evidence['selection_source'] == 'explicit_override'
    assert evidence['matched_record_id'] == 123
    assert result['configuration_sources'] == {'owner': 'saved', 'data_source': 'explicit_override',
        'resource_group_id': 'explicit_override', 'cu': 'explicit_override', 'image': 'saved'}
    changes = {item['field']: item for item in result['configuration_changes']}
    assert set(changes) == {'resource_group_id', 'cu'}
    assert changes['resource_group_id'] == {'field': 'resource_group_id', 'saved_value': 9,
        'selected_value': 'S_actual', 'effective_value': 'S_actual'}
    assert changes['cu'] == {'field': 'cu', 'saved_value': 0.5, 'selected_value': '2', 'effective_value': '2'}


def test_runtime_sources_keep_missing_optional_settings_unknown():
    original = saved()
    original['node_configuration'] = {'ResourceGroupId': 9}
    api = ResourceAPI(legacy=[{'Id': 9, 'Identifier': 'S_actual', 'Status': 0}])
    result = r.resolve_runtime(api, original)
    assert result['configuration_sources']['cu'] == 'unspecified'
    assert result['configuration_sources']['image'] == 'unspecified'
    assert result['configuration_changes'] == []
    assert result['resource_evidence']['selection_source'] == 'saved'
    assert result['resource_evidence']['selected_value'] == '9'
    assert result['resource_evidence']['matched_record_id'] == 9
    # The compatible resource-only entry also records what was actually selected.
    assert r.resolve_resource(api, original)['selection_source'] == 'saved'


def test_runtime_modern_evidence_and_equivalent_cu_override():
    api = ResourceAPI(modern=[[{'Id': 'chosen', 'Status': 'Normal', 'ResourceGroupType': 'CommonV2'}]])
    result = r.resolve_runtime(api, saved('chosen'), {'cu': '0.50'})
    assert result['resource_evidence']['matched_record_id'] == 'chosen'
    assert result['resource_evidence']['selected_value'] == 'chosen'
    assert result['resource_evidence']['selection_source'] == 'saved'
    assert result['configuration_sources']['cu'] == 'explicit_override'
    assert result['configuration_changes'] == []


@pytest.mark.parametrize('group', [
    {'Id': 'chosen', 'Status': 'Stop', 'ResourceGroupType': 'CommonV2'},
    {'Id': 'chosen', 'Status': 'Normal', 'ResourceGroupType': 'ExclusiveDataIntegration'},
])
def test_unusable_modern_group_cannot_run(group):
    with pytest.raises(ValueError):
        r.resolve_runtime(ResourceAPI(modern=[[group]]), saved(), {'resource_group_id': 'chosen'})


def test_legacy_denied_can_use_exact_modern_identifier():
    api = ResourceAPI(legacy_error=r.APIError('ListResourceGroups', '403'),
                      modern=[[{'Id': 'chosen', 'Status': 'Normal', 'ResourceGroupType': 'CommonV2'}]])
    result = r.resolve_runtime(api, saved('chosen'))
    assert result['resource_evidence']['identifier'] == 'chosen'


def test_permission_failure_is_not_reported_as_empty_resource_list():
    api = ResourceAPI(legacy_error=r.APIError('ListResourceGroups', '403', 'ram:PassRole'),
                      modern_error=r.APIError('ListResourceGroups', '403', 'dataworks:ListResourceGroups'))
    with pytest.raises(r.APIError):
        r.resolve_runtime(api, saved())


@pytest.mark.parametrize('overrides', [{'cu': '0'}, {'cu': '-1'}, {'cu': 'nan'}, {'cu': 'infinity'}, {'resource_group_id': ''}, {'data_source': ''}])
def test_invalid_runtime_overrides_fail_before_api(overrides):
    api = ResourceAPI()
    with pytest.raises(ValueError):
        r.resolve_runtime(api, saved(), overrides)
    assert not api.calls


def request():
    return r.build_adhoc_request(project_id=334179, task_name='test_job', owner='owner',
        bizdate='20260911', parameters='bizdate=20260911 a=1', source="print('中文')\r\n",
        runtime_resource={'ResourceGroupId': 'S_actual', 'Cu': '1', 'Image': 'img'},
        data_source={'Name': 'odps_first'}, unique_code='unique')


def observed():
    return {'ProjectId': 334179, 'ProjectEnv': 'Dev', 'TaskType': 'PYODPS3',
        'Script': {'Parameters': 'a=1 bizdate=20260911'}, 'DataSource': {'Name': 'odps_first'},
        'RuntimeResource': {'ResourceGroupId': 'S_actual', 'Cu': 1.0, 'Image': 'img'}}


def test_request_build_is_pure_and_preserves_source():
    result = request()
    assert result['EnvType'] == 'Dev'
    assert result['BizDate'] == 1789056000000
    assert result['Tasks'][0]['Script']['Content'] == "print('中文')\r\n"
    assert result['Tasks'][0]['ClientUniqueCode'] == 'unique'
    assert result['Name'] == 'pyodps3_debug_unique'


def test_configuration_match_normalizes_parameters_ids_and_cu():
    item = observed()
    item['ProjectId'] = '334179'
    assert r.observe_configuration(item, request()) == {
        'configuration_match': True, 'configuration_mismatches': [], 'configuration_unavailable': []}


def test_configuration_missing_is_unknown_not_success():
    item = observed()
    item.pop('DataSource')
    item['RuntimeResource'].pop('Image')
    result = r.observe_configuration(item, request())
    assert result['configuration_match'] is None
    assert set(result['configuration_unavailable']) == {'DataSource.Name', 'RuntimeResource.Image'}


def test_configuration_mismatch_wins_over_unknown():
    item = observed()
    item['ProjectEnv'] = 'Prod'
    item['Script']['Parameters'] = 'bizdate=20260101 a=1'
    item.pop('DataSource')
    result = r.observe_configuration(item, request())
    assert result['configuration_match'] is False
    assert {'ProjectEnv', 'Script.Parameters'} <= set(result['configuration_mismatches'])


class CallClient:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def __getattr__(self, name):
        def invoke(request, options):
            self.calls.append((name, options))
            value = self.outcomes.pop(0)
            if isinstance(value, Exception):
                raise value
            return SimpleNamespace(body=SimpleNamespace(to_map=lambda: value))
        return invoke


@pytest.mark.parametrize('error', [TimeoutError('timed out'), r.APIError('GetTaskInstanceLog', 'Throttling'),
                                  r.APIError('GetTaskInstanceLog', '503'), r.APIError('GetTaskInstanceLog', '429'),
                                  r.APIError('GetTaskInstanceLog', 'RequestTimeout')])
def test_read_retries_only_transient_failures_up_to_three(monkeypatch, error):
    waits = []
    monkeypatch.setattr(r.time, 'sleep', waits.append)
    api = r.DataWorksAPI()
    api._modern = CallClient([error, error, {'TaskInstanceLog': 'done'}])
    assert api.call('GetTaskInstanceLog', {'Id': 1})['TaskInstanceLog'] == 'done'
    assert len(api._modern.calls) == 3 and waits == [1, 2]
    assert all(options.autoretry is False for _, options in api._modern.calls)


def test_read_retry_limit_and_no_permission_retry(monkeypatch):
    monkeypatch.setattr(r.time, 'sleep', lambda _: None)
    for code, expected in [('500', 3), ('Forbidden', 1)]:
        api = r.DataWorksAPI()
        api._modern = CallClient([r.APIError('GetTaskInstance', code)] * 4)
        with pytest.raises(r.APIError):
            api.call('GetTaskInstance', {'Id': 1})
        assert len(api._modern.calls) == expected


def test_submission_never_retries(monkeypatch):
    monkeypatch.setattr(r.time, 'sleep', lambda _: pytest.fail('submit must not wait and retry'))
    api = r.DataWorksAPI()
    api._modern = CallClient([TimeoutError('timed out'), {}])
    with pytest.raises(r.APIError):
        api.call('ExecuteAdhocWorkflowInstance', request())
    assert len(api._modern.calls) == 1


def test_sdk_http_status_and_other_product_permission_extraction():
    exc = SimpleNamespace(data={'RequestId': 'req', 'AccessDeniedDetail': {'AuthAction': 'ram:PassRole'}},
                          code='Forbidden', message='raw dump')
    err = r.DataWorksAPI.error('ExecuteAdhocWorkflowInstance', exc)
    assert err.permission_action == 'ram:PassRole'
    assert 'raw dump' not in str(err)
    assert err.definite_rejection


def test_saved_lookup_failure_preserves_type_without_repeating_read(monkeypatch):
    import fetch_task_sql
    calls = []
    def fail(**kwargs):
        calls.append(kwargs)
        raise ValueError('multiple matching nodes')
    monkeypatch.setattr(fetch_task_sql, 'fetch_saved_task', fail)
    api = r.DataWorksAPI()
    api._legacy = object()
    with pytest.raises(ValueError, match='multiple matching'):
        api.saved_task(name='job')
    assert len(calls) == 1


def test_saved_lookup_timeout_retries_three_times(monkeypatch):
    import fetch_task_sql
    calls = []
    def fetch(**kwargs):
        calls.append(kwargs)
        if len(calls) < 3:
            raise TimeoutError('read timed out')
        return {'source': 'saved'}
    monkeypatch.setattr(fetch_task_sql, 'fetch_saved_task', fetch)
    monkeypatch.setattr(r.time, 'sleep', lambda _: None)
    api = r.DataWorksAPI()
    api._legacy = object()
    assert api.saved_task(file_id=10) == {'source': 'saved'}
    assert len(calls) == 3


def test_modern_pagination_stagnation_stops_without_guessing():
    class StagnantAPI(ResourceAPI):
        def call(self, operation, payload, legacy=False):
            if legacy:
                return {'Data': []}
            self.calls.append(payload)
            return {'PagingInfo': {'TotalCount': 100, 'ResourceGroupList': [
                {'Id': 'other', 'Status': 'Normal', 'ResourceGroupType': 'CommonV2'}]}}
    api = StagnantAPI()
    with pytest.raises(ValueError, match='分页'):
        r.resolve_resource(api, saved())
    assert len(api.calls) == 2


def test_atomic_write_uses_unique_temporary_flush_fsync_and_replace(tmp_path, monkeypatch):
    target = tmp_path / 'state.json'
    target.write_text('{"old": true}', encoding='utf8')
    replaced = []
    synced = []
    real_replace = r.os.replace
    def replace(source, destination):
        source = Path(source)
        assert source != target.with_name('state.json.tmp')
        assert source.parent == target.parent
        assert json.loads(source.read_text(encoding='utf8')) == {'中文': 1}
        replaced.append(source)
        real_replace(source, destination)
    monkeypatch.setattr(r.os, 'replace', replace)
    monkeypatch.setattr(r.os, 'fsync', synced.append)
    r.write_json(target, {'中文': 1})
    r.write_json(target, {'中文': 1})
    assert len(set(replaced)) == 2 and len(synced) == 2
    assert list(tmp_path.iterdir()) == [target]


def test_atomic_failure_preserves_existing_manifest_and_cleans_temp(tmp_path, monkeypatch):
    path = tmp_path / 'state.json'
    path.write_text('old', encoding='utf8')
    def fail(*args):
        raise OSError('replace failed')
    monkeypatch.setattr(r.os, 'replace', fail)
    with pytest.raises(OSError):
        r.write_json(path, {'new': True})
    assert path.read_text(encoding='utf8') == 'old'
    assert list(tmp_path.iterdir()) == [path]

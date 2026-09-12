"""Saved code selection tests: SDK mocks only, never contact DataWorks."""
import hashlib
from types import SimpleNamespace as NS

import pytest

import fetch_task_sql as fetch
from alibabacloud_dataworks_public20200518 import models


def file_response(content="\nprint('中文')\r\n\n", **overrides):
    values = dict(file_id=42, file_name='my_pyodps3', file_type=1221,
                  content=content, owner='owner', connection_name='odps_first',
                  commit_status=0, last_edit_time=1234, node_id=91)
    values.update(overrides)
    return NS(body=NS(success=True, data=models.GetFileResponseBodyData(
        file=models.GetFileResponseBodyDataFile(**values),
        node_configuration=models.GetFileResponseBodyDataNodeConfiguration(
            resource_group_id=900009000651, image_id='saved-image',
            para_value='bizdate=$bizdate'))))


class SavedClient:
    def __init__(self, response=None):
        self.response = response or file_response()
        self.calls = []

    def get_file(self, request):
        self.calls.append(request)
        return self.response

    def list_files(self, _):
        raise AssertionError('file ID must bypass search')

    def get_node_code(self, _):
        raise AssertionError('saved must never read production')

    def list_nodes_by_output(self, _):
        raise AssertionError('saved must never resolve by production output')


def test_saved_preserves_original_bytes_and_configuration():
    client = SavedClient()
    result = fetch.fetch_saved_task(file_id=42, client=client)
    original = "\nprint('中文')\r\n\n"
    assert result['sql_text'] == original
    assert result['code_sha256'] == hashlib.sha256(original.encode('utf-8')).hexdigest()
    assert result['source'] == 'saved'
    assert result['file_id'] == 42 and result['file_type'] == 1221
    assert result['commit_status'] == 0 and result['last_edit_time'] == 1234
    assert result['connection_name'] == 'odps_first' and result['owner'] == 'owner'
    assert result['node_configuration']['ResourceGroupId'] == 900009000651
    assert result['node_configuration']['ImageId'] == 'saved-image'
    assert client.calls[0].project_id == fetch.DATAWORKS_PROJECT_ID
    assert client.calls[0].file_id == 42


def test_saved_name_resolves_exact_candidate_then_reads_getfile(monkeypatch):
    monkeypatch.setattr(fetch, 'list_matching_files', lambda *_: [
        {'file_name': 'my_pyodps3_extra', 'file_id': 55},
        {'file_name': 'my_pyodps3', 'file_id': 42, 'content': 'stale list content'}])
    result = fetch.fetch_saved_task('my_pyodps3', client=SavedClient())
    assert 'stale' not in result['sql_text']


def test_same_name_candidates_are_rejected(monkeypatch):
    monkeypatch.setattr(fetch, 'list_matching_files', lambda *_: [
        {'file_name': 'my_pyodps3', 'file_id': 42},
        {'file_name': 'my_pyodps3', 'file_id': 43}])
    with pytest.raises(fetch.TaskSqlNotFound, match='多个|歧义'):
        fetch.fetch_saved_task('my_pyodps3', client=SavedClient())


def test_saved_rejects_nonexact_name_without_production_fallback(monkeypatch):
    monkeypatch.setattr(fetch, 'list_matching_files', lambda *_: [
        {'file_name': 'my_pyodps3_extra', 'file_id': 42}])
    with pytest.raises(fetch.TaskSqlNotFound):
        fetch.fetch_saved_task('my_pyodps3', client=SavedClient())


@pytest.mark.parametrize('content', [None, '', ' \r\n'])
def test_empty_saved_content_does_not_fall_back(content):
    with pytest.raises(fetch.TaskSqlNotFound):
        fetch.fetch_saved_task(file_id=42, client=SavedClient(file_response(content)))


@pytest.mark.parametrize('overrides', [{'file_id': 43}, {'file_name': 'another_task'}])
def test_id_and_name_are_cross_checked(overrides):
    with pytest.raises((ValueError, fetch.TaskSqlNotFound)):
        fetch.fetch_saved_task(name='my_pyodps3', file_id=42,
                               client=SavedClient(file_response(**overrides)))


@pytest.mark.parametrize('file_id', [0, -1, 'bad', True])
def test_file_id_validation(file_id):
    with pytest.raises(ValueError):
        fetch.fetch_saved_task(file_id=file_id, client=SavedClient())


def test_default_retains_production_priority_and_file_type(monkeypatch):
    match = {'file_name': 'my_pyodps3', 'file_id': 42, 'file_type': 1221,
             'node_id': 91, 'content': 'saved code'}
    monkeypatch.setattr(fetch, 'get_node_code', lambda *_: 'production code')
    result = fetch._result_from_match(object(), match)
    assert result['source'] == 'prod' and result['sql_text'] == 'production code'
    assert result['file_id'] == 42 and result['file_type'] == 1221
    assert fetch.task_code_kind(result) == 'PYODPS3'


def test_saved_api_failure_does_not_expose_credentials(monkeypatch):
    secret = 'SECRET_SHOULD_NEVER_APPEAR'
    class BrokenClient:
        def get_file(self, _):
            raise RuntimeError('AccessKeySecret=' + secret)
    with pytest.raises(RuntimeError) as error:
        fetch.fetch_saved_task(file_id=42, client=BrokenClient())
    assert secret not in str(error.value)
    assert 'GetFile' in str(error.value)


def test_saved_cli_accepts_file_id_without_name(tmp_path, monkeypatch):
    output = tmp_path / 'node.py'
    parser = fetch.build_parser()
    args = parser.parse_args(['--source', 'saved', '--file-id', '42', '--save', str(output)])
    monkeypatch.setattr(fetch, 'create_client', lambda: SavedClient())
    fetch.cmd_fetch(args)
    assert output.read_bytes() == "\nprint('中文')\r\n\n".encode('utf-8')


def test_saved_cli_header_identifies_python(monkeypatch, capsys):
    monkeypatch.setattr(fetch, 'create_client', lambda: SavedClient())
    args = fetch.build_parser().parse_args(['--source', 'saved', '--file-id', '42'])
    fetch.cmd_fetch(args)
    out = capsys.readouterr().out
    assert 'PYODPS3' in out and 'saved=保存态' in out and 'file_id' in out


def test_saved_requires_name_or_file_id():
    with pytest.raises(ValueError):
        fetch.fetch_saved_task(client=SavedClient())


def test_missing_saved_file_is_not_found():
    response = NS(body=NS(success=True, data=None))
    with pytest.raises(fetch.TaskSqlNotFound):
        fetch.fetch_saved_task(file_id=42, client=SavedClient(response))


def test_failed_api_response_omits_raw_error_text():
    response = NS(body=NS(success=False, error_code='Forbidden.GetFile',
                          error_message='AccessKeySecret=hidden-value'))
    with pytest.raises(RuntimeError) as error:
        fetch.fetch_saved_task(file_id=42, client=SavedClient(response))
    assert 'Forbidden.GetFile' in str(error.value)
    assert 'hidden-value' not in str(error.value)


def test_original_auto_development_fallback_keeps_type(monkeypatch):
    monkeypatch.setattr(fetch, 'get_node_code', lambda *_: '')
    result = fetch._result_from_match(object(), dict(file_name='name', file_id=42,
        node_id=91, content=' saved ', file_type=1221))
    assert result['source'] == 'dev' and result['sql_text'] == 'saved'
    assert result['file_id'] == 42 and fetch.task_code_kind(result) == 'PYODPS3'


def test_saved_listfiles_exception_is_sanitized(monkeypatch):
    def broken(*_):
        raise RuntimeError('AccessKeySecret=hidden-value')
    monkeypatch.setattr(fetch, 'list_matching_files', broken)
    with pytest.raises(RuntimeError) as error:
        fetch.fetch_saved_task(name='my_pyodps3', client=SavedClient())
    assert 'ListFiles' in str(error.value) and 'hidden-value' not in str(error.value)


def test_saved_api_error_retains_structured_diagnostics():
    response = NS(body=NS(success=False, error_code='Forbidden.GetFile',
                          error_message='No permission dataworks:GetFile; AccessKeySecret=hidden-value',
                          request_id='req-get-file'))
    with pytest.raises(fetch.SavedAPIError) as error:
        fetch.fetch_saved_task(file_id=42, client=SavedClient(response))
    exc = error.value
    assert exc.operation == 'GetFile' and exc.code == 'Forbidden.GetFile'
    assert exc.request_id == 'req-get-file' and exc.permission_action == 'dataworks:GetFile'
    assert 'hidden-value' not in str(exc)


@pytest.mark.parametrize('style', ['modern', 'legacy'])
def test_saved_exception_extracts_auth_action_from_sdk_details(style):
    if style == 'modern':
        from alibabacloud_tea_openapi.exceptions import ClientException
        sdk_error = ClientException(code='Forbidden.RAM', message='No permission', request_id='req-sdk',
            access_denied_detail={'AuthAction': 'dataworks:GetFile', 'EncodedDiagnosticMessage': 'do-not-show'})
    else:
        from Tea.exceptions import TeaException
        sdk_error = TeaException({'code': 'Forbidden.RAM', 'message': 'No permission', 'data': {
            'RequestId': 'req-sdk', 'AccessDeniedDetail': {'AuthAction': 'dataworks:GetFile',
                                                         'EncodedDiagnosticMessage': 'do-not-show'}}})
    exc = fetch._saved_api_error('GetFile', sdk_error)
    assert exc.code == 'Forbidden.RAM' and exc.request_id == 'req-sdk'
    assert exc.permission_action == 'dataworks:GetFile'
    assert 'do-not-show' not in str(exc)


def test_listfiles_error_response_keeps_original_operation():
    class ListClient:
        def list_files(self, request):
            return NS(body=NS(success=False, error_code='Forbidden.ListFiles',
                             error_message='not authorized dataworks:ListFiles', request_id='req-list'))
    with pytest.raises(fetch.SavedAPIError) as error:
        fetch.fetch_saved_task(name='my_pyodps3', client=ListClient())
    assert error.value.operation == 'ListFiles'
    assert error.value.code == 'Forbidden.ListFiles'
    assert error.value.request_id == 'req-list'


def test_nondict_sdk_error_data_cannot_mask_original_failure():
    exc = fetch._saved_api_error('GetFile', NS(data='upstream bad gateway', code='BadGateway'))
    assert exc.code == 'BadGateway'

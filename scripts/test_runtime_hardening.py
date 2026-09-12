"""Offline regressions for selection, parameter provenance, locks and exception groups."""
import concurrent.futures
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading

import pytest

import debug_pyodps3 as debug
import fetch_task_sql as fetch
import pyodps3_runtime as runtime
import repair_pyodps3 as repair
from test_debug_pyodps3 import FakeAPI
from test_saved_fetch import SavedClient


@pytest.mark.parametrize('saved,explicit', [('', ['target=a', 'target=b']),
    ('target=a target=b', []), ('', ['bizdate=20260101', 'bizdate=20260911'])])
def test_parameter_conflicts_are_not_hidden(saved, explicit):
    with pytest.raises(ValueError):
        runtime.build_parameters(saved, explicit, '20260911')


def test_identical_duplicates_and_cross_source_override():
    assert runtime.build_parameters('target=a target=a', ['target=b', 'target=b'],
                                    '20260911') == 'target=b bizdate=20260911'


def test_saved_submission_lock_covers_precheck(tmp_path):
    api = FakeAPI()
    entered, release = threading.Event(), threading.Event()
    original = api.call
    def call(operation, payload, legacy=False):
        if operation == 'ListResourceGroups':
            entered.set()
            assert release.wait(5)
        return original(operation, payload, legacy)
    api.call = call
    bundle = debug.inspect_saved(api, file_id=10, bizdate='20260911', output_dir=tmp_path)
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        first = pool.submit(debug.submit_saved, api, bundle)
        assert entered.wait(5)
        second = pool.submit(debug.submit_saved, api, bundle)
        try:
            with pytest.raises(ValueError):
                second.result(timeout=1)
        finally:
            release.set()
        first.result(timeout=5)
    assert len(api.submissions) == 1
    assert repair.session_lock is runtime.session_lock


GROUP = '''  + Exception Group Traceback (most recent call last):
  |   File "<pyodps_user_code>", line 3, in main
  | ExceptionGroup: outer (2 sub-exceptions)
  +-+---------------- 1 ----------------
    | ValueError: first
    +---------------- 2 ----------------
    | exceptiongroup.ExceptionGroup: nested (1 sub-exception)
    +-+---------------- 1 ----------------
      | TypeError: second
      +------------------------------------
'''


@pytest.mark.parametrize('prefix', ['', '2026-09-12 16:00:00 ERROR ', 'WARNING:odps.pyodpswrapper:'])
def test_exception_group_tree_preserves_children(prefix):
    raw = '\n'.join(prefix + line for line in GROUP.splitlines())
    result = debug.analyze_log(raw + '\nExit code of the Shell command 0')
    assert [e['type'] for e in result['exceptions']] == ['ExceptionGroup', 'ValueError', 'ExceptionGroup', 'TypeError']
    assert result['tracebacks'][0]['text'] == GROUP.rstrip()
    assert result['traceback_parse_status'] == 'complete'


@pytest.mark.parametrize('status,expected', [('Success', 'needs_traceback_review'), ('Failure', 'needs_fix')])
def test_group_does_not_auto_pass(tmp_path, status, expected):
    manifest = dict(submission_status='submitted', remote_status=status, log_available=True,
        execution_code_match=True, configuration_match=True, selection_verified=True,
        execution_verified=True, started_time=1, finished_time=2, runtime_process_id='p',
        analysis=debug.analyze_log(GROUP + 'Exit code of the Shell command 0'))
    repair.classify_attempt(tmp_path, manifest)
    assert manifest['repair_state'] == expected
    assert not manifest['runtime_verified']


@pytest.mark.parametrize('status', ['Success', 'Failure'])
def test_incomplete_group_requires_diagnosis_even_with_review(tmp_path, status):
    manifest = dict(submission_status='submitted', remote_status=status, log_available=True,
        execution_code_match=True, configuration_match=True, selection_verified=True,
        execution_verified=True, started_time=1, finished_time=2, runtime_process_id='p',
        analysis=debug.analyze_log(GROUP.rsplit('      | TypeError', 1)[0] + 'Exit code of the Shell command 0'))
    repair.classify_attempt(tmp_path, manifest, traceback_review='handled')
    assert manifest['analysis']['traceback_parse_status'] == 'unknown'
    assert manifest['repair_state'] == 'needs_diagnosis'
    assert not manifest['runtime_verified']


def test_real_backport_group_followed_by_regular_output():
    import exceptiongroup
    try:
        raise exceptiongroup.ExceptionGroup('outer', [ValueError('first'),
            exceptiongroup.ExceptionGroup('nested', [TypeError('second')])])
    except Exception as exc:
        raw = ''.join(exceptiongroup.format_exception(exc))
    result = debug.analyze_log(raw + 'application finished\nExit code of the Shell command 0')
    assert result['traceback_parse_status'] == 'complete'
    assert [e['type'] for e in result['exceptions']] == ['ExceptionGroup', 'ValueError', 'ExceptionGroup', 'TypeError']
    assert result['tracebacks'][0]['text'] == raw.rstrip()


@pytest.mark.parametrize('clipped', ['ExceptionGroup', 'exceptiongroup.ExceptionGroup',
    '  + Exception Group Traceback (most recent call last):'])
def test_clipped_exception_group_marker_is_unknown(clipped):
    result = debug.analyze_log(clipped + '\nExit code of the Shell command 0')
    assert result['tracebacks']
    assert result['traceback_parse_status'] == 'unknown'


@pytest.mark.parametrize('raw', [
    '  + Exception Group Traceback (most recent call last):\n'
    '    | ValueError: retained leaf\n    +------------------------------------\n',
    '  + Exception Group Traceback (most recent call last):\n'
    '  | ExceptionGroup: root (1 sub-exception)\n'
    '    | ValueError: missing child boundary\n    +------------------------------------\n',
    '  + Exception Group Traceback (most recent call last):\n'
    '    | ExceptionGroup: surviving nested group (1 sub-exception)\n'
    '    +-+---------------- 1 ----------------\n'
    '      | ValueError: leaf\n      +------------------------------------\n',
])
def test_group_missing_root_or_child_boundary_cannot_pass_review(tmp_path, raw):
    analysis = debug.analyze_log(raw + 'Exit code of the Shell command 0')
    assert analysis['traceback_parse_status'] == 'unknown'
    assert analysis['tracebacks'][0]['text'] == raw.rstrip()
    manifest = dict(submission_status='submitted', remote_status='Success', log_available=True,
        execution_code_match=True, configuration_match=True, selection_verified=True,
        execution_verified=True, started_time=1, finished_time=2, runtime_process_id='p', analysis=analysis)
    repair.classify_attempt(tmp_path, manifest, traceback_review='handled')
    assert manifest['repair_state'] == 'needs_diagnosis'
    assert not manifest['runtime_verified']


def test_exception_group_child_cause_chain_is_one_child():
    import exceptiongroup
    try:
        try:
            raise ValueError('cause')
        except ValueError as cause:
            raise TypeError('effect') from cause
    except TypeError as child:
        group = exceptiongroup.ExceptionGroup('root', [child])
    try:
        raise group
    except Exception as exc:
        raw = ''.join(exceptiongroup.format_exception(exc))
    result = debug.analyze_log(raw + 'Exit code of the Shell command 0')
    assert result['traceback_parse_status'] == 'complete'
    assert [e['type'] for e in result['exceptions']] == ['ExceptionGroup', 'ValueError', 'TypeError']
    assert result['tracebacks'][0]['text'] == raw.rstrip()


@pytest.mark.parametrize('version', [False, True])
def test_ambiguous_exact_files_include_candidate_identity(monkeypatch, version):
    candidates = [dict(file_name='same', file_id=n, node_id=n + 10, folder_path='/folder' + str(n), content='sql') for n in (1, 2)]
    monkeypatch.setattr(fetch, 'create_client', object)
    monkeypatch.setattr(fetch, 'list_matching_files', lambda *_: candidates)
    monkeypatch.setattr(fetch, 'get_node_code', lambda *_: 'sql')
    with pytest.raises(fetch.TaskSqlNotFound) as error:
        fetch.resolve_file_id(object(), 'same') if version else fetch.fetch_task_sql('same')
    assert '/folder1' in str(error.value) and 'node_id=12' in str(error.value)


@pytest.mark.parametrize('version', [False, True])
def test_ambiguous_output_nodes_rejected_before_read(monkeypatch, version):
    monkeypatch.setattr(fetch, 'create_client', object)
    monkeypatch.setattr(fetch, 'list_matching_files', lambda *_: [])
    monkeypatch.setattr(fetch, 'get_nodes_by_output', lambda *_: [dict(node_id=1, node_name='a'), dict(node_id=2, node_name='b')])
    monkeypatch.setattr(fetch, 'get_node_code', lambda *_: pytest.fail('ambiguous code read'))
    with pytest.raises(fetch.TaskSqlNotFound, match='node_id=1'):
        fetch.resolve_file_id(object(), 'same') if version else fetch.fetch_task_sql('same')


def test_explicit_file_auto_and_history_crosscheck(monkeypatch):
    client = SavedClient()
    monkeypatch.setattr(fetch, 'get_node_code', lambda *_: 'production')
    assert fetch.fetch_task_sql(file_id=42, client=client)['sql_text'] == 'production'
    assert fetch.resolve_file_id(client, file_id=42)['file_id'] == 42
    with pytest.raises(ValueError):
        fetch.fetch_task_sql('other', file_id=42, client=client)


def test_explicit_node_is_production_only(monkeypatch):
    monkeypatch.setattr(fetch, 'get_node_code', lambda *_: 'production')
    result = fetch.fetch_task_sql(node_id=91, client=object())
    assert result['node_id'] == 91 and result['source'] == 'prod'


@pytest.mark.parametrize('script,args', [('fetch_task_sql.py', ['--help']),
    ('debug_pyodps3.py', ['--help']), ('repair_pyodps3.py', ['--help']),
    ('debug_pyodps3.py', ['analyze-log', '--file', 'console.log'])])
def test_clean_offline_cli_without_config_or_cloud_sdks(tmp_path, script, args):
    source = Path(__file__).parent
    for file in source.glob('*.py'):
        if file.name != 'config.py':
            shutil.copyfile(file, tmp_path / file.name)
    (tmp_path / 'console.log').write_text('Exit code of the Shell command 0', encoding='utf8')
    runner = '''import sys,runpy
class Block:
 def find_spec(self, fullname, path=None, target=None):
  if fullname.startswith(('odps', 'alibabacloud', 'Tea')): raise ImportError('cloud SDK forbidden')
sys.meta_path.insert(0,Block())
sys.argv=sys.argv[1:]
runpy.run_path(sys.argv[0],run_name='__main__')
'''
    env = {k: v for k, v in os.environ.items() if not k.startswith(('ALIYUN_', 'ODPS_', 'DATAWORKS_'))}
    result = subprocess.run([sys.executable, '-X', 'utf8', '-B', '-c', runner, script, *args],
        cwd=tmp_path, env=env, capture_output=True, text=True, encoding='utf8')
    assert result.returncode == 0, result.stderr


def load_isolated_config(tmp_path, monkeypatch, local=None):
    for key in list(os.environ):
        if key.startswith(('ODPS_', 'ALIYUN_', 'DATAWORKS_')):
            monkeypatch.delenv(key)
    shutil.copyfile(Path(__file__).with_name('runtime_config.py'), tmp_path / 'runtime_config.py')
    if local is not None:
        (tmp_path / 'config.py').write_text(local, encoding='utf8')
    def load():
        spec = importlib.util.spec_from_file_location('isolated_runtime_config', tmp_path / 'runtime_config.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    return load


def test_config_without_local_file_defers_validation(tmp_path, monkeypatch):
    config = load_isolated_config(tmp_path, monkeypatch)()
    assert config.ODPS_PROJECT == '' and config.DATAWORKS_PROJECT_ID == 0
    with pytest.raises(ValueError, match='环境凭证'):
        config.require_credentials()


def test_config_precedence_and_credentials_are_environment_only(tmp_path, monkeypatch):
    load = load_isolated_config(tmp_path, monkeypatch,
        "ODPS_PROJECT='local_project'\nODPS_ENDPOINT='local_endpoint'\nACCESS_ID='ignored_id'\nSECRET='ignored_secret'\n")
    config = load()
    assert not config.ACCESS_ID and not config.SECRET
    assert config.ODPS_PROJECT == 'local_project'
    monkeypatch.setenv('ODPS_PROJECT', 'environment_project')
    monkeypatch.setenv('ODPS_ACCESS_ID', 'fallback_id')
    monkeypatch.setenv('ODPS_SECRET', 'fallback_secret')
    monkeypatch.setenv('ALIYUN_ACCESS_KEY_ID', 'preferred_id')
    monkeypatch.setenv('ALIYUN_ACCESS_KEY_SECRET', 'preferred_secret')
    config = load()
    assert config.ODPS_PROJECT == 'environment_project' and config.ODPS_ENDPOINT == 'local_endpoint'
    assert config.ACCESS_ID == 'preferred_id' and config.SECRET == 'preferred_secret'
    config.validate_connection('odps')


def test_invalid_config_value_does_not_break_import_or_expose_value(tmp_path, monkeypatch):
    load = load_isolated_config(tmp_path, monkeypatch)
    monkeypatch.setenv('ODPS_ACCESS_ID', 'fake_id')
    monkeypatch.setenv('ODPS_SECRET', 'fake_secret')
    monkeypatch.setenv('DATAWORKS_PROJECT_ID', 'invalid_sensitive_value')
    config = load()
    with pytest.raises(ValueError) as error:
        config.validate_connection('dataworks')
    assert 'DATAWORKS_PROJECT_ID' in str(error.value)
    assert 'invalid_sensitive_value' not in str(error.value)


@pytest.mark.parametrize('value', ['True', '1.5'])
def test_invalid_local_project_id_is_not_coerced(tmp_path, monkeypatch, value):
    load = load_isolated_config(tmp_path, monkeypatch,
        "DATAWORKS_ENDPOINT='configured_endpoint'\nDATAWORKS_PROJECT_ID=" + value)
    monkeypatch.setenv('ODPS_ACCESS_ID', 'fake_id')
    monkeypatch.setenv('ODPS_SECRET', 'fake_secret')
    with pytest.raises(ValueError, match='DATAWORKS_PROJECT_ID'):
        load().validate_connection('dataworks')


def test_repair_cli_incomplete_group_is_not_success_exit(monkeypatch):
    monkeypatch.setattr(repair, 'report_session', lambda *args: dict(
        remote_status='Success', repair_state='needs_diagnosis', runtime_verified=False))
    assert repair.main(['report', '--session-dir', 'unused']) == 8


@pytest.mark.parametrize('argv', [['--node-id', '91', 'name'], ['--node-id', '91', '--list-versions'],
    ['--node-id', '91', '--get-version', '1'], ['--node-id', '91', '--diff', '1', '2'],
    ['--node-id', '91', '--search'], ['--node-id', '91', '--source', 'saved'],
    ['--node-id', '91', '--file-id', '42'], ['--search'], ['--search', '--file-id', '42']])
def test_cli_rejects_invalid_selection_before_connection(monkeypatch, argv):
    monkeypatch.setattr(sys, 'argv', ['fetch_task_sql.py', *argv])
    monkeypatch.setattr(fetch, 'create_client', lambda: pytest.fail('must reject before connection'))
    with pytest.raises(SystemExit) as error:
        fetch.main()
    assert error.value.code in (2, 3)


@pytest.mark.parametrize('mode', [['--list-versions'], ['--get-version', '1'], ['--diff', '1', '2']])
def test_history_cli_accepts_explicit_file_without_name(monkeypatch, mode):
    monkeypatch.setattr(sys, 'argv', ['fetch_task_sql.py', '--file-id', '42', *mode])
    seen = []
    for command in ('cmd_list_versions', 'cmd_get_version', 'cmd_diff'):
        monkeypatch.setattr(fetch, command, lambda args: seen.append((args.file_id, args.name)))
    fetch.main()
    assert seen == [(42, None)]

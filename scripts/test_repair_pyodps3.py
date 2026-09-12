"""Stateful repair contracts. The fake is the external DataWorks boundary only."""
import importlib
import json
from pathlib import Path

import pytest
import debug_pyodps3 as d
from test_debug_pyodps3 import FakeAPI, CODE


def repair():
    assert importlib.util.find_spec('repair_pyodps3'), 'repair session tool is not implemented'
    return importlib.import_module('repair_pyodps3')


class RepairAPI(FakeAPI):
    def __init__(self):
        super().__init__()
        self.workflows = {}
        self.fail_after_accept = False
        self.code_mismatch = False
        self.config_mismatch = False
        self.skip = False
        self.status_error = None
        self.manual_trigger = False
        self.wrong_date = False

    def call(self, operation, payload, legacy=False):
        if operation == 'ExecuteAdhocWorkflowInstance':
            self.submissions.append(payload)
            wid = 100 + len(self.submissions)
            self.workflows[wid] = payload
            if self.fail_after_accept:
                raise d.APIError(operation, 'RequestTimeout', 'response lost')
            return {'WorkflowInstanceId': wid, 'RequestId': 'submitted'}
        if operation == 'ListWorkflowInstances':
            assert payload['BizDate'] == 1789056000000
            rows = [{'Id': wid, 'Name': p['Name'], 'ProjectId': p['ProjectId'], 'EnvType': 'Dev'}
                    for wid, p in self.workflows.items() if payload.get('Name', '') in p['Name']]
            return {'PagingInfo': {'TotalCount': len(rows), 'WorkflowInstances': rows}}
        if operation == 'GetWorkflowInstance':
            wid = payload['Id']; p = self.workflows[wid]
            return {'WorkflowInstance': {'Id': wid, 'Name': p['Name'], 'ProjectId': p['ProjectId'],
                    'BizDate': 1788969600000 if self.wrong_date else p['BizDate'], 'EnvType': 'Dev'}}
        if operation == 'ListTaskInstances':
            if not payload.get('WorkflowInstanceId'):
                assert payload['Bizdate'] == 1789056000000 and payload['ProjectEnv'] == 'Dev'
                assert payload['TaskName'] == 'test_task' and payload['TaskType'] == 'PYODPS3'
                rows = [{'Id': wid + 1000, 'WorkflowInstanceId': wid, 'TaskName': 'test_task',
                         'TaskType': 'PYODPS3', 'WorkflowInstanceType': 'ManualFlow'} for wid in self.workflows]
                return {'PagingInfo': {'TotalCount': len(rows), 'TaskInstances': rows}}
            wid = payload['WorkflowInstanceId']
            return {'PagingInfo': {'TotalCount': 1, 'TaskInstances': [{'Id': wid + 1000, 'WorkflowInstanceId': wid}]}}
        if operation == 'GetTaskInstance':
            if self.status_error:
                raise self.status_error
            wid = payload['Id'] - 1000; p = self.workflows[wid]; task = p['Tasks'][0]
            script = dict(task['Script'])
            if self.code_mismatch:
                script['Content'] += '\n# other code'
            runtime = dict(task['RuntimeResource'])
            if self.config_mismatch:
                runtime['ResourceGroupId'] = 'different'
            return {'TaskInstance': {'Id': payload['Id'], 'WorkflowInstanceId': wid,
                    'ProjectId': 334179, 'ProjectEnv': 'Dev', 'TaskType': 'PYODPS3',
                    'TaskName': task['Name'], 'Bizdate': p['BizDate'], 'RunNumber': 1,
                    'Status': self.status_value, 'Script': script,
                    'DataSource': task['DataSource'], 'RuntimeResource': runtime,
                    'TriggerRecurrence': 'Skip' if self.skip else 'Manual' if self.manual_trigger else 'Normal',
                    'TriggerType': 'Manual' if self.manual_trigger else 'Scheduler',
                    'Runtime': {'ProcessId': 'T3_test'},
                    'StartedTime': 100, 'FinishedTime': 200}}
        if operation == 'GetTaskInstanceLog':
            self.log_calls.append(payload)
            return {'TaskInstanceLog': self.log_text}
        return super().call(operation, payload, legacy)


def session(tmp_path, api=None, **kwargs):
    api = api or RepairAPI()
    root = repair().init_session(api, file_id=10, bizdate='20260911', output_dir=tmp_path, **kwargs)
    return api, root


def candidate(root, text='print("修复完成")\n', reason='修正根因'):
    p = Path(root) / 'working.py'; p.write_bytes(text.encode('utf-8'))
    return repair().prepare_candidate(root, p, reason)


def success(api):
    api.status_value = 'Success'
    api.log_text = '运行环境 Python=3.7.16\n修复完成\nExit code of the Shell command 0\n'


def test_failed_then_fixed_exports_exact_verified_candidate(tmp_path):
    api, root = session(tmp_path)
    first = candidate(root, 'raise ValueError("failure")\n')
    result = repair().run_attempt(api, root, first['attempt'], wait=False)
    assert result['repair_state'] == 'needs_fix'
    second = candidate(root)
    success(api)
    result = repair().run_attempt(api, root, second['attempt'], wait=False)
    assert result['runtime_verified'] is True
    report = repair().report_session(root)
    assert report['session_status'] == 'succeeded'
    assert Path(report['final_code']).read_bytes() == 'print("修复完成")\n'.encode()
    assert (Path(root) / 'snapshot.py').read_bytes() == CODE.encode()
    assert len(api.submissions) == 2
    assert all(p['EnvType'] == 'Dev' for p in api.submissions)


def test_max_attempts_and_unknown_submission_consume_budget(tmp_path):
    api, root = session(tmp_path, max_attempts=1)
    one = candidate(root)
    api.fail_after_accept = True
    result = repair().run_attempt(api, root, one['attempt'], wait=False)
    assert result['submission_status'] == 'unknown'
    with pytest.raises(ValueError):
        repair().run_attempt(api, root, one['attempt'], wait=False)
    with pytest.raises(ValueError):
        candidate(root, 'print("second")\n')
    assert repair().report_session(root)['submissions'] == 1
    assert len(api.submissions) == 1


def test_response_lost_resumes_existing_workflow_without_new_submit(tmp_path):
    api, root = session(tmp_path)
    one = candidate(root)
    api.fail_after_accept = True
    repair().run_attempt(api, root, one['attempt'], wait=False)
    success(api)
    result = repair().resume_session(api, root, wait=False)
    assert result['runtime_verified'] is True
    assert len(api.submissions) == 1


def test_unknown_adhoc_recovery_uses_task_index_when_workflow_list_omits_manualflow(tmp_path):
    class API(RepairAPI):
        def call(self, operation, payload, legacy=False):
            if operation == 'ListWorkflowInstances':
                return {'PagingInfo': {'TotalCount': 0, 'WorkflowInstances': []}}
            return super().call(operation, payload, legacy)
    api, root = session(tmp_path, API()); one = candidate(root); api.fail_after_accept = True
    repair().run_attempt(api, root, one['attempt'], wait=False); success(api)
    assert repair().resume_session(api, root, wait=False)['runtime_verified']
    evidence = list((Path(one['run_dir']) / 'recovery').glob('*.json'))
    assert evidence and 'ListTaskInstances' in evidence[0].read_text(encoding='utf-8')
    assert len(api.submissions) == 1


@pytest.mark.parametrize('case', ['empty', 'multiple', 'wrong_code', 'wrong_config', 'wrong_date'])
def test_uncertain_recovery_requires_unique_matching_execution(tmp_path, case):
    api, root = session(tmp_path)
    one = candidate(root)
    api.fail_after_accept = True
    repair().run_attempt(api, root, one['attempt'], wait=False)
    if case == 'empty':
        api.workflows = {}
    elif case == 'multiple':
        api.workflows[999] = next(iter(api.workflows.values()))
    elif case == 'wrong_code':
        api.code_mismatch = True
    elif case == 'wrong_date':
        api.wrong_date = True
    else:
        api.config_mismatch = True
    with pytest.raises(ValueError):
        repair().resume_session(api, root, wait=False)
    evidence = list((Path(one['run_dir']) / 'recovery').glob('*.json'))
    assert evidence and json.loads(evidence[0].read_text(encoding='utf-8')).get('error')
    assert len(api.submissions) == 1
    assert repair().report_session(root)['session_status'] != 'succeeded'


@pytest.mark.parametrize('case', ['no_log', 'wrong_code', 'wrong_config', 'skip', 'success_traceback', 'truncated', 'status_error'])
def test_missing_or_conflicting_evidence_never_exports_verified_code(tmp_path, case):
    api, root = session(tmp_path); one = candidate(root); success(api)
    if case == 'no_log': api.log_text = ''
    if case == 'wrong_code': api.code_mismatch = True
    if case == 'wrong_config': api.config_mismatch = True
    if case == 'skip': api.skip = True
    if case == 'success_traceback': api.log_text = 'Traceback (most recent call last):\nValueError: handled?\n'
    if case == 'truncated': api.log_text += '[日志截断]\n'
    if case == 'status_error': api.status_error = d.APIError('GetTaskInstance', '403', 'Forbidden')
    result = repair().run_attempt(api, root, one['attempt'], wait=False)
    assert result['runtime_verified'] is False
    report = repair().report_session(root)
    assert not report.get('final_code')
    assert not (Path(root) / 'final.py').exists()
    with pytest.raises(ValueError): candidate(root, 'print("next")\n')


def test_successful_handled_traceback_needs_explicit_review(tmp_path):
    api, root = session(tmp_path); one = candidate(root); success(api)
    api.log_text = 'Traceback (most recent call last):\nValueError: deliberate handled diagnostic\nExit code of the Shell command 0\n'
    repair().run_attempt(api, root, one['attempt'], wait=False)
    report = repair().report_session(root, traceback_review='检查候选：此异常在 except 中捕获并打印，是诊断流程预期输出')
    assert report['session_status'] == 'succeeded'
    assert report['final_code']


def test_swallowed_exception_review_allows_real_fix_without_claiming_success(tmp_path):
    api, root = session(tmp_path); one = candidate(root); success(api)
    api.log_text = 'Traceback (most recent call last):\nValueError: required validation failed\nExit code of the Shell command 0\n'
    repair().run_attempt(api, root, one['attempt'], wait=False)
    report = repair().report_session(root, traceback_review='代码捕获必要校验异常后直接返回，未完成处理，需修复', traceback_outcome='unresolved')
    assert report['session_status'] == 'needs_fix'
    assert not report['final_code']
    two = candidate(root, 'print("fixed required validation")\n'); success(api)
    assert repair().run_attempt(api, root, two['attempt'], wait=False)['runtime_verified']


def test_new_traceback_invalidates_previous_handled_review(tmp_path):
    api, root = session(tmp_path); one = candidate(root); success(api)
    api.log_text = 'Traceback (most recent call last):\nValueError: deliberately handled\nExit code of the Shell command 0\n'
    repair().run_attempt(api, root, one['attempt'], wait=False)
    assert repair().report_session(root, traceback_review='此ValueError是诊断中明确捕获的预期异常')['final_code']
    api.log_text += 'Traceback (most recent call last):\nRuntimeError: new unexpected failure\nExit code of the Shell command 0\n'
    result = repair().resume_session(api, root, wait=False)
    assert result['repair_state'] == 'needs_traceback_review'
    assert not repair().report_session(root)['final_code']


def test_incomplete_nonempty_terminal_log_cannot_verify_success(tmp_path):
    api, root = session(tmp_path); one = candidate(root); success(api)
    api.log_text = '开始内存诊断\n'
    result = repair().run_attempt(api, root, one['attempt'], wait=False)
    assert result['repair_state'] == 'waiting_logs'
    assert not repair().report_session(root)['final_code']
    success(api)
    assert repair().resume_session(api, root, wait=False)['runtime_verified']


def test_wait_continues_when_terminal_log_has_only_nonempty_prefix(tmp_path):
    class DelayedAPI(RepairAPI):
        def call(self, operation, payload, legacy=False):
            if operation == 'GetTaskInstanceLog' and self.log_calls:
                success(self)
            return super().call(operation, payload, legacy)
    api, root = session(tmp_path, DelayedAPI()); one = candidate(root); success(api)
    api.log_text = '开始内存诊断\n'
    result = repair().run_attempt(api, root, one['attempt'], wait=True, poll_seconds=1, timeout=5)
    assert result['runtime_verified'] and len(api.log_calls) == 2


def test_no_progress_and_identical_candidate_stop(tmp_path):
    api, root = session(tmp_path)
    one = candidate(root); repair().run_attempt(api, root, one['attempt'], wait=False)
    with pytest.raises(ValueError, match='相同|重复'):
        candidate(root)
    two = candidate(root, 'print("changed but failure same")\n')
    repair().run_attempt(api, root, two['attempt'], wait=False)
    assert repair().report_session(root)['session_status'] == 'no_progress'
    with pytest.raises(ValueError): candidate(root, 'print("third")\n')


@pytest.mark.parametrize('target', ['snapshot.py', 'runtime.json', 'candidate.py', 'request.json'])
def test_frozen_evidence_tampering_blocks_submission(tmp_path, target):
    api, root = session(tmp_path); one = candidate(root)
    path = Path(root) / target if target in ('snapshot.py', 'runtime.json') else Path(one['run_dir']) / target
    path.write_text(path.read_text(encoding='utf-8') + '\n ', encoding='utf-8')
    with pytest.raises(ValueError): repair().run_attempt(api, root, one['attempt'], wait=False)
    assert not api.submissions


def test_prepare_checks_syntax_without_executing_python(tmp_path):
    api, root = session(tmp_path)
    marker = Path(root) / 'must-not-exist'
    candidate(root, f'from pathlib import Path\nPath({str(marker)!r}).write_text("bad")\n')
    assert not marker.exists()
    with pytest.raises(ValueError): candidate(root, 'broken python ???')


def test_imported_log_does_not_claim_code_correspondence(tmp_path):
    log = tmp_path / 'existing.log'; log.write_text('old error\n', encoding='utf-8')
    api, root = session(tmp_path, log_file=log, log_partial=True)
    state = repair().load_session(root)
    assert state['initial_log']['code_match'] is None
    assert state['initial_log']['capture_partial'] is True
    assert not api.submissions


def test_cli_invalid_attempt_budget_fails_before_connection():
    with pytest.raises(SystemExit):
        repair().main(['init', '--file-id', '10', '--bizdate', '20260911', '--max-attempts', '0'])


@pytest.mark.parametrize('target', ['executed.py', 'observation.json', 'raw_log', 'missing_log'])
def test_report_rechecks_execution_evidence_before_export(tmp_path, target):
    api, root = session(tmp_path); one = candidate(root); success(api)
    result = repair().run_attempt(api, root, one['attempt'], wait=False)
    assert result['runtime_verified']
    folder = Path(one['run_dir'])
    path = folder / (result['log_file'] if target in ('raw_log', 'missing_log') else target)
    if target == 'missing_log': path.unlink()
    else: path.write_bytes(path.read_bytes() + b'\nchanged')
    report = repair().report_session(root)
    assert report['session_status'] != 'succeeded'
    assert not report['final_code']
    assert not (Path(root) / 'final.py').exists()


def test_refresh_failure_retracts_stale_success_export(tmp_path):
    api, root = session(tmp_path); one = candidate(root); success(api)
    repair().run_attempt(api, root, one['attempt'], wait=False)
    assert repair().report_session(root)['final_code']
    api.status_error = d.APIError('GetTaskInstance', '403', 'Forbidden')
    result = repair().resume_session(api, root, wait=False)
    assert not result['runtime_verified']
    report = repair().report_session(root)
    assert not report['final_code']
    assert not (Path(root) / 'final.py').exists()


def test_three_distinct_code_failures_exhaust_budget(tmp_path):
    api, root = session(tmp_path)
    for n in range(3):
        one = candidate(root, f'raise ValueError("failure {n}")\n')
        api.log_text = f'Traceback (most recent call last):\nValueError: failure {n}\n'
        repair().run_attempt(api, root, one['attempt'], wait=False)
    assert repair().report_session(root)['session_status'] == 'attempt_limit'
    with pytest.raises(ValueError): candidate(root, 'print("fourth")\n')
    assert len(api.submissions) == 3


def test_crash_before_network_never_resubmits_and_reconciles_budget(tmp_path, monkeypatch):
    api, root = session(tmp_path); one = candidate(root)
    real_save = repair().save_session
    def interrupt_save(root, state):
        if state['submissions']: raise OSError('simulated interruption before session write')
        return real_save(root, state)
    monkeypatch.setattr(repair(), 'save_session', interrupt_save)
    with pytest.raises(OSError): repair().run_attempt(api, root, one['attempt'], wait=False)
    monkeypatch.setattr(repair(), 'save_session', real_save)
    with pytest.raises(ValueError): repair().run_attempt(api, root, one['attempt'], wait=False)
    with pytest.raises(ValueError): repair().resume_session(api, root, wait=False)
    assert repair().load_session(root)['submissions'] == 1
    assert not api.submissions


def test_concurrent_process_cannot_submit_and_crashed_lock_is_released(tmp_path):
    import subprocess
    import sys
    api, root = session(tmp_path); one = candidate(root)
    code = 'import sys,os; from repair_pyodps3 import session_lock;\nwith session_lock(sys.argv[1]):\n print("locked",flush=True)\n sys.stdin.readline()\n os._exit(0)'
    process = subprocess.Popen([sys.executable, '-u', '-c', code, str(root)],
                               cwd=Path(__file__).parent, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == 'locked'
        with pytest.raises(ValueError, match='另一个进程'):
            repair().run_attempt(api, root, one['attempt'], wait=False)
        assert not api.submissions
    finally:
        process.communicate('\n', timeout=5)
    success(api)
    assert repair().run_attempt(api, root, one['attempt'], wait=False)['runtime_verified']


def test_prepare_interruption_recovers_without_network(tmp_path, monkeypatch):
    api, root = session(tmp_path)
    real_save = repair().save_session
    def interrupt_save(root, state):
        raise OSError('simulated interruption before prepare session commit')
    monkeypatch.setattr(repair(), 'save_session', interrupt_save)
    with pytest.raises(OSError): candidate(root)
    monkeypatch.setattr(repair(), 'save_session', real_save)
    prepared = candidate(root)
    assert prepared['attempt'] == 1
    assert not api.submissions
    assert list((Path(root) / 'abandoned').glob('*/candidate.py'))


def test_manually_triggered_adhoc_is_real_execution_with_process_and_exit(tmp_path):
    api, root = session(tmp_path); api.manual_trigger = True; success(api)
    one = candidate(root)
    assert repair().run_attempt(api, root, one['attempt'], wait=False)['runtime_verified']

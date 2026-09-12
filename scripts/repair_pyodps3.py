#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""PyODPS3 本地修复会话。Codex 修改候选；工具冻结、验证并恢复远端运行。

只使用临时工作流运行候选，不修改 DataWorks 原节点。run 需已获当前任务授权。
"""
import argparse
import ast
from datetime import datetime, timezone
import difflib
import hashlib
import json
from pathlib import Path
import sys
import uuid

import debug_pyodps3 as debug
from pyodps3_runtime import (APIError, DataWorksAPI, build_adhoc_request,
    resolve_runtime, observe_configuration, write_json, redacted_object, redact, session_lock)


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))



def load_session(root):
    state = read_json(Path(root) / 'session.json')
    if state.get('schema_version') != 1 or state.get('kind') != 'pyodps3_repair':
        raise ValueError('不是受支持的修复会话目录')
    return state


def save_session(root, state):
    state['updated_at'] = datetime.now(timezone.utc).isoformat()
    write_json(Path(root) / 'session.json', state)


def attempt_dir(root, number):
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise ValueError('轮次须为正整数')
    return Path(root) / 'attempts' / ('%03d' % number)


def check_frozen(root, state, manifest=None):
    root = Path(root)
    for filename, key in [('snapshot.py', 'baseline_sha256'), ('snapshot.json', 'metadata_sha256'),
                          ('runtime.json', 'runtime_sha256')]:
        if file_hash(root / filename) != state[key]:
            raise ValueError('冻结文件哈希改变：' + filename)
    if manifest:
        folder = attempt_dir(root, manifest['attempt'])
        for filename, key in [('candidate.py', 'code_sha256'), ('request.json', 'request_sha256')]:
            if file_hash(folder / filename) != manifest[key]:
                raise ValueError('候选冻结文件哈希改变：' + filename)
        request = read_json(folder / 'request.json')
        task = request['Tasks'][0]
        frozen = read_json(root / 'runtime.json')
        base = read_json(root / 'manifest.json')
        if (task['Script']['Content'].encode('utf-8') != (folder / 'candidate.py').read_bytes()
                or task['Script']['Parameters'] != state['parameters']
                or task['RuntimeResource'] != frozen['runtime_resource']
                or task['DataSource'] != frozen['data_source']
                or request['ProjectId'] != state['project_id'] or request['EnvType'] != 'Dev'
                or request['BizDate'] != debug.bizdate_millis(state['bizdate'])
                or base['code_sha256'] != state['baseline_sha256']):
            raise ValueError('请求与候选、会话参数或运行配置不一致')


def init_session(api, name=None, file_id=None, bizdate=None, parameters=None, output_dir=None,
                 max_attempts=3, runtime_overrides=None, log_run_dir=None, log_file=None,
                 log_partial=False):
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
        raise ValueError('max_attempts 必须为正整数')
    if log_run_dir and log_file:
        raise ValueError('已有诊断目录和日志文件只能选择一个')
    debug.bizdate_millis(bizdate)
    root = debug.inspect_saved(api, name=name, file_id=file_id, bizdate=bizdate,
                               overrides=parameters, output_dir=output_dir or 'outputs/pyodps3_repair')
    baseline = read_json(root / 'manifest.json')
    saved = read_json(root / 'snapshot.json')
    try:
        runtime = resolve_runtime(api, saved, runtime_overrides or {})
    except (APIError, ValueError) as exc:
        write_json(root / 'preflight.json', {'ready': False, 'error': str(exc), 'run_dir': str(root)})
        raise ValueError('运行预检未通过；证据目录 %s：%s' % (root, exc)) from None
    write_json(root / 'runtime.json', runtime)
    write_json(root / 'preflight.json', {'ready': True, 'runtime': redacted_object(runtime),
                                        'remote_submission': 'not_performed'})
    state = {'schema_version': 1, 'kind': 'pyodps3_repair', 'session_status': 'ready',
             'file_id': saved['file_id'], 'task_name': saved['task_name'],
             'project_id': baseline['project_id'], 'bizdate': bizdate,
             'parameters': baseline['parameters'], 'baseline_sha256': file_hash(root / 'snapshot.py'),
             'metadata_sha256': file_hash(root / 'snapshot.json'),
             'runtime_sha256': file_hash(root / 'runtime.json'), 'max_attempts': max_attempts,
             'submissions': 0, 'attempts': [], 'active_attempt': None,
             'business_validation': 'not_performed'}
    if log_file or log_run_dir:
        code_digest = None
        if log_run_dir:
            source_root = Path(log_run_dir)
            source_manifest = read_json(source_root / 'manifest.json')
            filename = source_manifest.get('log_file')
            if not filename:
                raise ValueError('已有诊断目录没有可读日志')
            path = (source_root / filename).resolve()
            if not path.is_relative_to(source_root.resolve()):
                raise ValueError('日志路径越出诊断目录')
            code_digest = source_manifest.get('execution_code_sha256')
            log_partial = log_partial or bool(source_manifest.get('possibly_truncated'))
        else:
            path = Path(log_file)
        raw = path.read_bytes()
        analysis = debug.analyze_log(raw.decode('utf-8-sig'), partial=log_partial)
        (root / 'initial.log').write_bytes(raw)
        write_json(root / 'initial.analysis.json', analysis)
        state['initial_log'] = {'source': str(path.resolve()), 'file': 'initial.log',
            'capture_partial': log_partial, 'sha256': file_hash(root / 'initial.log'),
            'code_match': code_digest == state['baseline_sha256'] if code_digest else None,
            'note': '只有代码哈希一致才可直接按原日志行号定位基准代码'}
    save_session(root, state)
    return root


def prepare_candidate(root, file, reason):
    root = Path(root)
    if not str(reason).strip():
        raise ValueError('必须记录本轮修复依据')
    source = Path(file).read_bytes()
    try:
        text = source.decode('utf-8-sig')
        ast.parse(text)
    except (UnicodeError, SyntaxError) as exc:
        raise ValueError('候选必须是有效的 UTF-8 Python：' + str(exc)) from None
    # Canonical bytes are exactly the string sent to the remote API (BOM is an encoding marker).
    source = text.encode('utf-8')
    with session_lock(root):
        state = load_session(root); check_frozen(root, state)
        if state['session_status'] in ('succeeded', 'no_progress'):
            raise ValueError('会话已成功或连续无进展，不能继续提交')
        if state['submissions'] >= state['max_attempts']:
            raise ValueError('已达到远端提交次数上限')
        if state['active_attempt']:
            previous = read_json(attempt_dir(root, state['active_attempt']) / 'manifest.json')
            if previous.get('repair_state') != 'needs_fix':
                raise ValueError('当前轮次尚不能进入修复；请恢复日志、运行或核实阻塞')
        digest = hashlib.sha256(source).hexdigest()
        for entry in state['attempts']:
            if entry['code_sha256'] == digest:
                raise ValueError('相同代码和配置已准备或提交，不重复运行')
        number = len(state['attempts']) + 1
        folder = attempt_dir(root, number)
        if folder.exists():
            abandoned_manifest = folder / 'manifest.json'
            if abandoned_manifest.exists() and read_json(abandoned_manifest).get('submission_status') != 'not_started':
                raise ValueError('孤立轮次可能已提交，必须核实，不能覆盖或重新提交')
            # Only unreferenced, never-submitted local preparations may be quarantined.
            destination = root / 'abandoned' / ('%03d_' % number + uuid.uuid4().hex)
            if not folder.resolve().is_relative_to(root.resolve()) or not destination.resolve().is_relative_to(root.resolve()):
                raise ValueError('轮次路径越出会话目录')
            destination.parent.mkdir(exist_ok=True)
            folder.replace(destination)
        folder.mkdir(parents=True, exist_ok=False)
        runtime = read_json(root / 'runtime.json')
        unique = uuid.uuid4().hex
        request = build_adhoc_request(project_id=state['project_id'], task_name=state['task_name'],
            owner=runtime['owner'], bizdate=state['bizdate'], parameters=state['parameters'],
            source=text, runtime_resource=runtime['runtime_resource'], data_source=runtime['data_source'],
            unique_code=unique)
        (folder / 'candidate.py').write_bytes(source)
        (folder / 'reason.txt').write_text(reason, encoding='utf-8')
        before = (attempt_dir(root, number - 1) / 'candidate.py') if number > 1 else root / 'snapshot.py'
        diff = ''.join(difflib.unified_diff(before.read_text(encoding='utf-8').splitlines(True),
                         text.splitlines(True), fromfile='previous.py', tofile='candidate.py'))
        (folder / 'changes.patch').write_text(diff, encoding='utf-8')
        write_json(folder / 'inspection.json', redacted_object(debug.scan_effects(text)))
        write_json(folder / 'request.json', request)
        manifest = {'schema_version': 1, 'source': 'candidate', 'attempt': number,
            'parent_attempt': number - 1 or None, 'file_id': state['file_id'],
            'task_name': state['task_name'], 'project_id': state['project_id'],
            'project_env': 'Dev', 'bizdate': state['bizdate'], 'parameters': state['parameters'],
            'code_sha256': digest, 'request_sha256': file_hash(folder / 'request.json'),
            'runtime_sha256': state['runtime_sha256'], 'workflow_name': request['Name'],
            'client_unique_code': unique, 'submission_status': 'not_started',
            'workflow_instance_id': None, 'task_instance_id': None, 'remote_status': None,
            'repair_state': 'prepared', 'runtime_verified': False, 'log_available': False,
            'business_validation': 'not_performed'}
        debug.save_manifest(folder, manifest)
        state['attempts'].append({'attempt': number, 'code_sha256': digest})
        state.update(active_attempt=number, session_status='prepared')
        save_session(root, state)
        return dict(manifest, run_dir=str(folder.resolve()))


def failure_fingerprint(analysis):
    # Platform timestamps and progress chatter must not make the same failure look new.
    evidence = {'exceptions': analysis.get('exceptions'), 'lines': analysis.get('user_code_lines'),
                'row_counts': analysis.get('row_counts'), 'diagnoses': analysis.get('diagnoses')}
    return hashlib.sha256(json.dumps(evidence, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def classify_attempt(folder, manifest, traceback_review=None):
    manifest['runtime_verified'] = False
    analysis = manifest.get('analysis') or {}
    if manifest.get('submission_status') in ('submitting', 'unknown'):
        manifest['repair_state'] = 'submission_unknown'
    elif manifest.get('submission_status') == 'rejected':
        manifest['repair_state'] = 'blocked'
    elif manifest.get('status_error') or manifest.get('status_cached') or manifest.get('log_state') == 'error':
        manifest['repair_state'] = 'collection_error'
    elif manifest.get('remote_status') not in ('Success', 'Failure'):
        manifest['repair_state'] = 'waiting'
    elif not manifest.get('log_available') or manifest.get('log_cached'):
        manifest['repair_state'] = 'waiting_logs'
    elif (manifest.get('execution_code_match') is not True or manifest.get('configuration_match') is not True
          or manifest.get('selection_verified') is not True or manifest.get('evidence_error')):
        manifest['repair_state'] = 'evidence_mismatch'
    elif analysis.get('possibly_truncated') or analysis.get('capture_partial'):
        manifest['repair_state'] = 'partial_log'
    elif analysis.get('traceback_parse_status') == 'unknown':
        manifest['repair_state'] = 'needs_diagnosis'
    elif manifest.get('remote_status') == 'Failure':
        diagnoses = analysis.get('diagnoses', [])
        if 'permission_possible' in diagnoses or not analysis.get('exceptions'):
            manifest['repair_state'] = 'needs_diagnosis'
        else:
            manifest['repair_state'] = 'needs_fix'
            manifest['failure_fingerprint'] = failure_fingerprint(analysis)
    elif not analysis.get('exit_codes'):
        manifest['repair_state'] = 'waiting_logs'
    elif (not manifest.get('execution_verified') or not manifest.get('started_time') or
          not manifest.get('finished_time') or not manifest.get('runtime_process_id')):
        manifest['repair_state'] = 'execution_unverified'
    elif any(code != 0 for code in analysis.get('exit_codes', [])):
        manifest['repair_state'] = 'evidence_mismatch'
    elif analysis.get('tracebacks') and not traceback_review:
        manifest['repair_state'] = 'needs_traceback_review'
    elif analysis.get('tracebacks') and manifest.get('traceback_outcome') == 'unresolved':
        manifest.update(repair_state='needs_fix', failure_fingerprint=failure_fingerprint(analysis),
                        traceback_review=traceback_review)
    else:
        manifest.update(repair_state='passed', runtime_verified=True)
        if traceback_review:
            manifest['traceback_review'] = traceback_review
    debug.save_manifest(folder, manifest)
    return manifest


def update_session(root, state, manifest):
    state['session_status'] = 'succeeded' if manifest.get('runtime_verified') else manifest['repair_state']
    number = manifest['attempt']
    if number > 1 and manifest.get('repair_state') == 'needs_fix':
        previous = read_json(attempt_dir(root, number - 1) / 'manifest.json')
        if previous.get('failure_fingerprint') == manifest.get('failure_fingerprint'):
            state['session_status'] = 'no_progress'
    if state['submissions'] >= state['max_attempts'] and state['session_status'] == 'needs_fix':
        state['session_status'] = 'attempt_limit'
    save_session(root, state)
    return dict(manifest, session_status=state['session_status'], session_dir=str(Path(root).resolve()))


def collect_attempt(api, root, state, manifest, wait, poll_seconds, timeout):
    folder = attempt_dir(root, manifest['attempt'])
    try:
        result = debug.read_instance_logs(api, folder, wait=wait, poll_seconds=poll_seconds, timeout=timeout)
    except APIError as exc:
        manifest.update(status_error=str(exc), repair_state='collection_error', runtime_verified=False)
        debug.save_manifest(folder, manifest)
        return update_session(root, state, manifest)
    except ValueError as exc:
        manifest.update(status_error=str(exc), repair_state='evidence_mismatch', runtime_verified=False)
        debug.save_manifest(folder, manifest)
        return update_session(root, state, manifest)
    request = read_json(folder / 'request.json')
    observed = folder / 'observation.json'
    if observed.is_file():
        result.update(observe_configuration(read_json(observed), request))
    else:
        result.update(configuration_match=None, configuration_unavailable=['observation'], configuration_mismatches=[])
    result.pop('evidence_error', None)
    result['evidence_hashes'] = {}
    for name in ('executed.py', 'observation.json', result.get('log_file')):
        if name and (folder / name).is_file():
            result['evidence_hashes'][name] = file_hash(folder / name)
    if result.get('traceback_review') and result.get('review_log_sha256') != result.get('log_sha256'):
        result.setdefault('superseded_reviews', []).append({k: result.get(k) for k in
            ('traceback_review', 'traceback_outcome', 'review_log_sha256')})
        for key in ('traceback_review', 'traceback_outcome', 'review_log_sha256'):
            result.pop(key, None)
    classify_attempt(folder, result, result.get('traceback_review'))
    return update_session(root, state, result)


def run_attempt(api, root, attempt=None, wait=True, poll_seconds=10, timeout=1800):
    root = Path(root)
    if not 1 <= poll_seconds <= 60 or timeout < 0:
        raise ValueError('轮询间隔须为1至60秒，等待时长不能为负')
    with session_lock(root):
        state = load_session(root)
        number = attempt or state['active_attempt']
        if number != state['active_attempt']:
            raise ValueError('只能运行当前活动轮次')
        folder = attempt_dir(root, number); manifest = read_json(folder / 'manifest.json')
        check_frozen(root, state, manifest)
        if manifest['submission_status'] != 'not_started':
            raise ValueError('此候选已经尝试提交；使用 resume，不得重复提交')
        if state['submissions'] >= state['max_attempts']:
            raise ValueError('已达到远端提交次数上限')
        # Both records are written before the network request. Recovery reconciles the counter.
        manifest.update(submission_status='submitting', repair_state='submission_unknown')
        debug.save_manifest(folder, manifest)
        state['submissions'] += 1; state['session_status'] = 'submission_unknown'
        save_session(root, state)
        request = read_json(folder / 'request.json')
        try:
            response = api.call('ExecuteAdhocWorkflowInstance', request)
            if not response.get('WorkflowInstanceId'):
                raise APIError('ExecuteAdhocWorkflowInstance', None, '响应没有工作流ID，提交结果不确定')
        except APIError as exc:
            manifest.update(submission_status='rejected' if exc.definite_rejection else 'unknown',
                            error=str(exc), repair_state='blocked' if exc.definite_rejection else 'submission_unknown')
            debug.save_manifest(folder, manifest)
            return update_session(root, state, manifest)
        manifest.update(submission_status='submitted', workflow_instance_id=response['WorkflowInstanceId'],
                        submit_request_id=response.get('RequestId'), repair_state='waiting')
        debug.save_manifest(folder, manifest)
        return collect_attempt(api, root, state, manifest, wait, poll_seconds, timeout)


def recover_workflow(api, root, state, manifest, workflow_instance_id):
    folder = attempt_dir(root, manifest['attempt'])
    request = read_json(folder / 'request.json')
    evidence = {'workflow_name': request['Name'], 'checked_at': datetime.now(timezone.utc).isoformat(),
                'explicit_workflow_instance_id': workflow_instance_id, 'queries': [], 'candidates': []}
    def call(operation, payload):
        query = {'operation': operation, 'payload': payload}
        evidence['queries'].append(query)
        result = api.call(operation, payload)
        query['request_id'] = result.get('RequestId')
        return result
    try:
        if workflow_instance_id is None:
            # Adhoc returns ManualFlow, which ListWorkflowInstances currently omits.
            # The task index includes it; names are only a candidate filter, never identity.
            page, ids, seen, last_batch = 1, set(), set(), None
            while True:
                response = call('ListTaskInstances', {'ProjectId': state['project_id'],
                    'ProjectEnv': 'Dev', 'Bizdate': debug.bizdate_millis(state['bizdate']),
                    'TaskName': state['task_name'], 'TaskType': 'PYODPS3', 'PageNumber': page, 'PageSize': 100})
                paging = response.get('PagingInfo') or {}; batch = paging.get('TaskInstances') or []
                for row in batch:
                    seen.add((row.get('Id'), row.get('RunNumber')))
                    if row.get('WorkflowInstanceId'):
                        ids.add(row['WorkflowInstanceId'])
                total = paging.get('TotalCount')
                if not batch or (isinstance(total, int) and len(seen) >= total) or (total is None and len(batch) < 100):
                    break
                if page >= 100 or batch == last_batch:
                    raise ValueError('恢复查询分页未前进，提交状态仍未知')
                last_batch = batch; page += 1
            matches = []
            for wid in sorted(ids):
                workflow = call('GetWorkflowInstance', {'Id': wid}).get('WorkflowInstance') or {}
                evidence['candidates'].append({k: workflow.get(k) for k in ('Id', 'Name', 'ProjectId', 'EnvType', 'Type', 'BizDate')})
                if (workflow.get('Name') == request['Name'] and workflow.get('Id') == wid
                        and str(workflow.get('ProjectId')) == str(state['project_id']) and workflow.get('EnvType') == 'Dev'
                        and workflow.get('BizDate') == request['BizDate']):
                    matches.append(wid)
            if len(matches) != 1:
                raise ValueError('提交结果未知，未找到唯一精确匹配的工作流；可核实已知工作流ID后 resume，不得自动重新提交')
            workflow_instance_id = matches[0]
        else:
            workflow = call('GetWorkflowInstance', {'Id': workflow_instance_id}).get('WorkflowInstance') or {}
            evidence['candidates'].append({k: workflow.get(k) for k in ('Id', 'Name', 'ProjectId', 'EnvType', 'Type', 'BizDate')})
            if (workflow.get('Id') != workflow_instance_id or workflow.get('Name') != request['Name']
                    or str(workflow.get('ProjectId')) != str(state['project_id']) or workflow.get('EnvType') != 'Dev'
                    or workflow.get('BizDate') != request['BizDate']):
                raise ValueError('恢复工作流的名称、项目或环境与冻结请求不匹配')
        probe = dict(manifest, workflow_instance_id=workflow_instance_id)
        instance_id = debug.discover_instance(api, probe)
        if not instance_id:
            raise ValueError('工作流任务实例暂未返回，保留未知状态并稍后 resume')
        item = call('GetTaskInstance', {'Id': instance_id}).get('TaskInstance') or {}
        content = (item.get('Script') or {}).get('Content')
        evidence.update(task_instance_id=instance_id, execution_code_sha256=debug.sha256(content) if isinstance(content, str) else None,
                        configuration=observe_configuration(item, request))
        if (item.get('WorkflowInstanceId') != workflow_instance_id or item.get('Id') != instance_id
                or item.get('TaskType') != 'PYODPS3' or item.get('Bizdate') != request['BizDate'] or not isinstance(content, str)
                or debug.sha256(content) != manifest['code_sha256']
                or evidence['configuration']['configuration_match'] is not True):
            raise ValueError('恢复实例代码或有效配置未能与本轮冻结请求核实一致')
        manifest.update(submission_status='submitted', workflow_instance_id=workflow_instance_id,
                        task_instance_id=instance_id, recovered=True, repair_state='waiting')
        manifest.pop('error', None)
        evidence['outcome'] = 'recovered'
    except (APIError, ValueError) as exc:
        evidence.update(outcome='unresolved', error=str(exc))
        raise
    finally:
        (folder / 'recovery').mkdir(exist_ok=True)
        path = folder / 'recovery' / (uuid.uuid4().hex + '.json')
        write_json(path, redacted_object(evidence))
        manifest['recovery_evidence'] = str(path.relative_to(folder))
        debug.save_manifest(folder, manifest)


def resume_session(api, root, workflow_instance_id=None, wait=True, poll_seconds=10, timeout=1800):
    root = Path(root)
    if not 1 <= poll_seconds <= 60 or timeout < 0:
        raise ValueError('轮询间隔须为1至60秒，等待时长不能为负')
    with session_lock(root):
        state = load_session(root)
        folder = attempt_dir(root, state['active_attempt']); manifest = read_json(folder / 'manifest.json')
        check_frozen(root, state, manifest)
        # A crash between attempt and session writes cannot regain a submission slot.
        state['submissions'] = max(state['submissions'], sum(
            read_json(attempt_dir(root, entry['attempt']) / 'manifest.json')['submission_status'] != 'not_started'
            for entry in state['attempts']))
        save_session(root, state)
        if manifest['submission_status'] in ('submitting', 'unknown'):
            recover_workflow(api, root, state, manifest, workflow_instance_id)
        elif manifest['submission_status'] != 'submitted':
            raise ValueError('此轮尚未成功提交或被明确拒绝，不能恢复运行')
        elif workflow_instance_id and workflow_instance_id != manifest['workflow_instance_id']:
            raise ValueError('不能将会话换绑到其他工作流')
        return collect_attempt(api, root, state, manifest, wait, poll_seconds, timeout)


def check_execution_evidence(folder, manifest):
    """An earlier success flag cannot replace the execution artifacts at export time."""
    required = ['executed.py', 'observation.json', manifest.get('log_file')]
    hashes = manifest.get('evidence_hashes') or {}
    for name in required:
        path = (folder / name).resolve() if name else None
        if (not path or not path.is_relative_to(folder.resolve()) or not path.is_file()
                or not hashes.get(name) or file_hash(path) != hashes[name]):
            manifest['evidence_error'] = '运行证据缺失或哈希改变：' + str(name)
            return False
    return True


def report_session(root, traceback_review=None, traceback_outcome='handled'):
    root = Path(root)
    if traceback_outcome not in ('handled', 'unresolved') or (traceback_outcome != 'handled' and not traceback_review):
        raise ValueError('traceback_outcome 必须与明确评审说明一起使用')
    with session_lock(root):
        state = load_session(root); check_frozen(root, state)
        manifests = []
        for entry in state['attempts']:
            manifest = read_json(attempt_dir(root, entry['attempt']) / 'manifest.json')
            check_frozen(root, state, manifest); manifests.append(manifest)
        if manifests and (manifests[-1].get('runtime_verified') or
                          manifests[-1].get('repair_state') == 'needs_traceback_review'):
            latest = manifests[-1]; folder = attempt_dir(root, latest['attempt'])
            if not check_execution_evidence(folder, latest):
                classify_attempt(folder, latest)
                update_session(root, state, latest)
        if traceback_review:
            if not str(traceback_review).strip() or not manifests or manifests[-1].get('repair_state') != 'needs_traceback_review':
                raise ValueError('只有运行成功但 traceback 待解释的活动轮次可记录该评审')
            manifests[-1]['traceback_outcome'] = traceback_outcome
            manifests[-1]['review_log_sha256'] = manifests[-1].get('log_sha256')
            classify_attempt(attempt_dir(root, manifests[-1]['attempt']), manifests[-1], traceback_review)
            update_session(root, state, manifests[-1])
        result = {k: state[k] for k in ('session_status', 'file_id', 'task_name', 'max_attempts', 'submissions', 'business_validation')}
        result.update(session_dir=str(root.resolve()), final_code=None, attempts=[],
                      frozen_runtime=redacted_object(read_json(root / 'runtime.json')),
                      runtime_evidence=str((root / 'runtime.json').resolve()),
                      baseline_code=str((root / 'snapshot.py').resolve()))
        for m in manifests:
            folder = attempt_dir(root, m['attempt'])
            result['attempts'].append({k: m.get(k) for k in ('attempt', 'submission_status', 'workflow_instance_id',
                'task_instance_id', 'run_number', 'remote_status', 'repair_state', 'runtime_verified',
                'code_sha256', 'execution_code_match', 'configuration_match', 'configuration_mismatches',
                'configuration_unavailable', 'started_time', 'finished_time', 'runtime_process_id',
                'log_state', 'log_cached', 'log_sha256', 'log_bytes', 'possibly_truncated', 'completeness',
                'log_recovery', 'traceback_review', 'traceback_outcome')})
            result['attempts'][-1].update(candidate=str((folder / 'candidate.py').resolve()),
                log=str((folder / m['log_file']).resolve()) if m.get('log_file') else None,
                actual_environment=(m.get('analysis') or {}).get('versions'),
                observed_configuration=m.get('observed_configuration'))
        if manifests:
            latest = manifests[-1]; source = attempt_dir(root, latest['attempt']) / 'candidate.py'
            filename = 'final.py' if latest.get('runtime_verified') else 'candidate_unverified.py'
            # Remove only the tool-owned counterpart; never leave an obsolete success artifact.
            (root / ('candidate_unverified.py' if latest.get('runtime_verified') else 'final.py')).unlink(missing_ok=True)
            (root / filename).write_bytes(source.read_bytes())
            result['final_code' if latest.get('runtime_verified') else 'unverified_code'] = str((root / filename).resolve())
            diff = ''.join(difflib.unified_diff((root / 'snapshot.py').read_text(encoding='utf-8').splitlines(True),
                source.read_text(encoding='utf-8').splitlines(True), fromfile='original.py', tofile=filename))
            (root / 'final.patch').write_text(diff, encoding='utf-8')
        write_json(root / 'report.json', redacted_object(result))
        lines = ['# PyODPS3 修复验证报告', '', '会话状态：' + state['session_status'],
                 '原节点未修改；业务核验：' + state['business_validation'], '',
                 '| 轮次 | 提交 | 远端状态 | 验证结论 |', '|---|---|---|---|']
        for m in manifests:
            lines.append('| %s | %s | %s | %s |' % (m['attempt'], m['submission_status'], m.get('remote_status'), m.get('repair_state')))
        lines.extend(['', '本地代码：' + str(result.get('final_code') or result.get('unverified_code') or '尚无候选'),
                      '每轮 candidate.py、changes.patch、reason.txt 和完整可取得日志均保存在 attempts 目录。'])
        (root / 'report.md').write_text(redact('\n'.join(lines)) + '\n', encoding='utf-8')
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    child = commands.add_parser('init', help='只读预检并创建本地会话，不运行原代码')
    selector = child.add_mutually_exclusive_group(required=True)
    selector.add_argument('--name'); selector.add_argument('--file-id', type=debug.positive_int)
    child.add_argument('--bizdate', required=True); child.add_argument('--param', action='append', default=[])
    child.add_argument('--max-attempts', type=debug.positive_int, default=3); child.add_argument('--output-dir')
    for option in ('resource-group-id', 'data-source', 'cu', 'image'):
        child.add_argument('--' + option)
    selector = child.add_mutually_exclusive_group()
    selector.add_argument('--log-run-dir'); selector.add_argument('--log-file')
    child.add_argument('--log-partial', action='store_true')
    for name in ('prepare', 'run', 'resume', 'report'):
        child = commands.add_parser(name); child.add_argument('--session-dir', required=True)
        if name == 'prepare':
            child.add_argument('--file', required=True); child.add_argument('--reason', required=True)
        elif name in ('run', 'resume'):
            child.add_argument('--no-wait', action='store_true')
            child.add_argument('--poll-seconds', type=int, default=10); child.add_argument('--timeout', type=int, default=1800)
            child.add_argument('--attempt' if name == 'run' else '--workflow-instance-id', type=debug.positive_int)
        else:
            child.add_argument('--traceback-review', help='记录本轮成功状态中 traceback 的判断依据；不可用于忽略失败')
            child.add_argument('--traceback-outcome', choices=['handled', 'unresolved'], default='handled',
                               help='handled 表示预期且已处理；unresolved 表示核实仍有代码问题，允许继续修复')
    args = parser.parse_args(argv)
    try:
        if args.command == 'init':
            overrides = {k: getattr(args, k) for k in ('resource_group_id', 'data_source', 'cu', 'image') if getattr(args, k) is not None}
            root = init_session(DataWorksAPI(), name=args.name, file_id=args.file_id, bizdate=args.bizdate,
                parameters=args.param, output_dir=args.output_dir, max_attempts=args.max_attempts,
                runtime_overrides=overrides, log_run_dir=args.log_run_dir, log_file=args.log_file, log_partial=args.log_partial)
            result = dict(load_session(root), session_dir=str(root.resolve()))
        elif args.command == 'prepare':
            result = prepare_candidate(args.session_dir, args.file, args.reason)
        elif args.command == 'report':
            result = report_session(args.session_dir, args.traceback_review, args.traceback_outcome)
        else:
            common = dict(wait=not args.no_wait, poll_seconds=args.poll_seconds, timeout=args.timeout)
            if args.command == 'run':
                result = run_attempt(DataWorksAPI(), args.session_dir, args.attempt, **common)
            else:
                result = resume_session(DataWorksAPI(), args.session_dir, args.workflow_instance_id, **common)
        print(json.dumps(redacted_object(result), ensure_ascii=False, indent=2), flush=True)
        if result.get('submission_status') in ('unknown', 'submitting', 'rejected') or result.get('status_error') or result.get('log_state') == 'error':
            return 5
        if result.get('wait_timed_out'): return 7
        if result.get('remote_status') == 'Failure': return 1
        if result.get('execution_code_match') is False or result.get('configuration_match') is False: return 6
        if result.get('repair_state') in ('needs_traceback_review', 'needs_diagnosis', 'partial_log', 'evidence_mismatch', 'execution_unverified'): return 8
        return 0
    except APIError as exc:
        print('[DataWorks API] ' + str(exc), file=sys.stderr); return 5
    except (ValueError, OSError, ImportError, KeyError) as exc:
        print('[错误] ' + redact(str(exc)), file=sys.stderr); return 3


if __name__ == '__main__':
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'): stream.reconfigure(encoding='utf-8')
    sys.exit(main())

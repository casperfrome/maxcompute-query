#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""DataWorks PyODPS3 保存态快照运行与诊断；只在明确授权后调用 run-saved。

不修改、提交或发布原节点，不本地执行任务代码，不自动重跑。
原始代码/参数/日志仅保存在本地诊断目录；控制台及诊断摘要脱敏。
"""
import argparse
import ast
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
import time
import uuid

import config
from pyodps3_log_utils import normalize_log, extract_tracebacks, render_log
from pyodps3_runtime import (sha256, redact, redacted_object, APIError, DataWorksAPI,
    bizdate_millis, build_parameters, write_json, new_bundle, load_manifest, save_manifest,
    resolve_resource, build_adhoc_request)


TERMINAL_STATES = {'Success', 'Failure'}
POLL_SECONDS = 10
WAIT_SECONDS = 1800
LOG_LIMIT_BYTES = 4 * 1024 * 1024
API_VERSIONS = ('2024-05-18', '2020-05-18')
WORKFLOW_INSTANCE_TYPES = ('Normal', 'Manual', 'SmokeTest', 'SupplementData', 'ManualWorkflow', 'TriggerWorkflow')


def scan_effects(source):
    """Evidence hints only: this is NOT a Python security sandbox/read-only validator."""
    result = {'table_constants': {}, 'write_calls': [], 'sql_write_lines': [],
              'dynamic_sql_lines': [], 'not_a_readonly_proof': True, 'syntax_error': None}
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        result['syntax_error'] = {'line': exc.lineno, 'message': str(exc.msg)}
        return result
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for target in node.targets:
                if isinstance(target, ast.Name) and re.search(r'TABLE|PROJECT|BIZDATE', target.id):
                    if isinstance(node.value.value, (str, int)):
                        result['table_constants'][target.id] = node.value.value
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            name = node.func.attr
            if name in ('create_table', 'delete_table', 'write_table', 'open_writer', 'create_partition', 'delete_partition'):
                result['write_calls'].append({'line': node.lineno, 'method': name})
            if name in ('execute_sql', 'run_sql'):
                if not node.args or not isinstance(node.args[0], ast.Constant):
                    result['dynamic_sql_lines'].append(node.lineno)
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if re.search(r'\b(?:INSERT\s+(?:OVERWRITE|INTO)|CREATE\s+TABLE|DROP\s+TABLE|TRUNCATE|DELETE\s+FROM)\b', node.value, re.I):
                result['sql_write_lines'].append(node.lineno)
    return result


def analyze_log(raw, partial=False):
    normalized = normalize_log(raw)
    versions = {}
    for key in ('python', 'pandas', 'pyodps'):
        match = re.search(r'\b' + key + r'\s*[=:]\s*([0-9][\w.+-]*)', normalized, re.I)
        versions[key] = match.group(1) if match else None
    path_python = re.search(r'/python([0-9]+\.[0-9]+)/site-packages/', normalized)
    if versions['python'] is None and path_python:
        versions['python'] = path_python.group(1)
    tracebacks, exceptions = extract_tracebacks(normalized, redact)
    diagnoses = []
    if 'groupby' in normalized and "unexpected keyword argument 'dropna'" in normalized:
        diagnoses.append('pandas_groupby_api')
    if '平台连接项目必须为' in normalized or ('project' in normalized.lower() and 'tst_mc_prod_dev' in normalized):
        diagnoses.append('connection_project')
    if 'NoneType' in normalized and ('_calc_count' in normalized or 'odps/readers.py' in normalized):
        diagnoses.append('pyodps_reader_step')
    if re.search(r'Got killed|OutOfMemory|MemoryError|oom[-_ ]kill', normalized, re.I):
        diagnoses.append('memory_limit_possible')
    if re.search(r'403|AccessDenied|Forbidden|无访问权限', normalized, re.I):
        diagnoses.append('permission_possible')
    if 'Instance Tunnel' in normalized or 'tunnel' in normalized.lower():
        diagnoses.append('inspect_tunnel_read_path')
    stage_lines = [redact(line) for line in normalized.splitlines() if re.search(
        r'开始|完成|发布|暂存|rows\s*=|行数|内存|运行环境|instance_id', line)]
    truncation_evidence = []
    if partial:
        truncation_evidence.append({'kind': 'capture_partial', 'detail': '采集方声明仅取得部分日志'})
    if len(raw.encode('utf-8')) >= LOG_LIMIT_BYTES:
        truncation_evidence.append({'kind': 'size_limit', 'limit_bytes': LOG_LIMIT_BYTES})
    for line in normalized.splitlines():
        if re.search(r'日志.{0,8}截断|log.{0,20}truncat|output.{0,20}truncat', line, re.I):
            truncation_evidence.append({'kind': 'platform_marker', 'detail': redact(line)})
        elif re.search(r'日志.{0,8}(?:已.{0,3}清理|已.{0,3}删除|已过期)|log.{0,24}(?:has expired|was deleted|has been (?:deleted|removed))', line, re.I):
            truncation_evidence.append({'kind': 'platform_retention', 'detail': redact(line)})
    return {'versions': versions, 'exceptions': exceptions, 'tracebacks': tracebacks,
            'user_code_lines': sorted(set(int(x) for x in re.findall(
                r'File\s+["\']<pyodps_user_code>["\'],\s*line\s+(\d+)', normalized))),
            'exit_codes': [int(x) for x in re.findall(r'Exit code of the Shell command\s+(-?\d+)', normalized)],
            'sql_instance_ids': list(dict.fromkeys(re.findall(r'\binstance_id\s*[=:]\s*([A-Za-z0-9_-]+)', normalized))),
            'row_counts': [int((m.group(1) or m.group(2)).replace(',', '')) for m in re.finditer(
                r'\brows\s*[=:]\s*([\d,]+)|(?:最终数据|候选数据|读取|上传)\s*([\d,]+)\s*行', normalized)],
            'memory_observations': [{'value': float(value), 'unit': unit} for value, unit in re.findall(
                r'(?:内存|memory)\s*[=:：]?\s*([0-9.]+)\s*(MiB|GiB|MB|GB)', normalized, re.I)],
            'stages': stage_lines, 'diagnoses': diagnoses,
            'capture_partial': partial,
            'possibly_truncated': bool(truncation_evidence),
            'completeness': 'partial' if truncation_evidence else 'unknown',
            'truncation_evidence': truncation_evidence,
            'log_recovery': ('unrecoverable_by_log_api' if any(e['kind'] in ('platform_marker', 'platform_retention')
                            for e in truncation_evidence) else 'unknown'),
            'log_bytes': len(raw.encode('utf-8')), 'business_validation': 'not_performed',
            'note': '日志是诊断证据；未出现成功输出不等于失败，出现成功文本也不等于远端终态或业务验收通过。'}


def inspect_saved(api, name=None, file_id=None, bizdate=None, overrides=None, output_dir=None):
    if bizdate is not None:
        bizdate_millis(bizdate)
    saved = api.saved_task(name=name, file_id=file_id)
    if saved.get('source') != 'saved' or int(saved.get('file_type') or 0) != 1221:
        raise ValueError('需要明确的 PyODPS3 保存态（FileType=1221），不能回退其他版本或类型')
    if saved.get('deleted_status') not in (None, 'NORMAL'):
        raise ValueError('保存态文件已删除或不可运行')
    source = saved['sql_text']
    if not source.strip() or sha256(source) != saved.get('code_sha256'):
        raise ValueError('保存态为空或代码哈希不一致')
    parameters = build_parameters(saved.get('node_configuration', {}).get('ParaValue'), overrides, bizdate) if bizdate else None
    bundle = new_bundle(output_dir)
    (bundle / 'snapshot.py').write_bytes(source.encode('utf-8'))
    write_json(bundle / 'snapshot.json', {k: v for k, v in saved.items() if k != 'sql_text'})
    write_json(bundle / 'inspection.json', redacted_object(scan_effects(source)))
    manifest = {'schema_version': 1, 'source': 'saved', 'file_id': saved['file_id'],
                'task_name': saved['task_name'], 'project_id': config.DATAWORKS_PROJECT_ID,
                'code_sha256': sha256(source), 'bizdate': bizdate, 'parameters': parameters,
                'submission_status': 'not_started', 'remote_status': None,
                'workflow_instance_id': None, 'task_instance_id': None,
                'execution_code_match': None, 'saved_execution_verified': False,
                'log_available': False, 'business_validation': 'not_performed'}
    save_manifest(bundle, manifest)
    return bundle


def submit_saved(api, bundle):
    bundle = Path(bundle)
    manifest = load_manifest(bundle)
    if manifest['submission_status'] != 'not_started':
        raise ValueError('此快照已经尝试运行，禁止重复提交；请使用 status/logs 核实已有运行')
    source = (bundle / 'snapshot.py').read_bytes().decode('utf-8')
    if sha256(source) != manifest['code_sha256']:
        raise ValueError('本地代码快照哈希改变，不能宣称原样执行保存态')
    saved = json.loads((bundle / 'snapshot.json').read_text(encoding='utf-8'))
    try:
        date_ms = bizdate_millis(manifest['bizdate'])
        if not saved.get('owner') or not saved.get('connection_name'):
            raise ValueError('保存配置缺少 Owner/ConnectionName，不能猜测运行配置')
        resource = resolve_resource(api, saved)
    except (ValueError, APIError) as exc:
        manifest.update(submission_status='blocked', error=redact(str(exc)))
        save_manifest(bundle, manifest)
        raise
    runtime = {'ResourceGroupId': resource['identifier']}
    for old, new in [('ImageId', 'Image'), ('Cu', 'Cu')]:
        value = saved.get('node_configuration', {}).get(old)
        if value is not None and value != '':
            runtime[new] = str(value)
    unique = uuid.uuid4().hex
    request = build_adhoc_request(project_id=manifest['project_id'], task_name=saved['task_name'],
        owner=saved['owner'], bizdate=manifest['bizdate'], parameters=manifest['parameters'],
        source=source, runtime_resource=runtime, data_source={'Name': saved['connection_name']}, unique_code=unique)
    write_json(bundle / 'request.json', request)
    write_json(bundle / 'resource_mapping.json', resource)
    manifest.update(submission_status='submitting', workflow_name=request['Name'])
    save_manifest(bundle, manifest)  # A crash after this point never causes an automatic re-submit.
    try:
        response = api.call('ExecuteAdhocWorkflowInstance', request)
        if not response.get('WorkflowInstanceId'):
            raise APIError('ExecuteAdhocWorkflowInstance', None, '响应没有 WorkflowInstanceId，提交结果不确定')
    except APIError as exc:
        manifest.update(submission_status='rejected' if exc.definite_rejection else 'unknown', error=str(exc))
        save_manifest(bundle, manifest)
        raise
    manifest.update(submission_status='submitted', workflow_instance_id=response['WorkflowInstanceId'],
                    submit_request_id=response.get('RequestId'))
    save_manifest(bundle, manifest)
    return manifest


def discover_instance(api, manifest):
    page, items = 1, []
    while True:
        response = api.call('ListTaskInstances', {'ProjectId': manifest['project_id'],
            'Bizdate': bizdate_millis(manifest['bizdate']), 'ProjectEnv': 'Dev',
            'WorkflowInstanceId': manifest['workflow_instance_id'], 'PageNumber': page, 'PageSize': 500})
        paging = response.get('PagingInfo') or {}
        batch = paging.get('TaskInstances') or []
        for item in batch:
            if item.get('WorkflowInstanceId') != manifest['workflow_instance_id']:
                raise ValueError('实例查询返回其他工作流，拒绝按名称猜测实例')
            items.append(item)
        if not batch or page * 500 >= (paging.get('TotalCount') or len(items)):
            break
        page += 1
    ids = {item['Id'] for item in items}
    if len(ids) > 1:
        raise ValueError('单任务工作流返回多个实例，需核实，不能选择最新一条')
    return ids.pop() if ids else None


def collect_log(api, bundle, manifest):
    run_number = manifest.get('run_number')
    if not isinstance(run_number, int) or run_number < 1:
        manifest.update(log_available=False, log_file=None, log_state='pending', log_cached=False)
        return
    filename = 'run-%s.log' % run_number
    def fetch():
        response = api.call('GetTaskInstanceLog', {'Id': manifest['task_instance_id'], 'RunNumber': run_number})
        return response.get('TaskInstanceLog'), response.get('RequestId')
    _collect_log_content(bundle, manifest, filename, fetch)


def _collect_log_content(bundle, manifest, filename, fetch):
    """Commit fetched evidence only after the backend verifies its run identity."""
    manifest.update(log_cached=False, log_state='pending')
    manifest.pop('log_error', None)
    path = Path(bundle) / filename
    manifest['log_available'] = path.is_file() and path.stat().st_size > 0
    manifest['log_file'] = filename if manifest['log_available'] else None
    raw = None
    try:
        raw, request_id = fetch()
        if raw is not None and not isinstance(raw, str):
            raise APIError('ReadInstanceLog', 'InvalidResponse', '日志响应不是字符串')
        manifest['log_request_id'] = request_id
    except APIError as exc:
        manifest.update(log_state='error', log_error=str(exc))
    manifest['log_checked_at'] = datetime.now(timezone.utc).isoformat()
    if raw:
        temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
        temporary.write_bytes(raw.encode('utf-8'))
        temporary.replace(path)
        manifest.update(log_available=True, log_file=filename, log_state='available',
                        log_fetched_at=manifest['log_checked_at'])
    else:
        manifest['log_cached'] = manifest['log_available']
    if manifest['log_available']:
        raw = path.read_bytes().decode('utf-8')
        analysis = analyze_log(raw)
        write_json(path.with_suffix('.analysis.json'), analysis)
        manifest.update(log_file=filename, log_sha256=sha256(raw), log_bytes=analysis['log_bytes'],
                        possibly_truncated=analysis['possibly_truncated'],
                        completeness=analysis['completeness'], truncation_evidence=analysis['truncation_evidence'],
                        log_recovery=analysis['log_recovery'])
    else:
        for key in ('log_sha256', 'log_bytes', 'possibly_truncated', 'log_fetched_at',
                    'completeness', 'truncation_evidence', 'log_recovery'):
            manifest.pop(key, None)


def task_instances(api, payload):
    """Paginate the official read API; never choose a run implicitly."""
    page = 1
    items = []
    while True:
        response = api.call('ListTaskInstances', dict(payload, PageNumber=page, PageSize=500))
        paging = response.get('PagingInfo') or {}
        batch = paging.get('TaskInstances') or []
        items.extend(batch)
        if not batch or len(items) >= (paging.get('TotalCount') or len(items)):
            return items
        page += 1


def list_instances(api, bizdate, name=None, task_id=None, project_env='Prod', workflow_instance_type=None):
    payload = {'ProjectId': config.DATAWORKS_PROJECT_ID, 'ProjectEnv': project_env,
               'Bizdate': bizdate_millis(bizdate), 'TaskType': 'PYODPS3', 'SortBy': 'StartedTime Desc'}
    if name:
        payload['TaskName'] = name
    if task_id:
        payload['TaskId'] = task_id
    if workflow_instance_type:
        if workflow_instance_type not in WORKFLOW_INSTANCE_TYPES:
            raise ValueError('无法识别 WorkflowInstanceType')
        payload['WorkflowInstanceType'] = workflow_instance_type
    items = task_instances(api, payload)
    fields = ('Id', 'TaskId', 'TaskName', 'TaskType', 'ProjectEnv', 'Bizdate', 'RunNumber',
              'Status', 'StartedTime', 'FinishedTime', 'WorkflowInstanceId', 'WorkflowInstanceType')
    return {'project_id': config.DATAWORKS_PROJECT_ID, 'project_env': project_env, 'query': payload,
            'bizdate': bizdate, 'instances': [{k: x.get(k) for k in fields} for x in items],
            'count': len(items), 'note': '按节点、开始时间及运行次数选定实例；查询不会运行节点。'}


def list_instance_histories(api, instance_id, project_env):
    if project_env not in ('Dev', 'Prod'):
        raise ValueError('旧 API 必须指定 --env Dev|Prod')
    payload = {'InstanceId': instance_id, 'ProjectEnv': project_env.upper()}
    response = api.call('ListInstanceHistory', payload, legacy=True)
    items = response.get('Instances') or []
    if any(item.get('InstanceId') != instance_id for item in items):
        raise ValueError('历史列表返回其他实例，拒绝按历史编号猜测')
    fields = ('InstanceId', 'InstanceHistoryId', 'NodeId', 'NodeName', 'Status',
              'BeginRunningTime', 'FinishTime', 'Bizdate', 'DagId')
    return {'api_version': '2020-05-18', 'query': payload,
            'histories': [{key: item.get(key) for key in fields} for item in items],
            'count': len(items), 'note': 'InstanceHistoryId 是旧版历史标识，不能转换为 RunNumber。'}


def _legacy_status(value):
    return {'SUCCESS': 'Success', 'FAILURE': 'Failure', 'RUNNING': 'Running',
            'NOT_RUN': 'NotRun', 'WAIT_TIME': 'WaitTime', 'WAIT_RESOURCE': 'WaitResource',
            'CHECKING': 'Checking', 'CHECKING_CONDITION': 'CheckingCondition'}.get(value, value)


def _legacy_instance(api, instance_id, project_env):
    item = api.call('GetInstance', {'InstanceId': instance_id,
                                  'ProjectEnv': project_env.upper()}, legacy=True).get('Data') or {}
    if item.get('InstanceId') != instance_id:
        raise APIError('GetInstance', 'InvalidIdentity', '返回实例 ID 与请求不一致')
    return item


def _legacy_fingerprint(item):
    started = item.get('BeginRunningTime')
    if isinstance(started, bool) or not isinstance(started, int) or started <= 0:
        return None
    # Status/FinishTime/ModifyTime may change during the SAME run; never use them as a run key.
    return {'instance_id': item['InstanceId'], 'begin_running_time': started}


def read_legacy_instance_logs(api, bundle, instance_id, instance_history_id, project_env,
                              wait=False, poll_seconds=POLL_SECONDS, timeout=WAIT_SECONDS):
    bundle = Path(bundle)
    manifest = load_manifest(bundle) if (bundle / 'manifest.json').exists() else {
        'schema_version': 1, 'source': 'existing_instance', 'project_id': config.DATAWORKS_PROJECT_ID,
        'task_instance_id': instance_id, 'submission_status': 'not_applicable',
        'remote_status': None, 'log_available': False, 'business_validation': 'not_performed'}
    instance_id = instance_id or manifest.get('task_instance_id')
    if not instance_id:
        raise ValueError('旧 API 读取必须提供任务实例 ID')
    selected = instance_history_id if instance_history_id is not None else manifest.get('instance_history_id')
    if selected is not None and (isinstance(selected, bool) or not isinstance(selected, int) or selected < 1):
        raise ValueError('InstanceHistoryId 必须为正整数')
    if manifest.get('selection_unverified') and manifest.get('log_file') and selected is None:
        raise ValueError('旧版当前日志缺少稳定开始时间，不能续读或等待；请使用新目录单次读取')
    if selected != manifest.get('instance_history_id'):
        manifest.update(remote_status=None, started_time=None, finished_time=None)
    manifest.update(api_version='2020-05-18', project_env=project_env, task_instance_id=instance_id,
                    instance_history_id=selected, run_number=None, log_run_number=None,
                    execution_code_match=None, execution_verified=False, saved_execution_verified=False,
                    instance_type_verification='unavailable_in_legacy_response',
                    wait_timed_out=False, status_cached=False)
    started = time.monotonic()
    while True:
        manifest.pop('status_error', None)
        manifest.pop('selection_unverified', None)
        fingerprint = manifest.get('legacy_run_fingerprint')
        filename = ('history-%s.log' % selected if selected is not None else
                    'legacy-current-%s.log' % (fingerprint['begin_running_time'] if fingerprint else 'unverified'))
        try:
            item = _legacy_instance(api, instance_id, project_env) if selected is None else {'InstanceId': instance_id}
            if selected is None:
                write_json(bundle / 'instance.json', item)
            observation = item
            if selected is not None:
                observation = {}
                try:
                    histories = list_instance_histories(api, instance_id, project_env)['histories']
                    matches = [x for x in histories if x.get('InstanceHistoryId') == selected]
                    if len(matches) == 1:
                        observation = matches[0]
                        item = observation
                except APIError as exc:
                    manifest['status_error'] = str(exc)
                if not observation:
                    manifest.setdefault('status_error', '未找到指定历史 ID 的唯一状态，不能套用当前状态')
                manifest['selection_verified'] = bool(observation)
            else:
                current = _legacy_fingerprint(item)
                if fingerprint and current != fingerprint:
                    raise APIError('GetInstance', 'InvalidSelection',
                                   '运行开始时间已变化，拒绝附加新运行日志；请使用新目录或指定历史 ID')
                fingerprint = fingerprint or current
                manifest['legacy_run_fingerprint'] = fingerprint
                manifest['selection_verified'] = fingerprint is not None
                if fingerprint:
                    filename = 'legacy-current-%s.log' % fingerprint['begin_running_time']
                else:
                    manifest['selection_unverified'] = '平台未提供稳定开始时间；仅单次读取，不能用于修复成功验收'
                    if wait:
                        raise ValueError('旧版当前日志缺少稳定开始时间，不能等待或跨轮续读')
            manifest.update(remote_status=_legacy_status(observation.get('Status')),
                            started_time=observation.get('BeginRunningTime'),
                            finished_time=observation.get('FinishTime'), status_cached=False,
                            task_name=item.get('NodeName'), task_id=item.get('NodeId'),
                            bizdate_millis=item.get('Bizdate'))
            write_json(bundle / 'observation.json', observation)
            def fetch():
                payload = {'InstanceId': instance_id, 'ProjectEnv': project_env.upper()}
                if selected is not None:
                    payload['InstanceHistoryId'] = selected
                response = api.call('GetInstanceLog', payload, legacy=True)
                if selected is None and fingerprint:
                    after = _legacy_instance(api, instance_id, project_env)
                    if _legacy_fingerprint(after) != fingerprint:
                        manifest['selection_verified'] = False
                        raise APIError('GetInstanceLog', 'InvalidSelection',
                                       '日志获取期间运行开始时间变化，未保存无法归属的日志')
                    manifest.update(remote_status=_legacy_status(after.get('Status')),
                                    finished_time=after.get('FinishTime'))
                    write_json(bundle / 'observation.json', after)
                return response.get('Data'), response.get('RequestId')
            _collect_log_content(bundle, manifest, filename, fetch)
        except APIError as exc:
            manifest.update(status_error=str(exc), status_cached=True, selection_verified=False)
            def failed_fetch():
                raise exc
            _collect_log_content(bundle, manifest, filename, failed_fetch)
        save_manifest(bundle, manifest)
        complete = manifest.get('log_state') == 'available' and (
            manifest.get('remote_status') in TERMINAL_STATES or selected is not None)
        if not wait or complete or manifest.get('log_state') == 'error':
            break
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            manifest['wait_timed_out'] = True
            save_manifest(bundle, manifest)
            break
        print('等待旧版实例 %s 日志，状态=%s' % (instance_id, manifest.get('remote_status')),
              file=sys.stderr, flush=True)
        time.sleep(min(poll_seconds, remaining))
    result = dict(manifest, run_dir=str(bundle.resolve()))
    if result.get('log_available'):
        result['analysis'] = analyze_log((bundle / result['log_file']).read_bytes().decode('utf-8'))
    return result


def read_instance_logs(api, bundle, instance_id=None, run_number=None, wait=False,
                       poll_seconds=POLL_SECONDS, timeout=WAIT_SECONDS, api_version=None,
                       instance_history_id=None, project_env=None):
    """Choose one explicit backend; persisted bundles never silently switch APIs."""
    bundle = Path(bundle)
    recorded = load_manifest(bundle) if (bundle / 'manifest.json').exists() else {}
    version = api_version or recorded.get('api_version') or API_VERSIONS[0]
    if version not in API_VERSIONS:
        raise ValueError('不支持的 API 版本')
    recorded_version = recorded.get('api_version', API_VERSIONS[0])
    if recorded and api_version and api_version != recorded_version:
        raise ValueError('诊断目录 API 版本已固定，请使用新目录读取其他版本')
    if instance_id and recorded.get('task_instance_id') not in (None, instance_id):
        raise ValueError('任务实例 ID 与诊断目录不一致')
    if project_env and recorded.get('project_env') not in (None, project_env):
        raise ValueError('环境与诊断目录不一致')
    if not 1 <= poll_seconds <= 60 or timeout < 0:
        raise ValueError('轮询间隔须为1至60秒，等待时长不能为负')
    if version == '2020-05-18':
        if run_number is not None:
            raise ValueError('旧 API 使用 InstanceHistoryId，不能使用 --run-number')
        env = project_env or recorded.get('project_env')
        if env not in ('Dev', 'Prod'):
            raise ValueError('旧 API 必须指定 --env Dev|Prod，或使用已记录环境的目录')
        if recorded.get('source') in ('saved', 'candidate'):
            raise ValueError('保存态/候选运行需使用新版 API 核对实际执行代码和工作流')
        return read_legacy_instance_logs(api, bundle, instance_id, instance_history_id,
                                        env, wait, poll_seconds, timeout)
    if instance_history_id is not None:
        raise ValueError('InstanceHistoryId 仅用于旧 API，不能当作 RunNumber')
    return _read_modern_instance_logs(api, bundle, instance_id, run_number, wait,
                                     poll_seconds, timeout, project_env)


def _read_modern_instance_logs(api, bundle, instance_id=None, run_number=None, wait=False,
                              poll_seconds=POLL_SECONDS, timeout=WAIT_SECONDS, project_env=None):
    """Attach to an existing run without saved code, resource mapping or submit.

    log_run_number is pinned across polls and continuation. remote_status refers
    to this selected run only; current_run_number describes the latest attempt.
    """
    if not 1 <= poll_seconds <= 60 or timeout < 0:
        raise ValueError('轮询间隔须为1至60秒，等待时长不能为负')
    bundle = Path(bundle)
    if (bundle / 'manifest.json').exists():
        manifest = load_manifest(bundle)
    else:
        manifest = {'schema_version': 1, 'source': 'existing_instance',
                    'project_id': config.DATAWORKS_PROJECT_ID, 'task_instance_id': instance_id,
                    'submission_status': 'not_applicable', 'remote_status': None,
                    'business_validation': 'not_performed', 'log_available': False}
    manifest['api_version'] = '2024-05-18'
    selected = run_number or manifest.get('log_run_number') or manifest.get('run_number')
    if selected is not None:
        manifest['log_run_number'] = selected
    if not manifest.get('task_instance_id'):
        if not manifest.get('workflow_instance_id'):
            raise ValueError('无任务实例或工作流实例 ID，不能通过重新运行来读取日志')
    manifest.update(wait_timed_out=False, status_cached=False)
    manifest.pop('status_error', None)
    started = time.monotonic()
    while True:
        try:
            if not manifest.get('task_instance_id'):
                manifest['task_instance_id'] = discover_instance(api, manifest)
            if manifest.get('task_instance_id'):
                item = (api.call('GetTaskInstance', {'Id': manifest['task_instance_id']}).get('TaskInstance') or {})
                if item.get('Id') != manifest['task_instance_id'] or item.get('ProjectId') != manifest['project_id']:
                    raise ValueError('任务实例 ID / 项目与请求不一致')
                if item.get('TaskType') != 'PYODPS3' or item.get('ProjectEnv') not in ('Dev', 'Prod'):
                    raise ValueError('任务实例不是可识别环境下的 PyODPS3 节点')
                if project_env and item.get('ProjectEnv') != project_env:
                    raise ValueError('任务实例环境与指定环境不一致')
                if manifest.get('source') in ('saved', 'candidate') and (
                        item.get('ProjectEnv') != 'Dev' or item.get('WorkflowInstanceId') != manifest.get('workflow_instance_id')):
                    raise ValueError('任务实例与保存态运行的工作流 / 环境不一致')
                current_run = item.get('RunNumber')
                if isinstance(current_run, bool) or not isinstance(current_run, int) or current_run < 1:
                    manifest.update(remote_status=item.get('Status'), log_available=False,
                                    log_state='pending', log_cached=False)
                else:
                    selected = selected or current_run
                    if selected > current_run:
                        raise ValueError('指定运行次数大于实例当前运行次数')
                    manifest.update(run_number=selected, log_run_number=selected,
                                    current_run_number=current_run, task_name=item.get('TaskName'),
                                    task_id=item.get('TaskId'), project_env=item['ProjectEnv'],
                                    workflow_instance_id=item.get('WorkflowInstanceId'),
                                    bizdate_millis=item.get('Bizdate'), status_cached=False)
                    manifest.pop('status_error', None)
                    observation = item
                    if selected != current_run:
                        observation = {}
                        if item.get('Bizdate') is not None:
                            try:
                                history = task_instances(api, {'ProjectId': manifest['project_id'],
                                    'ProjectEnv': item['ProjectEnv'], 'Bizdate': item['Bizdate'], 'Id': item['Id']})
                                matches = [x for x in history if x.get('Id') == item['Id'] and x.get('RunNumber') == selected]
                                if len(matches) == 1:
                                    observation = matches[0]
                            except APIError as exc:
                                manifest['status_error'] = str(exc)
                        if not observation:
                            manifest.setdefault('status_error', '平台未返回指定历史运行的状态，不能套用最新一次状态')
                    manifest.update(remote_status=observation.get('Status'),
                                    started_time=observation.get('StartedTime'), finished_time=observation.get('FinishedTime'))
                    write_json(bundle / 'instance.json', item)
                    write_json(bundle / 'observation.json', observation)
                    manifest['selection_verified'] = bool(observation)
                    # Old saved bundles retain their snapshot verification only for the same attempt.
                    if manifest.get('source') in ('saved', 'candidate'):
                        content = (observation.get('Script') or {}).get('Content')
                        digest = sha256(content) if isinstance(content, str) else None
                        manifest.update(execution_code_sha256=digest,
                                        execution_code_match=(digest == manifest.get('code_sha256')) if digest else None,
                                        trigger_recurrence=observation.get('TriggerRecurrence'),
                                        trigger_type=observation.get('TriggerType'),
                                        runtime_process_id=(observation.get('Runtime') or {}).get('ProcessId'))
                        if isinstance(content, str):
                            (bundle / 'executed.py').write_bytes(content.encode('utf-8'))
                        manifest['observed_configuration'] = {
                            'source': 'GetTaskInstance' if selected == current_run else 'ListTaskInstances',
                            'script_parameters': (observation.get('Script') or {}).get('Parameters'),
                            'data_source': observation.get('DataSource'),
                            'runtime_resource': observation.get('RuntimeResource'),
                            'owner': observation.get('Owner'), 'task_timeout': observation.get('Timeout')}
                        # TriggerRecurrence is documented as effective only for Scheduler.
                        # Real adhoc instances return Manual/Manual; require process/timing evidence.
                        manual_execution = (observation.get('TriggerType') == 'Manual' and
                            observation.get('TriggerRecurrence') in (None, 'Manual', 'Normal') and
                            bool(manifest['runtime_process_id']) and bool(manifest['started_time']) and
                            bool(manifest['finished_time']))
                        manifest['execution_verified'] = (
                            manifest['remote_status'] == 'Success' and manifest['execution_code_match'] is True
                            and (observation.get('TriggerRecurrence') == 'Normal' or manual_execution))
                        manifest['saved_execution_verified'] = (
                            manifest.get('source') == 'saved' and manifest['execution_verified'])
                        manifest['runtime_resource'] = observation.get('RuntimeResource')
                    collect_log(api, bundle, manifest)
            else:
                manifest.update(log_state='pending', log_cached=False, log_available=False)
        except APIError as exc:
            # A previously verified bundle remains readable even if metadata refresh fails.
            same_attempt = selected is not None and selected == manifest.get('run_number')
            path = bundle / ('run-%s.log' % selected)
            available = same_attempt and path.is_file() and path.stat().st_size > 0
            manifest.update(status_error=str(exc), status_cached=same_attempt,
                            log_error=str(exc), log_state='error', log_cached=bool(available),
                            log_available=bool(available), log_file=path.name if available else None)
            if not same_attempt:
                manifest.update(remote_status=None, run_number=selected, started_time=None, finished_time=None)
        save_manifest(bundle, manifest)
        with (bundle / 'status_history.jsonl').open('a', encoding='utf-8') as handle:
            handle.write(json.dumps({'observed_at': datetime.now(timezone.utc).isoformat(),
                'status': manifest.get('remote_status'), 'task_instance_id': manifest.get('task_instance_id'),
                'run_number': manifest.get('run_number')}) + '\n')
        # No retry loop for denied or invalid requests. Empty terminal logs may arrive late.
        complete = manifest.get('log_state') == 'available' and (
            manifest.get('remote_status') in TERMINAL_STATES or
            selected != manifest.get('current_run_number'))
        if complete and manifest.get('source') == 'candidate':
            terminal_analysis = analyze_log((bundle / manifest['log_file']).read_bytes().decode('utf-8'))
            # A nonempty startup prefix may arrive before the terminal console output.
            complete = bool(terminal_analysis['exit_codes'] or terminal_analysis['possibly_truncated'] or
                            (manifest.get('remote_status') == 'Failure' and terminal_analysis['exceptions']))
        if not wait or complete or manifest.get('log_state') == 'error':
            break
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            manifest['wait_timed_out'] = True
            save_manifest(bundle, manifest)
            break
        print('等待实例 %s 第 %s 次日志，状态=%s，日志=%s' % (
            manifest.get('task_instance_id'), selected, manifest.get('remote_status'), manifest.get('log_state')),
            file=sys.stderr, flush=True)
        time.sleep(min(poll_seconds, remaining))
    result = dict(manifest, run_dir=str(bundle.resolve()))
    if result.get('log_available'):
        raw = (bundle / result['log_file']).read_bytes().decode('utf-8')
        result['analysis'] = analyze_log(raw)
    return result


def collect_run(api, bundle, wait=False, poll_seconds=POLL_SECONDS, timeout=WAIT_SECONDS):
    """Compatibility entry point: one fixed run, including delayed terminal logs."""
    return read_instance_logs(api, bundle, wait=wait, poll_seconds=poll_seconds, timeout=timeout)


def positive_int(value):
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError('必须为正整数') from None
    if number < 1:
        raise argparse.ArgumentTypeError('必须为正整数')
    return number


def add_log_display(child):
    child.add_argument('--format', choices=('json', 'text', 'traceback'), default='json',
                       help='json=摘要；text=完整脱敏控制台；traceback=完整异常链')
    child.add_argument('--tail', type=positive_int, help='仅 text 展示最后 N 行，不裁剪原始日志')


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('inspect', 'run-saved'):
        child = commands.add_parser(name)
        child.add_argument('name', nargs='?')
        child.add_argument('--file-id', type=int)
        child.add_argument('--bizdate', required=name == 'run-saved')
        child.add_argument('--param', action='append', default=[])
        child.add_argument('--output-dir', help='诊断根目录；每次自动新建独立子目录')
    for name in ('status', 'logs'):
        child = commands.add_parser(name)
        if name == 'status':
            child.add_argument('--run-dir', required=True)
        else:
            selector = child.add_mutually_exclusive_group(required=True)
            selector.add_argument('--run-dir', help='已有诊断子目录，支持原保存态运行目录')
            selector.add_argument('--instance-id', type=positive_int, help='DataWorks 任务实例 ID，不是文件 ID 或页面运行 ID')
            child.add_argument('--run-number', type=positive_int, help='运行次数；默认固定到首次读取时的次数')
            child.add_argument('--api-version', choices=API_VERSIONS,
                               help='默认新版；续读目录时沿用已记录版本，禁止自动回退')
            child.add_argument('--instance-history-id', type=positive_int,
                               help='旧版历史 ID，来自 histories；不能与 RunNumber 混用')
            child.add_argument('--env', choices=('Dev', 'Prod'),
                               help='旧版必填（续读可沿用目录）；新版可用于核对环境')
            child.add_argument('--output-dir', help='按实例 ID 新建诊断目录时的输出根目录')
            add_log_display(child)
        child.add_argument('--wait', action='store_true')
        child.add_argument('--poll-seconds', type=int, default=POLL_SECONDS)
        child.add_argument('--timeout', type=int, default=WAIT_SECONDS)
    child = commands.add_parser('instances', help='只读列出 PyODPS3 运维实例，不自动选择或运行')
    selector = child.add_mutually_exclusive_group(required=True)
    selector.add_argument('--name', help='节点名称，平台支持模糊查询')
    selector.add_argument('--task-id', type=positive_int, help='任务 ID（不是文件 ID）')
    child.add_argument('--bizdate', required=True, help='业务日期 YYYYMMDD，Asia/Shanghai')
    child.add_argument('--env', choices=('Dev', 'Prod'), default='Prod')
    child.add_argument('--workflow-instance-type', choices=WORKFLOW_INSTANCE_TYPES)
    child = commands.add_parser('histories', help='列出旧版实例历史 ID，不运行节点')
    child.add_argument('--instance-id', type=positive_int, required=True)
    child.add_argument('--env', choices=('Dev', 'Prod'), required=True)
    child = commands.add_parser('analyze-log')
    selector = child.add_mutually_exclusive_group(required=True)
    selector.add_argument('--file', help='UTF-8 控制台日志文件')
    selector.add_argument('--stdin', action='store_true', help='从标准输入读取 UTF-8 日志，不连接 DataWorks')
    child.add_argument('--partial', action='store_true', help='页面仅采集了局部日志，显式标记不完整')
    child.add_argument('--output-dir')
    add_log_display(child)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, 'tail', None) is not None and args.format != 'text':
        parser.error('--tail 仅用于 --format text')
    if args.command == 'logs' and args.run_dir and args.output_dir:
        parser.error('--output-dir 仅用于 --instance-id；已有 --run-dir 原地续读')
    raw = None
    try:
        if args.command == 'analyze-log':
            if args.stdin:
                source = sys.stdin.buffer.read() if hasattr(sys.stdin, 'buffer') else sys.stdin.read().encode('utf-8')
            else:
                source = Path(args.file).read_bytes()
            raw = source.decode('utf-8-sig')
            result = analyze_log(raw, partial=args.partial)
            if args.output_dir:
                bundle = new_bundle(args.output_dir)
                (bundle / 'input.log').write_bytes(source)
                write_json(bundle / 'analysis.json', result)
                result['run_dir'] = str(bundle)
        elif args.command == 'instances':
            result = list_instances(DataWorksAPI(), args.bizdate, args.name, args.task_id,
                                    args.env, args.workflow_instance_type)
        elif args.command == 'histories':
            result = list_instance_histories(DataWorksAPI(), args.instance_id, args.env)
        elif args.command == 'logs':
            if not 1 <= args.poll_seconds <= 60 or args.timeout < 0:
                raise ValueError('轮询间隔须为1至60秒，等待时长不能为负')
            bundle = Path(args.run_dir) if args.run_dir else new_bundle(args.output_dir)
            print('诊断目录：' + str(bundle.resolve()), file=sys.stderr, flush=True)
            result = read_instance_logs(DataWorksAPI(), bundle, args.instance_id, args.run_number,
                                       args.wait, args.poll_seconds, args.timeout, args.api_version,
                                       args.instance_history_id, args.env)
            if result.get('log_available'):
                raw = (bundle / result['log_file']).read_bytes().decode('utf-8')
        elif args.command in ('inspect', 'run-saved'):
            api = DataWorksAPI()
            bundle = inspect_saved(api, args.name, args.file_id, args.bizdate, args.param, args.output_dir)
            print('诊断目录：' + str(bundle), flush=True)
            if args.command == 'run-saved':
                result = submit_saved(api, bundle)
            else:
                result = load_manifest(bundle)
                result['inspection'] = json.loads((bundle / 'inspection.json').read_text(encoding='utf-8'))
                result['saved_configuration'] = json.loads((bundle / 'snapshot.json').read_text(encoding='utf-8'))
            result['run_dir'] = str(bundle)
        else:
            result = collect_run(DataWorksAPI(), args.run_dir, args.wait, args.poll_seconds, args.timeout)
        if getattr(args, 'format', 'json') == 'json':
            print(json.dumps(redacted_object(result), ensure_ascii=False, indent=2), flush=True)
        else:
            analysis = result if args.command == 'analyze-log' else result.get('analysis', {})
            metadata = {k: result[k] for k in ('task_instance_id', 'run_number', 'remote_status',
                'api_version', 'instance_history_id', 'selection_verified', 'selection_unverified',
                'log_state', 'log_cached', 'status_cached', 'status_error', 'log_error', 'run_dir') if k in result}
            metadata['possibly_truncated'] = analysis.get('possibly_truncated', False)
            metadata['completeness'] = analysis.get('completeness', 'unknown')
            if args.tail:
                metadata['display_tail_lines'] = args.tail
            print(json.dumps(redacted_object(metadata), ensure_ascii=False), file=sys.stderr, flush=True)
            if raw is not None:
                print(render_log(raw, analysis, args.format, redact, args.tail), flush=True)
            else:
                print('日志尚未返回。' if result.get('log_state') == 'pending' else '日志获取失败；详情见元信息。', flush=True)
        if args.command in ('logs', 'status') and (result.get('log_state') == 'error' or result.get('status_error')):
            return 5
        if result.get('remote_status') == 'Failure':
            return 1
        if result.get('execution_code_match') is False:
            return 6
        if result.get('wait_timed_out'):
            return 7
        return 0
    except APIError as exc:
        print('[DataWorks API] ' + str(exc), file=sys.stderr)
        return 5
    except (ValueError, OSError, ImportError) as exc:
        print('[错误] ' + redact(str(exc)), file=sys.stderr)
        return 3


if __name__ == '__main__':
    # PowerShell pipes are often not UTF-8 by default; keep this CLI deterministic.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    sys.exit(main())

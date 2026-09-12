#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""DataWorks PyODPS3 保存态快照运行与诊断；只在明确授权后调用 run-saved。

不修改、提交或发布原节点，不本地执行任务代码，不自动重跑。
原始代码/参数/日志仅保存在本地诊断目录；控制台及诊断摘要脱敏。
"""
import argparse
import ast
from datetime import datetime, timedelta, timezone
import hashlib
import html
import json
import os
from pathlib import Path
import re
import shlex
import sys
import time
import uuid

import config


TERMINAL_STATES = {'Success', 'Failure'}
POLL_SECONDS = 10
WAIT_SECONDS = 1800
LOG_LIMIT_BYTES = 4 * 1024 * 1024


def sha256(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def redact(text):
    """Display-only redaction; original local artifacts remain unchanged."""
    text = str(text)
    for key, value in os.environ.items():
        if value and len(value) >= 4 and (key.startswith('SKYNET_') or re.search(
                r'SECRET|TOKEN|PASSWORD|ACCESS_KEY|ACCESS_ID|ODPS_SECRET', key, re.I)):
            text = text.replace(value, '[REDACTED]')
    text = re.sub(r'(?im)(\bSKYNET_\w+\s*[=:]).*$', r'\1[REDACTED]', text)
    text = re.sub(r'(?i)([?&](?:Signature|OSSAccessKeyId|AccessKeyId|SecurityToken|X-Amz-Signature|X-Amz-Credential|X-Amz-Security-Token)=)[^&\s]+',
                  r'\1[REDACTED]', text)
    text = re.sub(r'''(?i)(["']?(?:password|access_key_secret|access_key_id|token|secret|authorization)["']?\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,;}]+)''',
                  r'\1[REDACTED]', text)
    return text


def redacted_object(value):
    if isinstance(value, dict):
        return {k: ('[REDACTED]' if re.search(r'SKYNET_|secret|token|password|access_key|authorization', k, re.I)
                    else redacted_object(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redacted_object(v) for v in value]
    return redact(value) if isinstance(value, str) else value


class APIError(RuntimeError):
    def __init__(self, operation, code=None, message='', request_id=None):
        self.operation, self.code, self.request_id = operation, code, request_id
        # Do not render SDK response dumps / EncodedDiagnosticMessage.
        actions = re.findall(r'dataworks:[A-Za-z]+', str(message))
        self.permission_action = actions[0] if actions else None
        safe_message = ('缺少权限 ' + self.permission_action if self.permission_action else redact(message))
        super().__init__('%s failed code=%s request_id=%s: %s' %
                         (operation, code, request_id, safe_message))

    @property
    def definite_rejection(self):
        return str(self.code).startswith(('400', '401', '403', '404', 'Invalid', 'Forbidden', 'AccessDenied'))


class DataWorksAPI:
    """Lazy clients: uses the existing config, never constructs an ODPS connection."""
    def __init__(self):
        self._legacy = self._modern = None

    @property
    def legacy(self):
        if self._legacy is None:
            import fetch_task_sql
            self._legacy = fetch_task_sql.create_client()
        return self._legacy

    @property
    def modern(self):
        if self._modern is None:
            from alibabacloud_dataworks_public20240518.client import Client
            from alibabacloud_tea_openapi.models import Config
            config.require_credentials()
            self._modern = Client(Config(access_key_id=config.ACCESS_ID, access_key_secret=config.SECRET,
                                         endpoint=config.DATAWORKS_ENDPOINT))
        return self._modern

    def saved_task(self, name=None, file_id=None):
        import fetch_task_sql
        try:
            return fetch_task_sql.fetch_saved_task(name=name, file_id=file_id, client=self.legacy)
        except (ValueError, fetch_task_sql.TaskSqlNotFound):
            raise
        except Exception as exc:
            raise self.error('GetFile', exc) from None

    @staticmethod
    def error(operation, exc):
        data = getattr(exc, 'data', None) or {}
        data = data if isinstance(data, dict) else {}
        details = getattr(exc, 'access_denied_detail', None) or data.get('AccessDeniedDetail') or {}
        details = details if isinstance(details, dict) else {}
        action = getattr(exc, 'permission_action', None) or details.get('AuthAction')
        message = action or getattr(exc, 'message', None) or type(exc).__name__
        return APIError(getattr(exc, 'operation', None) or operation, getattr(exc, 'code', None) or data.get('Code'),
                        message,
                        data.get('RequestId') or getattr(exc, 'request_id', None))

    def call(self, operation, payload, legacy=False):
        from alibabacloud_tea_util.models import RuntimeOptions
        if legacy:
            from alibabacloud_dataworks_public20200518 import models
        else:
            from alibabacloud_dataworks_public20240518 import models
        method = re.sub(r'(?<!^)(?=[A-Z])', '_', operation).lower() + '_with_options'
        try:
            request = getattr(models, operation + 'Request')().from_map(payload)
            client = self.legacy if legacy else self.modern
            # In particular, a timed-out submit MUST NOT be transparently resubmitted.
            response = getattr(client, method)(request, RuntimeOptions(
                autoretry=False, connect_timeout=20000, read_timeout=45000))
            body = response.body.to_map()
            if body.get('Success') is False:
                raise APIError(operation, body.get('ErrorCode') or body.get('HttpStatusCode'),
                               body.get('ErrorMessage', ''), body.get('RequestId'))
            return body
        except APIError:
            raise
        except Exception as exc:
            raise self.error(operation, exc) from None


def bizdate_millis(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{8}', value):
        raise ValueError('bizdate 必须是有效的 YYYYMMDD，不能包含占位符')
    try:
        date = datetime.strptime(value, '%Y%m%d').replace(tzinfo=timezone(timedelta(hours=8)))
    except ValueError:
        raise ValueError('bizdate 日期非法：' + value) from None
    return int(date.timestamp() * 1000)


def build_parameters(saved, overrides, bizdate):
    bizdate_millis(bizdate)
    parameters = {}
    for pair in shlex.split(saved or '') + list(overrides or []):
        key, sep, value = pair.partition('=')
        if not sep or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key):
            raise ValueError('参数必须为 KEY=VALUE：' + redact(pair))
        parameters[key] = value
    explicit = dict(pair.split('=', 1) for pair in (overrides or []))
    if 'bizdate' in explicit and explicit['bizdate'] != bizdate:
        raise ValueError('--param bizdate 与 --bizdate 不一致')
    parameters['bizdate'] = bizdate
    for key, value in parameters.items():
        if value in ('$bizdate', '${bizdate}'):
            parameters[key] = bizdate
        elif '$' in value or any(c.isspace() for c in value):
            raise ValueError('参数 %s 含未解析表达式或空白，请提供明确的单值 --param' % key)
    return ' '.join(k + '=' + v for k, v in parameters.items())


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


def analyze_log(raw):
    # Decode a copy, keep the raw artifact byte-for-byte. Markdown escapes often surround tracebacks.
    normalized = html.unescape(raw)
    normalized = re.sub(r'\\([_<>])', r'\1', normalized)
    if '\n' not in normalized and '\\n' in normalized:
        normalized = normalized.replace('\\r\\n', '\n').replace('\\n', '\n')
    versions = {}
    for key in ('python', 'pandas', 'pyodps'):
        match = re.search(r'\b' + key + r'\s*[=:]\s*([0-9][\w.+-]*)', normalized, re.I)
        versions[key] = match.group(1) if match else None
    path_python = re.search(r'/python([0-9]+\.[0-9]+)/site-packages/', normalized)
    if versions['python'] is None and path_python:
        versions['python'] = path_python.group(1)
    exceptions = [{'type': m.group(1), 'message': redact(m.group(2))} for m in re.finditer(
        r'(?m)^\s*(?:[\w.]+\.)?([A-Za-z_]\w*(?:Error|Exception)|KeyboardInterrupt|SystemExit):\s*(.*)$', normalized)]
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
    return {'versions': versions, 'exceptions': exceptions,
            'user_code_lines': sorted(set(int(x) for x in re.findall(
                r'File\s+["\']<pyodps_user_code>["\'],\s*line\s+(\d+)', normalized))),
            'exit_codes': [int(x) for x in re.findall(r'Exit code of the Shell command\s+(-?\d+)', normalized)],
            'sql_instance_ids': list(dict.fromkeys(re.findall(r'\binstance_id\s*[=:]\s*([A-Za-z0-9_-]+)', normalized))),
            'row_counts': [int((m.group(1) or m.group(2)).replace(',', '')) for m in re.finditer(
                r'\brows\s*[=:]\s*([\d,]+)|(?:最终数据|候选数据|读取|上传)\s*([\d,]+)\s*行', normalized)],
            'memory_observations': [{'value': float(value), 'unit': unit} for value, unit in re.findall(
                r'(?:内存|memory)\s*[=:：]?\s*([0-9.]+)\s*(MiB|GiB|MB|GB)', normalized, re.I)],
            'stages': stage_lines, 'diagnoses': diagnoses,
            'possibly_truncated': len(raw.encode('utf-8')) >= LOG_LIMIT_BYTES or bool(re.search(
                r'日志.{0,8}截断|log.{0,20}truncat|output.{0,20}truncat', normalized, re.I)),
            'log_bytes': len(raw.encode('utf-8')), 'business_validation': 'not_performed',
            'note': '日志是诊断证据；未出现成功输出不等于失败，出现成功文本也不等于远端终态或业务验收通过。'}


def write_json(path, value):
    # Atomic replacement avoids half-written manifests after interruption.
    path = Path(path)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def new_bundle(output_dir=None):
    root = Path(output_dir) if output_dir else Path('outputs/pyodps3_debug')
    path = root / (datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:12])
    path.mkdir(parents=True, exist_ok=False)
    return path.resolve()


def load_manifest(bundle):
    return json.loads((Path(bundle) / 'manifest.json').read_text(encoding='utf-8'))


def save_manifest(bundle, manifest):
    manifest['updated_at'] = datetime.now(timezone.utc).isoformat()
    write_json(Path(bundle) / 'manifest.json', manifest)


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


def resolve_resource(api, saved):
    numeric = saved.get('node_configuration', {}).get('ResourceGroupId')
    if numeric is None or str(numeric).strip() == '':
        raise ValueError('保存配置缺少 ResourceGroupId；不能猜测资源组')
    if not str(numeric).isdigit():
        # Already an official identifier in the saved configuration.
        return {'saved_id': numeric, 'identifier': str(numeric), 'evidence': 'GetFile.NodeConfiguration.ResourceGroupId'}
    matches = []
    for category in ('default', 'single'):
        response = api.call('ListResourceGroups', {'ResourceGroupType': 1, 'BizExtKey': category}, legacy=True)
        matches.extend(x for x in response.get('Data', []) if str(x.get('Id')) == str(numeric))
        if matches:
            break
    identifiers = {str(x['Identifier']) for x in matches if x.get('Identifier') and str(x.get('Status')) == '0'}
    if len(identifiers) != 1:
        raise ValueError('无法将保存态资源组 %s 唯一映射到正常资源组标识；不能直接用数字ID提交' % numeric)
    return {'saved_id': numeric, 'identifier': identifiers.pop(), 'evidence': 'ListResourceGroups.Id/Identifier',
            'request_id': response.get('RequestId')}


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
    request = {'ProjectId': manifest['project_id'], 'EnvType': 'Dev', 'BizDate': date_ms,
               'Name': 'pyodps3_debug_' + unique, 'Owner': saved['owner'],
               'Tasks': [{'Name': saved['task_name'], 'Type': 'PYODPS3', 'Owner': saved['owner'],
                          'ClientUniqueCode': unique,
                          'Script': {'Content': source, 'Parameters': manifest['parameters']},
                          'DataSource': {'Name': saved['connection_name']}, 'RuntimeResource': runtime}]}
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
    if run_number is None:
        manifest['log_available'] = False
        return
    try:
        response = api.call('GetTaskInstanceLog', {'Id': manifest['task_instance_id'], 'RunNumber': run_number})
    except APIError as exc:
        manifest.update(log_available=False, log_error=str(exc))
        return
    raw = response.get('TaskInstanceLog')
    manifest['log_available'] = isinstance(raw, str) and bool(raw)
    if manifest['log_available']:
        filename = 'run-%s.log' % int(run_number)
        (Path(bundle) / filename).write_bytes(raw.encode('utf-8'))
        analysis = analyze_log(raw)
        write_json(Path(bundle) / ('run-%s.analysis.json' % int(run_number)), analysis)
        manifest.update(log_file=filename, log_sha256=sha256(raw), log_bytes=analysis['log_bytes'],
                        possibly_truncated=analysis['possibly_truncated'])
        manifest.pop('log_error', None)


def collect_run(api, bundle, wait=False, poll_seconds=POLL_SECONDS, timeout=WAIT_SECONDS):
    if not 1 <= poll_seconds <= 60 or timeout < 0:
        raise ValueError('轮询间隔须为1至60秒，等待时长不能为负')
    bundle = Path(bundle)
    manifest = load_manifest(bundle)
    if not manifest.get('workflow_instance_id'):
        raise ValueError('尚无工作流实例ID；不得因提交状态不确定而重新提交')
    started = time.monotonic()
    manifest['wait_timed_out'] = False
    while True:
        if not manifest.get('task_instance_id'):
            manifest['task_instance_id'] = discover_instance(api, manifest)
        if manifest.get('task_instance_id'):
            response = api.call('GetTaskInstance', {'Id': manifest['task_instance_id']})
            item = response.get('TaskInstance') or {}
            if item.get('Id') != manifest['task_instance_id'] or item.get('WorkflowInstanceId') != manifest['workflow_instance_id']:
                raise ValueError('任务实例身份与本次工作流不一致')
            if item.get('ProjectId') != manifest['project_id'] or item.get('ProjectEnv') != 'Dev' or item.get('TaskType') != 'PYODPS3':
                raise ValueError('任务实例的项目/环境/类型不一致')
            write_json(bundle / 'instance.json', item)
            content = (item.get('Script') or {}).get('Content')
            digest = sha256(content) if isinstance(content, str) else None
            if isinstance(content, str):
                (bundle / 'executed.py').write_bytes(content.encode('utf-8'))
            manifest.update(remote_status=item.get('Status'), run_number=item.get('RunNumber'),
                execution_code_sha256=digest, execution_code_match=(digest == manifest['code_sha256']) if digest else None,
                runtime_resource=item.get('RuntimeResource'), started_time=item.get('StartedTime'),
                finished_time=item.get('FinishedTime'))
            manifest['trigger_recurrence'] = item.get('TriggerRecurrence')
            manifest['saved_execution_verified'] = (
                manifest['remote_status'] == 'Success' and manifest['execution_code_match'] is True
                and isinstance(manifest['run_number'], int) and manifest['run_number'] > 0
                and manifest['trigger_recurrence'] == 'Normal')
            collect_log(api, bundle, manifest)
            # Terminal read follows the state read, so logs are fetched once more after reaching terminal status.
            if manifest['remote_status'] in TERMINAL_STATES:
                collect_log(api, bundle, manifest)
        with (bundle / 'status_history.jsonl').open('a', encoding='utf-8') as handle:
            handle.write(json.dumps({'observed_at': datetime.now(timezone.utc).isoformat(),
                'status': manifest['remote_status'], 'task_instance_id': manifest['task_instance_id'],
                'run_number': manifest.get('run_number')}) + '\n')
        save_manifest(bundle, manifest)
        if manifest['remote_status'] in TERMINAL_STATES or not wait:
            return manifest
        if time.monotonic() - started >= timeout:
            manifest['wait_timed_out'] = True
            save_manifest(bundle, manifest)
            return manifest
        print('等待实例 %s，状态=%s' % (manifest.get('task_instance_id'), manifest['remote_status']), flush=True)
        time.sleep(min(poll_seconds, max(0, timeout - (time.monotonic() - started))))


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
        child.add_argument('--run-dir', required=True)
        child.add_argument('--wait', action='store_true')
        child.add_argument('--poll-seconds', type=int, default=POLL_SECONDS)
        child.add_argument('--timeout', type=int, default=WAIT_SECONDS)
    child = commands.add_parser('analyze-log')
    child.add_argument('--file', required=True)
    child.add_argument('--output-dir')
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if args.command == 'analyze-log':
            source = Path(args.file).read_bytes()
            raw = source.decode('utf-8-sig')
            result = analyze_log(raw)
            if args.output_dir:
                bundle = new_bundle(args.output_dir)
                (bundle / 'input.log').write_bytes(source)
                write_json(bundle / 'analysis.json', result)
                result['run_dir'] = str(bundle)
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
            if args.command == 'logs' and result.get('log_available'):
                analysis_path = Path(args.run_dir) / ('run-%s.analysis.json' % int(result['run_number']))
                result['analysis'] = json.loads(analysis_path.read_text(encoding='utf-8'))
        print(json.dumps(redacted_object(result), ensure_ascii=False, indent=2), flush=True)
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
    sys.exit(main())

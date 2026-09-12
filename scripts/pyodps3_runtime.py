"""Shared DataWorks runtime helpers; importing does not connect or run user code."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import tempfile
import time
import uuid

import runtime_config as config
from contextlib import contextmanager


@contextmanager
def session_lock(root):
    """OS-owned lock: a crashed process releases it; an existing file is not a lock."""
    path = Path(root) / '.session.lock'
    with path.open('a+b') as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b'\0'); handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError('会话正在由另一个进程处理；不得另建目录重复提交') from None
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class APIError(RuntimeError):
    def __init__(self, operation, code=None, message='', request_id=None):
        self.operation, self.code, self.request_id = operation, code, request_id
        actions = re.findall(r'\b[a-z][a-z0-9-]*:[A-Za-z][A-Za-z0-9*_-]*', str(message))
        self.permission_action = actions[0] if actions else None
        safe_message = '缺少权限 ' + self.permission_action if self.permission_action else redact(message)
        self.retryable = bool(re.search(
            r'throttl|toomanyrequests|timeout|internalerror|internalfailure|serviceunavailable|systemerror|^429$|^5\d\d',
            str(code), re.I))
        super().__init__('%s failed code=%s request_id=%s: %s' %
                         (operation, code, request_id, safe_message))

    @property
    def definite_rejection(self):
        return str(self.code).startswith(('400', '401', '403', '404', 'Invalid', 'Forbidden', 'AccessDenied'))


class DataWorksAPI:
    """Lazy SDK clients with bounded retries for transient Get/List failures only."""
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
            config.validate_connection('dataworks')
            from alibabacloud_dataworks_public20240518.client import Client
            from alibabacloud_tea_openapi.models import Config
            self._modern = Client(Config(access_key_id=config.ACCESS_ID, access_key_secret=config.SECRET,
                                         endpoint=config.DATAWORKS_ENDPOINT))
        return self._modern

    @staticmethod
    def error(operation, exc):
        if isinstance(exc, APIError):
            return exc
        data = getattr(exc, 'data', None) or {}
        data = data if isinstance(data, dict) else {}
        details = getattr(exc, 'access_denied_detail', None) or data.get('AccessDeniedDetail') or {}
        details = details if isinstance(details, dict) else {}
        action = getattr(exc, 'permission_action', None) or details.get('AuthAction')
        message = action or getattr(exc, 'message', None) or type(exc).__name__
        code = getattr(exc, 'code', None) or data.get('Code') or data.get('ErrorCode')
        status = (getattr(exc, 'status_code', None) or getattr(exc, 'statusCode', None)
                  or data.get('statusCode') or data.get('StatusCode') or data.get('HttpStatusCode'))
        error = APIError(getattr(exc, 'operation', None) or operation, code or status, message,
                         data.get('RequestId') or getattr(exc, 'request_id', None))
        if str(status).startswith('5') or isinstance(exc, TimeoutError) or re.search(
                r'timeout|timed\s*out', type(exc).__name__ + ' ' + str(getattr(exc, 'message', '')), re.I):
            error.retryable = True
        return error

    @staticmethod
    def _invoke(operation, function, passthrough=()):
        attempts = 3 if operation.startswith(('Get', 'List')) else 1
        for attempt in range(attempts):
            try:
                return function()
            except passthrough:
                raise
            except Exception as exc:
                error = DataWorksAPI.error(operation, exc)
                if attempt + 1 >= attempts or not error.retryable:
                    raise error from None
                time.sleep(attempt + 1)

    def saved_task(self, name=None, file_id=None):
        import fetch_task_sql
        return self._invoke('GetFile', lambda: fetch_task_sql.fetch_saved_task(
            name=name, file_id=file_id, client=self.legacy),
            passthrough=(ValueError, fetch_task_sql.TaskSqlNotFound))

    def call(self, operation, payload, legacy=False):
        from alibabacloud_tea_util.models import RuntimeOptions
        if legacy:
            from alibabacloud_dataworks_public20200518 import models
        else:
            from alibabacloud_dataworks_public20240518 import models
        method = re.sub(r'(?<!^)(?=[A-Z])', '_', operation).lower() + '_with_options'
        def invoke():
            request = getattr(models, operation + 'Request')().from_map(payload)
            client = self.legacy if legacy else self.modern
            response = getattr(client, method)(request, RuntimeOptions(
                autoretry=False, connect_timeout=20000, read_timeout=45000))
            body = response.body.to_map()
            if body.get('Success') is False:
                raise APIError(operation, body.get('ErrorCode') or body.get('HttpStatusCode'),
                               body.get('ErrorMessage', ''), body.get('RequestId'))
            return body
        return self._invoke(operation, invoke)


def write_json(path, value):
    """Durably replace one JSON file without sharing a temporary name across writers."""
    path = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='\n',
                dir=path.parent, prefix='.' + path.name + '.', suffix='.tmp', delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _resolve_resource(api, saved, selected, selection_source='saved'):
    if selected is None or not str(selected).strip():
        raise ValueError('保存配置缺少 ResourceGroupId；不能猜测资源组')
    selected = str(selected).strip()
    numeric = selected.isdigit()
    legacy_errors, matches = [], []
    for category in ('default', 'single'):
        try:
            response = api.call('ListResourceGroups', {'ResourceGroupType': 1, 'BizExtKey': category}, legacy=True)
        except APIError as exc:
            legacy_errors.append(exc)
            continue
        found = [x for x in response.get('Data', []) if
                 str(x.get('Id') if numeric else x.get('Identifier')) == selected]
        matches.extend(found)
        identifiers = {str(x['Identifier']) for x in matches if x.get('Identifier') and str(x.get('Status')) == '0'}
        if len(identifiers) == 1 and all(str(x.get('Status')) == '0' for x in matches):
            return {'saved_id': saved.get('node_configuration', {}).get('ResourceGroupId'),
                    'identifier': identifiers.pop(), 'evidence': 'ListResourceGroups.Id/Identifier',
                    'selected_value': selected, 'selection_source': selection_source,
                    'matched_record_id': matches[0].get('Id'),
                    'api_version': '2020-05-18', 'request_id': response.get('RequestId')}
    if matches:
        raise ValueError('资源组 %s 状态不正常或标识不唯一，不能运行' % selected)
    # Modern IDs are opaque identifiers. Never derive them from a numeric suffix.
    page, seen, modern_matches = 1, set(), []
    while True:
        payload = {'ProjectId': saved.get('project_id') or config.DATAWORKS_PROJECT_ID,
                   'ResourceGroupTypes': ['CommonV2', 'ExclusiveScheduler'], 'PageSize': 100, 'PageNumber': page}
        response = api.call('ListResourceGroups', payload)
        paging = response.get('PagingInfo') or {}
        groups = paging.get('ResourceGroupList') or []
        if not isinstance(groups, list):
            raise ValueError('新版资源组列表响应无效')
        modern_matches.extend(x for x in groups if not numeric and str(x.get('Id')) == selected)
        previous_count = len(seen)
        for group in groups:
            seen.add(str(group.get('Id')))
        if groups and len(seen) == previous_count:
            raise ValueError('资源组分页没有进展，不能确认唯一标识')
        total = paging.get('TotalCount')
        if not groups or (isinstance(total, int) and len(seen) >= total) or (total is None and len(groups) < 100):
            break
        if page > 10000:
            raise ValueError('资源组分页未结束，不能确认唯一标识')
        page += 1
    usable = [x for x in modern_matches if x.get('Status') == 'Normal' and
              x.get('ResourceGroupType') in ('CommonV2', 'ExclusiveScheduler')]
    if len(modern_matches) == len(usable) == 1:
        return {'saved_id': saved.get('node_configuration', {}).get('ResourceGroupId'),
                'identifier': str(usable[0]['Id']), 'evidence': 'ListResourceGroups.PagingInfo.ResourceGroupList.Id',
                'selected_value': selected, 'selection_source': selection_source,
                'matched_record_id': usable[0]['Id'],
                'api_version': '2024-05-18', 'request_id': response.get('RequestId'),
                'resource_group_type': usable[0]['ResourceGroupType'], 'project_id': payload['ProjectId']}
    if not modern_matches and legacy_errors:
        raise legacy_errors[-1]
    raise ValueError('无法将保存态资源组 %s 唯一映射到正常资源组标识；不能猜测或替换资源组' % selected)


def resolve_resource(api, saved):
    return _resolve_resource(api, saved, saved.get('node_configuration', {}).get('ResourceGroupId'))


def resolve_runtime(api, saved, overrides=None):
    overrides = dict(overrides or {})
    allowed = {'resource_group_id', 'data_source', 'cu', 'image'}
    if set(overrides) - allowed:
        raise ValueError('未知运行配置覆盖：' + ', '.join(sorted(set(overrides) - allowed)))
    for key, value in overrides.items():
        if value is None or not str(value).strip():
            raise ValueError('运行配置覆盖 %s 不能为空' % key)
    node = saved.get('node_configuration') or {}
    owner = saved.get('owner')
    source = overrides.get('data_source', saved.get('connection_name'))
    if not owner or not source:
        raise ValueError('保存配置缺少 Owner/ConnectionName，不能猜测运行配置')
    runtime = {}
    cu = overrides.get('cu', node.get('Cu'))
    if cu is not None and cu != '':
        try:
            number = Decimal(str(cu))
            if not number.is_finite() or number <= 0:
                raise ValueError
        except (InvalidOperation, ValueError):
            raise ValueError('Cu 必须为有限正数') from None
        runtime['Cu'] = str(cu)
    image = overrides.get('image', node.get('ImageId'))
    if image is not None and image != '':
        runtime['Image'] = str(image)
    resource = _resolve_resource(api, saved, overrides.get('resource_group_id', node.get('ResourceGroupId')),
                                 'explicit_override' if 'resource_group_id' in overrides else 'saved')
    runtime = {'ResourceGroupId': resource['identifier'], **runtime}
    original = {'owner': owner, 'resource_group_id': node.get('ResourceGroupId'),
                'data_source': saved.get('connection_name'), 'cu': node.get('Cu'), 'image': node.get('ImageId')}
    effective = {'owner': str(owner), 'resource_group_id': resource['identifier'],
                 'data_source': str(source), 'cu': runtime.get('Cu'), 'image': runtime.get('Image')}
    sources = {key: 'explicit_override' if key in overrides else
               'unspecified' if value is None or value == '' else 'saved' for key, value in original.items()}
    changes = []
    for key, selected in overrides.items():
        previous = original[key]
        equivalent = str(previous) == str(selected)
        if key == 'cu' and previous is not None:
            try:
                equivalent = Decimal(str(previous)) == Decimal(str(selected))
            except InvalidOperation:
                equivalent = False
        if not equivalent:
            changes.append({'field': key, 'saved_value': previous, 'selected_value': selected,
                            'effective_value': effective[key]})
    return {'owner': str(owner), 'data_source': {'Name': str(source)}, 'runtime_resource': runtime,
            'resource_evidence': resource, 'overrides': overrides,
            'configuration_sources': sources, 'configuration_changes': changes}


def build_adhoc_request(*, project_id, task_name, owner, bizdate, parameters, source,
                        runtime_resource, data_source, unique_code):
    if not all((project_id, task_name, owner, unique_code)):
        raise ValueError('临时运行缺少项目、任务名称、责任人或唯一标识')
    if not isinstance(source, str) or not source.strip():
        raise ValueError('运行代码不能为空')
    if not runtime_resource.get('ResourceGroupId'):
        raise ValueError('运行配置缺少 ResourceGroupId')
    return {'ProjectId': project_id, 'EnvType': 'Dev', 'BizDate': bizdate_millis(bizdate),
            'Name': 'pyodps3_debug_' + unique_code, 'Owner': owner,
            'Tasks': [{'Name': task_name, 'Type': 'PYODPS3', 'Owner': owner, 'ClientUniqueCode': unique_code,
                       'Script': {'Content': source, 'Parameters': parameters},
                       'DataSource': dict(data_source), 'RuntimeResource': dict(runtime_resource)}]}


def observe_configuration(item, request):
    """Compare echoed runtime fields; a missing field is evidence unavailable."""
    expected_task = request['Tasks'][0]
    mismatches, unavailable = [], []
    def check(label, actual, expected, normalizer=str):
        if actual is None:
            unavailable.append(label)
            return
        try:
            match = normalizer(actual) == normalizer(expected)
        except (ValueError, TypeError, InvalidOperation):
            match = False
        if not match:
            mismatches.append(label)
    def params(value):
        if isinstance(value, dict):
            return {str(k): str(v) for k, v in value.items()}
        result = {}
        for pair in shlex.split(value):
            key, sep, val = pair.partition('=')
            if not sep or key in result:
                raise ValueError('invalid parameter echo')
            result[key] = val
        return result
    check('ProjectId', item.get('ProjectId'), request['ProjectId'])
    check('ProjectEnv', item.get('ProjectEnv'), request['EnvType'])
    check('TaskType', item.get('TaskType'), expected_task['Type'])
    check('Script.Parameters', (item.get('Script') or {}).get('Parameters'),
          expected_task['Script'].get('Parameters'), params)
    expected_source = expected_task.get('DataSource') or {}
    if 'Name' in expected_source:
        check('DataSource.Name', (item.get('DataSource') or {}).get('Name'), expected_source['Name'])
    actual_runtime = item.get('RuntimeResource') or {}
    for key, value in (expected_task.get('RuntimeResource') or {}).items():
        check('RuntimeResource.' + key, actual_runtime.get(key), value,
              (lambda x: Decimal(str(x))) if key == 'Cu' else str)
    return {'configuration_match': False if mismatches else None if unavailable else True,
            'configuration_mismatches': mismatches, 'configuration_unavailable': unavailable}


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
    def parse_source(pairs, source):
        result = {}
        for pair in pairs:
            key, sep, value = pair.partition('=')
            if not sep or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key):
                raise ValueError('参数必须为 KEY=VALUE：' + redact(pair))
            if key in result and result[key] != value:
                raise ValueError('%s 参数 %s 重复且值冲突' % (source, key))
            result[key] = value
        return result
    parameters = parse_source(shlex.split(saved or ''), '保存态')
    explicit = parse_source(list(overrides or []), '显式')
    parameters.update(explicit)
    if 'bizdate' in explicit and explicit['bizdate'] != bizdate:
        raise ValueError('--param bizdate 与 --bizdate 不一致')
    parameters['bizdate'] = bizdate
    for key, value in parameters.items():
        if value in ('$bizdate', '${bizdate}'):
            parameters[key] = bizdate
        elif '$' in value or any(c.isspace() for c in value):
            raise ValueError('参数 %s 含未解析表达式或空白，请提供明确的单值 --param' % key)
    return ' '.join(k + '=' + v for k, v in parameters.items())


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

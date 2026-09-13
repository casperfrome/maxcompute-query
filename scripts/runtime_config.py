"""Optional local non-secret settings; credentials always come from environment.

Imports and offline commands never require configuration or cloud SDKs.
Call validate_connection(service) immediately before creating a cloud client.
"""
import importlib.util
import os
from pathlib import Path

_local = None
_local_error = False
_path = Path(__file__).with_name('config.py')
if _path.is_file():
    try:
        _spec = importlib.util.spec_from_file_location('_maxcompute_local_config', _path)
        _local = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_local)
    except Exception:
        # Do not print config exceptions: they can contain credential values.
        _local, _local_error = None, True


def _setting(name, default):
    return os.environ.get(name, getattr(_local, name, default))


ACCESS_ID = os.environ.get('ALIYUN_ACCESS_KEY_ID') or os.environ.get('ODPS_ACCESS_ID', '')
SECRET = os.environ.get('ALIYUN_ACCESS_KEY_SECRET') or os.environ.get('ODPS_SECRET', '')
ODPS_PROJECT = _setting('ODPS_PROJECT', '')
ODPS_ENDPOINT = _setting('ODPS_ENDPOINT', '')
ODPS_TUNNEL_ENDPOINT = _setting('ODPS_TUNNEL_ENDPOINT', '')
DATAWORKS_ENDPOINT = _setting('DATAWORKS_ENDPOINT', '')
DATAWORKS_PROJECT_ID = _setting('DATAWORKS_PROJECT_ID', 0)
try:
    if isinstance(DATAWORKS_PROJECT_ID, str):
        DATAWORKS_PROJECT_ID = int(DATAWORKS_PROJECT_ID)
except (ValueError, TypeError):
    pass  # Invalid values are reported only at connection time.
DATAWORKS_ODPS_PROJECT_NAME = _setting('DATAWORKS_ODPS_PROJECT_NAME', ODPS_PROJECT)


def require_credentials():
    if not ACCESS_ID or not SECRET:
        raise ValueError('缺少环境凭证：设置 ALIYUN_ACCESS_KEY_ID / ALIYUN_ACCESS_KEY_SECRET，或 ODPS_ACCESS_ID / ODPS_SECRET。')


def validate_connection(service='odps', *, project=None):
    if service not in ('odps', 'dataworks'):
        raise ValueError('未知连接类型。')
    require_credentials()
    names = ('ODPS_PROJECT', 'ODPS_ENDPOINT') if service == 'odps' else (
        'DATAWORKS_ENDPOINT', 'DATAWORKS_PROJECT_ID')
    invalid = []
    for name in names:
        value = project if name == 'ODPS_PROJECT' and project is not None else globals()[name]
        if name == 'DATAWORKS_PROJECT_ID':
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                invalid.append(name)
        elif not isinstance(value, str) or not value.strip() or '<' in value or value.startswith('your_'):
            invalid.append(name)
    if invalid:
        raise ValueError('连接配置缺失或无效：' + ', '.join(invalid) +
                         ('；可选 config.py 加载失败，请检查非密钥配置。' if _local_error else '。'))

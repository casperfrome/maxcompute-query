"""Optional non-secret local settings: copy to config.py only if needed.

Environment variables with these names take priority. Empty defaults deliberately
require an explicit project/endpoint before connecting. --help and offline tools
need no config.py. Never put credentials in this file: runtime_config reads only
ALIYUN_ACCESS_KEY_ID / ALIYUN_ACCESS_KEY_SECRET, falling back to
ODPS_ACCESS_ID / ODPS_SECRET environment variables.
"""
ODPS_PROJECT = ''
ODPS_ENDPOINT = ''  # e.g. https://service.<region>.maxcompute.aliyun.com/api
ODPS_TUNNEL_ENDPOINT = ''  # Optional; let the SDK resolve when unspecified.
DATAWORKS_ENDPOINT = ''  # e.g. dataworks.<region>.aliyuncs.com
DATAWORKS_PROJECT_ID = 0
DATAWORKS_ODPS_PROJECT_NAME = ''


def require_credentials():
    """Compatibility helper; application code imports runtime_config instead."""
    from runtime_config import require_credentials as require_environment_credentials
    return require_environment_credentials()

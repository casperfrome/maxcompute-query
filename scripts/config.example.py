# -*- coding: utf-8 -*-
"""ODPS / DataWorks 连接配置模板。

使用方法：把本文件复制为同目录下的 `config.py`，再填入自己的凭证，
或（推荐）只通过环境变量提供凭证、保持明文默认值为空。
`config.py` 已被 .gitignore 排除，不会被提交。
"""
import os

# AK/SK：ODPS 与 DataWorks 是同一套凭证；优先 ALIYUN_*，回退 ODPS_*，再回退内置默认。
# 不要把真实 AccessKey 明文写进会被提交的文件里——优先使用环境变量。
ACCESS_ID = (
    os.environ.get("ALIYUN_ACCESS_KEY_ID")
    or os.environ.get("ODPS_ACCESS_ID")
    or ""  # 在本地 config.py 中填入，或用环境变量提供
)
SECRET = (
    os.environ.get("ALIYUN_ACCESS_KEY_SECRET")
    or os.environ.get("ODPS_SECRET")
    or ""  # 在本地 config.py 中填入，或用环境变量提供
)

# ODPS / MaxCompute（把 <region> 换成你的地域，如 cn-shanghai；内网环境用对应的 VPC 端点）
ODPS_PROJECT = os.environ.get("ODPS_PROJECT", "your_odps_project")
ODPS_ENDPOINT = os.environ.get(
    "ODPS_ENDPOINT",
    "https://service.<region>.maxcompute.aliyun.com/api",
)
ODPS_TUNNEL_ENDPOINT = os.environ.get(
    "ODPS_TUNNEL_ENDPOINT",
    "https://dt.<region>.maxcompute.aliyun.com",
)

# DataWorks
DATAWORKS_ENDPOINT = os.environ.get(
    "DATAWORKS_ENDPOINT", "dataworks.<region>.aliyuncs.com"
)
DATAWORKS_PROJECT_ID = int(os.environ.get("DATAWORKS_PROJECT_ID", "0"))
DATAWORKS_ODPS_PROJECT_NAME = os.environ.get(
    "DATAWORKS_ODPS_PROJECT_NAME", "your_odps_project"
)

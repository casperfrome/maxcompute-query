#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
fetch_task_sql.py — 按表名/任务名从 DataWorks 拉取线上 SQL 任务代码

用途：当用户只给了表名/任务名（如「看看 dws_xxx 这个任务有没有逻辑问题」「帮我改下 xxx
这个任务」）而没有贴出 SQL 时，本脚本直连 DataWorks OpenAPI，定位产出该表的任务节点，
把它的生产态（取不到再退而求开发态）SQL 打印出来，供后续审查/修改使用。

    python fetch_task_sql.py <表名或任务名>              # 打印该任务当前 SQL（默认）
    python fetch_task_sql.py <表名> --save out.sql       # 同时落盘到 .sql 文件
    python fetch_task_sql.py <关键字> --search           # 列出所有名字匹配的候选任务（消歧用）

历史版本（看「上一版/某次提交/版本对比」时用）：
    python fetch_task_sql.py <表名> --list-versions      # 列出该任务所有历史版本
    python fetch_task_sql.py <表名> --get-version 7      # 取第 7 版的完整 SQL（可配 --save）
    python fetch_task_sql.py <表名> --diff 6 7           # 对比第 6、7 版改了什么

配置默认沿用项目凭证，可用环境变量覆盖（AK/SK 与 mc_query.py 的 ODPS 凭证相同）：
    ALIYUN_ACCESS_KEY_ID / ALIYUN_ACCESS_KEY_SECRET （回退 ODPS_ACCESS_ID / ODPS_SECRET）
    DATAWORKS_ENDPOINT / DATAWORKS_PROJECT_ID / DATAWORKS_ODPS_PROJECT_NAME

⚠ 重要：拉回的 SQL 是「待审查 / 待修改的任务代码」，里面通常含 DROP / CREATE / INSERT
等写语句。它只供阅读分析，**绝不要把它丢给 mc_query.py 执行**。要验证前提或结果时，
请另写只读 SELECT 走 mc_query.py。
"""

import argparse
import datetime
import difflib
import os
import sys

# stdout 用 utf-8，避免 Windows GBK 控制台打印中文/SQL 注释乱码
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# 把脚本所在目录加入 import 路径，确保以文件方式直接运行时也能 import 同目录的 config
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config  # noqa: E402  # 连接凭证/端点的单一来源（与 mc_query.py 共用，可用环境变量覆盖）
from alibabacloud_dataworks_public20200518 import models as dataworks_models  # noqa: E402
from alibabacloud_dataworks_public20200518.client import Client as DataWorksClient  # noqa: E402
from alibabacloud_tea_openapi import models as open_api_models  # noqa: E402

# DataWorks 用的 AK/SK 与 mc_query.py 里的 ODPS 凭证是同一套，统一从 config 读取。
ACCESS_ID = config.ACCESS_ID
SECRET = config.SECRET
DATAWORKS_ENDPOINT = config.DATAWORKS_ENDPOINT
DATAWORKS_PROJECT_ID = config.DATAWORKS_PROJECT_ID
DATAWORKS_ODPS_PROJECT_NAME = config.DATAWORKS_ODPS_PROJECT_NAME


class TaskSqlNotFound(LookupError):
    """按表名/任务名在 DataWorks 中找不到对应的 SQL 任务。"""


# ---------------------------------------------------------------------------
# DataWorks 客户端 & 取码
# ---------------------------------------------------------------------------
def create_client():
    config = open_api_models.Config(
        access_key_id=ACCESS_ID,
        access_key_secret=SECRET,
    )
    config.endpoint = DATAWORKS_ENDPOINT
    return DataWorksClient(config)


def list_matching_files(client, task_name):
    """按关键字分页搜 DataWorks 文件，返回名字真正包含该关键字的文件（含开发态 content）。"""
    results = []
    seen_file_ids = set()
    page_number = 1
    page_size = 50
    keyword_lower = task_name.strip().lower()

    while True:
        request = dataworks_models.ListFilesRequest(
            project_id=DATAWORKS_PROJECT_ID,
            page_number=page_number,
            page_size=page_size,
            keyword=task_name,
            use_type="NORMAL",
            need_content=True,
            need_absolute_folder_path=True,
        )
        response = client.list_files(request)
        if not response.body.success:
            raise RuntimeError(
                "ListFiles failed: "
                + (response.body.error_message or "unknown error")
            )

        data = response.body.data
        files = list(getattr(data, "files", []) or [])
        for item in files:
            file_name = item.file_name or ""
            if keyword_lower not in file_name.lower():
                continue
            if item.file_id in seen_file_ids:
                continue
            seen_file_ids.add(item.file_id)
            results.append(
                {
                    "file_name": file_name,
                    "node_id": item.node_id,
                    "file_id": item.file_id,
                    "file_type": item.file_type,
                    "content": item.content or "",
                    "folder_path": item.absolute_folder_path or "",
                    "owner": item.owner,
                }
            )

        total_count = getattr(data, "total_count", 0) or 0
        if not files or page_number * page_size >= total_count:
            break
        page_number += 1

    results.sort(
        key=lambda x: (
            (x.get("file_name") or "").lower(),
            (x.get("folder_path") or "").lower(),
            x.get("node_id") or 0,
        )
    )
    return results


def get_nodes_by_output(client, task_name):
    """按「产出表名」反查生产环境的任务节点。任务文件名 ≠ 表名时靠这个兜底。"""
    request = dataworks_models.ListNodesByOutputRequest(
        project_env="PROD",
        outputs=f"{DATAWORKS_ODPS_PROJECT_NAME}.{task_name}",
    )
    response = client.list_nodes_by_output(request)
    if not response.body.success:
        raise RuntimeError(
            "ListNodesByOutput failed: "
            + (response.body.error_message or "unknown error")
        )

    nodes = []
    for output_pair in list(response.body.data or []):
        for node in list(getattr(output_pair, "node_list", []) or []):
            nodes.append(
                {
                    "node_id": node.node_id,
                    "node_name": node.node_name,
                    "program_type": node.program_type,
                    "output": output_pair.output,
                }
            )
    return nodes


def get_node_code(client, node_id):
    """取生产环境某节点的 SQL 代码。"""
    request = dataworks_models.GetNodeCodeRequest(
        node_id=node_id,
        project_env="PROD",
    )
    response = client.get_node_code(request)
    if not response.body.success:
        raise RuntimeError(
            f"GetNodeCode failed for node {node_id}: "
            + (response.body.error_message or "unknown error")
        )
    return response.body.data or ""


def _result_from_match(client, match):
    """把一条文件匹配转成结果 dict：优先生产态 SQL，没有再用开发态 content。"""
    task_name = (match.get("file_name") or "").strip()
    if not task_name:
        return None

    dev_sql = (match.get("content") or "").strip()
    node_id = match.get("node_id")
    prod_sql = ""
    if node_id:
        prod_sql = get_node_code(client, node_id).strip()

    if prod_sql:
        return {
            "task_name": task_name,
            "node_id": node_id,
            "folder_path": match.get("folder_path") or "",
            "source": "prod",
            "sql_text": prod_sql,
            "dev_sql_available": bool(dev_sql),
        }
    if dev_sql:
        return {
            "task_name": task_name,
            "node_id": node_id,
            "folder_path": match.get("folder_path") or "",
            "source": "dev",
            "sql_text": dev_sql,
            "dev_sql_available": True,
        }
    return None


def fetch_task_sql(task_name):
    """主入口：先精确文件名匹配，再按产出表名反查节点；都没有则抛 TaskSqlNotFound。"""
    name = task_name.strip()
    if not name:
        raise ValueError("表名/任务名不能为空。")

    client = create_client()

    # 1) 文件名与传入名完全相等的，优先取它的 prod/dev SQL
    matches = list_matching_files(client, name)
    exact = [m for m in matches if (m.get("file_name") or "").lower() == name.lower()]
    for match in exact:
        result = _result_from_match(client, match)
        if result is not None:
            return result

    # 2) 兜底：按产出表名反查生产节点（任务名 ≠ 表名时也能命中）
    for node in get_nodes_by_output(client, name):
        prod_sql = get_node_code(client, node["node_id"]).strip()
        if prod_sql:
            return {
                "task_name": name,
                "node_id": node["node_id"],
                "folder_path": "",
                "source": "prod",
                "sql_text": prod_sql,
                "dev_sql_available": False,
            }

    raise TaskSqlNotFound(f"在 DataWorks 中找不到任务 '{name}' 的 SQL。")


def search_task_sqls(task_name):
    """返回所有名字匹配的候选任务（用于 --search 消歧）。"""
    name = task_name.strip()
    if not name:
        raise ValueError("关键字不能为空。")
    client = create_client()
    results = []
    for match in list_matching_files(client, name):
        result = _result_from_match(client, match)
        if result is not None:
            results.append(result)
    return results


# ---------------------------------------------------------------------------
# 历史版本
# ---------------------------------------------------------------------------
def _fmt_ts(ms):
    """毫秒时间戳 -> 本地可读时间 'YYYY-MM-DD HH:MM:SS'；空值返回 '-'。"""
    if not ms:
        return "-"
    try:
        return datetime.datetime.fromtimestamp(int(ms) / 1000).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    except (ValueError, OSError, OverflowError):
        return str(ms)


def resolve_file_id(client, name):
    """把「表名/任务名」解析成 file_id（版本接口必需）。

    顺序：①文件名精确相等 → ②唯一候选 → ③多候选抛歧义 → ④按产出表名反查节点，
    用 node_name 回查文件 → ⑤仍无则抛 TaskSqlNotFound。
    返回 {file_id, file_name, folder_path}。
    """
    name = name.strip()
    if not name:
        raise ValueError("表名/任务名不能为空。")

    matches = list_matching_files(client, name)

    # ① 文件名与传入名完全相等
    exact = [m for m in matches if (m.get("file_name") or "").lower() == name.lower()]
    pick = None
    if exact:
        pick = exact[0]
    elif len(matches) == 1:
        # ② 只有唯一候选，直接用它
        pick = matches[0]
    elif len(matches) > 1:
        # ③ 多个候选，无法确定是哪一个
        names = ", ".join(sorted({(m.get("file_name") or "") for m in matches})[:8])
        raise TaskSqlNotFound(
            f"名字匹配 '{name}' 的任务有多个（{names} ...），无法确定取哪个的版本历史。"
            f"请用 `--search` 看候选后，传精确任务名重试。"
        )

    if pick is None:
        # ④ 兜底：按产出表名反查生产节点，再用 node_name 回查文件拿 file_id
        for node in get_nodes_by_output(client, name):
            node_name = (node.get("node_name") or "").strip()
            if not node_name:
                continue
            for m in list_matching_files(client, node_name):
                if (m.get("file_name") or "").lower() == node_name.lower() and m.get(
                    "file_id"
                ):
                    pick = m
                    break
            if pick is not None:
                break

    if pick is None or not pick.get("file_id"):
        # ⑤ 实在解析不到 file_id
        raise TaskSqlNotFound(
            f"找不到任务 '{name}' 对应的文件（file_id），无法查版本历史。"
            f"该任务可能不是文件态节点，或不在当前 DataWorks 工作空间。"
        )

    return {
        "file_id": pick["file_id"],
        "file_name": pick.get("file_name") or name,
        "folder_path": pick.get("folder_path") or "",
    }


def list_task_versions(client, file_id):
    """分页拉取某文件的所有历史版本，按版本号倒序（最新在前）返回元信息列表。"""
    versions = []
    page_number = 1
    page_size = 100

    while True:
        request = dataworks_models.ListFileVersionsRequest(
            project_id=DATAWORKS_PROJECT_ID,
            file_id=file_id,
            page_number=page_number,
            page_size=page_size,
        )
        response = client.list_file_versions(request)
        if not response.body.success:
            raise RuntimeError(
                "ListFileVersions failed: "
                + (response.body.error_message or "unknown error")
            )

        data = response.body.data
        items = list(getattr(data, "file_versions", []) or [])
        for v in items:
            content = v.file_content or ""
            versions.append(
                {
                    "file_version": v.file_version,
                    "commit_time": v.commit_time,
                    "commit_user": v.commit_user,
                    "comment": v.comment or "",
                    "status": v.status or "",
                    "is_current_prod": bool(v.is_current_prod),
                    "change_type": v.change_type or "",
                    "char_count": len(content),
                }
            )

        total_count = getattr(data, "total_count", 0) or 0
        if not items or page_number * page_size >= total_count:
            break
        page_number += 1

    versions.sort(key=lambda x: (x.get("file_version") or 0), reverse=True)
    return versions


def get_task_version_sql(client, file_id, version):
    """取指定历史版本的完整 SQL + 元信息。空内容/接口失败给干净报错。"""
    request = dataworks_models.GetFileVersionRequest(
        project_id=DATAWORKS_PROJECT_ID,
        file_id=file_id,
        file_version=version,
    )
    response = client.get_file_version(request)
    if not response.body.success:
        raise RuntimeError(
            f"GetFileVersion failed for file {file_id} v{version}: "
            + (response.body.error_message or "unknown error")
        )

    data = response.body.data
    # 不存在的版本号：接口仍返回 success=True，但 data 为空 / file_version 为 None
    if data is None or data.file_version is None:
        raise TaskSqlNotFound(f"版本 {version} 不存在（file_id={file_id}）。")

    return {
        "file_version": data.file_version,
        "commit_time": data.commit_time,
        "commit_user": data.commit_user,
        "comment": data.comment or "",
        "status": data.status or "",
        "is_current_prod": bool(data.is_current_prod),
        "sql_text": (data.file_content or "").strip(),
    }


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------
def _print_header(result):
    print("=" * 70)
    print(f"任务名     : {result['task_name']}")
    print(f"来源       : {result['source']}  (prod=生产态 / dev=开发态)")
    print(f"node_id    : {result.get('node_id')}")
    if result.get("folder_path"):
        print(f"目录       : {result['folder_path']}")
    print(f"开发态存在 : {result.get('dev_sql_available')}")
    print("=" * 70)


def cmd_fetch(args):
    try:
        result = fetch_task_sql(args.name)
    except TaskSqlNotFound as e:
        print(f"[未找到] {e}", file=sys.stderr)
        print(
            "提示：确认表名/任务名拼写无误；可用 "
            "`fetch_task_sql.py <关键字> --search` 看候选，"
            "或用 `mc_query.py list-tables <关键字>` 核对真实表名。",
            file=sys.stderr,
        )
        sys.exit(4)

    _print_header(result)
    print(result["sql_text"])

    if args.save:
        with open(args.save, "w", encoding="utf-8") as f:
            f.write(result["sql_text"])
        print(f"\n（已落盘 {len(result['sql_text'])} 字符至 {args.save}）", file=sys.stderr)


def cmd_search(args):
    results = search_task_sqls(args.name)
    if not results:
        print(f"未找到名字匹配 '{args.name}' 的任务。", file=sys.stderr)
        sys.exit(4)
    print(f"匹配到 {len(results)} 个候选任务（关键字='{args.name}'）：")
    for r in results:
        folder = f"  [{r['folder_path']}]" if r.get("folder_path") else ""
        print(
            f"  {r['task_name']}\t(source={r['source']}, node_id={r.get('node_id')},"
            f" {len(r['sql_text'])} 字符){folder}"
        )
    print(
        "\n确定目标后，用 `fetch_task_sql.py <精确任务名>` 取它的完整 SQL。",
        file=sys.stderr,
    )


def cmd_list_versions(args):
    client = create_client()
    info = resolve_file_id(client, args.name)
    versions = list_task_versions(client, info["file_id"])
    if not versions:
        print(f"任务 '{info['file_name']}' 没有历史版本记录。", file=sys.stderr)
        sys.exit(4)

    print("=" * 70)
    print(f"任务名     : {info['file_name']}")
    print(f"file_id    : {info['file_id']}")
    if info.get("folder_path"):
        print(f"目录       : {info['folder_path']}")
    print(f"历史版本数 : {len(versions)}")
    print("=" * 70)
    print(f"{'版本':>4}  {'提交时间':<19}  {'提交人':<22}  "
          f"{'状态':<10}  {'生产版':<5}  {'变更':<7}  {'字符数':>7}  备注")
    print("-" * 110)
    for v in versions:
        cur = "★" if v["is_current_prod"] else ""
        print(
            f"{(v['file_version'] or 0):>4}  "
            f"{_fmt_ts(v['commit_time']):<19}  "
            f"{(v['commit_user'] or '-'):<22}  "
            f"{(v['status'] or '-'):<10}  "
            f"{cur:<5}  "
            f"{(v['change_type'] or '-'):<7}  "
            f"{v['char_count']:>7}  "
            f"{v['comment']}"
        )
    print(
        "\n（★=与当前生产环境一致的版本）"
        "\n取某版完整代码：`--get-version <版本号>`；对比两版差异：`--diff <版本A> <版本B>`。",
        file=sys.stderr,
    )


def cmd_get_version(args):
    client = create_client()
    info = resolve_file_id(client, args.name)
    try:
        v = get_task_version_sql(client, info["file_id"], args.get_version)
    except TaskSqlNotFound as e:
        print(f"[未找到] {e}", file=sys.stderr)
        print(
            "提示：用 `--list-versions` 看该任务有哪些版本号。",
            file=sys.stderr,
        )
        sys.exit(4)

    print("=" * 70)
    print(f"任务名     : {info['file_name']}")
    print(f"版本       : {v['file_version']}  "
          f"{'（当前生产版）' if v['is_current_prod'] else ''}")
    print(f"提交时间   : {_fmt_ts(v['commit_time'])}")
    print(f"提交人     : {v['commit_user'] or '-'}")
    print(f"状态       : {v['status'] or '-'}")
    if v["comment"]:
        print(f"备注       : {v['comment']}")
    print("=" * 70)
    print(v["sql_text"])

    if args.save:
        with open(args.save, "w", encoding="utf-8") as f:
            f.write(v["sql_text"])
        print(
            f"\n（已落盘 {len(v['sql_text'])} 字符至 {args.save}）", file=sys.stderr
        )


def cmd_diff(args):
    client = create_client()
    info = resolve_file_id(client, args.name)
    va, vb = args.diff
    try:
        a = get_task_version_sql(client, info["file_id"], va)
        b = get_task_version_sql(client, info["file_id"], vb)
    except TaskSqlNotFound as e:
        print(f"[未找到] {e}", file=sys.stderr)
        print("提示：用 `--list-versions` 看该任务有哪些版本号。", file=sys.stderr)
        sys.exit(4)

    label_a = f"v{a['file_version']} ({_fmt_ts(a['commit_time'])})"
    label_b = f"v{b['file_version']} ({_fmt_ts(b['commit_time'])})"
    print("=" * 70)
    print(f"任务名 : {info['file_name']}")
    print(f"对比   : {label_a}  →  {label_b}")
    print("=" * 70)

    diff_lines = list(
        difflib.unified_diff(
            a["sql_text"].splitlines(),
            b["sql_text"].splitlines(),
            fromfile=label_a,
            tofile=label_b,
            lineterm="",
        )
    )
    if not diff_lines:
        print("两个版本内容完全一致，无差异。", file=sys.stderr)
        return
    print("\n".join(diff_lines))


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(
        description="按表名/任务名从 DataWorks 拉取线上 SQL 任务代码（只取码，不执行）",
    )
    p.add_argument("name", help="表名或任务名（精确名取完整 SQL；配 --search 时当关键字用）")
    p.add_argument("--save", help="把 SQL 落盘到该路径（建议 .sql）")

    mode = p.add_mutually_exclusive_group()
    mode.add_argument(
        "--search",
        action="store_true",
        help="列出所有名字匹配的候选任务而非取单个（消歧用）",
    )
    mode.add_argument(
        "--list-versions",
        "--versions",
        dest="list_versions",
        action="store_true",
        help="列出该任务的所有历史版本（版本号/提交时间/提交人/状态/字符数等）",
    )
    mode.add_argument(
        "--get-version",
        dest="get_version",
        type=int,
        metavar="N",
        help="取第 N 个历史版本的完整 SQL（可配 --save 落盘）",
    )
    mode.add_argument(
        "--diff",
        dest="diff",
        nargs=2,
        type=int,
        metavar=("A", "B"),
        help="对比两个历史版本的差异（输出 unified diff）",
    )
    return p


def main():
    args = build_parser().parse_args()
    try:
        if args.search:
            cmd_search(args)
        elif args.list_versions:
            cmd_list_versions(args)
        elif args.get_version is not None:
            cmd_get_version(args)
        elif args.diff is not None:
            cmd_diff(args)
        else:
            cmd_fetch(args)
    except TaskSqlNotFound as e:
        # 版本类命令解析 file_id 失败等：给干净提示而非 traceback
        print(f"[未找到] {e}", file=sys.stderr)
        sys.exit(4)
    except ValueError as e:
        print(f"[错误] {e}", file=sys.stderr)
        sys.exit(3)
    except RuntimeError as e:
        # DataWorks API 返回的失败（鉴权、配额、接口报错等），给干净提示而非 traceback
        print(f"[DataWorks 接口出错] {e}", file=sys.stderr)
        sys.exit(5)


if __name__ == "__main__":
    main()

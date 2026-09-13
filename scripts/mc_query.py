#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
mc_query.py — MaxCompute / ODPS 数仓排查取数辅助工具

提供一组只读命令行子命令，帮助先探查表结构、再编写并执行 MaxCompute SQL：

    python mc_query.py list-tables [pattern]        # 找表
    python mc_query.py desc <table>                 # 看字段 + 分区
    python mc_query.py partitions <table> [-n 20]   # 看最近分区 + 最新分区
    python mc_query.py sample <table> [-n 10]       # 按最新分区采样几行
    python mc_query.py list-functions [pattern]     # 找自定义函数 (UDF)
    python mc_query.py func <name>                  # 读 UDF 注册信息 + 实现源码
    python mc_query.py resource <name>              # 读单个资源文件内容
    python mc_query.py sql -q "<inline sql>"        # 执行只读 SQL
    python mc_query.py sql -f query.sql --save out.xlsx

连接配置默认沿用项目现有凭证，可用环境变量覆盖：
    ODPS_ACCESS_ID / ODPS_SECRET / ODPS_PROJECT / ODPS_ENDPOINT / ODPS_TUNNEL_ENDPOINT

安全：所有 SQL 执行前做只读校验，命中写操作（INSERT/UPDATE/DELETE/DROP/
ALTER/CREATE/TRUNCATE/MERGE/...）直接拒绝退出。本工具只用于排查取数，不改数。
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
import warnings

warnings.filterwarnings(
    "ignore",
    category=FutureWarning,
    message="The behavior of DataFrame concatenation with empty or all-NA entries is deprecated",
)

# stdout 用 utf-8，避免 Windows GBK 控制台打印中文/markdown 报错
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# 把脚本所在目录加入 import 路径，确保以文件方式直接运行时也能 import 同目录的 config
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import runtime_config as config  # noqa: E402
from sql_utils import (Query, mask_literals as _mask_literals, partition_constrained, tokenize,
                       assert_readonly, _scrub_for_check, WRITE_KEYWORDS, READ_STARTERS)

# 打印结果时的默认行数上限（保护超大结果集，可用 --max-rows 调整）
DEFAULT_MAX_PRINT_ROWS = 200

# ---------------------------------------------------------------------------
# 分区过滤静态检查：绑定每次关系引用及其作用域；失败显式返回未知。
# ---------------------------------------------------------------------------
def extract_table_refs(sql: str):
    query = Query(sql)
    names = {d.token.name.lower() for d in query.definitions}
    refs = list(dict.fromkeys('.'.join(rel.parts) for _, rel in query.physical))
    return names, refs


def check_partition_filters(odps, sql):
    """Return issue dictionaries; metadata/parser failures must not imply safety."""
    try:
        tokens = tokenize(sql)
        if tokens and tokens[0].word in ('DESC', 'DESCRIBE', 'SHOW'):
            return []
        if tokens and tokens[0].word == 'EXPLAIN':
            sql = sql[tokens[0].end:]
        query = Query(sql)
    except ValueError as exc:
        return [dict(table='<查询>', status='未知', reason=str(exc), missing=[])]
    project = getattr(odps, 'project', None)
    issues, metadata = [], {}
    for scope, relation in query.physical:
        table = '.'.join(relation.parts)
        if table not in metadata:
            try:
                if not odps.exist_table(table):
                    raise ValueError('表不存在或不可访问')
                metadata[table] = [p.name for p in odps.get_table(table).table_schema.partitions]
            except Exception:
                metadata[table] = None
        columns = metadata[table]
        if columns is None:
            issues.append(dict(table=table, status='未知', reason='无法读取分区元数据', missing=[]))
            continue
        missing = [column for column in columns if not any(
            partition_constrained(query, scope, relation, column, start, end, project)
            for start, end in relation.predicates)]
        if missing:
            issues.append(dict(table=table, status='缺失', reason='未识别到限定该表的分区谓词',
                               missing=missing, alias=relation.alias))
    return issues


# ---------------------------------------------------------------------------
# ODPS 连接 & 取数
# ---------------------------------------------------------------------------
def get_odps(project=None) -> ODPS:
    config.validate_connection('odps', project=project)
    from odps import ODPS
    return ODPS(
        access_id=config.ACCESS_ID,
        secret_access_key=config.SECRET,
        project=(config.ODPS_PROJECT if project is None else project).strip(),
        endpoint=config.ODPS_ENDPOINT,
        tunnel_endpoint=config.ODPS_TUNNEL_ENDPOINT,
    )


def _safe(fn):
    """Optional UDF metadata must not prevent reading the available source."""
    try:
        return fn()
    except Exception:
        return None


def run_select_df(odps: ODPS, sql: str = None, **options):
    """Compatibility entry: bounded preview, raw text, or streamed export + metadata."""
    from sql_execution import execute_query
    return execute_query(odps, sql=sql, **options)


def format_run_meta(meta: dict) -> str:
    """Never label downloaded rows as the remote result's total size."""
    def value(key):
        item = meta.get(key)
        return '未知' if item is None else str(item)
    def flag(key):
        item = meta.get(key)
        return '未知' if item is None else '是' if item else '否'
    parts = [f"instance_id={value('instance_id')}", f"project={value('project')}",
             f"状态={value('state')}", f"总行数={value('total_rows')}",
             f"下载行数={value('downloaded_rows')}", f"展示行数={value('displayed_rows')}",
             f"截断={flag('truncated')}", f"受限={flag('restricted')}", f"耗时={value('elapsed_s')}s"]
    if meta.get('scanned') is not None:
        parts.append(f"扫描输入≈{meta['scanned']}")
    if meta.get('run_dir'):
        parts.append(f"记录={meta['run_dir']}")
    # LogView is a signed URL. Complete address stays in the local state record.
    if meta.get('logview'):
        parts.append('LogView=见本地记录')
    from pyodps3_runtime import redact
    return redact('-- 运行元信息: ' + ' | '.join(parts))


def execute_or_report(odps: ODPS, sql: str = None, **options):
    from sql_execution import ExecutionError
    try:
        return run_select_df(odps, sql, **options)
    except ExecutionError as error:
        from pyodps3_runtime import redact
        print('[查询未完成] ' + redact(str(error)), file=sys.stderr)
        print(format_run_meta(error.meta), file=sys.stderr)
        raise SystemExit(error.exit_code) from None


def df_to_markdown(df, max_rows: int) -> str:
    shown = df.head(max_rows)
    try:
        table = shown.to_markdown(index=False)
    except Exception:
        table = shown.to_string(index=False)
    return table + f"\n\n预览行数: {len(shown)}（远端总量见运行元信息）"


def _print_query_result(data, meta, max_rows):
    if isinstance(data, str):
        print(data)
    elif data is not None:
        print(df_to_markdown(data, max_rows=max_rows))
    if meta.get('output_path'):
        print(f"已完整导出 {meta.get('downloaded_rows')} 行至 {meta['output_path']}", file=sys.stderr)
    print(format_run_meta(meta), file=sys.stderr)


# ---------------------------------------------------------------------------
# 子命令实现
# ---------------------------------------------------------------------------
def cmd_list_tables(args):
    odps = get_odps()
    pattern = (args.pattern or "").lower()
    names = []
    for t in odps.list_tables():
        name = t.name
        if pattern and pattern not in name.lower():
            continue
        names.append(name)
    if not names:
        print(f"未找到匹配 '{args.pattern}' 的表。" if args.pattern else "未找到任何表。")
        return
    print(f"匹配到 {len(names)} 张表" + (f"（pattern='{args.pattern}'）" if args.pattern else "") + "：")
    for n in names:
        print(f"  {n}")


def cmd_desc(args):
    odps = get_odps()
    table = args.table
    if not odps.exist_table(table):
        print(f"表不存在：{table}", file=sys.stderr)
        sys.exit(1)
    t = odps.get_table(table)
    schema = t.table_schema

    print(f"表: {table}")
    if getattr(t, "comment", None):
        print(f"注释: {t.comment}")
    print("\n普通字段:")
    for col in schema.columns:
        comment = f"  -- {col.comment}" if col.comment else ""
        print(f"  {col.name}\t{str(col.type)}{comment}")

    if schema.partitions:
        print("\n分区字段:")
        for p in schema.partitions:
            comment = f"  -- {p.comment}" if p.comment else ""
            print(f"  {p.name}\t{str(p.type)}{comment}")
        print("\n提示: 这是分区表，查询时务必按分区过滤，例如 "
              f"WHERE {schema.partitions[0].name}=MAX_PT('{table}')")
    else:
        print("\n（非分区表）")


def cmd_partitions(args):
    odps = get_odps()
    table = args.table
    if not odps.exist_table(table):
        print(f"表不存在：{table}", file=sys.stderr)
        sys.exit(1)
    t = odps.get_table(table)
    if not t.table_schema.partitions:
        print(f"{table} 是非分区表。")
        return
    parts = [str(p.name) for p in t.partitions]
    if not parts:
        print(f"{table} 暂无分区数据。")
        return
    parts_sorted = sorted(parts)
    latest = parts_sorted[-1]
    tail = parts_sorted[-args.n:]
    print(f"{table} 共 {len(parts_sorted)} 个分区，最近 {len(tail)} 个：")
    for p in tail:
        flag = "  <-- 最新" if p == latest else ""
        print(f"  {p}{flag}")


def cmd_sample(args):
    project = getattr(args, 'project', None)
    odps = get_odps(project=project) if project is not None else get_odps()
    table = args.table
    if not odps.exist_table(table):
        print(f"表不存在：{table}", file=sys.stderr)
        sys.exit(1)
    t = odps.get_table(table)
    n = args.n
    if t.table_schema.partitions:
        pcol = t.table_schema.partitions[0].name
        sql = f"SELECT * FROM {table} WHERE {pcol}=MAX_PT('{table}') LIMIT {n}"
    else:
        sql = f"SELECT * FROM {table} LIMIT {n}"
    print(f"-- 采样 SQL: {sql}", file=sys.stderr)
    data, meta = execute_or_report(odps, sql, max_rows=n, wait_timeout=getattr(args, "wait_timeout", 600))
    _print_query_result(data, meta, n)


# ---------------------------------------------------------------------------
# UDF / 资源读取（只读：排查时读懂自定义函数的实现，不执行、不改）
# ---------------------------------------------------------------------------
# 审查任务 SQL 常遇到非内建函数调用（如 greedy_session(...)），光看调用点猜不出它在算什么。
# 这组命令把 UDF 的注册信息（AS 类名 / USING 资源）和**实现源码**拉出来读：Python UDF 的源码
# 在它 USING 的 .py 资源里，Java UDF 在二进制 jar 里（无源码可读），嵌入式/SQL 函数则内联在
# Function.code。读取走 pyodps 的只读接口，不触碰 SQL 引擎、不调用函数本身。

# 能当文本读出源码的资源类型（其余如 JAR/ARCHIVE 是二进制，只标注不读内容）
_TEXT_RESOURCE_TYPES = {"PY", "FILE"}


def _resource_type_name(res) -> str:
    """取资源类型名（PY/FILE/JAR/ARCHIVE/TABLE/...），取不到返回 UNKNOWN。"""
    t = getattr(res, "type", None)
    return getattr(t, "name", str(t)) if t is not None else "UNKNOWN"


def _read_resource_text(res):
    """按资源类型读出文本内容，返回 (text, note)。

    - PY/FILE 文本资源 → (源码字符串, None)
    - JAR/ARCHIVE 二进制 → (None, 说明)：源码不在资源文件里（如 Java UDF 编译进了 jar）
    - TABLE 资源 → (None, 引用的表名)
    - 读取异常 → (None, 错误说明)，元信息式降级，不抛出
    """
    rtype = _resource_type_name(res)
    if rtype == "TABLE":
        src = _safe(lambda: res.get_source_table())
        return None, f"表资源，引用表：{src}" if src else "表资源（引用的表名取不到）"
    if rtype not in _TEXT_RESOURCE_TYPES:
        return None, f"{rtype} 资源为二进制（如 Java UDF 的 jar），源码不在资源文件内，无法以文本读取"
    try:
        with res.open(mode="r") as f:
            return f.read(), None
    except Exception as e:
        return None, f"（无法以文本读取该资源：{e}）"


def cmd_list_functions(args):
    odps = get_odps()
    pattern = (args.pattern or "").lower()
    names = []
    for fn in odps.list_functions():
        name = fn.name
        if pattern and pattern not in name.lower():
            continue
        names.append(name)
    if not names:
        print(f"未找到匹配 '{args.pattern}' 的函数。" if args.pattern else "未找到任何自定义函数。")
        return
    print(f"匹配到 {len(names)} 个函数" + (f"（pattern='{args.pattern}'）" if args.pattern else "") + "：")
    for n in names:
        print(f"  {n}")


def cmd_func(args):
    odps = get_odps()
    name = args.name
    if not odps.exist_function(name):
        print(f"函数不存在：{name}（用 list-functions 核对真实函数名）", file=sys.stderr)
        sys.exit(1)
    fn = odps.get_function(name)

    print(f"函数: {name}")
    if getattr(fn, "owner", None):
        print(f"owner: {fn.owner}")
    ctime = _safe(lambda: fn.creation_time)
    if ctime:
        print(f"创建时间: {ctime}")
    if getattr(fn, "class_type", None):
        print(f"AS (类名/函数体): {fn.class_type}")
    lang = _safe(lambda: fn.program_language)
    if lang:
        print(f"语言: {lang}")
    is_sql = _safe(lambda: fn.is_sql_function)
    if is_sql:
        print("类型: SQL/嵌入式函数")

    resources = _safe(lambda: list(fn.resources)) or []
    if resources:
        print("\nUSING 资源:")
        for res in resources:
            print(f"  {res.name}\t[{_resource_type_name(res)}]")

    # 嵌入式/SQL 函数：实现内联在 code
    code = _safe(lambda: fn.code)
    if code:
        print("\n--- 函数实现 (embedded code) ---")
        print(code)

    # 普通 UDF：实现源码在 USING 的文本资源里
    text_sources = []
    for res in resources:
        text, note = _read_resource_text(res)
        if text is not None:
            text_sources.append((res.name, text))
        elif note:
            print(f"\n[{res.name}] {note}", file=sys.stderr)

    if not text_sources and not code:
        print("\n（未找到可读取的源码：可能是 Java UDF（jar 二进制）或无关联资源）", file=sys.stderr)

    if args.save:
        _save_sources(args.save, text_sources, code)
        return

    for res_name, text in text_sources:
        print(f"\n--- 源码: {res_name} ---")
        print(text)


def cmd_resource(args):
    odps = get_odps()
    name = args.name
    if not odps.exist_resource(name):
        print(f"资源不存在：{name}", file=sys.stderr)
        sys.exit(1)
    res = odps.get_resource(name)
    text, note = _read_resource_text(res)
    if text is None:
        print(f"[{name}] {note}", file=sys.stderr)
        sys.exit(1)
    if args.save:
        with open(args.save, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"已保存资源 {name} 至 {args.save}")
    else:
        print(text)


def _save_sources(path, text_sources, code):
    """把 UDF 源码落盘：单一来源直接写；多来源/含 code 时加分隔标题汇总到一个文件。"""
    parts = []
    if code:
        parts.append(f"-- embedded code --\n{code}")
    for res_name, text in text_sources:
        parts.append(f"-- 源码: {res_name} --\n{text}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n\n".join(parts))
    print(f"已保存 {len(parts)} 份源码至 {path}")


def cmd_sql(args):
    instance_id = getattr(args, 'instance_id', None)
    if sum(value is not None for value in (args.query, args.file, instance_id)) != 1:
        raise ValueError('请用 -q、-f 或 --instance-id 三选一提供查询或恢复标识。')
    if args.save is not None and Path(args.save).suffix.lower() not in ('.csv', '.xlsx'):
        raise ValueError('--save 只支持 .csv 或 .xlsx')
    sql = None
    if instance_id is None:
        sql = Path(args.file).read_text(encoding='utf-8-sig') if args.file is not None else args.query
        assert_readonly(sql)
    project = getattr(args, 'project', None)
    odps = get_odps(project=project) if project is not None else get_odps()
    # Recovery reads an already-submitted instance; never rebuild or resubmit its SQL.
    if sql is not None and not args.allow_full_scan:
        issues = check_partition_filters(odps, sql)
        if issues:
            print('[分区过滤告警] 以下关系的分区过滤缺失或未知：', file=sys.stderr)
            for issue in issues:
                detail = f"，缺过滤 {issue['missing']}" if issue['missing'] else ''
                print(f"  - {issue['table']}：{issue['status']}，{issue['reason']}{detail}", file=sys.stderr)
            if args.strict:
                print('[--strict] 已拒绝执行。确认全扫描范围后可显式使用 --allow-full-scan。', file=sys.stderr)
                raise SystemExit(3)
    data, meta = execute_or_report(odps, sql, instance_id=instance_id, max_rows=args.max_rows,
        wait_timeout=getattr(args, 'wait_timeout', 600), save=args.save,
        batch_size=getattr(args, 'batch_size', 10000))
    _print_query_result(data, meta, args.max_rows)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def nonempty(value):
    text = value.strip()
    if not text:
        raise argparse.ArgumentTypeError('标识不能为空')
    return text


def positive_integer(value):
    try:
        number = int(value)
    except (ValueError, TypeError):
        raise argparse.ArgumentTypeError('需要正整数') from None
    if number <= 0:
        raise argparse.ArgumentTypeError('需要正整数')
    return number


def build_parser():
    p = argparse.ArgumentParser(
        description="MaxCompute 数仓排查取数辅助工具（只读）",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("list-tables", help="列出表（可按名称子串过滤）")
    sp.add_argument("pattern", nargs="?", default=None, help="名称子串过滤")
    sp.set_defaults(func=cmd_list_tables)

    sp = sub.add_parser("desc", help="查看表字段与分区")
    sp.add_argument("table")
    sp.set_defaults(func=cmd_desc)

    sp = sub.add_parser("partitions", help="查看最近分区与最新分区")
    sp.add_argument("table")
    sp.add_argument("-n", type=int, default=20, help="显示最近 N 个分区，默认 20")
    sp.set_defaults(func=cmd_partitions)

    sp = sub.add_parser("sample", help="按最新分区采样几行")
    sp.add_argument("table")
    sp.add_argument("-n", type=positive_integer, default=10, help="采样行数，默认 10")
    sp.add_argument("--project", type=nonempty, help="本次项目；默认配置项目")
    sp.add_argument("--wait-timeout", type=positive_integer, default=600, help="查询等待秒数，默认 600；超时不取消")
    sp.set_defaults(func=cmd_sample)

    sp = sub.add_parser("list-functions", help="列出自定义函数 UDF（可按名称子串过滤）")
    sp.add_argument("pattern", nargs="?", default=None, help="名称子串过滤")
    sp.set_defaults(func=cmd_list_functions)

    sp = sub.add_parser("func", help="读取 UDF 的注册信息与实现源码")
    sp.add_argument("name")
    sp.add_argument("--save", help="把源码落盘到该路径")
    sp.set_defaults(func=cmd_func)

    sp = sub.add_parser("resource", help="读取单个资源文件的文本内容")
    sp.add_argument("name")
    sp.add_argument("--save", help="把资源内容落盘到该路径")
    sp.set_defaults(func=cmd_resource)

    sp = sub.add_parser("sql", help="执行只读 SQL")
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("-q", "--query", help="行内 SQL")
    g.add_argument("-f", "--file", help="SQL 文件路径")
    g.add_argument("--instance-id", type=nonempty, help="恢复指定实例；不提交新 SQL")
    sp.add_argument("--project", type=nonempty, help="本次提交或恢复所属项目；默认配置项目")
    sp.add_argument("--wait-timeout", type=positive_integer, default=600, help="查询等待秒数，默认 600；超时不取消")
    sp.add_argument("--batch-size", type=positive_integer, default=10000, help="完整导出每批行数，默认 10000")
    sp.add_argument("--save", help="落盘路径（.csv 或 .xlsx）")
    sp.add_argument("--max-rows", type=positive_integer, default=DEFAULT_MAX_PRINT_ROWS,
                    help=f"打印行数上限，默认 {DEFAULT_MAX_PRINT_ROWS}")
    sp.add_argument("--strict", action="store_true",
                    help="把分区过滤告警升级为硬拦截（默认只告警不拦截）")
    sp.add_argument("--allow-full-scan", action="store_true",
                    help="跳过分区过滤检查（确实需要全表扫描时用）")
    sp.set_defaults(func=cmd_sql)

    return p


def main():
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except (ValueError, OSError) as e:
        from pyodps3_runtime import redact
        print(f"[错误] {redact(str(e))}", file=sys.stderr)
        sys.exit(3)
    except KeyboardInterrupt:
        print("[已停止本地操作] 未自动取消或重新提交；请按上方记录恢复或先核实提交状态。", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()

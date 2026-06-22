#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
mc_query.py — MaxCompute / ODPS 数仓排查取数辅助工具

提供一组只读命令行子命令，帮助先探查表结构、再编写并执行 MaxCompute SQL：

    python mc_query.py list-tables [pattern]        # 找表
    python mc_query.py desc <table>                 # 看字段 + 分区
    python mc_query.py partitions <table> [-n 20]   # 看最近分区 + 最新分区
    python mc_query.py sample <table> [-n 10]       # 按最新分区采样几行
    python mc_query.py sql -q "<inline sql>"        # 执行只读 SQL
    python mc_query.py sql -f query.sql --save out.xlsx

连接配置默认沿用项目现有凭证，可用环境变量覆盖：
    ODPS_ACCESS_ID / ODPS_SECRET / ODPS_PROJECT / ODPS_ENDPOINT / ODPS_TUNNEL_ENDPOINT

安全：所有 SQL 执行前做只读校验，命中写操作（INSERT/UPDATE/DELETE/DROP/
ALTER/CREATE/TRUNCATE/MERGE/...）直接拒绝退出。本工具只用于排查取数，不改数。
"""

import argparse
import os
import re
import sys
import time
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

import config  # noqa: E402  # 连接凭证/端点的单一来源（可用环境变量覆盖）
from odps import ODPS  # noqa: E402
from odps.errors import ODPSError  # noqa: E402

# 打印结果时的默认行数上限（保护超大结果集，可用 --max-rows 调整）
DEFAULT_MAX_PRINT_ROWS = 200

# ---------------------------------------------------------------------------
# 只读校验
# ---------------------------------------------------------------------------
# 真正的只读保证来自两道结构性检查：① 单语句（按 ; 切分，多于一条直接拒）② 首词必须是
# 只读词。下面这份写动词黑名单只是纵深防御，它唯一不可或缺的价值是堵
# `WITH cte AS (...) INSERT OVERWRITE ...` 这种「首词是 WITH、语句体却在写」的向量——
# 所以 INSERT 等真实写动词必须保留。反过来，它不该误伤和写动词同名的函数/列名：
# 像字符串函数 REPLACE(...)，或对安全零贡献、却易撞到列名的 SET/USE/ADD/LOAD/COPY/WRITE
# （后者要么只能作首词被①②拦下、要么撞到普通标识符），都不该进这份名单。
WRITE_KEYWORDS = [
    "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "TRUNCATE",
    "MERGE", "RENAME", "GRANT", "REVOKE", "UNLOAD", "MSCK", "PURGE", "RESTORE",
]
# 允许的首关键字（语句必须以其一开头）
READ_STARTERS = ["SELECT", "WITH", "DESC", "DESCRIBE", "SHOW", "EXPLAIN", "READ"]


def _scrub_for_check(sql: str) -> str:
    """单遍扫描：去注释 + 抹空字符串字面量与反引号标识符内容，供只读校验切分/查词用。

    只用于只读校验；真正执行用的是原始 SQL。这里只为「看清结构、不被串内字符或保留字
    列名误导」：注释抹成空格，字符串/反引号内容抹空，而 ; 括号 关键字等结构原样保留，
    所以拦截写操作的能力不变。

    为什么要单遍：两遍正则（先去注释 vs 先屏蔽串）都切不对——串里可能含 -- 或 /* */，
    注释里可能含引号。一次走完、带状态地处理，才能同时正确应对
    `remark='a--b'`（串内注释符）、`/* it's fine */ SELECT ...`（注释内引号）、
    以及 `update`/`set` 这类反引号引用的保留字列名。
    """
    out, i, n = [], 0, len(sql)
    while i < n:
        two = sql[i:i + 2]
        if two == "--":                      # 行注释 → 抹到行尾
            j = sql.find("\n", i)
            if j == -1:
                break
            out.append(" ")
            i = j
            continue
        if two == "/*":                      # 块注释 → 抹掉
            j = sql.find("*/", i + 2)
            out.append(" ")
            i = n if j == -1 else j + 2
            continue
        c = sql[i]
        if c in "'\"":                       # 字符串字面量 → 抹空（处理 \ 转义与 '' 双写）
            q = c
            out.append(q)
            i += 1
            while i < n:
                if sql[i] == "\\" and i + 1 < n:
                    i += 2
                    continue
                if sql[i] == q:
                    if q == "'" and sql[i + 1:i + 2] == "'":
                        i += 2          # MaxCompute 用 '' 表示一个单引号
                        continue
                    break
                i += 1
            out.append(q)
            i += 1
            continue
        if c == "`":                         # 反引号标识符（保留字列名如 `update`）→ 抹空
            out.append("`")
            i += 1
            while i < n and sql[i] != "`":
                i += 1
            out.append("`")
            i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out).strip()


def assert_readonly(sql: str) -> None:
    """只读校验：不通过则抛 ValueError。"""
    scrubbed = _scrub_for_check(sql)
    if not scrubbed:
        raise ValueError("SQL 为空。")

    # 只支持单条语句，避免 "SELECT ...; DROP ..." 绕过
    statements = [s for s in (scrubbed.rstrip(";").split(";")) if s.strip()]
    if len(statements) > 1:
        raise ValueError("一次只允许执行一条 SQL 语句（检测到多条，可能含写操作）。")

    upper = scrubbed.upper()
    first_word = re.match(r"^\s*([A-Z]+)", upper)
    if not first_word or first_word.group(1) not in READ_STARTERS:
        raise ValueError(
            f"只允许只读查询（{'/'.join(READ_STARTERS)}）。检测到的起始关键字："
            f"{first_word.group(1) if first_word else '<空>'}"
        )

    for kw in WRITE_KEYWORDS:
        # KEYWORD( 是函数调用（如 TRUNCATE(x)），不算写语句；真正的写语句形如
        # KEYWORD <空格> ...（如 INSERT OVERWRITE、DROP TABLE），用负向预查区分二者。
        if re.search(r"\b" + kw + r"\b(?!\s*\()", upper):
            raise ValueError(f"检测到写操作关键字 `{kw}`，已拒绝执行（本工具仅只读）。")


# ---------------------------------------------------------------------------
# 分区过滤静态检查（痛点 4）
# ---------------------------------------------------------------------------
# 只读校验只管「是不是写操作」，不管「扫了多少分区」。漏分区过滤的查询会全表扫描——又慢又贵，
# 是排查时最易踩的雷，尤其是把多 tmp 链路重构成的长 WITH（FROM 里十几张大表，漏一张就烧钱）。
# 下面这组纯函数在执行前做一道 best-effort 静态体检：找出 FROM/JOIN 的真实分区表、检查其分区列
# 有没有在查询里作为过滤出现，缺了就提示。它是「提早、清晰地报警」，不追求 100% 精确——
# 宁可漏报（少数复杂查询没拦住，运行时 MaxCompute 一般也会对全表扫描兜底报错），不可误伤
# （把 CTE/别名/子查询误判成缺过滤的表，逼用户加无意义的过滤）。


def _mask_literals(sql: str) -> str:
    """返回与原串**等长**的掩码串：注释与字符串/反引号字面量的内容替换为空格，但保留分号、
    括号、关键字、标识符等结构字符**与原始字符偏移**。

    与 `_scrub_for_check` 的区别：那个会把注释/串折叠（长度改变），只够做切分与查词；这个保长，
    所以可以拿掩码里的位置直接回原文切片——语句切分、表引用提取、build_validation_sql.py 的
    tmp 链转写都依赖这点。两者状态机一致：都不会被串内的 `;`/`--`/写词，或反引号保留字列名误导。
    """
    out = list(sql)
    i, n = 0, len(sql)
    while i < n:
        two = sql[i:i + 2]
        if two == "--":                      # 行注释 → 抹到行尾（保留换行）
            j = sql.find("\n", i)
            end = n if j == -1 else j
            for k in range(i, end):
                out[k] = " "
            i = end
            continue
        if two == "/*":                      # 块注释 → 抹掉（保留长度）
            j = sql.find("*/", i + 2)
            end = n if j == -1 else j + 2
            for k in range(i, end):
                out[k] = " "
            i = end
            continue
        c = sql[i]
        if c in "'\"":                       # 字符串字面量 → 内容抹空，保留引号
            q = c
            i += 1
            while i < n:
                if sql[i] == "\\" and i + 1 < n:
                    out[i] = " "
                    out[i + 1] = " "
                    i += 2
                    continue
                if sql[i] == q:
                    if q == "'" and sql[i + 1:i + 2] == "'":
                        out[i] = " "
                        out[i + 1] = " "
                        i += 2
                        continue
                    break
                out[i] = " "
                i += 1
            i += 1                           # 跳过闭合引号（保留）
            continue
        if c == "`":                         # 反引号标识符内容 → 抹空，保留反引号
            i += 1
            while i < n and sql[i] != "`":
                out[i] = " "
                i += 1
            i += 1
            continue
        i += 1
    return "".join(out)


def extract_table_refs(sql: str):
    """从 SQL 里提取 (CTE 名集合, FROM/JOIN 后的真实表引用列表)。基于掩码，避免被串/注释干扰。

    - CTE 名：`WITH a AS (...), b AS (...)` 里的 a/b——用来从表引用里排除掉（它们不是物理表）。
    - 表引用：`FROM x` / `JOIN x` 后紧跟的标识符；后面是 `(` 的是子查询，不算表。
    宁可把可疑的当成 CTE 多排除（顶多漏报），也不把 CTE 当成表（会误伤）。
    """
    mask = _mask_literals(sql)
    cte_names = {m.group(1).lower() for m in re.finditer(r"\b([A-Za-z_]\w*)\s+AS\s*\(", mask)}
    refs = []
    for m in re.finditer(r"\b(?:FROM|JOIN)\s+([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?)", mask):
        refs.append(m.group(1))
    # 去重保序
    seen, uniq = set(), []
    for r in refs:
        if r.lower() not in seen:
            seen.add(r.lower())
            uniq.append(r)
    return cte_names, uniq


def check_partition_filters(odps, sql):
    """对每张被引用的真实分区表，检查其分区列是否在查询里出现过过滤；返回告警列表。

    判定「出现过过滤」用的是粗粒度启发式：分区列名只要在掩码里任意位置出现即视为已过滤
    （多表查询里各表常各带自己的 ds 过滤，精确绑定到具体表代价高且易误伤，这里从宽）。
    每条告警是 (表名, 全部分区列, 缺失的分区列)。表不存在/解析不到的当作 CTE/别名跳过。
    """
    mask = _mask_literals(sql)
    cte_names, refs = extract_table_refs(sql)
    warnings_out = []
    for t in refs:
        if t.lower() in cte_names:
            continue
        try:
            if not odps.exist_table(t):
                continue
            tbl = odps.get_table(t)
        except Exception:
            continue
        parts = tbl.table_schema.partitions
        if not parts:
            continue
        pcols = [p.name for p in parts]
        missing = [pc for pc in pcols
                   if not re.search(r"\b" + re.escape(pc) + r"\b", mask, re.I)]
        if missing:
            warnings_out.append((t, pcols, missing))
    return warnings_out


# ---------------------------------------------------------------------------
# ODPS 连接 & 取数
# ---------------------------------------------------------------------------
def get_odps() -> ODPS:
    return ODPS(
        access_id=config.ACCESS_ID,
        secret_access_key=config.SECRET,
        project=config.ODPS_PROJECT,
        endpoint=config.ODPS_ENDPOINT,
        tunnel_endpoint=config.ODPS_TUNNEL_ENDPOINT,
    )


def _safe(fn):
    """跑一个可能失败的取元信息动作，失败就返回 None（元信息是锦上添花，不该拖垮主流程）。"""
    try:
        return fn()
    except Exception:
        return None


def _input_bytes_human(inst) -> str:
    """best-effort 从 task summary 的 Inputs 抠出扫描输入字节数（各输入分区字节求和），转人类可读。

    Inputs 形如 {'proj.table/ds=20260620': [行数, 字节数, ...], ...}——取每项的第 2 个元素求和。
    取不到/解析不了返回 None（元信息是锦上添花，绝不拖垮主流程）。
    """
    def _compute():
        names = inst.get_task_names()
        summary = inst.get_task_summary(names[0])
        inputs = dict(summary).get("Inputs") or {}
        total = sum(v[1] for v in inputs.values() if isinstance(v, (list, tuple)) and len(v) > 1)
        return total

    total = _safe(_compute)
    if not total:
        return None
    n = float(total)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f}{unit}"
        n /= 1024


def run_select_df(odps: ODPS, sql: str):
    """执行只读 SQL，返回 (DataFrame, meta)。meta 含 instance_id/行数/耗时/logview/扫描量，
    供调用方打印「运行元信息」——让子代理回传的结论可被核验（确属真跑、扫了多少）。出错时抛 ODPSError。"""
    assert_readonly(sql)
    t0 = time.time()
    inst = odps.execute_sql(sql)
    with inst.open_reader(tunnel=True) as reader:
        df = reader.to_pandas(n_process=8)
    meta = {
        "instance_id": getattr(inst, "id", None),
        "rows": len(df),
        "elapsed_s": round(time.time() - t0, 1),
        "logview": _safe(lambda: inst.get_logview_address()),
        "scanned": _input_bytes_human(inst),
    }
    return df, meta


def format_run_meta(meta: dict) -> str:
    """把 meta 拼成一行紧凑的「运行元信息」footer。"""
    parts = [f"instance_id={meta.get('instance_id')}"]
    if meta.get("scanned"):
        parts.append(f"扫描输入≈{meta['scanned']}")
    parts.append(f"输出行数={meta.get('rows')}")
    parts.append(f"耗时={meta.get('elapsed_s')}s")
    if meta.get("logview"):
        parts.append(f"logview={meta['logview']}")
    return "-- 运行元信息: " + " | ".join(parts)


def execute_or_report(odps: ODPS, sql: str):
    """执行 SQL；若 MaxCompute 返回错误，打印干净、可据此修正的错误信息并以退出码 4 退出。

    这样调用方（写 SQL 的 Claude）看到的是结构化的报错+出错 SQL，而不是一坨
    Python traceback，便于读懂错误、自己改 SQL 再重试。返回 (DataFrame, meta)。
    """
    try:
        return run_select_df(odps, sql)
    except ODPSError as e:
        print("[SQL 执行失败]", file=sys.stderr)
        print(f"错误信息: {e}", file=sys.stderr)
        print("出错的 SQL:", file=sys.stderr)
        print(sql.strip(), file=sys.stderr)
        print(
            "\n提示: 请根据上面的错误修改 SQL 后重试（常见原因与改法见 "
            "references/maxcompute_sql.md 的『常见报错对照』；列名/表名不确定时先用 desc/list-tables 核对）。",
            file=sys.stderr,
        )
        sys.exit(4)


def df_to_markdown(df, max_rows: int) -> str:
    total = len(df)
    shown = df.head(max_rows)
    try:
        table = shown.to_markdown(index=False)
    except Exception:
        # 没装 tabulate 时退回到普通字符串表格
        table = shown.to_string(index=False)
    note = ""
    if total > max_rows:
        note = f"\n\n（结果共 {total} 行，仅显示前 {max_rows} 行；如需全部请用 --save 落盘）"
    elif total == 0:
        note = "\n（查询返回 0 行）"
    return table + note + (f"\n\n行数: {total}" if total else "")


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
    odps = get_odps()
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
    print(f"-- 采样 SQL: {sql}\n")
    df, meta = execute_or_report(odps, sql)
    print(df_to_markdown(df, max_rows=n))
    print("\n" + format_run_meta(meta))


def cmd_sql(args):
    if bool(args.query) == bool(args.file):
        print("请用 -q \"<sql>\" 或 -f <file.sql> 二选一提供 SQL。", file=sys.stderr)
        sys.exit(2)

    if args.file:
        with open(args.file, encoding="utf-8") as f:
            sql = f.read()
    else:
        sql = args.query

    try:
        assert_readonly(sql)
    except ValueError as e:
        print(f"[只读校验未通过] {e}", file=sys.stderr)
        sys.exit(3)

    odps = get_odps()

    # 分区过滤静态体检（痛点 4）：默认只告警，不拦截；--strict 升级为拦截，--allow-full-scan 静音。
    if not args.allow_full_scan:
        warns = check_partition_filters(odps, sql)
        if warns:
            print("[分区过滤告警] 以下分区表疑似缺少分区过滤，可能触发全表扫描（又慢又贵）：",
                  file=sys.stderr)
            for t, pcols, missing in warns:
                print(f"  - {t}：分区列 {pcols}，缺过滤 {missing}"
                      f"（按 {missing[0]}=MAX_PT('{t}') 或具体分区补上）", file=sys.stderr)
            if args.strict:
                print("[--strict] 存在分区过滤告警，已拒绝执行。确认要全表扫描请加 --allow-full-scan。",
                      file=sys.stderr)
                sys.exit(3)

    df, meta = execute_or_report(odps, sql)

    if args.save:
        out = args.save
        if out.lower().endswith(".csv"):
            df.to_csv(out, index=False, encoding="utf-8-sig")
        else:
            df.to_excel(out, index=False)
        print(f"已保存 {len(df)} 行至 {out}")
    else:
        print(df_to_markdown(df, max_rows=args.max_rows))
    print("\n" + format_run_meta(meta))


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
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
    sp.add_argument("-n", type=int, default=10, help="采样行数，默认 10")
    sp.set_defaults(func=cmd_sample)

    sp = sub.add_parser("sql", help="执行只读 SQL")
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("-q", "--query", help="行内 SQL")
    g.add_argument("-f", "--file", help="SQL 文件路径")
    sp.add_argument("--save", help="落盘路径（.csv 或 .xlsx）")
    sp.add_argument("--max-rows", type=int, default=DEFAULT_MAX_PRINT_ROWS,
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
    except ValueError as e:
        print(f"[错误] {e}", file=sys.stderr)
        sys.exit(3)


if __name__ == "__main__":
    main()

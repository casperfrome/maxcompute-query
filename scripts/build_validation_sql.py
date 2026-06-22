#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
build_validation_sql.py — 把「在线数仓任务代码」改写成可只读验证的 SQL（本身不连库、不执行）

它解决两件事，对应 maxcompute-query skill 的两个痛点：

  inline   把含多个 tmp 中间表的任务正文（DROP/CREATE TABLE AS/INSERT OVERWRITE tmp 链路）
           机械转成**单条只读 `WITH ... SELECT`**，让你能用真实数据端到端验证任务逻辑，而不是
           人肉把链路重写一遍（人肉翻译正是最容易引入错误、让你验的是「脑补的等价版」的地方）。

  compare  生成「改前 vs 改后」双跑对照 SQL：FULL OUTER JOIN on 唯一键，分桶统计
           仅旧有 / 仅新有 / 键同值不同 / 完全一致——证明重构没把数据改坏的最硬证据。
           这条 SQL 的 NULL 判断和 FULL OUTER JOIN 极易手写出错，故用生成器固化。

两个子命令都**只吐 SQL 文本**。生成物要验证时，喂给 `mc_query.py sql -f`（仍走只读校验+分区体检）。

  python build_validation_sql.py inline task.sql                     # 转成单条 WITH 打到 stdout
  python build_validation_sql.py inline task.sql --list-vars         # 只列出 ${...} 变量占位符
  python build_validation_sql.py inline task.sql --var bizdate=20260620 --save inlined.sql
  python build_validation_sql.py inline task.sql --target tmp_xxx     # 只验证到某张中间表
  python build_validation_sql.py compare old.sql new.sql --key request_id,file_id,node_oper \
         --measure operate_time,if_read

设计取舍（保守转写，绝不静默猜）：能干净机械转的（线性 CTAS / INSERT OVERWRITE tmp 链）自动转；
吃不准的片段（同名 tmp 多次写入、动态分区写 tmp、引用了尚未定义的 tmp=疑似依赖顺序倒置、
非 SELECT 体写语句）**在 stderr 显式标记出来让人确认**，并尽量按原文保留以忠实复现，不替你拍板。
"""

import argparse
import os
import re
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mc_query import _mask_literals  # noqa: E402  # 复用「等长掩码」做结构感知的切分/查词


# ---------------------------------------------------------------------------
# 语句切分 / 结构工具（都基于等长掩码，按原文偏移切片）
# ---------------------------------------------------------------------------
def split_statements(sql: str):
    """按顶层 `;`（不在字符串/注释/括号内）把 SQL 切成语句列表，丢掉纯注释/空白语句。返回原文片段。"""
    mask = _mask_literals(sql)
    stmts, start = [], 0
    for m in re.finditer(r";", mask):
        idx = m.start()
        if mask[start:idx].strip():
            stmts.append(sql[start:idx].strip())
        start = idx + 1
    if mask[start:].strip():
        stmts.append(sql[start:].strip())
    return stmts


def _match_paren(mask: str, i: int) -> int:
    """mask[i] 是 '('，返回与之匹配的 ')' 的下标；找不到返回 -1。"""
    depth, n = 0, len(mask)
    while i < n:
        if mask[i] == "(":
            depth += 1
        elif mask[i] == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def find_vars(sql: str):
    """找出所有 ${name} 变量占位符（去重保序）。"""
    seen, out = set(), []
    for m in re.finditer(r"\$\{(\w+)\}", sql):
        if m.group(1) not in seen:
            seen.add(m.group(1))
            out.append(m.group(1))
    return out


def apply_vars(sql: str, var_map: dict) -> str:
    for k, v in var_map.items():
        sql = sql.replace("${" + k + "}", v)
    return sql


# ---------------------------------------------------------------------------
# inline：tmp 链路 → 单条只读 WITH
# ---------------------------------------------------------------------------
class Cte:
    __slots__ = ("name", "body", "written", "had_partition")

    def __init__(self, name, body):
        self.name = name
        self.body = body          # None 表示是建表骨架，待后续 INSERT 填充
        self.written = 0          # 被 INSERT 写入的次数
        self.had_partition = False


def inline_task(sql: str, target: str = None):
    """把任务正文转成单条 WITH。返回 (with_sql, warnings, cte_names, final_target)。"""
    stmts = split_statements(sql)
    ctes = []                     # 有序 Cte 列表
    by_name = {}                  # name.lower() -> ctes 下标
    warnings = []
    final_target = None
    final_body = None

    re_ctas = re.compile(
        r"(?is)^\s*create\s+table\s+(?:if\s+not\s+exists\s+)?"
        r"([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?)\b(.*?)\bas\b")
    re_create_skel = re.compile(
        r"(?is)^\s*create\s+table\s+(?:if\s+not\s+exists\s+)?"
        r"([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?)\b")
    re_insert = re.compile(
        r"(?is)^\s*insert\s+(overwrite|into)\s+table\s+"
        r"([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?)\s*(partition\s*\([^)]*\))?\s*")
    re_drop = re.compile(r"(?is)^\s*drop\b")

    def short(name):
        return name.split(".")[-1]

    for stmt in stmts:
        mstmt = _mask_literals(stmt)

        if re_drop.match(mstmt):
            continue                              # 清理语句，忽略

        m = re_ctas.match(mstmt)
        if m:
            name = m.group(1)
            body = stmt[m.end():].strip()
            ctes.append(Cte(name, body))
            by_name[short(name).lower()] = len(ctes) - 1
            continue

        m = re_insert.match(mstmt)
        if m:
            into_kind, tgt, part = m.group(1), m.group(2), m.group(3)
            body = stmt[m.end():].strip()
            key = short(tgt).lower()
            if key in by_name:                    # 写的是某张 tmp
                cte = ctes[by_name[key]]
                if cte.body is None:              # 给骨架填充 body
                    cte.body = body
                    cte.written = 1
                else:
                    cte.written += 1
                    warnings.append(
                        f"⚠ tmp `{short(tgt)}` 被多次写入（第 {cte.written} 次，"
                        f"{into_kind.upper()}）——CTE 无法表达 append/多次覆盖，已用最后一次的 body，"
                        f"请人工确认这里是否真要合并多段写入。")
                    cte.body = body
                if part:
                    cte.had_partition = True
                    warnings.append(
                        f"⚠ tmp `{short(tgt)}` 是带 PARTITION 的写入——CTE 不表达分区语义，"
                        f"已忽略 PARTITION 子句，若依赖动态分区结果请人工确认。")
                continue
            # 写的是非 tmp 目标 → 最终产出
            if final_target is not None:
                warnings.append(
                    f"⚠ 检测到多个最终输出目标（已有 `{short(final_target)}`，又见 `{short(tgt)}`），"
                    f"已用最后一个作收尾，请人工确认。")
            final_target, final_body = tgt, body
            continue

        if re_create_skel.match(mstmt):           # 无 AS 的建表骨架，等后续 INSERT 填充
            name = re_create_skel.match(mstmt).group(1)
            ctes.append(Cte(name, None))
            by_name[short(name).lower()] = len(ctes) - 1
            continue

        kw = re.match(r"(?is)^\s*([A-Za-z_]+)", mstmt)
        kw = kw.group(1).upper() if kw else "?"
        if kw in ("SELECT", "WITH"):              # 裸 SELECT/WITH，当作收尾
            final_target, final_body = None, stmt.strip()
            continue
        warnings.append(f"⚠ 跳过无法转写的语句（以 `{kw}` 开头）：{stmt[:60]}…")

    # 骨架始终没被填充 body 的，剔除并告警
    real = []
    for c in ctes:
        if c.body is None:
            warnings.append(f"⚠ tmp `{short(c.name)}` 只有建表骨架、没找到对应 INSERT，已忽略。")
        else:
            real.append(c)
    ctes = real
    by_name = {short(c.name).lower(): i for i, c in enumerate(ctes)}

    # 依赖顺序体检：某 CTE body 里引用了「定义在它之后」的 tmp → 疑似依赖顺序倒置的 bug
    for i, c in enumerate(ctes):
        mbody = _mask_literals(c.body)
        for tok in re.finditer(r"\b([A-Za-z_]\w*)\b", mbody):
            j = by_name.get(tok.group(1).lower())
            if j is not None and j > i:
                warnings.append(
                    f"⚠ tmp `{short(c.name)}` 在其定义处引用了尚未定义的 `{short(ctes[j].name)}`"
                    f"（定义更靠后）——疑似依赖顺序倒置：线上会读到该表上一批的旧数据。"
                    f"已按原文保留以忠实复现该 bug。")

    # 选定收尾
    if target:
        key = short(target).lower()
        if key not in by_name:
            raise ValueError(
                f"--target `{target}` 不在识别到的 tmp 列表里。可用：{[short(n) for n in cte_names]}")
        idx = by_name[key]
        ctes = ctes[:idx + 1]
        terminal = f"SELECT * FROM {ctes[idx].name}"
    else:
        if final_body is None:
            raise ValueError(
                "未找到最终输出语句（INSERT OVERWRITE 到非 tmp 目标或裸 SELECT）。"
                "若想验证到某张中间表，请用 --target <tmp名>。")
        terminal = final_body

    cte_names = [c.name for c in ctes]            # 反映最终（--target 切片后）实际入选的 CTE
    if not ctes:
        with_sql = terminal                       # 没有 tmp，原样就是只读查询
    else:
        defs = ",\n".join(f"{c.name} AS (\n{c.body}\n)" for c in ctes)
        with_sql = f"WITH\n{defs}\n{terminal}"
    return with_sql, warnings, cte_names, final_target


# ---------------------------------------------------------------------------
# compare：改前/改后双跑对照
# ---------------------------------------------------------------------------
def parse_with_query(sql: str):
    """把一段查询拆成 (cte_list[(name,body)], final_select)。裸 SELECT → ([], sql)。
    解析失败返回 None（调用方据此退回「嵌套」兜底形式）。"""
    sql = sql.strip().rstrip(";")
    mask = _mask_literals(sql)
    if not re.match(r"(?is)^\s*with\b", mask):
        return [], sql
    pos = re.match(r"(?is)^\s*with\b", mask).end()
    ctes = []
    while True:
        m = re.match(r"(?is)^\s*([A-Za-z_]\w*)\s*(?:\([^()]*\))?\s*as\s*\(", mask[pos:])
        if not m:
            return None
        name = m.group(1)
        open_idx = pos + m.end() - 1
        close_idx = _match_paren(mask, open_idx)
        if close_idx == -1:
            return None
        ctes.append((name, sql[open_idx + 1:close_idx].strip()))
        pos = close_idx + 1
        m2 = re.match(r"(?is)^\s*,", mask[pos:])
        if m2:
            pos += m2.end()
            continue
        return ctes, sql[pos:].strip()


def _prefix_idents(fragment: str, names: set, prefix: str) -> str:
    """把 fragment 里出现的、属于 names 的标识符（定义处和引用处）统一加前缀。基于掩码，不碰串/注释。"""
    mask = _mask_literals(fragment)
    spans = [(m.start(), m.end()) for m in re.finditer(r"\b[A-Za-z_]\w*\b", mask)
             if m.group(0).lower() in names]
    out = fragment
    for s, e in reversed(spans):
        out = out[:s] + prefix + out[s:e] + out[e:]
    return out


def _lift(sql: str, prefix: str):
    """把一段（可能是 WITH 的）查询提升成「带前缀的 CTE 定义片段 + 收尾别名」。
    返回 (defs_list[str], final_select_str)；解析不了就退回嵌套形式（defs 为空，final 为子查询）。"""
    parsed = parse_with_query(sql)
    if parsed is None:
        return [], f"(\n{sql.strip().rstrip(';')}\n)"   # 兜底：整体当子查询（可能触发嵌套 WITH，会告警）
    ctes, final = parsed
    names = {n.lower() for n, _ in ctes}
    defs = []
    for n, body in ctes:
        defs.append(f"{prefix}{n} AS (\n{_prefix_idents(body, names, prefix)}\n)")
    final = _prefix_idents(final, names, prefix)
    return defs, final


def build_compare(old_sql: str, new_sql: str, keys, measures):
    """生成改前/改后对照 SQL（单条只读语句）。"""
    o_defs, o_final = _lift(old_sql, "o_")
    n_defs, n_final = _lift(new_sql, "n_")

    nested_warn = ""
    if (not o_defs and o_final.startswith("(")) or (not n_defs and n_final.startswith("(")):
        nested_warn = ("-- ⚠ 有一侧无法解析为可合并的 WITH，已用子查询嵌套形式。"
                       "若 MaxCompute 对嵌套 WITH 报错，请先用 mc_query 跑通各侧、或手工展开。\n")

    all_defs = o_defs + [f"__old AS (\n{o_final}\n)"] + n_defs + [f"__new AS (\n{n_final}\n)"]
    with_block = "WITH\n" + ",\n".join(all_defs)

    on = " AND ".join(f"o.{k} = n.{k}" for k in keys)
    k0 = keys[0]
    if measures:
        diff = " OR ".join(
            f"((o.{m} IS NULL) <> (n.{m} IS NULL) OR o.{m} <> n.{m})" for m in measures)
        value_when = f"           WHEN {diff} THEN '键同值不同'\n"
        else_label = "完全一致"
    else:
        value_when = ""
        else_label = "键匹配(未比较值，--measure 可加上)"

    body = f"""{nested_warn}{with_block}
SELECT diff_type, COUNT(*) AS cnt
FROM (
  SELECT CASE
           WHEN o.{k0} IS NULL THEN '仅新增(新有旧无)'
           WHEN n.{k0} IS NULL THEN '仅旧有(旧无新有)'
{value_when}           ELSE '{else_label}'
         END AS diff_type
  FROM __old o
  FULL OUTER JOIN __new n ON {on}
) z
GROUP BY diff_type
ORDER BY cnt DESC"""
    return body


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _collect_vars(pairs):
    var_map = {}
    for p in pairs or []:
        if "=" not in p:
            print(f"[错误] --var 需写成 k=v，收到：{p}", file=sys.stderr)
            sys.exit(2)
        k, v = p.split("=", 1)
        var_map[k.strip()] = v
    return var_map


def _emit(sql, warnings, vars_left, save):
    for w in warnings:
        print(w, file=sys.stderr)
    if vars_left:
        print(f"\n⚠ 仍有未代入的变量占位符 {['${' + v + '}' for v in vars_left]}，"
              f"用 --var k=v 代入后才能执行（如 --var bizdate=20260620）。", file=sys.stderr)
    if save:
        with open(save, "w", encoding="utf-8") as f:
            f.write(sql)
        print(f"（已写出 {len(sql)} 字符至 {save}）", file=sys.stderr)
    else:
        print(sql)


def cmd_inline(args):
    with open(args.file, encoding="utf-8") as f:
        sql = f.read()

    if args.list_vars:
        vs = find_vars(sql)
        print("变量占位符：" + (", ".join("${" + v + "}" for v in vs) if vs else "（无）"))
        return

    with_sql, warnings, cte_names, final_target = inline_task(sql, target=args.target)
    if cte_names:
        print(f"识别到 {len(cte_names)} 张 tmp："
              f"{[n.split('.')[-1] for n in cte_names]}", file=sys.stderr)
    with_sql = apply_vars(with_sql, _collect_vars(args.var))
    _emit(with_sql, warnings, find_vars(with_sql), args.save)


def cmd_compare(args):
    with open(args.old, encoding="utf-8") as f:
        old_sql = f.read()
    with open(args.new, encoding="utf-8") as f:
        new_sql = f.read()
    keys = [k.strip() for k in args.key.split(",") if k.strip()]
    measures = [m.strip() for m in (args.measure or "").split(",") if m.strip()]
    if not keys:
        print("[错误] --key 不能为空，至少给一个唯一键列。", file=sys.stderr)
        sys.exit(2)
    sql = build_compare(old_sql, new_sql, keys, measures)
    sql = apply_vars(sql, _collect_vars(args.var))
    _emit(sql, [], find_vars(sql), args.save)


def build_parser():
    p = argparse.ArgumentParser(
        description="把在线数仓任务改写成可只读验证的 SQL（只生成文本，不连库、不执行）")
    sub = p.add_subparsers(dest="cmd", required=True)

    si = sub.add_parser("inline", help="tmp 链路任务正文 → 单条只读 WITH")
    si.add_argument("file", help="任务 SQL 文件（如 fetch_task_sql.py 落盘的 .sql）")
    si.add_argument("--target", help="只验证到某张中间 tmp（默认转到最终产出）")
    si.add_argument("--list-vars", action="store_true", help="只列出 ${...} 变量占位符后退出")
    si.add_argument("--var", action="append", help="代入变量，写成 k=v，可重复（如 --var bizdate=20260620）")
    si.add_argument("--save", help="把生成的 SQL 落盘到该路径")
    si.set_defaults(func=cmd_inline)

    sc = sub.add_parser("compare", help="改前/改后双跑对照 SQL（FULL OUTER JOIN on 唯一键）")
    sc.add_argument("old", help="改前逻辑的 SQL 文件（通常是 inline 旧任务的产物）")
    sc.add_argument("new", help="改后逻辑的 SQL 文件")
    sc.add_argument("--key", required=True, help="唯一键列，逗号分隔（如 request_id,file_id,node_oper）")
    sc.add_argument("--measure", help="要逐值对比的度量列，逗号分隔（可选，不给则只比键集合）")
    sc.add_argument("--var", action="append", help="代入变量，写成 k=v，可重复")
    sc.add_argument("--save", help="把生成的 SQL 落盘到该路径")
    sc.set_defaults(func=cmd_compare)

    return p


def main():
    args = build_parser().parse_args()
    try:
        args.func(args)
    except (ValueError, FileNotFoundError) as e:
        print(f"[错误] {e}", file=sys.stderr)
        sys.exit(3)


if __name__ == "__main__":
    main()

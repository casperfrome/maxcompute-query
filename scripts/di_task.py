#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""di_task.py — 识别并解读 DataWorks「数据集成·离线同步」任务（DataX 配置）

背景：`fetch_task_sql.py` 从 DataWorks 拉回的「任务代码」既可能是 ODPS SQL，也可能是
**数据集成离线同步节点**的 DataX JSON 配置（reader 源 → writer 目标）。后者直接吐原始
JSON 很难读，且 reader/writer 的 column 是**按位置一一对应**的，最容易错位却最难肉眼核对。

本模块只做三件事，纯文本处理、不连网（便于离线单测）：
  - detect_di_config(content)  判断一段任务代码是不是 DI 同步配置（是→返回解析后的 dict）
  - parse_di_config(data)      从配置里抽出「源 / 目标」结构
  - render_di_summary(data)    渲染成可读摘要：源 → 目标 + 写入模式 + 列映射对照(按位置)

只保留同步任务真正关心的四样：源、目标、写入模式、列映射；并发/脏数据阈值/资源组等
运行设置刻意不收录，避免摘要被次要信息淹没。

列映射审查的口径（避免狼来了）：
  - **列数不一致** → `⚠` 重点告警：按位置映射必然整体错位，几乎一定是 bug。
  - **列数一致但有列名不同** → 仅 `≠` 标注 + 一行 `ℹ` 说明：离线同步按位置映射、源目标改名
    很常见且多半是有意为之，这里只提示人工确认，不当成错误。
  - **完全同名一一对应** → `✓`。

只读：本模块不发起任何写操作，也不连库；要核对源/目标表真实行数请另走 mc_query / holo-query。
"""

import json


# ---------------------------------------------------------------------------
# 检测
# ---------------------------------------------------------------------------
def detect_di_config(content):
    """判断 content 是否为 DataX 离线同步配置；是则返回解析后的 dict，否则返回 None。

    判据：能 json.loads 成 dict，且 steps 里同时有 reader、writer 两类 step；或退一步，
    顶层 type=="job" 且 extend.formatType=="datax"。SQL 文本不会被解析成这种 dict，天然区分。
    """
    if not content:
        return None
    text = content.strip()
    if not text.startswith("{"):  # SQL/Shell 等不会以 { 开头，省一次解析
        return None
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None

    steps = data.get("steps")
    if isinstance(steps, list):
        cats = {s.get("category") for s in steps if isinstance(s, dict)}
        if "reader" in cats and "writer" in cats:
            return data

    extend = data.get("extend")
    if (
        data.get("type") == "job"
        and isinstance(extend, dict)
        and extend.get("formatType") == "datax"
    ):
        return data
    return None


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------
def _col_name(c):
    """列条目取名：DataX 多数是字符串，少数 reader/writer 用 {"name":..,"type":..} 对象。"""
    if isinstance(c, str):
        return c
    if isinstance(c, dict):
        return c.get("name") or c.get("column") or json.dumps(c, ensure_ascii=False)
    return str(c)


def _find_step(steps, category):
    for s in steps:
        if isinstance(s, dict) and s.get("category") == category:
            return s
    return None


def _as_list(v):
    if v is None:
        return []
    if isinstance(v, list):
        return v
    return [v]


def parse_di_config(data):
    """从解析后的 DataX 配置抽出 {source, target, runtime} 三块结构。"""
    steps = data.get("steps") or []
    reader = _find_step(steps, "reader") or {}
    writer = _find_step(steps, "writer") or {}
    rp = reader.get("parameter") or {}
    wp = writer.get("parameter") or {}

    source = {
        "step_type": reader.get("stepType"),
        "datasource": rp.get("datasource"),
        "table": rp.get("table"),
        "partition": [str(p) for p in _as_list(rp.get("partition"))],
        "where": (rp.get("where") or "").strip() if rp.get("where") else "",
        "columns": [_col_name(c) for c in _as_list(rp.get("column"))],
    }
    target = {
        "step_type": writer.get("stepType"),
        "datasource": wp.get("datasource"),
        "database": wp.get("selectedDatabase"),
        "table": wp.get("table"),
        # 不同 writer 写入模式字段名不一：holo=conflictMode，其余可能是 writeMode
        "write_mode": wp.get("conflictMode") or wp.get("writeMode"),
        "truncate": wp.get("truncate"),
        "columns": [_col_name(c) for c in _as_list(wp.get("column"))],
    }
    return {"source": source, "target": target}


def audit_column_mapping(src_cols, dst_cols):
    """按位置对齐 reader/writer 列，返回 (level, rows, diff_count)。

    level: 'error'=列数不一致（必然错位）/ 'info'=列数一致但有改名 / 'ok'=完全同名对应。
    rows : [(idx, src_or_None, dst_or_None, same_bool), ...]
    """
    n = max(len(src_cols), len(dst_cols))
    rows = []
    diff_count = 0
    for i in range(n):
        s = src_cols[i] if i < len(src_cols) else None
        d = dst_cols[i] if i < len(dst_cols) else None
        same = s is not None and d is not None and s == d
        if not same:
            diff_count += 1
        rows.append((i, s, d, same))
    if len(src_cols) != len(dst_cols):
        level = "error"
    elif diff_count:
        level = "info"
    else:
        level = "ok"
    return level, rows, diff_count


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------
def _fmt(v, dash="-"):
    if v is None or v == "":
        return dash
    return str(v)


def render_di_summary(data):
    """把 DI 配置渲染成可读摘要文本（源→目标 / 列映射对照 / 写入设置）。"""
    info = parse_di_config(data)
    src, dst = info["source"], info["target"]
    lines = []
    rline = f"reader={_fmt(src['step_type'])} → writer={_fmt(dst['step_type'])}"
    lines.append("—" * 70)
    lines.append(f"数据集成·离线同步摘要   |   {rline}")
    lines.append("—" * 70)

    # 源
    lines.append("源 (Reader)")
    lines.append(f"  数据源   : {_fmt(src['datasource'])}  ({_fmt(src['step_type'])})")
    lines.append(f"  表       : {_fmt(src['table'])}")
    lines.append(f"  分区     : {', '.join(src['partition']) if src['partition'] else '-（非分区/未指定）'}")
    if src["where"]:
        lines.append(f"  过滤     : {src['where']}")
    lines.append(f"  列数     : {len(src['columns'])}")

    # 目标
    lines.append("目标 (Writer)")
    lines.append(f"  数据源   : {_fmt(dst['datasource'])}  ({_fmt(dst['step_type'])})")
    db = dst["database"]
    tbl = _fmt(dst["table"])
    lines.append(f"  库.表    : {tbl}" + (f"   (selectedDatabase={db})" if db else ""))
    wm = _fmt(dst["write_mode"])
    if dst["truncate"] is not None:
        wm += f"   (truncate={_fmt(dst['truncate'])})"
    lines.append(f"  写入模式 : {wm}")
    lines.append(f"  列数     : {len(dst['columns'])}")

    # 列映射审查
    level, rows, diff_count = audit_column_mapping(src["columns"], dst["columns"])
    lines.append("")
    lines.append("列映射审查（按位置 reader[i] ↔ writer[i]）")
    if level == "error":
        lines.append(
            f"  ⚠ 列数不一致：源 {len(src['columns'])} ≠ 目标 {len(dst['columns'])}"
            f" —— 按位置映射会整体错位，务必逐列核对！"
        )
    elif level == "info":
        lines.append(
            f"  ℹ 列数一致（{len(src['columns'])}），其中 {diff_count} 处源/目标列名不同。"
            f"离线同步按位置映射，改名通常是有意为之——请人工确认未发生错位。"
        )
    else:
        lines.append(f"  ✓ {len(src['columns'])} 列按位置一一对应且同名。")

    # 对照表（列名不同的行标 ≠，越界缺列标 <缺>）
    sw = max([len(_fmt(s, "<缺>")) for _, s, _, _ in rows] + [len("源(reader)")])
    dw = max([len(_fmt(d, "<缺>")) for _, _, d, _ in rows] + [len("目标(writer)")])
    header = f"   {'#':>3}  {'源(reader)':<{sw}}  {'目标(writer)':<{dw}}  名称"
    lines.append(header)
    for idx, s, d, same in rows:
        mark = "=" if same else "≠"
        lines.append(
            f"   {idx:>3}  {_fmt(s, '<缺>'):<{sw}}  {_fmt(d, '<缺>'):<{dw}}  {mark}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 便于命令行快速看：python di_task.py <配置.json>
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if len(sys.argv) != 2:
        print("用法：python di_task.py <DataX配置.json>", file=sys.stderr)
        sys.exit(2)
    with open(sys.argv[1], "r", encoding="utf-8") as fh:
        content = fh.read()
    data = detect_di_config(content)
    if data is None:
        print("不是数据集成(离线同步)配置——可能是 SQL 或其他类型任务。", file=sys.stderr)
        sys.exit(1)
    print(render_di_summary(data))

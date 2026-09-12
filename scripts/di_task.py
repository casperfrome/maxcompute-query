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
  - **列数不一致** → `⚠` 无法建立完整一一对应，逐列核对缺口。
  - **列数一致但有列名不同** → 仅 `≠` 标注 + 一行 `ℹ` 说明：离线同步按位置映射、源目标改名
    很常见且多半是有意为之，这里只提示人工确认，不当成错误。
  - **完全同名一一对应** → `✓`。

只读：本模块不发起任何写操作，也不连库；要核对源/目标表真实行数请另走 mc_query / holo-query。
"""

import json
import re


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
        return c.strip() or None
    if isinstance(c, dict):
        value = c.get("name") or c.get("column")
        return value.strip() if isinstance(value, str) and value.strip() else None
    return None


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


def _parameters(step):
    value = step.get('parameter')
    return value if isinstance(value, dict) else {}


def parse_di_config(data):
    """抽取单 reader/writer 的源目标；多节点显式返回 unsupported。"""
    steps = data.get("steps") or []
    steps = steps if isinstance(steps, list) else []
    readers = [s for s in steps if isinstance(s, dict) and s.get('category') == 'reader']
    writers = [s for s in steps if isinstance(s, dict) and s.get('category') == 'writer']
    if len(readers) > 1 or len(writers) > 1:
        return {'source': None, 'target': None, 'mapping_status': 'unsupported',
                'reader_count': len(readers), 'writer_count': len(writers),
                'candidates': [{'category': s.get('category'), 'step_type': s.get('stepType'),
                    'table': _parameters(s).get('table')} for s in readers + writers]}
    reader = _find_step(steps, "reader") or {}
    writer = _find_step(steps, "writer") or {}
    rp = _parameters(reader)
    wp = _parameters(writer)

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

    level: unknown=缺有效列信息 / error=列数不一致 / info=有改名 / ok=同名对应。
    rows : [(idx, src_or_None, dst_or_None, same_bool), ...]
    """
    rows = []
    diff_count = 0
    if not src_cols or not dst_cols or any(not isinstance(c, str) or not c.strip() or c.strip() == '*'
                                          or re.search(r'\$\{|#\{|\$[A-Za-z_]\w*', c)
                                          for c in list(src_cols) + list(dst_cols)):
        return 'unknown', [], 0
    n = max(len(src_cols), len(dst_cols))
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
    if info.get('mapping_status') == 'unsupported':
        lines = ['数据集成·离线同步摘要',
                 '暂不支持完整映射：reader=%s，writer=%s；需要按完整拓扑逐组核对。' %
                 (info['reader_count'], info['writer_count'])]
        lines.extend('  %s %s table=%s' % (s['category'], _fmt(s['step_type']), _fmt(s['table']))
                     for s in info['candidates'])
        return '\n'.join(lines)
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
    lines.append(f"  分区     : {', '.join(src['partition']) if src['partition'] else '-（未提供分区信息）'}")
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
    if level == 'unknown':
        lines.append('  不足以判断：reader/writer 缺少有效的显式列信息；需补齐列顺序后核对。')
    elif level == "error":
        lines.append(
            f"  ⚠ 列数不一致：源 {len(src['columns'])} ≠ 目标 {len(dst['columns'])}"
            f" —— 不能建立完整一一对应，务必逐列核对！"
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


def main(argv=None):
    import argparse
    from pathlib import Path
    parser = argparse.ArgumentParser(description='离线读取 DataX 配置并检查列映射；不连接或执行任务。')
    parser.add_argument('file', help='UTF-8 DataX 配置文件')
    args = parser.parse_args(argv)
    data = detect_di_config(Path(args.file).read_text(encoding='utf-8-sig'))
    if data is None:
        parser.error('不是数据集成离线同步配置。')
    print(render_di_summary(data))
    return 0


if __name__ == '__main__':
    import sys
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(main())

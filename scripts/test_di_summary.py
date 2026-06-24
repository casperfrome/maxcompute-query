#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""test_di_summary.py — di_task 的确定性回归用例（不连库/网，秒级跑完）。

只导入 di_task 的纯函数，对内置的 DataX 配置样本断言：
  - detect_di_config 能区分「DI 同步配置」与「SQL / 普通 JSON」；
  - parse_di_config 抽出的源/目标表、列数正确；
  - audit_column_mapping 的判级正确：列数不一致→error、有改名→info、完全同名→ok；
  - 渲染不抛错。
跑法：
    python test_di_summary.py
全绿退出码 0；任一用例不符合预期则打印明细并以退出码 1 退出。

为什么用它验证：DI 解析是本次新增的核心逻辑，确定性单测信噪比最高——尤其是「列数不一致
必须 ⚠」这条审查口径，不该因为后续重构而悄悄失效。样本结构取自真实任务（odps→holo）。
"""
import json
import os
import sys

try:  # Windows GBK 控制台下也能正常打印中文
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from di_task import (  # noqa: E402
    audit_column_mapping,
    detect_di_config,
    parse_di_config,
    render_di_summary,
)


def _datax(reader_cols, writer_cols, reader_table="src_tbl", writer_table="dst.dst_tbl",
           partition="ds=${bizdate}", conflict="update"):
    """造一个最小可用的 DataX odps→holo 同步配置（结构同真实任务）。"""
    return json.dumps({
        "extend": {"mode": "wizard", "resourceGroup": "S_res_x", "formatType": "datax"},
        "type": "job",
        "version": "2.0",
        "steps": [
            {"stepType": "odps", "category": "reader", "name": "Reader",
             "parameter": {"datasource": "odps_ds", "table": reader_table,
                           "partition": [partition], "column": reader_cols}},
            {"stepType": "holo", "category": "writer", "name": "Writer",
             "parameter": {"datasource": "holo_ds", "selectedDatabase": "dst",
                           "table": writer_table, "conflictMode": conflict,
                           "truncate": "false", "column": writer_cols}},
        ],
        "setting": {"errorLimit": {"record": "0"},
                    "speed": {"throttle": False, "concurrent": 2}},
    }, ensure_ascii=False)


def main() -> int:
    failures = []

    def check(label, cond):
        if not cond:
            failures.append(label)

    # 1) 正常 DI 配置：列数一致、有 2 处改名 → detect 命中、info 级
    cfg_renamed = _datax(
        ["a", "issue_count", "unreform_report_count", "d"],
        ["a", "issue_cnt", "unreform_report_cnt", "d"],
    )
    data = detect_di_config(cfg_renamed)
    check("正常DI应被识别", data is not None)
    if data:
        info = parse_di_config(data)
        check("源表解析正确", info["source"]["table"] == "src_tbl")
        check("目标库.表解析正确", info["target"]["table"] == "dst.dst_tbl")
        check("源分区解析正确", info["source"]["partition"] == ["ds=${bizdate}"])
        check("写入模式解析正确", info["target"]["write_mode"] == "update")
        check("源列数=4", len(info["source"]["columns"]) == 4)
        level, rows, diff = audit_column_mapping(
            info["source"]["columns"], info["target"]["columns"])
        check("有改名应判 info", level == "info")
        check("改名计数=2", diff == 2)
        check("对照行数=4", len(rows) == 4)
        # 渲染不抛错且含关键字
        out = render_di_summary(data)
        check("渲染含‘列映射审查’", "列映射审查" in out)
        check("渲染含改名标记≠", "≠" in out)
        check("渲染含写入模式", "写入模式" in out)
        # 精简：运行设置（并发/脏数据阈值/资源组）刻意不再输出，防止以后被无意加回
        check("渲染不含‘运行设置’", "运行设置" not in out)
        check("渲染不含‘并发’", "并发" not in out)
        check("渲染不含‘errorLimit’", "errorLimit" not in out)

    # 2) 完全同名一一对应 → ok 级
    cfg_ok = _datax(["a", "b", "c"], ["a", "b", "c"])
    data_ok = detect_di_config(cfg_ok)
    level_ok, _, diff_ok = audit_column_mapping(
        parse_di_config(data_ok)["source"]["columns"],
        parse_di_config(data_ok)["target"]["columns"])
    check("完全同名应判 ok", level_ok == "ok")
    check("完全同名改名计数=0", diff_ok == 0)

    # 3) 列数不一致 → error 级（按位置映射必然错位，这条最该 ⚠）
    cfg_mismatch = _datax(["a", "b", "c"], ["a", "b"])
    data_mm = detect_di_config(cfg_mismatch)
    level_mm, rows_mm, _ = audit_column_mapping(
        parse_di_config(data_mm)["source"]["columns"],
        parse_di_config(data_mm)["target"]["columns"])
    check("列数不一致应判 error", level_mm == "error")
    check("缺列行用 None 占位", rows_mm[2][2] is None)
    check("列数不一致渲染含⚠", "⚠" in render_di_summary(data_mm))

    # 4) 列条目是对象 {name:..} 也能取名
    cfg_obj = json.dumps({
        "type": "job", "extend": {"formatType": "datax"},
        "steps": [
            {"category": "reader", "stepType": "odps",
             "parameter": {"table": "s", "column": [{"name": "x"}, {"name": "y"}]}},
            {"category": "writer", "stepType": "holo",
             "parameter": {"table": "d", "column": [{"name": "x"}, {"name": "y"}]}},
        ],
    }, ensure_ascii=False)
    data_obj = detect_di_config(cfg_obj)
    check("对象列条目应被识别", data_obj is not None)
    if data_obj:
        check("对象列取名正确",
              parse_di_config(data_obj)["source"]["columns"] == ["x", "y"])

    # 5) 反例：SQL / 普通 JSON / 空串 都不应误判为 DI
    check("SQL 不应误判", detect_di_config(
        "INSERT OVERWRITE TABLE t SELECT * FROM s") is None)
    check("注释开头 SQL 不应误判", detect_di_config(
        "--odps sql\nSELECT 1") is None)
    check("普通 JSON(无 reader/writer) 不应误判", detect_di_config(
        '{"a":1,"b":2}') is None)
    check("空串不应误判", detect_di_config("") is None)

    if failures:
        print(f"FAIL: {len(failures)} 个用例不符合预期：")
        for f in failures:
            print("  - " + f)
        return 1
    print("PASS: di_task 全部用例符合预期。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

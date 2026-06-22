#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""test_readonly.py — assert_readonly 的确定性回归用例（不连库，秒级跑完）。

只导入 mc_query 里的纯函数 assert_readonly，对一组 SQL 断言「应放行 / 应拦截」。
跑法：
    python test_readonly.py
全绿退出码 0；任一用例不符合预期则打印明细并以退出码 1 退出。

为什么用它验证：本次改的是只读校验这层正则/扫描逻辑，确定性单测信噪比最高——
既能证实之前被误杀的只读查询（REPLACE 函数、反引号保留字列名、串内 -- 等）现在放行，
也能证实真正的写操作（含 WITH..INSERT 这种隐蔽向量、多语句绕过）仍被拦下。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mc_query import assert_readonly  # noqa: E402

# 应放行：合法只读查询，assert_readonly 不应抛错
SHOULD_PASS = [
    ("REPLACE 字符串函数（报告的 bug）",
     "SELECT REPLACE(addr,'区','') FROM t WHERE ds=MAX_PT('t')"),
    ("REGEXP_REPLACE 函数",
     "SELECT regexp_replace(phone,'-','') c FROM t WHERE ds=MAX_PT('t')"),
    ("反引号引用的保留字列名",
     "SELECT `update`,`set`,`add` FROM t WHERE ds=MAX_PT('t')"),
    ("字符串里含 -- ",
     "SELECT id FROM t WHERE remark='a--b' AND ds=MAX_PT('t')"),
    ("字符串里含 ; ",
     "SELECT id FROM t WHERE note='a;b' AND ds=MAX_PT('t')"),
    ("字符串里含写词",
     "SELECT id FROM t WHERE node_oper='DELETE' AND ds=MAX_PT('t')"),
    ("块注释含引号+写词",
     "/* it's fine, may DROP later */ SELECT 1"),
    ("行注释含写词",
     "SELECT 1 -- DROP TABLE x"),
    ("下划线列名（add_time/load_ts）",
     "SELECT add_time, load_ts FROM t WHERE ds=MAX_PT('t')"),
    ("WITH ... SELECT（纯只读 CTE）",
     "WITH c AS (SELECT 1 a) SELECT a FROM c"),
    ("DESC", "DESC t"),
    ("SHOW PARTITIONS", "SHOW PARTITIONS t"),
    ("EXPLAIN SELECT", "EXPLAIN SELECT 1"),
]

# 应拦截：写操作 / 绕过尝试，assert_readonly 必须抛 ValueError
SHOULD_BLOCK = [
    ("DROP", "DROP TABLE x"),
    ("UPDATE", "UPDATE t SET x=1"),
    ("DELETE", "DELETE FROM t WHERE 1=1"),
    ("TRUNCATE", "TRUNCATE TABLE t"),
    ("INSERT OVERWRITE", "INSERT OVERWRITE TABLE t SELECT * FROM s"),
    ("WITH ... INSERT（首词 WITH，语句体在写）",
     "WITH c AS (SELECT 1) INSERT OVERWRITE TABLE t SELECT * FROM c"),
    ("多语句（; 绕过）", "SELECT 1; DROP TABLE t"),
    ("ALTER", "ALTER TABLE t ADD COLUMNS (x STRING)"),
    ("CREATE TABLE AS", "CREATE TABLE t AS SELECT * FROM s"),
    ("MERGE", "MERGE INTO t USING s ON t.id=s.id WHEN MATCHED THEN UPDATE SET t.a=s.a"),
    ("SET 配置语句（首词兜底）", "SET odps.sql.allow.fullscan=true"),
    ("空串", ""),
]


def main() -> int:
    failures = []

    for label, sql in SHOULD_PASS:
        try:
            assert_readonly(sql)
        except Exception as e:  # noqa: BLE001
            failures.append(f"[应放行却被拦] {label}: {e}\n    SQL: {sql}")

    for label, sql in SHOULD_BLOCK:
        try:
            assert_readonly(sql)
            failures.append(f"[应拦截却放行] {label}\n    SQL: {sql}")
        except ValueError:
            pass  # 预期内
        except Exception as e:  # noqa: BLE001
            failures.append(f"[应拦截但抛了非 ValueError] {label}: {type(e).__name__}: {e}")

    total = len(SHOULD_PASS) + len(SHOULD_BLOCK)
    if failures:
        print(f"FAIL: {len(failures)}/{total} 个用例不符合预期：\n")
        for f in failures:
            print("  " + f)
        return 1
    print(f"PASS: 全部 {total} 个用例符合预期"
          f"（放行 {len(SHOULD_PASS)} / 拦截 {len(SHOULD_BLOCK)}）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

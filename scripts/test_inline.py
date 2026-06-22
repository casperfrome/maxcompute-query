#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""test_inline.py — build_validation_sql 转写器 + 分区 lint 纯函数的确定性回归（不连库，秒级）。

跑法：
    python test_inline.py
全绿退出码 0；任一断言不符则打印明细并以退出码 1 退出。

为什么用它验证：inline/compare 的核心价值是「把人肉翻译的错误转移到一个**可测**的工具上」，所以
它本身必须有强不变量兜底——最硬的一条是：**任何生成物都必须是单条、能过只读校验的语句**
（assert_readonly 通过 = 单语句 + 只读 + 无写词绕过）。其余断言锁住「依赖顺序倒置被标记」
「同名 tmp 多写被标记」「compare 前缀提升不撞名」「表引用/CTE 正确区分」等具体行为。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mc_query import assert_readonly, extract_table_refs  # noqa: E402
from build_validation_sql import (  # noqa: E402
    inline_task, build_compare, split_statements, find_vars,
)

failures = []


def check(cond, msg):
    if not cond:
        failures.append(msg)


def expect_readonly(sql, label):
    """生成物必须是单条、只读语句——这是转写器最重要的不变量。"""
    try:
        assert_readonly(sql)
    except Exception as e:  # noqa: BLE001
        failures.append(f"[{label}] 生成物未通过 assert_readonly（非单条/含写操作）：{e}\n    >>> {sql[:120]}")


# ---------------------------------------------------------------------------
# inline：正常的线性 tmp 链
# ---------------------------------------------------------------------------
LINEAR = """
drop table if exists tmp_a;
create table tmp_a as SELECT id, name FROM src_x WHERE ds='${bizdate}';

drop table if exists tmp_b;
create table tmp_b lifecycle 7 as
SELECT a.id, a.name, b.amt
FROM tmp_a a LEFT JOIN src_y b ON a.id=b.id
WHERE b.ds='${bizdate}';

INSERT OVERWRITE TABLE final_out PARTITION(ds='${bizdate}')
SELECT id, name, amt FROM tmp_b WHERE amt > 0;
"""

sql, warns, ctes, tgt = inline_task(LINEAR)
expect_readonly(sql, "linear")
check(len(ctes) == 2, f"[linear] 期望 2 张 tmp，得到 {len(ctes)}")
check(tgt == "final_out", f"[linear] 最终目标应为 final_out，得到 {tgt}")
check(sql.strip().upper().startswith("WITH"), "[linear] 应以 WITH 开头")
check("tmp_a AS (" in sql and "tmp_b AS (" in sql, "[linear] 两张 tmp 都应成为 CTE")
check(sql.index("tmp_a AS (") < sql.index("tmp_b AS ("), "[linear] CTE 顺序应与源码一致（a 在 b 前）")
check(not warns, f"[linear] 正常链路不应有告警，却有：{warns}")
check(find_vars(sql) == ["bizdate"], "[linear] 应保留 ${bizdate} 占位符待代入")

# --target：只验证到中间表
sql_t, _, ctes_t, _ = inline_task(LINEAR, target="tmp_a")
expect_readonly(sql_t, "target")
check(len(ctes_t) == 1, f"[target] --target tmp_a 应只含 1 张 CTE，得到 {len(ctes_t)}")
check(sql_t.rstrip().endswith("SELECT * FROM tmp_a"), "[target] 收尾应为 SELECT * FROM tmp_a")
check("tmp_b" not in sql_t, "[target] 不应包含 tmp_a 之后的 tmp_b")


# ---------------------------------------------------------------------------
# inline：依赖顺序倒置（tmp_early 引用了定义更靠后的 tmp_late）→ 必须标记
# ---------------------------------------------------------------------------
DEP_BUG = """
create table tmp_early as SELECT * FROM tmp_late WHERE ds='${bizdate}';
create table tmp_late  as SELECT id FROM src WHERE ds='${bizdate}';
INSERT OVERWRITE TABLE out1 SELECT * FROM tmp_early;
"""
sql2, warns2, _, _ = inline_task(DEP_BUG)
expect_readonly(sql2, "dep-bug")
check(any("依赖顺序倒置" in w for w in warns2),
      f"[dep-bug] 应标记依赖顺序倒置，告警为：{warns2}")


# ---------------------------------------------------------------------------
# inline：同名 tmp 被多次写入 → 必须标记
# ---------------------------------------------------------------------------
MULTI_WRITE = """
create table tmp_c (id bigint);
insert overwrite table tmp_c SELECT id FROM s1 WHERE ds='${bizdate}';
insert into table tmp_c SELECT id FROM s2 WHERE ds='${bizdate}';
INSERT OVERWRITE TABLE out2 SELECT id FROM tmp_c;
"""
sql3, warns3, _, _ = inline_task(MULTI_WRITE)
expect_readonly(sql3, "multi-write")
check(any("多次写入" in w for w in warns3),
      f"[multi-write] 应标记同名 tmp 多次写入，告警为：{warns3}")


# ---------------------------------------------------------------------------
# inline：没有 tmp 的纯查询任务（直接 INSERT OVERWRITE ... SELECT）
# ---------------------------------------------------------------------------
NO_TMP = "INSERT OVERWRITE TABLE out3 PARTITION(ds='${bizdate}') SELECT a, b FROM src WHERE ds='${bizdate}';"
sql4, warns4, ctes4, _ = inline_task(NO_TMP)
expect_readonly(sql4, "no-tmp")
check(not ctes4, "[no-tmp] 不应有 CTE")
check(sql4.strip().upper().startswith("SELECT"), "[no-tmp] 应直接是 SELECT")


# ---------------------------------------------------------------------------
# 语句切分：不被串内/注释里的 ; 误导
# ---------------------------------------------------------------------------
TRICKY = "SELECT ';' AS a FROM t WHERE x='a;b' -- ; not a sep\n; SELECT 2;"
stmts = split_statements(TRICKY)
check(len(stmts) == 2, f"[split] 串/注释内的 ; 不应被当分隔符，期望 2 条得到 {len(stmts)}")


# ---------------------------------------------------------------------------
# 分区 lint 纯函数：表引用 vs CTE 名正确区分（不连库）
# ---------------------------------------------------------------------------
LINT_SQL = """
WITH c AS (SELECT id FROM real_tbl_a WHERE ds='20260620')
SELECT c.id, b.v
FROM c LEFT JOIN real_tbl_b b ON c.id=b.id
WHERE b.ds='20260620'
"""
cte_names, refs = extract_table_refs(LINT_SQL)
check("c" in cte_names, "[lint] 应识别出 CTE 名 c")
check("real_tbl_a" in refs and "real_tbl_b" in refs, "[lint] 应提取出两张真实基表")
# 真实待校验表 = 表引用里排除掉 CTE 名（lint 正是这么 continue 跳过 CTE 的）
base = [r.lower() for r in refs if r.lower() not in cte_names]
check("c" not in base, "[lint] CTE 名 c 应被排除出真实表清单")
check(set(base) == {"real_tbl_a", "real_tbl_b"}, f"[lint] 真实基表应为两张，得到 {sorted(set(base))}")


# ---------------------------------------------------------------------------
# compare：两侧都是 WITH，前缀提升不撞名 + 单条只读 + 含关键结构
# ---------------------------------------------------------------------------
OLD = "WITH t AS (SELECT id, amt FROM s WHERE ds='20260620') SELECT id AS k, MAX(amt) amt FROM t GROUP BY id"
NEW = "WITH t AS (SELECT id, amt FROM s WHERE ds='20260620') SELECT id AS k, SUM(amt) amt FROM t GROUP BY id"
cmp_sql = build_compare(OLD, NEW, ["k"], ["amt"])
expect_readonly(cmp_sql, "compare")
check("o_t AS (" in cmp_sql and "n_t AS (" in cmp_sql, "[compare] 两侧同名 CTE 应被前缀为 o_t / n_t")
check("FROM o_t" in cmp_sql and "FROM n_t" in cmp_sql, "[compare] CTE 引用应同步改名")
check("FULL OUTER JOIN __new n ON o.k = n.k" in cmp_sql, "[compare] 应按唯一键 FULL OUTER JOIN")
check("键同值不同" in cmp_sql, "[compare] 给了 --measure 应包含值比对分桶")

# compare：多键 + 无 measure
cmp2 = build_compare("SELECT a,b FROM x", "SELECT a,b FROM y", ["a", "b"], [])
expect_readonly(cmp2, "compare-multikey")
check("o.a = n.a AND o.b = n.b" in cmp2, "[compare-multikey] 多键应 AND 连接")
check("键同值不同" not in cmp2, "[compare-multikey] 没给 measure 不应有值比对分桶")


# ---------------------------------------------------------------------------
def main():
    total = "（见各断言）"
    if failures:
        print(f"FAIL: {len(failures)} 条断言不符合预期：\n")
        for f in failures:
            print("  - " + f)
        return 1
    print("PASS: build_validation_sql 转写器 + 分区 lint 纯函数全部断言通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

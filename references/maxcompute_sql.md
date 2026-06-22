# MaxCompute (ODPS) SQL 方言要点

排查取数时最常踩的坑都在分区和函数上。下面是写 MaxCompute SQL 必须记住的关键点。

## 目录
- [分区：永远先过滤](#分区永远先过滤)
- [常用元数据 / 探查](#常用元数据--探查)
- [字符串与空值](#字符串与空值)
- [日期时间](#日期时间)
- [JSON 解析](#json-解析)
- [聚合与窗口](#聚合与窗口)
- [常见报错对照](#常见报错对照)

## 分区：永远先过滤

MaxCompute 是按分区扫描计费的，不带分区过滤的查询会**全表扫描**，又慢又贵，排查时极易踩雷。

- 取最新分区：`WHERE ds = MAX_PT('project.table')` 或同库下 `MAX_PT('table')`。
  - `MAX_PT` 返回该表**有数据**的最大分区值（字符串），比手写日期更稳。
- 指定某天：`WHERE ds = '20240506'`（分区值通常是字符串，注意加引号）。
- 区间：`WHERE ds BETWEEN '20240501' AND '20240506'`。
- 分区字段名常见为 `ds`（按天），也可能是 `pt`、`dt` 或多级分区。**先用 `desc` 确认分区字段名**，别想当然。
- 多级分区表要把每级分区都加上过滤。

> 经验法则：写完 SQL 先自检——FROM 的每张分区表，WHERE 里是否都带了分区过滤？

## 常用元数据 / 探查

辅助脚本已封装这些，优先用脚本而不是手写：
- `mc_query.py list-tables <子串>` → 找表
- `mc_query.py desc <表>` → 字段名/类型/注释 + 分区字段
- `mc_query.py partitions <表>` → 看有哪些分区、最新是哪天
- `mc_query.py sample <表>` → 看真实数据长什么样

手写等价 SQL（必要时）：
```sql
SHOW PARTITIONS table_name;            -- 看分区
SELECT MAX_PT('table_name');           -- 看最新分区值
SELECT * FROM t WHERE ds=MAX_PT('t') LIMIT 10;
```

## 字符串与空值

- 拼接：`CONCAT(a, b)`；带分隔符 `CONCAT_WS(',', a, b)`。
- 截取：`SUBSTR(s, start, len)`（下标从 1 开始）。
- 包含：`s LIKE '%x%'`；正则 `s RLIKE 'pattern'`。
- 空值判断：`col IS NULL` / `col IS NOT NULL`；空串和 NULL 不同，排查"为空"时常需 `col IS NULL OR col = ''`。
- 空值兜底：`COALESCE(col, '默认')`、`NVL(col, '默认')`。
- 拆分：`SPLIT(s, ',')` 返回 array；配合 `LATERAL VIEW EXPLODE(...)` 行转列。

## 日期时间

- 当前：`GETDATE()`。
- 格式化：`TO_CHAR(dt, 'yyyymmdd')`、`FROM_UNIXTIME(ts)`、`UNIX_TIMESTAMP(s)`。
- 解析：`TO_DATE('2024-05-06', 'yyyy-mm-dd')`。
- 加减天：`DATEADD(dt, -1, 'dd')`、`DATEDIFF(d1, d2, 'dd')`。
- 分区 ds（字符串 yyyymmdd）转日期：`TO_DATE(ds, 'yyyymmdd')`。

## JSON 解析

- `GET_JSON_OBJECT(json_str, '$.field')` 取字段。
- 嵌套：`GET_JSON_OBJECT(s, '$.a.b[0]')`。

## 聚合与窗口

- `GROUP BY` 后 SELECT 的非聚合列必须出现在 GROUP BY 中。
- 计数去重：`COUNT(DISTINCT col)`。
- Top N：`... ORDER BY cnt DESC LIMIT 10`。
- 窗口：`ROW_NUMBER() OVER (PARTITION BY a ORDER BY b DESC)`，分组取 Top N 的标准做法。

## 常见报错对照

| 现象 | 多半原因 |
| --- | --- |
| 查询很久 / 扫描量巨大 | 漏了分区过滤，全表扫描 |
| `Table not found` | 表名拼错，或需带 project 前缀；先 `list-tables` 确认 |
| `Column not found` | 列名猜错；先 `desc` 确认字段名 |
| 分区过滤无结果 | `ds` 值类型/格式不对（字符串要加引号），或该分区无数据，用 `MAX_PT` 更稳 |
| `MAX_PT` 报错 | 表名要带引号字符串，如 `MAX_PT('t')`，且表需已有数据分区 |

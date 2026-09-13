# MaxCompute (ODPS) SQL 方言要点

排查取数时最常踩的坑都在分区和函数上。下面是写 MaxCompute SQL 必须记住的关键点。

表名写法遵循 [SKILL.md 的表名前缀规范](../SKILL.md#执行边界)：`tmp` 开头的物理表无前缀，其他物理表带真实所属前缀；CTE 名称和别名不加前缀。

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

- 取最新分区：非 `tmp` 物理表使用 `WHERE ds = MAX_PT('project.table')`，表名与 FROM/JOIN 中的完整表名一致；`tmp` 开头的分区表则使用不带前缀的表名。
  - `MAX_PT` 返回该表**有数据**的最大分区值（字符串），比手写日期更稳。
- 指定某天：`WHERE ds = '20240506'`（分区值通常是字符串，注意加引号）。
- 区间：`WHERE ds BETWEEN '20240501' AND '20240506'`。
- 分区字段名常见为 `ds`（按天），也可能是 `pt`、`dt` 或多级分区。**先用 `desc` 确认分区字段名**，别想当然。
- 多级分区表要把每级分区都加上过滤。

> 经验法则：写完 SQL 先自检——FROM 的每张分区表，WHERE 里是否都带了分区过滤？

## 常用元数据 / 探查

辅助脚本已封装这些，下面是参数索引；可执行命令使用本节后面的绝对工具路径：
- `mc_query.py list-tables <子串>` → 找表
- `mc_query.py desc <表>` → 字段名/类型/注释 + 分区字段
- `mc_query.py partitions <表>` → 看有哪些分区、最新是哪天
- `mc_query.py sample <表>` → 看真实数据长什么样

手写等价 SQL（必要时）：
```sql
SHOW PARTITIONS tst_mc_prod.table_name;             -- 看分区
SELECT MAX_PT('tst_mc_prod.table_name');            -- 看最新分区值
SELECT * FROM tst_mc_prod.table_name
WHERE ds=MAX_PT('tst_mc_prod.table_name') LIMIT 10;
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
| 查询返回零条 | 核对分区、条件与类型；条件正确时零条有效，不为非空改分区或放宽过滤 |
| `MAX_PT` 报错 | 表名要带引号字符串，如 `MAX_PT('tst_mc_prod.table_name')`，且表需已有数据分区 |


## 查询命令、UDF 与导出

以下用本项目绝对脚本路径；示例对象与文件位置替换为实际已核实对象。PowerShell 读取 SQL 用 `Get-Content -LiteralPath <文件> -Raw -Encoding UTF8`，不要用默认编码读中文。

```powershell
$queryTool = 'D:\AllForCareer\数仓问题排查\.agents\skills\maxcompute-query\scripts\mc_query.py'
& 'D:\PythonVenv\Scripts\python.exe' -X utf8 -B $queryTool list-tables example
& 'D:\PythonVenv\Scripts\python.exe' -X utf8 -B $queryTool desc tst_mc_prod.example_table
& 'D:\PythonVenv\Scripts\python.exe' -X utf8 -B $queryTool partitions tst_mc_prod.example_table
& 'D:\PythonVenv\Scripts\python.exe' -X utf8 -B $queryTool sample tst_mc_prod.example_table -n 5
& 'D:\PythonVenv\Scripts\python.exe' -X utf8 -B $queryTool sql -f outputs/query.sql --strict
& 'D:\PythonVenv\Scripts\python.exe' -X utf8 -B $queryTool sql -f outputs/query.sql --save outputs/result.xlsx
& 'D:\PythonVenv\Scripts\python.exe' -X utf8 -B $queryTool sql -f outputs/query.sql --save outputs/result.csv
# 只读取注册和资源，不执行 UDF
& 'D:\PythonVenv\Scripts\python.exe' -X utf8 -B $queryTool list-functions greedy
& 'D:\PythonVenv\Scripts\python.exe' -X utf8 -B $queryTool func greedy_session
& 'D:\PythonVenv\Scripts\python.exe' -X utf8 -B $queryTool resource greedy_session.py
```

func 读取注册类名与 USING 资源，Python 资源可提供源码，嵌入式/SQL 函数看返回代码；Java jar 无源码时明确不可得，不凭函数名补逻辑。取回的 Python 仅阅读，不本地 import/exec。表名、字段不存在先根据错误 SQL 核对元数据；解析/类型错误做针对性纠正，权限与平台故障如实记录，不反复扩大扫描尝试。

查询输出预览和完整导出分别说明；保留查询器实际返回的 instance_id、行数、扫描量、耗时及 logview 等可得证据。导出的文件可能含业务敏感数据，按当前任务交付范围展示；链接中的签名参数脱敏。空结果是有效返回之一，不能把“无重复键”查询的零行当运行失败。

分区检查按表/别名及查询作用域判断 WHERE/ON 谓词，不把投影、注释、另一表的 ds 条件当当前表的过滤。`--strict` 对缺失过滤和无法确认的作用域/元数据均拒绝；普通告警也需要阅读和解释。若本来就需要全扫描，先核对授权范围并说明影响，不为绕过检查改参数。

## 离线生成与改前改后验证

```powershell
$validationTool = 'D:\AllForCareer\数仓问题排查\.agents\skills\maxcompute-query\scripts\build_validation_sql.py'
# 只列变量，无凭证/SDK/本地 config 要求
& 'D:\PythonVenv\Scripts\python.exe' -X utf8 -B $validationTool inline outputs/task.sql --list-vars
& 'D:\PythonVenv\Scripts\python.exe' -X utf8 -B $validationTool inline outputs/task.sql --project tst_mc_prod --var bizdate=20260911 --save outputs/task_readonly.sql
& 'D:\PythonVenv\Scripts\python.exe' -X utf8 -B $validationTool compare outputs/old.sql outputs/new.sql --key request_id,file_id --measure amount --save outputs/compare.sql
& 'D:\PythonVenv\Scripts\python.exe' -X utf8 -B $queryTool sql -f outputs/compare.sql --strict
```

inline 只在支持范围生成文本：`CREATE TABLE <表名> [LIFECYCLE n] AS SELECT/WITH` 建立的单次物化链，加单一最终 INSERT OVERWRITE TABLE 或 SELECT。需要查看中间产出可按 `--help` 使用明确 target；`--target` 仍校验完整输入，不能绕过后续不支持写入；最终独立 WITH 与中间 CTAS 链暂拒绝合并作用域。typed 建表骨架、分区写、同表多次写、INSERT INTO 追加、未知语句、未定义依赖、多输出、未替换变量等停止生成。仅允许中间表创建前的一次 DROP TABLE 清理，其他 DROP 同样拒绝，不手工丢掉再声称整体等价；无损性无法保证时只做明确范围的局部验证。

`--project` 默认共享配置 `ODPS_PROJECT`，离线配置可空；未知项目不把 qualified/unqualified 表合并，已知跨项目同名表也保留身份。重命名 CTE 时须维护关系作用域与列限定符，不修改同名普通列。成功生成仍只说明受支持的文本转换，需要只读校验和真实执行才有数据证据。

失败输出 stderr、退出码 3，不覆盖 `--save` 指定的既有文件。调用方检查退出码后再读输出；旧文件存在不代表本轮生成成功。为避免范围混淆，变量只使用已核实的日期/参数，不把历史示例值当当前任务默认值。

compare 输出固定两列 `diff_type,cnt`。先验证每侧复合键非 NULL 且唯一：任何 NULL 键或重复键都会使结果只包含校验失败行，不出现正常差异分桶。校验失败计数不能解释为“仅旧有/仅新增”，需要先确认键和数据语义。指定 `--measure` 的合法一对一输入才输出“仅旧有 / 仅新增 / 键同值不同 / 完全一致”四类；未指定度量时匹配类别为“键匹配(未比较值，--measure 可加上)”，不能宣称值一致，是否存在某侧由独立标记判断，不能看第一个业务键是否 NULL。

本工具不提供多重集差异或自定义 NULL 键匹配；有此需求先明确规则再单独设计验证，不能直接套用一对一比较。差异为零也只限比较列、键与输入范围，不能扩大为所有字段或所有日期一致；改后新唯一键仍需满足用户要求。


## 查询实例恢复与完整导出

`sql -q/-f` 提交只读查询；`sql --instance-id` 只恢复已有实例，三者互斥。`--project` 覆盖本次项目，不修改共享配置；恢复时必须指定原实例所属项目。新提交仍先执行只读与分区检查，恢复不改 SQL、分区或原实例。`sample` 也使用有限下载，支持本次 `--project/--wait-timeout`。

```powershell
$queryTool = 'D:\AllForCareer\数仓问题排查\.agents\skills\maxcompute-query\scripts\mc_query.py'
& 'D:\PythonVenv\Scripts\python.exe' -X utf8 -B $queryTool sql -f outputs/query.sql --strict --wait-timeout 600 --max-rows 200
# 用上次输出的真实 instance_id 和所属项目恢复；不是重新提交
& 'D:\PythonVenv\Scripts\python.exe' -X utf8 -B $queryTool sql --instance-id '<实际实例ID>' --project '<实际项目>' --max-rows 200
& 'D:\PythonVenv\Scripts\python.exe' -X utf8 -B $queryTool sql --instance-id '<实际实例ID>' --project '<实际项目>' --save outputs/full.csv --batch-size 10000
```

提交后先输出并刷新实例 ID、项目、本地记录路径，然后等待与读取。stdout 保留结果；stderr 提供运行状态和证据，调用方要同时留存。当前目录 `.maxcompute-query-runs/` 中每次操作有独立记录，包含查询原文（能取得时）、SQL 哈希、项目、端点、实例、时间及读取状态。完整 LogView 仅保存在本地记录，分享摘要时脱敏签名与凭证。

默认等待 600 秒，仅限制查询状态等待，不是结果下载的时限；单次底层网络请求仍受 SDK 的网络超时控制。到时退出本地等待，不停止云端实例。网络中断、超时或下载失败优先恢复已记录 ID；提交异常且没有 ID 时是“提交结果未知”，不能推断未创建或自动重试。不要把另一个项目、DataWorks 工作流或任务实例 ID 当成 MaxCompute SQL instance_id。

| 参数/结果字段 | 语义 |
|---|---|
| `--max-rows` | 正整数，默认 200；预览的下载及展示上限，不修改 SQL，也不限制扫描量 |
| `--batch-size` | 正整数，默认 10,000；完整导出的每批行数，不截断总结果 |
| `total_rows` | 非受限完整读取会话提供的远端总量；不可得则未知，不能用预览长度代替 |
| `downloaded_rows / displayed_rows` | 本次实际下载/展示行数；导出不在终端展示数据行 |
| `truncated / restricted` | 是否截断展示与是否受读取保护限制，未知必须保留未知 |

预览只在明确的读取权限保护错误下尝试受限 Tunnel，标明受限且总量未知；其他读取错误不通过换接口掩盖。完整导出显式使用 `tunnel=True, limit=False`，禁止退回受限接口；同一 reader 分批读取，累计行数和完整会话总数不一致、总数无法确认或读取中断均失败。

CSV 使用 UTF-8 BOM 且只写一次表头；XLSX 流式写入单个工作表，最多 1,048,575 条数据行加表头、16,384 列，超限提示 CSV，不自动拆表。空结果导出只包含表头。DESC/SHOW/EXPLAIN 等文本结果按文本展示，不伪装为可完整导出的表格。先写同目录临时文件，校验成功后原子替换；失败保留旧文件。再次导出从原实例重新下载，不追加旧文件或恢复半成品偏移。

导出在 reader 和写入器关闭后才发布。若文件已完整发布、仅最终运行记录更新失败，仍报告导出成功并明确告警，不能据旧记录重复提交。Excel 中长整数、Decimal 和带时区时间以文本保留精度；字符串不作为公式执行，单元格超过 32,767 字符时拒绝并提示 CSV。

| 退出码 | 调用方处理 |
|---|---|
| 0 | 本次读取或完整导出成功；仍需检查预览截断/受限状态和业务证据 |
| 2 / 3 | 命令参数 / 本地输入、配置或只读分区检查不通过；修正明确问题 |
| 4 | 提交或等待阶段出错；依据记录区分远端失败与提交/状态未知，不自动重提 |
| 5 | 本地等待超时；保留云端任务，按 ID 恢复 |
| 6 | 读取或导出失败；旧文件不变，按原实例重新下载 |
| 130 | 用户停止本地操作；未取消云端任务 |

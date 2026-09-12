# skill 优化方向

审查日期：2026-09-12。代码基线：[2b27e8d](https://github.com/casperfrome/maxcompute-query/commit/2b27e8defaba9381399050618a4c94bcf7671fa2)，已原样推送至远程 `main`。本次只新增本文，未修改原有代码或说明。

## 1. 最值得先做：精简 SKILL.md

实测 **224 行、14,144 字符**（UTF-8 读取后按 LF 统计，不是 token 数）。建议目标 **110–145 行、7,500–9,500 字符**，约减少 **33%–47%**。这是合并与外移的估算，尚未实施；不要把减行数本身当目标。

| 重复内容与位置 | 具体处理 |
|---|---|
| “只给任务名先取码”重复于第 10、78–79、87、119、137 行；B/C 重复解释为什么要验证 | 路由表统一规定 B/C 先取码；共用分析和证据合并要求，C 明确“改前验证 → 修改 SQL → 改后验证” |
| 第 31–49 行已完整解释何时澄清，第 71、126、138、153、218 行继续展开 | 合并为三条：事实能查就查；影响结果的业务歧义收集证据后澄清；不依赖答复的工作继续。删除重复例子和理由 |
| 第 51–71 行讲委派，后续流程反复强调并行 | 保留一处委派条件、两个模板链接和证据回传要求；小任务直接做，独立问题才并行 |
| 第 150–221 行是 72 行操作教程，其中表探查、报错对照与参考文档重复 | 正文留五步流程与一个最小命令；UDF、导出、错误对照移入参考文档。取码命令集中到现有 `references/fetch_task_sql.md` |

建议正文结构：**触发范围 → 执行边界 → A/B/C/D 路由 → 共用流程 → 澄清与委派 → 参考索引**。外移后写清“什么场景读哪个文件”，并修正参考文档的反向引用，避免来回跳转仍找不到命令。

必须保留：SQL 只读及禁止绕过；真实项目、`tmp` 前缀规则与分区范围；保存态严格选取、缺失不回退；取回 Python 不在本地导入或执行；按实际写入目标和参数核对授权、已有授权不重复询问；原节点不更新；提交不确定先恢复；运行轮次及代码哈希；日志脱敏；提交成功、运行成功、业务正确分开报告；指定 Python 与 UTF-8。

## 2. 已确认的脚本问题

以下为离线复现或代码直接确认，**没有提交真实云任务**。P1 表示可能重复执行或污染验证结论；P2 表示功能、检查能力或安装可用性问题。

### P1：保存态旧入口可并发重复提交

- 位置：[debug_pyodps3.py:145–175](https://github.com/casperfrome/maxcompute-query/blob/2b27e8d/scripts/debug_pyodps3.py#L145)。先读 `not_started`，稍后才写 `submitting`，中间没有互斥锁。
- 复现：同一目录两个并发调用均通过检查，mock 收到 **2 次** `ExecuteAdhocWorkflowInstance`，工作流名不同，最终清单只保留一个实例 ID。可能重复执行脚本中的写入，并丢失另一次运行的追踪信息。
- 建议：复用修复会话已有的锁，覆盖读取状态、冻结请求和提交状态落盘；并发回归应断言提交次数为 1。原子写文件不能替代整个状态转换的锁。

### P1：inline 会把其他项目的同名表改成当前 CTE

- 位置：[build_validation_sql.py:105–120](https://github.com/casperfrome/maxcompute-query/blob/2b27e8d/scripts/build_validation_sql.py#L105)、[第 242–247 行](https://github.com/casperfrome/maxcompute-query/blob/2b27e8d/scripts/build_validation_sql.py#L242)。替换按短表名匹配，丢失项目身份。
- 复现：`CREATE TABLE tmp_x AS SELECT 1 AS id; INSERT OVERWRITE TABLE p.out SELECT * FROM other.tmp_x;` 被转成读取本地 CTE `tmp_x`，**没有告警**，验证的已是另一份数据。
- 建议：按完整表身份和 SQL 引用位置替换，只映射实际创建的目标；未知项目归属不得靠短名合并。

### P1：inline 对多次写入只告警，仍生成不等价的验证 SQL

- 位置：[build_validation_sql.py:166–182](https://github.com/casperfrome/maxcompute-query/blob/2b27e8d/scripts/build_validation_sql.py#L166)。同名表保留最后一次写入，分区子句直接忽略；CLI 仍可成功导出。
- 复现：`tmp_a=1 → tmp_b读取tmp_a → tmp_a覆盖为2 → 输出tmp_b`，原链路应输出 `1`，生成的 WITH 输出 `2`。单条只读 SQL 合法，不能证明转写等价。
- 建议：无法保证语义时停止生成“可验证产物”，或按每次写入生成独立版本 CTE；分区写入、多输出、跳过语句、未替换变量都要有明确的不支持状态。

### P1：compare 的 NULL 键会误分桶，重复键会放大计数

- 位置：[build_validation_sql.py:332–352](https://github.com/casperfrome/maxcompute-query/blob/2b27e8d/scripts/build_validation_sql.py#L332)。用第一个业务键是否为 NULL 判断某侧是否存在，没有校验唯一键前提。
- 复现：旧侧只有 `id=NULL`、新侧为空，被标为“仅新增”；两侧各有两条相同 `id=1,v=1`，得到“完全一致 **4** 条”，实际每侧只有 2 条。
- 建议：两侧增加独立存在标记；先校验键唯一性及 NULL 策略，重复键不得直接进入一对一比较；需要重复行比较时采用多重集语义。

### P2：ExceptionGroup 漏识别，可绕过 traceback 复核

- 位置：[pyodps3_log_utils.py:12–15](https://github.com/casperfrome/maxcompute-query/blob/2b27e8d/scripts/pyodps3_log_utils.py#L12)、[repair_pyodps3.py:267–273](https://github.com/casperfrome/maxcompute-query/blob/2b27e8d/scripts/repair_pyodps3.py#L267)。解析器只识别普通 traceback，不识别异常组的树形前缀。
- 复现：真实生成异常组日志，再配合 mock 的远端 `Success`、退出码 0 及其余完整证据，得到 `tracebacks=[]`、`runtime_verified=True` 并导出 `final.py`，跳过异常复核。适用场景是异常被捕获后打印；**不是说远端 Failure 也会通过，更不代表业务已验收**。
- 建议：支持 Python 3.11 异常组和旧版本 backport 的树形结构；无法完整解析但有异常组标记时转人工诊断。增加嵌套异常组及带平台前缀的样例。

### P2：compare 改 CTE 名时漏改列限定符，还会误改同名字段

- 位置：[build_validation_sql.py:288–315](https://github.com/casperfrome/maxcompute-query/blob/2b27e8d/scripts/build_validation_sql.py#L288)。基于词匹配改名，忽略点号两侧的标识符。
- 复现：`WITH a AS (SELECT 1 AS id) SELECT a.id FROM a` 变成 `SELECT a.id FROM o_a`，`a` 已不存在；另一例中普通字段 `a` 被误改为 `o_a`。
- 建议：按作用域改写关系引用及对应限定符，保留字段名、别名和 CTE 列清单。先覆盖这些具体语法，不宜继续叠加无作用域的正则。

### P2：--strict 并不能保证每张表有分区过滤

- 位置：[mc_query.py:225–272](https://github.com/casperfrome/maxcompute-query/blob/2b27e8d/scripts/mc_query.py#L225)。只检查分区列名字是否出现在整条 SQL；反引号表名还会被掩码抹掉，元数据失败则静默跳过。
- 复现：`SELECT ds FROM p.t` 没有过滤却无告警；两张表都按 `ds` 分区，仅过滤 `a.ds`，`b` 也无告警。`--strict` 依赖同一结果，因此同样漏报。
- 建议：跟踪表别名与谓词归属，检查 WHERE/ON 中真实过滤；解析或元数据不确定应单独报告，严格模式不能视为通过。至少补投影同名列、多表同分区列、反引号和元数据失败用例。

### P2：取码和版本查询遇到同名任务会静默选第一个

- 位置：[fetch_task_sql.py:360–381](https://github.com/casperfrome/maxcompute-query/blob/2b27e8d/scripts/fetch_task_sql.py#L360)、[第 428–457 行](https://github.com/casperfrome/maxcompute-query/blob/2b27e8d/scripts/fetch_task_sql.py#L428)。保存态入口有消歧，普通取码及历史版本入口没有同等约束。
- 复现：两个目录各有同名文件，返回第一个文件的代码；多个产出节点也按迭代顺序取首个。可能审查了错误任务。
- 建议：复用保存态的候选校验，返回目录、file_id、node_id；普通取码及版本查询支持明确 ID，多个精确同名也不能直接选首项。

### P2：干净安装连离线 --help 都依赖未提交的 config.py

- 位置：[mc_query.py:48](https://github.com/casperfrome/maxcompute-query/blob/2b27e8d/scripts/mc_query.py#L48)、[build_validation_sql.py:42](https://github.com/casperfrome/maxcompute-query/blob/2b27e8d/scripts/build_validation_sql.py#L42)。仓库只有配置模板，却在导入期强制导入本地配置。
- 复现：将公开脚本复制到干净临时目录，`mc_query`、`build_validation_sql`、`debug_pyodps3`、`repair_pyodps3` 的 `--help` 全部因 `ModuleNotFoundError: config` 退出。
- 建议：配置默认值可直接加载、仅连接前校验；纯 SQL/日志工具与云客户端解耦；补最小依赖清单及从干净目录运行的检查。不要让离线转写和日志阅读要求配置云连接。

### P2：重复显式参数冲突被静默覆盖

- 位置：[pyodps3_runtime.py:346–353](https://github.com/casperfrome/maxcompute-query/blob/2b27e8d/scripts/pyodps3_runtime.py#L346)。与 `references/pyodps3_debugging.md:164` 的“重复参数值冲突时报错”不符。
- 复现：传入 `target=table_a`、`target=table_b` 后直接采用 `table_b`；显式 `bizdate` 先传错误日期、后传正确日期，也会掩盖前一个冲突。
- 建议：保留“保存态 → 显式覆盖”的优先级；同一层参数重复且值不同则拒绝，不让运行目标或日期取决于参数排列。

## 3. 文档与验证逻辑还应同步修正

| 问题 | 建议 |
|---|---|
| `SKILL.md:36/53/71` 硬编码 `AskUserQuestion`、`general-purpose`，并断言子代理看不到对话和工作目录 | 改为遵循宿主实际工具；委派时传完整任务和绝对路径，不依赖错误的环境假设 |
| `agents/verifier.md:19–20/28/33/50` 残留 `.claude`、裸 `python`，还把 `mc_query.py` 文件路径拼成子目录；`task-analyzer.md:18–19/56` 有同类问题 | 分别传 Python、查询器、取码器、转写器绝对路径；统一使用 `D:\PythonVenv\Scripts\python.exe` |
| `README.md:12` 仍写不自动修复重跑，目录也缺修复会话脚本 | 区分单次保存态调试与有预算的修复会话，和现有参考文档一致 |
| `SKILL.md:140` 把当前分区没重复解释为“根本不需要去重”；第 216 行及验证器模板容易把空结果当失败 | 结论限定到已查分区；用户要求的唯一性仍需保障。空结果核对条件后可以是有效证据，不能为得到非空结果放宽业务范围 |
| `evals/evals.json:110/126` 对“最新分区”硬编码历史数字和“约 5 倍”；第 16 行要求非空；部分附件未入库 | 数字放固定 fixture；线上评测核验取数依据与结论一致性，允许真实零结果；补齐附件。26 个 eval 均为人工断言，不能当成已执行的自动回归 |
| `di_task.py:188–202` 对缺失 reader/writer 列信息显示“✓ 0 列…同名” | 缺信息返回“不足以判断”；多 reader/writer 不应只看首项就输出完整映射结论 |

## 4. 值得采用的现有 API / SDK

本地核实：PyODPS **0.12.6**；DataWorks 20200518 **8.0.4**、20240518 **8.0.3**；Credentials **1.0.7**。以下接口已存在，无需先做整体升级。

| 优先级 | API 与落点 | 收益及边界 |
|---|---|---|
| 高 | `ODPS.run_sql()` → `Instance.wait_for_success(timeout=...)`，`ODPS.get_instance(id)` 恢复 | `mc_query.py:326` 目前阻塞等待，结果取完才输出实例 ID。应提交后立即记录 ID/LogView，断线恢复原实例。超时不会自动取消；取消用 `Instance.stop()`。本地 `execute_sql()` 不处理 `wait_timeout`，不能直接加这个参数。[官方实例文档](https://pyodps.readthedocs.io/en/stable/base-instances.html) |
| 高 | `Instance.to_pandas(count=...)`、`Instance.iter_pandas(batch_size=...)` | `mc_query.py:328/374` 先全量下载，再只展示前 200 行。预览限量下载，完整导出分批处理；记录总行数、下载行数及截断状态。**限制下载量不减少 SQL 扫描量**，也不必每次固定 8 个进程。[官方结果读取](https://pyodps.readthedocs.io/en/stable/base-sql.html#view-sql-results) |
| 中 | `Table.get_max_partition(spec=..., skip_empty=True)`，读取 `partition_spec` | `mc_query.py:446/468` 字符串排序不排除空分区，采样只约束第一分区字段。明确多级分区的业务范围，再选择完整分区规范。SDK 本地实现仍枚举分区，不能承诺必然提速；“字典序最大”也不等于“业务最新”。[分区 API](https://pyodps.readthedocs.io/en/stable/base-tables.html#table-partitions)、[MAX_PT 一级分区限制](https://help.aliyun.com/zh/maxcompute/user-guide/max-pt) |
| 中 | `ODPS.execute_sql_cost(sql)` | 当前只有执行后扫描量；可为大查询提供提交前 `input_size/complexity/udf_num` 预估。它是独立 SQLCostTask，增加调用和等待，结果不是账单；外部表计算不在支持范围。[SDK 接口](https://pyodps.readthedocs.io/en/stable/api-entry.html#odps.ODPS.execute_sql_cost)、[官方限制](https://help.aliyun.com/zh/maxcompute/product-overview/billing-1) |
| 有临时凭证需求时 | 共用 `alibabacloud_credentials.client.Client`；DataWorks `Config(credential=...)`、PyODPS `CredentialProviderAccount(...)` | 当前直接传 AK/SK，未接入 STS Token 和刷新。保留现有 `ALIYUN_*`/`ODPS_*` 兼容层；自动刷新需动态凭证提供者，静态 Token 不会自动变新。接口已核实，真实刷新尚未验证。[Credentials](https://help.aliyun.com/zh/sdk/developer-reference/v2-manage-python-access-credentials)、[PyODPS 接入示例](https://help.aliyun.com/en/pai/configure-the-dlc-ram-role) |

不建议贸然采用的方案：

- **全部换成 DataWorks 2024 API**：数据开发接口须匹配 Data Studio 新旧版本，已有双版本适配有必要。[官方版本选择](https://help.aliyun.com/zh/dataworks/developer-reference/use-dataworks-openapi/)
- **把异步结果接口接到现有 UNKNOWN 恢复**：`GetCreateWorkflowInstancesResult` 要求 `CreateWorkflowInstances` 的 OperationId，不能恢复当前 `ExecuteAdhocWorkflowInstance`；`ClientUniqueCode` 也没有文档保证提交幂等。保留查实例并核验冻结代码的思路。[临时工作流](https://help.aliyun.com/zh/dataworks/developer-reference/api-dataworks-public-2024-05-18-executeadhocworkflowinstance)、[异步结果](https://help.aliyun.com/zh/dataworks/developer-reference/api-dataworks-public-2024-05-18-getcreateworkflowinstancesresult)
- **直接让 SQLGlot 全面接管 MaxCompute 转写**：值得小范围验证作用域分析，但官方方言列表没有 MaxCompute，不能假设 Hive/Spark 完全兼容。先用本节已复现样例和真实 SQL 建兼容用例，再决定是否引入。[官方方言定义](https://github.com/tobymao/sqlglot/blob/main/sqlglot/dialects/dialect.py)

## 5. 验证记录与待补事项

- 已执行：指定 Python 环境下 `pytest scripts -q -p no:cacheprovider`，**187 passed**；另跑 `test_readonly.py`（25 例）、`test_inline.py`、`test_di_summary.py`，均通过。后三者的主要断言在 `main()` 中，不能只跑 pytest 就认为覆盖了它们。
- 补充复现：纯函数、假元数据、mock API、临时目录；SQL 的 CTE/NULL/重复键语义使用 SQLite 离线交叉检查，未作 MaxCompute 服务端兼容性认证。测试全过仍留下第 2 节问题，后续应先将这些反例纳入回归。
- 文档精简验收：同组 A/B/C/D 场景比较路由、取码来源、授权边界、是否正确保留零结果与业务歧义，再比较正文字符量；不要只检查字数。
- 本次无需补凭证。后续需补：脱敏的真实 API/日志响应以验证 SDK 契约；现有测试项目的只读访问以验证多级分区、下载与成本预估；若验证真实提交，再单独明确测试写入目标和运行范围。**这些未验证项不影响本次交付。**

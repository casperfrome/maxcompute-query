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


## 6. 第 1/2/3 节实施状态（2026-09-13 追加）

上文为 2026-09-12 的原始审查与当时复现，原文完整保留；其中“当前/目前/未实施”均指审查基线，不代表本节追加后的实现。第 4 节 API/SDK 增强未纳入此次实施，不以文档出现建议当作已完成接入。

| 范围 | 本轮实施状态与行为 |
|---|---|
| 入口与引用 | SKILL 按触发、执行边界、A/B/C/D、共用流程、澄清委派、参考索引重组，7,966 字符；命令、UDF、导出、错误与转换细节移入现有 references，保持项目/分区、授权、保存态、防重、脱敏和三层结果边界 |
| 保存态并发/参数 | 共享 OS session_lock 保护读清单至网络提交/落盘；进程崩溃释放锁。同来源参数不同值重复拒绝，相同重复允许，保存态到显式覆盖保持优先级 |
| 异常组 | Python 原生/backport 树形、嵌套、子异常和平台前缀纳入分析；完整组仍需评审，残缺结构 unknown/needs_diagnosis，不能靠 Success 或 handled 解释绕过 |
| inline | 按项目与关系作用域识别表；仅处理支持的物化链与单一输出，typed 建表、分区/重复/追加写、未知语句、依赖倒置、多输出和未替换变量拒绝；最终独立 WITH 与中间 CTAS 链暂拒绝；target 仍检查完整输入 |
| compare | NULL/重复键仅输出校验失败行；合法一对一输入再比较，存在性独立标记。未指定 measure 仅键匹配，不宣称值一致 |
| 分区检查 | 依据作用域与各表谓词核对，strict 对缺失和未知阻止执行；不以投影 ds、其他表过滤或元数据错误替代通过 |
| 取码/配置 | auto/history/saved 同名消歧；file-id 支持 auto/history 且核对名称，node-id 仅直接生产取码。共享 runtime_config，环境优先、凭证只来自原环境变量，help/离线工具不需本地 config 或云 SDK |
| 模板/README | 模板显式绝对 Python 和各脚本路径，采用宿主可用委派/提问能力；README 记录本地候选修复会话、预算和恢复，保持原节点不变 |
| DI | 缺有效列为不足以判断，多 reader/writer 暂不支持完整映射，不从首项给整体绿灯；DI CLI 支持 help，原 test_di_summary 断言纳入 pytest 且保留直接入口 |
| 评测 | 保留 26 场景并区分 live/offline；live 以当前查询证据为准，允许零条。4 个 fixture 文件明确替代缺附件或归档旧数字，合成/历史数字不作为最新线上期望 |

验证使用指定 D:\PythonVenv\Scripts\python.exe -X utf8 -B 和 UTF-8，未安装或升级依赖、未连接云端提交任务，也未修改忽略的本地 config.py。运行/配置/取码提交为 a77af41，SQL 提交为 32c3a8c；文档与 DI 单独提交，交付分支为 main，最终远端哈希见交付回复。本节的实现状态不宣称真实云契约、真实任务运行或业务核验已通过。

文档/DI 的首轮新增回归为 16 failed、2 passed，分别覆盖缺列、多节点、路由、引用及评测附件缺口；修复后另补非法参数块和缺失列列表，先观察 2 项失败再修复。技能 frontmatter 验证通过。A/B/C/D 自动检查属于文档结构与契约验收；26 个 manual 场景仍需分别执行，不据 pytest 通过宣称人工评测已完成。

最终独立验收另发现 DI 未展开 `${columns}`（字符串/对象列名）仍被视为同名映射；新增 2 项回归先失败后修复为 unknown。eval 13 的模拟 GetFile 字段已核对为 Data.File.Content 与 CommitStatus=0，缺失保存内容场景检查 Content，不沿用错误的 FileContent/IsCommit。


### 最终集成验收

- 指定 Python 3.10.11 环境全量命令：`D:\PythonVenv\Scripts\python.exe -X utf8 -B -m pytest scripts -q -p no:cacheprovider`，**329 passed（16.64 秒）**。原有三个直接运行入口也通过，断言已纳入 pytest。
- 10 项原问题均加入回归；首轮 SQL 反例 23 failed、运行/取码反例 20 failed，确认旧行为可复现后修复。独立复核另外覆盖 CTE 名称遮蔽、保留字列、尾注释、UDF/STRUCT 限定符、异常组缺根/因果链、DI 列变量及输出文件替换失败；失败时旧 SQL 文件不变。
- SKILL 最终 **7,966 字符，减少 43.7%**；A/B/C/D 文档契约、相对链接和附件检查通过。保留 **26 场景（7 live、19 offline）**及 4 个明确标注的 fixture，原审查全文前缀逐字一致。
- 验证只使用离线输入、SQLite 语义交叉检查、mock 和临时目录；未执行真实云任务、未认证 MaxCompute 服务端全部语法、未完成 26 项人工模型行为评测或线上业务验收。后续线上检查需要相应只读环境/脱敏响应，真实写入验证仍需明确目标与范围；本轮无需补凭证或用户操作。

## 7. 第 4 节两项高优先级实施状态（2026-09-13 追加）

以 `22722eb` 为基线，上述原始审查和前轮记录完整保留。本轮两项高优先级建议已实施；中优先级分区 API、成本预估及临时凭证接入仍未实施。沿用 PyODPS **0.12.6**，未升级依赖、修改本地配置或提交真实云任务。

| 范围 | 已完成行为 |
|---|---|
| 实例执行与恢复 | `run_sql()` 一次提交，等待前将实例 ID、项目和记录路径写 stderr 并刷新；当前目录 `.maxcompute-query-runs/` 原子保存 SQL、哈希、时间及状态，已忽略入库。完整 LogView 仅留本地；屏蔽 SDK 进度日志泄露签名 |
| 等待与失败 | 默认等待 600 秒；超时退出码 5，读取/导出失败为 6。已知实例保留恢复命令；无 ID 的异常提交标为未知，不自动重提或取消。`--instance-id` 路径仅 `get_instance()`，`--project` 仅覆盖本次调用 |
| 有限预览 | 同一显式 Tunnel reader，默认最多下载/展示 200 行，单进程，不改原 SQL。总量、下载量、展示量、截断与受限状态分别记录；受限预览总量未知。`sample` 同样有限读取，下载限制不减少扫描量 |
| 完整导出 | `tunnel=True, limit=False`，默认每批 10,000 行；逐批核对列顺序与累计总量。CSV 为 UTF-8 BOM、单表头；XLSX 流式写入，超过 1,048,575 条数据或 16,384 列拒绝并提示 CSV。零结果保留表头，文本结果不能导出为表格 |
| 文件发布 | 读取、写入、关闭和总量校验完成后才原子替换；失败保留旧文件。文件发布成功而最终记录失败时明确告警并保留成功结果。恢复导出从原实例重新下载，不续写半成品 |
| 文档 | SKILL、查询参考、两份模板及 README 同步实例恢复和预览边界；SKILL 当前 8,355 字符。26 个评测场景保留，未将离线测试包装为线上验收 |

验证使用 `D:\PythonVenv\Scripts\python.exe -X utf8 -B`（Python 3.10.11）：

- 全量 `-m pytest scripts -q -p no:cacheprovider`：**389 passed，26 subtests passed（21.94 秒）**；`test_readonly.py`、`test_inline.py`、`test_di_summary.py` 三个直接入口均通过。A/B/C/D 路由、链接、附件和离线入口检查通过。
- 首轮 CLI 新反例在旧实现上为 **10 failed、7 passed**；后端先确认缺失 API 和导出行为失败再实现。覆盖提交前后记录、超时/中断、不重提恢复、百万行有限预览、受限/未知总量、空结果、分批无重复、行数不符、关闭/替换失败及 Excel 上下限；临界值采用缩小容量的 mock，同时核对实际容量常量。
- 独立代码复核未发现待修正确性问题；核对本地 SDK 的等待、显式 reader 和迭代实现。实现按实例执行、结果导出、文档验收分组提交，最终 main 远端哈希见交付回复。

**未验证：**真实 MaxCompute 服务权限、结果保留期、网络故障及超大结果的线上表现，26 项人工模型行为和业务验收。现有故障覆盖使用 mock，不代表云端兼容性认证；后续需要对应项目的只读访问及实际实例。本轮无需补凭证或用户操作。

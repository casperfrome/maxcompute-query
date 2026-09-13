# 取数验证器模板

供编排者通过宿主当前可用的委派机制使用；一个模板只接一个自包含数据问题。单个小查询无需委派，不假定代理类型、主对话可见性或默认工作目录。编排者填入问题、已知表/字段/分区、要检验的断言、已有授权范围与预期产物路径；事实未知可继续探查，业务取舍未知则回传证据，不擅选答案。

## 执行环境与工具

以下为本项目的绝对路径；迁移工作区时由编排者传入实际绝对路径，不能拼接到脚本文件名后面。

```powershell
$taskPython = 'D:\PythonVenv\Scripts\python.exe'
$queryTool = 'D:\AllForCareer\数仓问题排查\.agents\skills\maxcompute-query\scripts\mc_query.py'
$fetchTool = 'D:\AllForCareer\数仓问题排查\.agents\skills\maxcompute-query\scripts\fetch_task_sql.py'
$validationTool = 'D:\AllForCareer\数仓问题排查\.agents\skills\maxcompute-query\scripts\build_validation_sql.py'
& $taskPython -X utf8 -B $queryTool desc tst_mc_prod.example_table
```

所有文本按 UTF-8 读写，PowerShell 用 `Get-Content -Encoding UTF8`。输入/输出文件由编排者提供明确位置。你只使用查询器执行 SELECT/WITH/DESC/SHOW/EXPLAIN，不自建云连接、不运行取回 SQL/DI/Python、不修改节点或调用临时工作流提交。

## 处理一个问题

先核实表名、字段、分区与口径，按需要 `list-tables/desc/partitions/sample`，再构造带每张分区表过滤的只读查询。默认项目不能替换已确认的跨项目表，生成 SQL 的 tmp 前缀规则见 [技能执行边界](../SKILL.md#执行边界)。`--strict` 缺失或未知都拦截，不能通过投影分区列、只过滤另一张表、或修改扫描范围消除问题。

表名、字段和语法错误据真实元数据自行纠正；平台/权限缺口如实回传。空结果核对分区和条件后可以有效，不为获得非空放宽过滤。口径有多个合理解释时记录候选、影响和已查事实，由编排者按宿主方式澄清；已有用户答案继续沿用。

改前/改后校验使用 [只读验证流程](../references/maxcompute_sql.md#离线生成与改前改后验证)。inline 仅在支持语法下生成；拒绝就解释原因，不用残留旧文件当新产物，不手工简化后声称整体等价。compare 遇 NULL/重复键只返回校验失败行，先检查 `diff_type,cnt` 的校验状态；指定度量且通过键校验的四类差异才可作为一对一比较结果；不带 --measure 时仅说明键匹配，未比较值。所有生成物仍经查询器检查后执行。

## 回传格式

- 结论：确认、证伪、真实数值或待决口径，限定到已查分区和范围。
- 关键数据：支撑判断的计数、分布、必要样本；允许零条，不引用固定历史数字。
- 最终 SQL：真正执行的只读 SQL 和本地文件路径，保留过滤和真实表身份。
- 执行证据：实际 instance_id，以及查询器提供的行数、扫描量、耗时等元信息；不可得标未知。模拟、缓存与线上执行分别标注。
- 决策点与缺口：需用户定义的口径/取舍及候选证据；技术性缺口另列，不把猜测当事实。

编排者依据原始 SQL、数字和实例证据合并结论，不能只转述本模板回报“没问题”。


查询证据按 [实例恢复与完整导出](../references/maxcompute_sql.md#查询实例恢复与完整导出)核对：默认等待 600 秒，超时/下载失败按原项目和 `--instance-id` 恢复，不自动重提或取消。stdout 为结果，stderr 含实例和运行证据，两者均需留存。预览默认只下载 200 行；分别回传总行数、下载/展示行数和截断/受限状态，未知保留未知，不能把预览当完整结果。需要统计结论时执行相应聚合查询，需要完整明细时用经总量核验的 `--save`；有限下载不减少 SQL 扫描量。不要把既有旧文件或失败导出半成品当本轮产物。

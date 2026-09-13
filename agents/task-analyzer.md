# 任务代码分析器模板

用于 B 审查、C 修改前分析、D PyODPS3 日志诊断。编排者填写对象或完整代码、来源/版本、期望改动、分区、已知 ID、已有日志和授权边界，再通过宿主可用的委派机制发出。不要假定代理类型或主对话可见性。默认只取码、读元数据与分析；PyODPS3 运行和恢复由单一持有授权的编排者管理。

## 明确绝对路径

```powershell
$taskPython = 'D:\PythonVenv\Scripts\python.exe'
$fetchTool = 'D:\AllForCareer\数仓问题排查\.agents\skills\maxcompute-query\scripts\fetch_task_sql.py'
$queryTool = 'D:\AllForCareer\数仓问题排查\.agents\skills\maxcompute-query\scripts\mc_query.py'
$validationTool = 'D:\AllForCareer\数仓问题排查\.agents\skills\maxcompute-query\scripts\build_validation_sql.py'
$debugTool = 'D:\AllForCareer\数仓问题排查\.agents\skills\maxcompute-query\scripts\debug_pyodps3.py'
& $taskPython -X utf8 -B $fetchTool example_task --search
```

所有 Python 调用带 `-X utf8 -B`，PowerShell 读取文本用 `-Encoding UTF8`。脚本与输入/输出路径分开传递，不在 fetch/query 的文件路径后追加另一个脚本名。

## SQL 与 DI 分支

有完整代码先阅读；只有任务/表名时按 [取码参考](../references/fetch_task_sql.md) 获取。自动取码生产优先、开发兜底；指定保存态只取 GetFile 原文，缺失不回退。多个精确同名文件或产出节点不选首个，用目录/file_id/node_id 核对；文件 ID 加名称时必须匹配。历史版本使用明确文件和版本，不把保存态当提交版本。

取回 SQL/DI 配置只读，不直接运行。通读产出粒度、唯一键、来源与 JOIN、窗口分组、过滤、临时依赖顺序及 UDF；未知 UDF 用查询器 `func/list-functions/resource` 读取注册信息和源码，二进制没有源码就标明缺口，不执行函数或取回 Python。少量结构探查与验证也只通过 mc_query，并保留每张分区表的范围。

疑点分为“代码可定论问题”和“需数据验证假设”；后一类写成包含表/字段/分区的独立问题，交编排者安排验证。C 另列方案依赖（重复键、时间格式、NULL/并列、JOIN 基数）及改后断言；当前分区无重复不能取消用户要求的唯一性。业务口径未知时先收集证据，回传候选和影响，不擅自选最早/最新或任意并列行。

涉及多临时表时按 [inline/compare 参考](../references/maxcompute_sql.md#离线生成与改前改后验证) 评估是否支持。分区写、重复写、未知语句或依赖倒置等拒绝转换，不能把拒绝当作仍可执行的普通告警；compare 的 NULL/重复键校验失败也不能当正常四类差异。报告具体不支持片段与未覆盖范围。

DI 按位置核对 reader/writer 列。缺有效列信息是“不足以判断”，多 reader/writer 是“暂不支持完整映射”；不能按首个节点宣布整条同步正确。列数不一致提示无法完整对应，改名仅要求核对，完全同名也不证明类型/主键/数据相同。MaxCompute 源侧可只读取数；Hologres 等目标侧需要宿主可用的相应连接，未查不写已核对。

## PyODPS3 分支

先读 [PyODPS3 调试参考](../references/pyodps3_debugging.md)。只贴日志就离线分析；完整控制台、traceback 和摘要分开读取，不提交或重跑制造日志。已有实例固定运行次数/API 版本，编辑器页面记录先核实映射，不混用文件 ID、实例 ID 和历史 ID。

保存态精确取码并留存类型、元信息和哈希；不本地 import/exec 取回代码。结合本轮实际代码、完整异常链、用户行号、SDK 路径与阶段分析，不以本地版本填远端未知项。ExceptionGroup 的树与子异常不能漏读，残缺组保持 unknown/needs_diagnosis；Success 中的完整 traceback 仍需解释，不能吞异常给成功背书。

分析实际项目、目标表、分区、参数及外部调用副作用；Dev/test 命名不证明只写测试环境。提交成功、远端运行成功、业务一致分别报告。代码、日志与签名 URL 展示脱敏，原文留本地，不整包回传。提交不确定先恢复当前轮，分析器不创建替代会话或调用提交接口。

## 回传结构

B/C：对象及来源/版本 → 粒度和关键依赖 → 带位置的代码问题 → 自包含数据验证问题 → C 的前提与改后断言 → 待决业务分叉及证据 → 本地代码路径和未覆盖范围。

D：对象/source/SHA256/实例与次数 → 提交、执行、业务三层状态 → 异常链与最后可证阶段 → 版本/配置/日志缺口 → 保留业务语义的本地修复建议 → 可执行但尚未完成的验证建议。

只回传必要片段、事实与可定位文件，不大段重复原文，不把模拟结果冒充真实云端验证。


查询证据按 [实例恢复与完整导出](../references/maxcompute_sql.md#查询实例恢复与完整导出)核对：默认等待 600 秒，超时/下载失败按原项目和 `--instance-id` 恢复，不自动重提或取消。stdout 为结果，stderr 含实例和运行证据，两者均需留存。预览默认只下载 200 行；分别回传总行数、下载/展示行数和截断/受限状态，未知保留未知，不能把预览当完整结果。需要统计结论时执行相应聚合查询，需要完整明细时用经总量核验的 `--save`；有限下载不减少 SQL 扫描量。不要把既有旧文件或失败导出半成品当本轮产物。

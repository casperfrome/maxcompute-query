# DataWorks 基础取码、消歧、保存态、版本与 DI

B/C 只有任务或表名时先读本文件；已提供完整代码不重复替换。取码本身不执行任务，SQL/DI 是审查对象，验证另写只读 SQL。取回 Python 不本地导入或执行；授权调试走 [PyODPS3 专用入口](pyodps3_debugging.md)。返回 [技能场景路由](../SKILL.md#场景路由)。

## 基础取码与候选身份

```powershell
$fetchTool = 'D:\AllForCareer\数仓问题排查\.agents\skills\maxcompute-query\scripts\fetch_task_sql.py'
# 名称或 MaxCompute 产出表名；auto 优先生产代码，空生产内容时采用匹配文件的开发内容
& 'D:\PythonVenv\Scripts\python.exe' -X utf8 -B $fetchTool example_task
# 关键字搜索必须带名称，不自动选择候选
& 'D:\PythonVenv\Scripts\python.exe' -X utf8 -B $fetchTool example --search
# 精确文件 ID 支持 auto；提供名称时必须与 GetFile 返回名称一致
& 'D:\PythonVenv\Scripts\python.exe' -X utf8 -B $fetchTool example_task --file-id 42 --save outputs/task.sql
# 仅凭生产节点 ID 直接取生产代码；不会回退到其他文件
& 'D:\PythonVenv\Scripts\python.exe' -X utf8 -B $fetchTool --node-id 91 --save outputs/prod.sql
```

42、91 和任务名均是合成示例，替换为当前核实的身份。多精确同名文件或多产出节点返回歧义，报告包含可得的目录、file_id、node_id；缺少某项时不能猜值。文件代码为空也不能改选同名另一任务。先查看候选与目录消歧，若多个目标都合理且无法靠用户上下文决定，按宿主可用方式澄清。

`--file-id` 与 `--node-id` 互斥。`--node-id` 只用于直接获取生产态，不能同时给名称，也不能与 saved、search、list-versions/get-version/diff 混用。`--search` 必须有名称，不能带文件 ID。普通精确文件取码会核对文件身份，优先生产内容，生产内容为空才使用该文件开发内容；API 权限错误不是“内容为空”，不能静默跳过错误。

## 严格保存态

```powershell
& 'D:\PythonVenv\Scripts\python.exe' -X utf8 -B $fetchTool --source saved --file-id 42 --save outputs/saved.py
& 'D:\PythonVenv\Scripts\python.exe' -X utf8 -B $fetchTool example_task --source saved
```

saved 只取 GetFile 当前保存内容，保留 UTF-8 原文和 SHA256、文件 ID、类型、修改/提交元信息与可得配置。1221 标为 PYODPS3；保存态不等于生产发布版或最近提交版。多个同名文件必须指定 ID；空保存代码或不存在时失败，绝不回退 ListFiles.content、生产或历史版本。`--source saved` 不与搜索/历史模式混用。只需审查可保存原文；需要写入目标预检和运行证据包用调试脚本 inspect。

## 历史版本

```powershell
# 文件 ID 可用于所有历史操作，名称可省略；同给名称时仍 cross-check
& 'D:\PythonVenv\Scripts\python.exe' -X utf8 -B $fetchTool --file-id 42 --list-versions
& 'D:\PythonVenv\Scripts\python.exe' -X utf8 -B $fetchTool --file-id 42 --get-version 7 --save outputs/v7.sql
& 'D:\PythonVenv\Scripts\python.exe' -X utf8 -B $fetchTool --file-id 42 --diff 6 7
```

先看版本列表中的时间、提交人、备注及当前生产标记，再选择版本。版本号不存在时退出 4，回列表核对。名称解析和产出反查每层都要唯一；不能按第一个同名文件查历史，也不能把名称相同但 node_id 不同的文件当生产节点对应文件。对“回退某版”先保留目标版本为基线，再修改本地文件。历史 API 可在保存内容为空时读取版本元信息，不因此改为 saved 运行。

常见退出码：2 参数解析错误，3 选择组合或输入无效，4 未找到/歧义，5 API 错误。API 不可用时保留操作、错误码、请求 ID 等安全诊断，不展示签名请求或凭证，不假称取码成功。

## DI 同步配置

DI 请按任务名查，MaxCompute 产出表反查不能定位 Hologres 目标表。auto 识别 DataX 配置后打印源、目标、写入模式、按位置列映射摘要；`--save` 对 DI 的 auto 结果保存摘要，saved 则保存原始内容。摘要不是原始配置备份。

- 源：reader 类型、数据源、表、分区/过滤与列顺序。分区未提供时标缺口，不能自动判断为非分区。
- 目标：writer 类型、库表、配置中的写入模式及 truncate；实际主键/更新语义需核对目标引擎与配置，不能仅凭字段名推断。
- 显式列数不等：无法建立完整一一对应，逐列查缺口；不能断言所有位置都错位。
- 列数相同但改名：按位置对照确认语义，名称不同本身不证明错误。
- 完全同名：仅名称与位置对应，仍未证明类型、数据、主键或同步结果一致。
- 列信息缺失、空列表、通配符、未展开参数（如 `${columns}`）或无有效名称：**不足以判断**，不能显示“0 列一致”或放行。
- 多 reader/writer：**暂不支持完整映射**，列出节点候选；按完整拓扑补充逐组核对，不从首项推出整体通过。

源在 MaxCompute 时可用查询器核实分区、行数和分布，真实零条不自动判异常。目标为 Hologres 等其他引擎时需宿主当前可用的相应只读能力；未接入或未查询则明确目标侧未验证，不把源侧成功当同步成功。

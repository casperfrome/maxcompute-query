# PyODPS3 保存态运行与日志诊断

用于 DataWorks PyODPS3 节点的保存态检查、用户已授权的单次运行，以及输出、traceback、执行日志诊断。只收到日志时可直接离线分析，不必连接 DataWorks。SQL 取数仍通过 `mc_query.py` 的只读校验。

## 命令与执行边界

以下命令在项目根目录运行，统一使用指定 Python 环境；节点名、日期和文件 ID 需替换为当前任务。`inspect` 和 `run-saved` 都支持名称或 `--file-id`、`--bizdate`、可重复的 `--param KEY=VALUE` 及可选 `--output-dir`。

```powershell
# 读取保存态及运行配置，保存快照并检查写入目标；不运行节点
& 'D:\PythonVenv\Scripts\python.exe' .agents/skills/maxcompute-query/scripts/debug_pyodps3.py inspect --file-id 505649513 --bizdate 20260911 --param bizdate=20260911 --output-dir outputs/pyodps3_inspect

# 仅在当前用户授权覆盖快照内副作用、且运行配置已核实时提交一次
& 'D:\PythonVenv\Scripts\python.exe' .agents/skills/maxcompute-query/scripts/debug_pyodps3.py run-saved --file-id 505649513 --bizdate 20260911 --param bizdate=20260911 --output-dir outputs/pyodps3_run

# 将下方路径替换为 run-saved 命令打印的实际诊断子目录，再续查已创建的实例
$pyodpsRunDir = 'outputs/pyodps3_run/<命令打印的时间_uuid子目录>'
& 'D:\PythonVenv\Scripts\python.exe' .agents/skills/maxcompute-query/scripts/debug_pyodps3.py status --run-dir $pyodpsRunDir --wait --poll-seconds 10 --timeout 1800
& 'D:\PythonVenv\Scripts\python.exe' .agents/skills/maxcompute-query/scripts/debug_pyodps3.py logs --run-dir $pyodpsRunDir

# 用户粘贴内容保存为 UTF-8 文本后离线诊断；不连接数据库
& 'D:\PythonVenv\Scripts\python.exe' .agents/skills/maxcompute-query/scripts/debug_pyodps3.py analyze-log --file outputs/user_error.log --output-dir outputs/pyodps3_log_analysis
```

`--output-dir` 指定输出根目录；命令会在其下创建新的时间与 UUID 子目录并打印实际路径。`status` / `logs` 的 `--run-dir` 必须使用包含运行清单的实际子目录，不能传输出根目录。每次运行的证据独立，可在等待超时或断开后继续收集已有实例。

`--bizdate` 显式覆盖保存态中的 `bizdate` 参数；若同时使用 `--param bizdate=...`，值必须一致。其他 `--param` 用于明确的参数覆盖，重复参数值冲突时报错。合并后仍含未解析调度表达式或含空白的参数值时明确报错，不猜测表达式含义，也不静默截断参数。

授权检查关注实际副作用：节点名以 `test` 开头、`EnvType=Dev` 或连接项目以 `_dev` 结尾，均不能证明脚本只写测试表。先检查明确项目的 SQL 目标、SDK `project=`、分区及上传调用；动态生成或经外部函数执行的写入无法静态确定时，保留“未知”并结合代码人工核实。静态扫描结果不等于完整的副作用证明。

已有用户授权覆盖当前快照及运行范围时直接继续，不重复请求确认；未覆盖的新目标或新分区不能沿用另一任务的授权。日志分析不需要运行授权。运行入口不能用于把用户未授权的 SQL 写操作包装成 Python 绕过只读查询通道。

## 保存态与提交

1. 通过文件 ID 获取保存态原文，记录文件名、类型、修改时间、提交状态、SHA256 和保存配置。文件类型 `1221` 为 PyODPS3；同名多候选必须消歧。保存内容不可用时停止，不退回生产版或最近提交版本。
2. 冻结代码原文与有效参数。快照不添加日志、不改表名前缀、不注入修复，不在本地导入或 `exec` 执行。业务日期和显式脚本参数分别按接口传递，不能假定 Python 正文中的 `${bizdate}` 会被替换。
3. 核实责任人、连接、资源组标识、参数及保存配置。保留保存态对应的资源组、镜像、CU，不自行切换。旧文件配置中的数字资源组 ID 与新 API 的标识不能直接视为相同；映射无法核实或权限不足时停止提交，报告所需标识或被拒绝的 API，不猜测、不改权限。
4. 使用官方 `ExecuteAdhocWorkflowInstance`，`EnvType=Dev`、脚本 `Type=PYODPS3`，传入冻结代码、业务日期、参数、责任人、连接和已核实的资源组。只运行这份临时工作流，不开启依赖任务；它不修改原节点保存态，也不向原节点提交或发布版本。
5. 记录工作流 ID，查找对应任务实例，获取运行次数、状态和实际执行代码。对实际执行代码计算哈希并与快照核对；不相等或平台未返回代码时单独报告，不能宣称执行的就是原文。

提交超时、断网或响应不确定时，不自动再次调用提交接口。记录“不确定是否已创建”，优先通过已返回标识恢复查询；无标识且无法查证时说明缺口。收到实例 ID 仅代表提交成功，远端任务仍可能失败。

## 日志与诊断包

默认每 10 秒查一次状态，单次等待最长 30 分钟。等待超时表示本地停止等待，不代表远端已取消或失败；保留实例 ID，使用 `status --run-dir ...` 继续查询。运行中按对应任务实例及运行次数获取日志，进入终态后再获取一次；日志暂未生成时保留“尚未返回”，不要诊断为空结果。

运行目录保存代码快照、运行清单与参数、工作流和任务实例 ID、状态、实际执行代码、日志以及诊断摘要。保留远端原文在本地；展示或发送给用户的摘要需脱敏，不打印 AK/SK、Token、签名 URL 查询参数或 `SKYNET_` 原值。原始证据包可能含敏感内容，不直接整包外发。用户粘贴的 HTML 实体（如 `&#x20;`）及 Markdown 转义只在解析副本还原，原文不变。

摘要按证据提取下列信息；未出现的字段留空，不能用本地版本或原计划版本补齐：

| 信息 | 证据与读法 |
|---|---|
| 运行环境 | Python、pandas、PyODPS 的实际版本日志；堆栈路径仅支持其明确显示的信息 |
| 失败位置 | 全部异常链、首个业务失败点、最终异常、用户代码行号与代码快照上下文 |
| 执行进度 | 阶段、SQL 实例 ID、读取/候选/输出/暂存行数、内存日志、退出码 |
| 结果层次 | 提交成功与否、远端执行成功与否、业务数据是否经过独立核验 |
| 证据限制 | 日志尚未生成、截断、缺失开头或结尾、执行代码不可得、哈希不一致 |

平台日志有容量限制（接口日志最多约 4 MB）。保留截断或缺页迹象；没有出现某条成功日志不能单独证明该阶段未执行。打印的 DataFrame 通常也是摘要，不能当成完整结果；完整行数、键或字段一致性需要额外只读查询、完整导出或同一输入的本地回放证据。没有这些证据时标为“业务结果未验证”。

## 诊断时的关键区分

- `groupby() got an unexpected keyword argument 'dropna'`：当前 pandas 的该接口不接受此参数；检查实际版本与全部调用点，不能只删除启动检查。若需要兼容旧版，NULL 分组键语义仍应保留。此错误本身不能给出精确 pandas 版本。
- `平台连接项目必须为 ... 当前为 ..._dev`：先区分脚本主动校验与平台权限错误，再查所有 SQL/SDK 表项目归属。不能为消除异常直接改写到生产项目，也不能认为开发连接不会写生产表。
- SDK 读取堆栈中 `_calc_count` 对 `None` 作除法：结合 `read` / `to_pandas` 调用参数及该 SDK 版本检查步长，不能判为 SQL 无数据或取消完整读取与行数核对。堆栈中的 `python3.7` 说明实际路径，与计划镜像不一致时明确报告。
- `WARNING:odps.pyodpswrapper` 与 `Shell run failed` 常为外层汇总，根因通常在前面的异常链；不要把最后一行当成唯一根因。

上述是诊断线索，不是无条件自动修复规则。按用户请求给出修复建议或本地候选补丁；运行失败后不自动更新保存态、不重跑、不切换镜像。任何修复验证都明确区分本地模拟、缓存业务数据回放和真实节点运行。

## 官方依据

提交及采集接口变动或参数有疑问时查看官方文档，不通过控制台内部接口绕行：

- [ExecuteAdhocWorkflowInstance](https://help.aliyun.com/zh/dataworks/developer-reference/api-dataworks-public-2024-05-18-executeadhocworkflowinstance)：临时工作流运行。
- [ListTaskInstances](https://help.aliyun.com/zh/dataworks/developer-reference/api-dataworks-public-2024-05-18-listtaskinstances)、[GetTaskInstance](https://help.aliyun.com/zh/dataworks/developer-reference/api-dataworks-public-2024-05-18-gettaskinstance)：任务实例与运行信息。
- [GetTaskInstanceLog](https://help.aliyun.com/zh/dataworks/developer-reference/api-dataworks-public-2024-05-18-gettaskinstancelog)：按实例及运行次数获取日志。
- [PyODPS 3 节点](https://help.aliyun.com/zh/dataworks/user-guide/pyodps-3-node)、[PyODPS DataWorks 参数与平台入口](https://pyodps.readthedocs.io/zh-cn/stable/platform-d2.html)、[SQL 结果完整读取](https://pyodps.readthedocs.io/zh-cn/stable/base-sql.html)：运行环境、参数和 SDK 行为。

验收报告分别列出本地测试、真实提交、真实运行及业务核验。资源组映射未解决等未完成项应如实记录；文档和示例不代表节点已成功运行。

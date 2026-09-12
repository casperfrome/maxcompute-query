# PyODPS3 日志诊断、修复与运行验证

用于 DataWorks PyODPS3 节点的完整控制台与 traceback 采集、本地代码修复、临时工作流验证和继续修复。`debug_pyodps3.py` 负责读取与单次保存态调试；`repair_pyodps3.py` 管理有预算、可恢复的修复会话；两者共用 `pyodps3_runtime.py` 的 API、配置解析和运行观测。Codex 分析日志并编写候选，脚本不自动猜测修复。最终代码仅留本地，不更新、保存、提交或发布原节点。SQL 取数仍通过 `mc_query.py` 的只读校验。

## 先核实运行标识，再选择日志入口

| 来源 | 入口 |
|---|---|
| 周期调度、补数据、测试等运维实例 | `instances` 列候选，`logs --instance-id` 直接读取 |
| 本脚本已有诊断目录 | `logs --run-dir`，兼容旧保存态目录 |
| 数据开发编辑器点击“运行/带参运行” | 先核实是否存在任务实例映射；无法映射时从“运行历史”采集或导入文本 |
| 已复制或下载的日志 | `analyze-log --file` 或 `--stdin`，无需凭证 |

文件 ID、任务 ID、工作流实例 ID、任务实例 ID、MaxCompute SQL 实例 ID 和页面运行记录标识不能互换。只根据节点、工作空间、时间和实例元数据确认映射，不能只凭外观或编号相同。读取已有实例不需要代码快照、资源组映射或新运行。仅为读日志时不提交；用户已授权修复与验证、且无旧日志可用时，可以提交一次冻结基准临时运行，计入会话预算。后续候选统一从临时工作流返回的任务实例读取日志。

### 运维实例：列出、选定、读取

```powershell
$pyodpsTool = 'D:\AllForCareer\数仓问题排查\.agents\skills\maxcompute-query\scripts\debug_pyodps3.py'

# 业务日期必填，默认 Prod；--name 支持模糊匹配，也可改用 --task-id
& 'D:\PythonVenv\Scripts\python.exe' -X utf8 -B $pyodpsTool instances --name test_job --bizdate 20260911 --env Prod

# 开发环境的冒烟测试可增加运行类型筛选
& 'D:\PythonVenv\Scripts\python.exe' -X utf8 -B $pyodpsTool instances --name test_job --bizdate 20260911 --env Dev --workflow-instance-type SmokeTest

# 替换为上一步核实的任务实例 ID。元信息写 stderr，控制台正文写 stdout
& 'D:\PythonVenv\Scripts\python.exe' -X utf8 -B $pyodpsTool logs --instance-id 910172566372 --run-number 1 --format text
& 'D:\PythonVenv\Scripts\python.exe' -X utf8 -B $pyodpsTool logs --instance-id 910172566372 --run-number 1 --format traceback
```

候选列表包含实例 ID、节点、运行次数、状态、业务日期和开始时间，并回显项目、环境、日期及运行类型筛选；按用户指定的日期、环境和运行时间消歧，有多个合理候选时不要擅选。查询为空仅说明当前条件下没有可见记录，应核对项目、环境、业务日期和运行类型。业务日期与实际开始日期可能不同，`--bizdate` 是 Asia/Shanghai 当天零点，不能拿脚本参数中恰好同名的日期代替实例业务日期。

`--workflow-instance-type` 支持 `Normal`、`Manual`、`SmokeTest`、`SupplementData`、`ManualWorkflow`、`TriggerWorkflow`，省略时不限定运行类型。

`logs` 的 `--instance-id` 与 `--run-dir` 互斥。前者自动创建诊断子目录，可用 `--output-dir` 指定根目录；后者原地续读。新目录默认 API 版本 `2024-05-18`，新版未指定 `--run-number` 时先查当前次数，并固定到该次运行；续读目录也保持该次数，主动改读另一次才显式传 `--run-number`。历史状态从平台历史记录取得；查不到时状态留空，不能套用最新状态。旧保存态目录仍核对工作流、项目和开发环境身份。

旧版后端显式使用 `--api-version 2020-05-18`，直接实例读取必须传 `--env Dev|Prod`。历史标识由 `histories` 获取，使用 `--instance-history-id`；不能把新版运行次数填成旧版历史 ID，也不能因权限错误静默回退另一版本：

```powershell
# 只列旧版历史候选，不擅选；返回缺少历史 ID 时不能猜测
& 'D:\PythonVenv\Scripts\python.exe' -X utf8 -B $pyodpsTool histories --instance-id 910172566372 --env Prod

# 读取旧版当前运行；如明确选中历史，则增加 --instance-history-id <返回的历史ID>
& 'D:\PythonVenv\Scripts\python.exe' -X utf8 -B $pyodpsTool logs --instance-id 910172566372 --api-version 2020-05-18 --env Prod --format traceback
```

续读诊断目录默认沿用记录的版本、环境和历史选择；换 API 版本需新建目录。旧版未选历史时在读取前后核对实例及开始时间，防止重跑把日志换成另一轮；开始时间变化时保留旧缓存并报告错误。没有稳定开始时间只能单次读取，标记 `selection_unverified`，不能等待、续读或据此宣称验证成功。旧版路径只支持日志证据，不提供修复会话要求的完整代码和节点类型验证。

`--format json`（默认）输出摘要及本地证据路径；`text` 输出完整脱敏控制台正文；`traceback` 输出保留堆栈、代码行、多行消息及因果连接的异常链。`--tail N` 仅可与 `text` 联用，原始日志仍完整落盘。平台合并返回的 stdout/stderr 保持原顺序，不人为拆分。输出没有 traceback 时也保留普通打印和警告。

`--wait --poll-seconds 10 --timeout 1800` 可等待运行及日志；终态但空日志仍可继续等待。权限错误等获取失败立即返回，不反复请求。清单中 `log_state` 为 `available`、`pending` 或 `error`；`log_available` 表示本地有可读内容，`log_cached=true` 表示本轮未取到新日志，展示的是以前的证据。元数据刷新失败另有 `status_cached`、`status_error`，不能把缓存状态当实时状态。

`logs` 退出码：0 表示本次读取流程正常（无日志时仍需看 `log_state`）；1 为选中运行远端失败；3 为输入/文件/身份校验错误；5 为日志或状态获取错误；6 为保存态代码哈希不匹配；7 为本地等待超时。获取错误优先于远端失败，所有状态仍保留在结果中；退出码不代替业务验收。

### 页面调试：浏览器运行历史 → 文本解析

先核对已有记录是否能映射到任务实例，核实后使用 API；找不到映射不表示后台没有接口。使用当前可用的浏览器工具及其使用说明，优先复用已登录的 DataWorks 标签页。进入目标工作空间的数据开发“运行历史”，按节点名称和运行时间筛选，核对记录状态、时间和用户指定的运行后，打开对应运行日志。不要把页面记录 ID 当作 `--instance-id`，也不要因为运维 API 返回空列表就认定页面没有运行过。

优先使用页面提供的全文复制或下载入口，得到日志文本后交给脚本；没有全文入口时通过浏览器工具逐段读取可见日志，保持顺序与换行，检查分页、虚拟滚动、折叠和“加载更多”。不要把整页导航或可访问性树当成日志正文，也不要仅凭截图 OCR 宣称日志完整。只读到局部内容时传 `--partial`。采集文件应记录来源页面、节点、运行时间及采集范围作为单独的元信息，不能把这些说明拼入原始日志。

读取期间只操作历史筛选和日志查看/复制/下载，不点击运行、重跑、保存或发布。浏览器连接失败时报告工具实际错误；未登录时说明需恢复 DataWorks 登录。继续处理已有运维实例或日志文本，不用重新运行节点制造日志、不导出 Cookie，也不编造控制台日志端点。控制台请求采集器须先用真实样本核实鉴权、标识、分页和结束标志，再实现及验收；当前不能宣称稳定支持。`GetSemanticJobLog` 仅是语义任务的线索，不能套用到普通 PyODPS3。ActionTrail 只能辅助查元数据，不是控制台日志仓库，其权限缺失不阻断其他采集入口。

```powershell
# 浏览器复制或下载的日志按 UTF-8 保存后读取；--partial 仅用于采集不全
& 'D:\PythonVenv\Scripts\python.exe' -X utf8 -B $pyodpsTool analyze-log --file outputs/page_console.log --format traceback --output-dir outputs/page_console_analysis

# 从已有 UTF-8 文件经标准输入传递；Windows PowerShell 必须设置管道编码
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Get-Content -LiteralPath 'outputs/page_console.log' -Raw -Encoding UTF8 | & 'D:\PythonVenv\Scripts\python.exe' -X utf8 -B $pyodpsTool analyze-log --stdin --partial --format text
```

文件入口保留输入字节；标准输入保存接收到的 UTF-8 内容，PowerShell 管道可能追加换行，需字节一致时优先 `--file`。离线分析不连接 DataWorks，无法从日志中的成功文字推断平台实时状态。

## 本地候选修复会话

用户目标是“修好并验证”时使用此入口；只读已有日志继续用前面的 `debug_pyodps3.py`。默认最多 3 次远端提交，已有日志导入不计次数，新提交的基准运行计次数。以下命令在项目根目录执行，使用 UTF-8 文件；示例节点、日期和候选内容均须替换为当前任务。

```powershell
$pyodpsRepair = 'D:\AllForCareer\数仓问题排查\.agents\skills\maxcompute-query\scripts\repair_pyodps3.py'

# 获取严格保存态，冻结基准、参数和配置，执行只读预检，不提交
& 'D:\PythonVenv\Scripts\python.exe' -X utf8 -B $pyodpsRepair init --name test_job --bizdate 20260911 --max-attempts 3 --output-dir outputs/pyodps3_repair

# 使用上一步打印的实际会话子目录；不要传输出根目录
$pyodpsSession = 'outputs/pyodps3_repair/<实际会话子目录>'

# Codex 根据基准代码和完整 traceback 生成本地候选，再冻结为一轮
& 'D:\PythonVenv\Scripts\python.exe' -X utf8 -B $pyodpsRepair prepare --session-dir $pyodpsSession --file outputs/candidate.py --reason '修复日期解析，保留原处理范围和必要校验'

# 默认等待运行和日志；只发起一次提交，原节点不变
& 'D:\PythonVenv\Scripts\python.exe' -X utf8 -B $pyodpsRepair run --session-dir $pyodpsSession --poll-seconds 10 --timeout 1800

# 超时、断开或日志暂缺后恢复当前轮，不重新提交
& 'D:\PythonVenv\Scripts\python.exe' -X utf8 -B $pyodpsRepair resume --session-dir $pyodpsSession

# 已确认代码执行失败时，读取本轮日志、修复候选，再 prepare 和 run
# 成功且证据满足要求后导出；不满足时报告未通过的候选
& 'D:\PythonVenv\Scripts\python.exe' -X utf8 -B $pyodpsRepair report --session-dir $pyodpsSession
```

`init` 用 `--file-id` 精确选文件，与 `--name` 互斥。可使用 `--log-run-dir` 关联已有诊断目录，或 `--log-file` 导入日志文本；部分日志加 `--log-partial`。导入证据没有代码哈希或与基准不同时标明不一致/未知，不能直接按旧行号修改新代码。`--param KEY=VALUE` 可重复；显式配置覆盖为 `--resource-group-id`、`--data-source`、`--cu`、`--image`，配置来源与保存态差异留存。无明确覆盖时使用可核实的保存配置，不能为消除环境错误随意切换。

没有可用旧日志时，先将本会话 `snapshot.py` 作为 `prepare --file` 的输入，修复说明填“获取基准运行证据”，再运行这一轮；不要凭空修改或猜测 traceback。基准运行失败后，再据其日志创建下一候选。

`prepare` 仅做语法检查和冻结，不在本地 import/exec 候选。每轮目录含 `candidate.py`、`changes.patch`、`reason.txt`、代码哈希、有效配置、`request.json` 与 `manifest.json`。修改外部候选文件不会改变已冻结轮次；下一候选必须重新 `prepare`。基准不可变，真实提交只替换临时工作流的内联代码，不能把候选验证称为“原保存态原文运行”。

`run --attempt N` 可明确选轮次，省略时选择当前准备轮；`--no-wait` 提交后即返回，后续用 `resume`。会话采用系统文件锁与原子清单写入，避免并发提交；唯一工作流标识和请求在发送前落盘。只读请求可做有界重试，提交请求不自动重试。提交响应不确定也占一次预算，`resume --workflow-instance-id ID` 可提供已核实的工作流；无工作流 ID 时，恢复先用 `ListTaskInstances` 按业务日期、项目、Dev 环境和节点筛候选，再用 `GetWorkflowInstance` 核对唯一工作流名，最后用 `GetTaskInstance` 核对候选代码和配置。实测 `ListWorkflowInstances` 未列出临时 `ManualFlow`，不能因其空结果认定未提交。每轮 `recovery/*.json` 保存查询条件、候选及核对结果；查不到、多匹配、代码不符都不能重新 `run` 或开新会话绕过防重。

每轮按下面的证据决定下一步：

| 当前情况 | 操作 |
|---|---|
| 运行中、等待超时、终态但日志暂缺、获取失败 | 保留并恢复当前轮；明确缓存及缺口，不把采集错误当 Python 错误 |
| 有明确代码执行失败且日志足以诊断 | 对照本轮实际代码、完整异常链、环境及进度修改候选；授权范围内继续准备下一轮 |
| 提交结果不确定 | 核查已知工作流或唯一标识，不能自动重发 |
| 同一代码和配置已提交 | 不无依据重复提交；恢复原轮或给出新的有证据修复 |
| 连续两轮同异常、同位置且进度无改善，或预算已用完 | 停止并交付失败证据和未验证候选，不为用满预算继续试跑 |
| Success 但日志有 traceback，处于 `needs_traceback_review` | 核对代码及日志，用 `report --traceback-review` 留存判断；符合预期选 `handled`，仍有代码问题选 `unresolved` 后继续修复 |
| 成功且非空跑、实际代码与候选相符、可返回配置一致、日志证据足够且异常已核实 | 运行验证通过；业务断言另做只读核验 |

临时工作流 API 实测返回 `TriggerType=Manual`、`TriggerRecurrence=Manual`。`TriggerRecurrence` 的调度语义不能单独用来否定手动运行；非空跑还需核对 `Runtime.ProcessId`、有效的 `StartedTime` / `FinishedTime`、控制台内容与 Shell 退出码。成功验证需要退出码 0，不能仅要求字段为 `Normal`，也不能只凭状态 Success 通过。

修复先定位异常链中的业务失败点及最终异常，再检查周边代码和运行环境；不要只改最后一行 `Shell run failed`。每次提交前复核差异和实际副作用。当前用户已授权的节点、目标、分区和参数范围内继续，不重复请求许可；新增写入目标或改变执行范围才重新确认。技术失败不能用吞异常、删除计数/完整读取校验或缩小数据范围来“修复”。

`report` 导出前重新核验执行证据哈希；证据通过时生成会话根目录中的 `final.py`、`final.patch` 及 `report.md` / `report.json`，未通过时导出 `candidate_unverified.py` 与对应差异。状态降级后移除旧 `final.py`，不能拿历史成功文件代表当前验证结果。

Success 中有 traceback 时，先读取代码的异常处理和完整日志，只对 `needs_traceback_review` 轮次记录评审：`--traceback-outcome handled`（默认）表示异常符合预期且已处理；`unresolved` 表示仍存在代码问题，将本轮标记为 `needs_fix`，可继续 `prepare` 下一候选。两种判断都需 `--traceback-review '具体证据与解释'`，不能用空泛说明给吞异常制造的成功背书。

```powershell
# 已核实异常为预期且已处理；说明必须对应真实代码与日志证据
& 'D:\PythonVenv\Scripts\python.exe' -X utf8 -B $pyodpsRepair report --session-dir $pyodpsSession --traceback-outcome handled --traceback-review '说明异常处理语义及验证证据'

# 已核实仍有问题：保留未通过结果，修改候选后继续 prepare/run
& 'D:\PythonVenv\Scripts\python.exe' -X utf8 -B $pyodpsRepair report --session-dir $pyodpsSession --traceback-outcome unresolved --traceback-review '必要处理失败后被捕获，成功状态未反映业务失败；指出代码位置与日志证据'
```

评审绑定本次日志哈希，日志内容刷新后必须重新评审，不能把旧解释套到新增异常。评审不能绕过代码/配置不匹配、空跑、证据哈希改变、缺日志或采集错误；这些情况先恢复证据或报告缺口。未返回的镜像或版本保留未知，不能以本地环境或计划配置补齐。

交付给用户的是本地 Python 文件、主要修改与差异、各轮工作流/任务实例和日志路径、停止原因或运行验证结果。平台 Success 不能代表业务字段、行数或结果一致；没有单独完成业务断言时明确“业务结果未验证”。不更新、保存、提交或发布 DataWorks 原节点。

## 单次保存态调试与执行边界

以下命令在项目根目录运行，统一使用指定 Python 环境；节点名、日期和文件 ID 需替换为当前任务。`inspect` 和 `run-saved` 都支持名称或 `--file-id`、`--bizdate`、可重复的 `--param KEY=VALUE` 及可选 `--output-dir`。

```powershell
# 读取保存态及运行配置，保存快照并检查写入目标；不运行节点
& 'D:\PythonVenv\Scripts\python.exe' -X utf8 -B 'D:\AllForCareer\数仓问题排查\.agents\skills\maxcompute-query\scripts\debug_pyodps3.py' inspect --file-id 505649513 --bizdate 20260911 --param bizdate=20260911 --output-dir outputs/pyodps3_inspect

# 仅在当前用户授权覆盖快照内副作用、且运行配置已核实时提交一次
& 'D:\PythonVenv\Scripts\python.exe' -X utf8 -B 'D:\AllForCareer\数仓问题排查\.agents\skills\maxcompute-query\scripts\debug_pyodps3.py' run-saved --file-id 505649513 --bizdate 20260911 --param bizdate=20260911 --output-dir outputs/pyodps3_run

# 将下方路径替换为 run-saved 命令打印的实际诊断子目录，再续查已创建的实例
$pyodpsRunDir = 'outputs/pyodps3_run/<命令打印的时间_uuid子目录>'
& 'D:\PythonVenv\Scripts\python.exe' -X utf8 -B 'D:\AllForCareer\数仓问题排查\.agents\skills\maxcompute-query\scripts\debug_pyodps3.py' status --run-dir $pyodpsRunDir --wait --poll-seconds 10 --timeout 1800
& 'D:\PythonVenv\Scripts\python.exe' -X utf8 -B 'D:\AllForCareer\数仓问题排查\.agents\skills\maxcompute-query\scripts\debug_pyodps3.py' logs --run-dir $pyodpsRunDir

# 用户粘贴内容保存为 UTF-8 文本后离线诊断；不连接数据库
& 'D:\PythonVenv\Scripts\python.exe' -X utf8 -B 'D:\AllForCareer\数仓问题排查\.agents\skills\maxcompute-query\scripts\debug_pyodps3.py' analyze-log --file outputs/user_error.log --output-dir outputs/pyodps3_log_analysis
```

`--output-dir` 指定输出根目录；命令会在其下创建新的时间与 UUID 子目录并打印实际路径。`status` / `logs` 的 `--run-dir` 必须使用包含运行清单的实际子目录，不能传输出根目录。每次运行的证据独立，可在等待超时或断开后继续收集已有实例。

`--bizdate` 显式覆盖保存态中的 `bizdate` 参数；若同时使用 `--param bizdate=...`，值必须一致。其他 `--param` 用于明确的参数覆盖，保存态与显式参数分别校验，同一来源重复且值不同时报错，相同重复可接受；保存态到显式覆盖保留优先级，不能靠最后一个 bizdate 掩盖前一个冲突。合并后仍含未解析调度表达式或含空白的参数值时明确报错，不猜测表达式含义，也不静默截断参数。

授权检查关注实际副作用：节点名以 `test` 开头、`EnvType=Dev` 或连接项目以 `_dev` 结尾，均不能证明脚本只写测试表。先检查明确项目的 SQL 目标、SDK `project=`、分区及上传调用；动态生成或经外部函数执行的写入无法静态确定时，保留“未知”并结合代码人工核实。静态扫描结果不等于完整的副作用证明。

已有用户授权覆盖当前快照及运行范围时直接继续，不重复请求确认；未覆盖的新目标或新分区不能沿用另一任务的授权。日志分析不需要运行授权。运行入口不能用于把用户未授权的 SQL 写操作包装成 Python 绕过只读查询通道。

## 保存态原文运行与提交

1. 通过文件 ID 获取保存态原文，记录文件名、类型、修改时间、提交状态、SHA256 和保存配置。文件类型 `1221` 为 PyODPS3；同名多候选必须消歧。保存内容不可用时停止，不退回生产版或最近提交版本。
2. 冻结代码原文与有效参数。快照不添加日志、不改表名前缀、不注入修复，不在本地导入或 `exec` 执行。业务日期和显式脚本参数分别按接口传递，不能假定 Python 正文中的 `${bizdate}` 会被替换。
3. 核实责任人、连接、资源组标识、参数及保存配置。默认沿用保存配置；用户指定的资源组、数据源、CU、镜像覆盖需记录来源和差异。旧数字资源组 ID 通过平台记录映射到新标识，或由显式标识核实，不能根据名称近似或数字后缀猜测。只读预检报告凭证、依赖、类型、参数及资源配置缺口；无法核实时不提交，不自动创建资源或调整权限。
4. 使用官方 `ExecuteAdhocWorkflowInstance`，`EnvType=Dev`、脚本 `Type=PYODPS3`，传入冻结代码、业务日期、参数、责任人、连接和已核实的资源组。只运行这份临时工作流，不开启依赖任务；它不修改原节点保存态，也不向原节点提交或发布版本。
5. 记录工作流 ID，查找对应任务实例，获取运行次数、状态和实际执行代码。对实际执行代码计算哈希并与快照核对；不相等或平台未返回代码时单独报告，不能宣称执行的就是原文。

提交超时、断网或响应不确定时，不自动再次调用提交接口。记录“不确定是否已创建”，优先通过已返回标识恢复查询；无标识且无法查证时说明缺口。收到实例 ID 仅代表提交成功，远端任务仍可能失败。

## 日志与诊断包

默认每 10 秒查一次状态，单次等待最长 30 分钟。只读 Get/List 请求对超时、限流和临时服务错误最多尝试 3 次；权限及参数错误立即报告。等待超时表示本地停止等待，不代表远端已取消或失败；保留实例 ID，使用 `logs --run-dir ...` 继续查询所选运行的日志和状态。修复会话使用 `resume`，不要向 `debug_pyodps3.py` 的 `--run-dir` 传会话根目录。运行中按对应任务实例及运行次数获取日志，进入终态后读取终态日志；日志暂未生成时保留“尚未返回”，不要诊断为空结果。

保存态运行目录保存代码快照、运行清单与参数、工作流和任务实例 ID、状态、实际执行代码、日志以及诊断摘要。直接实例日志目录只保存实例元信息、运行清单、日志及摘要，不创建或冒充代码快照。保留远端原文在本地；展示或发送给用户的内容需脱敏，不打印 AK/SK、Token、签名 URL 查询参数或 `SKYNET_` 原值。原始证据包可能含敏感内容，不直接整包外发。用户粘贴的 HTML 实体（如 `&#x20;`）及 Markdown 转义只在解析副本还原，原文不变。

摘要按证据提取下列信息；未出现的字段留空，不能用本地版本或原计划版本补齐：

| 信息 | 证据与读法 |
|---|---|
| 运行环境 | Python、pandas、PyODPS 的实际版本日志；堆栈路径仅支持其明确显示的信息 |
| 失败位置 | 全部异常链、首个业务失败点、最终异常、用户代码行号与代码快照上下文 |
| 执行进度 | 阶段、SQL 实例 ID、读取/候选/输出/暂存行数、内存日志、退出码 |
| 结果层次 | 提交成功与否、远端执行成功与否、业务数据是否经过独立核验 |
| 证据限制 | 日志尚未生成、截断、缺失开头或结尾、执行代码不可得、哈希不一致 |

全量保存本次接口返回的内容；这不等于恢复了平台未保留的全文。采集成功、读到当前末尾、平台截断与是否可证完整分别记录，分析字段 `completeness` 为 `unknown` 或 `partial`，`truncation_evidence` 保存具体依据；没有已知截断证据时也保留“未知”。明确的平台截断、清理或过期标记会设 `log_recovery=unrecoverable_by_log_api`；仅页面部分采集或接近大小限制时保留 `unknown`，不要把这类缺口误写成不可恢复。平台日志约 4 MB 上限附近、明确截断提示或页面缺页都要留证，已清理或截断部分不能宣称可恢复；没有出现某条成功日志不能单独证明该阶段未执行。打印的 DataFrame 通常也是摘要，不能当成完整结果；完整行数、键或字段一致性需要额外只读查询、完整导出或同一输入的本地回放证据。没有这些证据时标为“业务结果未验证”。

## 异常组与共享锁

解析支持 Python 3.11 与 backport 的 `ExceptionGroup`/`BaseExceptionGroup` 树形前缀、嵌套组、子异常和平台日志前缀，展示保留树形文本。完整组配合 Success 和退出码 0 仍进入 `needs_traceback_review`；不完整、裁剪或无法核实声明子异常数的组标 `traceback_parse_status=unknown`，转 `needs_diagnosis`，不能用 handled 评审跳过。远端 Failure 保留失败；日志解析完整不等于平台日志完整。

单次 `submit_saved` 与修复会话共用 `pyodps3_runtime.session_lock`，修复模块仍保留同名兼容入口。锁覆盖状态读取、预检、冻结、网络提交和最终落盘；进程崩溃后系统释放锁，磁盘残留锁文件不代表仍占用。相同目录的并发请求只允许一次提交，不能自行删锁文件或另建目录绕过防重。

共享 `runtime_config` 在导入/help/离线日志时无需 config.py、凭证或云 SDK，仅实际连接前校验。可选 config.py 只放非密钥配置，环境变量覆盖；错误配置不能阻断离线分析。参数与配置缺口和用户是否授权分别判断，不能为帮助页或日志分析要求用户补凭证。

## 诊断时的关键区分

- `groupby() got an unexpected keyword argument 'dropna'`：当前 pandas 的该接口不接受此参数；检查实际版本与全部调用点，不能只删除启动检查。若需要兼容旧版，NULL 分组键语义仍应保留。此错误本身不能给出精确 pandas 版本。
- `平台连接项目必须为 ... 当前为 ..._dev`：先区分脚本主动校验与平台权限错误，再查所有 SQL/SDK 表项目归属。不能为消除异常直接改写到生产项目，也不能认为开发连接不会写生产表。
- SDK 读取堆栈中 `_calc_count` 对 `None` 作除法：结合 `read` / `to_pandas` 调用参数及该 SDK 版本检查步长，不能判为 SQL 无数据或取消完整读取与行数核对。堆栈中的 `python3.7` 说明实际路径，与计划镜像不一致时明确报告。
- `WARNING:odps.pyodpswrapper` 与 `Shell run failed` 常为外层汇总，根因通常在前面的异常链；不要把最后一行当成唯一根因。

上述是诊断线索，不是无条件替换规则。按实际代码及日志修复本地候选；不得通过吞异常、删除必要校验或缩小处理范围制造成功。授权范围内按修复会话继续验证，配置变更要留证，新增目标或改变运行范围需确认。任何验证都区分本地模拟、缓存数据回放和真实远端执行。

## 官方依据

提交及采集接口参数有疑问时查看官方文档；没有真实契约或请求样本的入口不列为已支持：

- [ExecuteAdhocWorkflowInstance](https://help.aliyun.com/zh/dataworks/developer-reference/api-dataworks-public-2024-05-18-executeadhocworkflowinstance)：临时工作流运行。
- [ListTaskInstances](https://help.aliyun.com/zh/dataworks/developer-reference/api-dataworks-public-2024-05-18-listtaskinstances)、[GetTaskInstance](https://help.aliyun.com/zh/dataworks/developer-reference/api-dataworks-public-2024-05-18-gettaskinstance)：任务实例与运行信息。
- [GetTaskInstanceLog](https://help.aliyun.com/zh/dataworks/developer-reference/api-dataworks-public-2024-05-18-gettaskinstancelog)：按实例及运行次数获取日志。
- [GetInstanceLog](https://help.aliyun.com/zh/dataworks/developer-reference/api-dataworks-public-2020-05-18-getinstancelog)、[ListInstanceHistory](https://help.aliyun.com/zh/dataworks/developer-reference/api-dataworks-public-2020-05-18-listinstancehistory/)：旧版实例日志及独立历史标识。
- [PyODPS 3 节点](https://help.aliyun.com/zh/dataworks/user-guide/pyodps-3-node)、[PyODPS DataWorks 参数与平台入口](https://pyodps.readthedocs.io/zh-cn/stable/platform-d2.html)、[SQL 结果完整读取](https://pyodps.readthedocs.io/zh-cn/stable/base-sql.html)：运行环境、参数和 SDK 行为。

验收报告分别列出本地测试、真实提交、真实运行及业务核验。资源组映射未解决等未完成项应如实记录；文档和示例不代表节点已成功运行。

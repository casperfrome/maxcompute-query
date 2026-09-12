# maxcompute-query

MaxCompute/ODPS 只读取数、DataWorks 任务审查和 PyODPS3 本地修复验证技能。路由与执行边界见 [SKILL.md](SKILL.md)，命令细节按场景进入参考文件。

## 能力与边界

- 表结构、分区、样本、分布及只读 SQL；UDF 注册和源码读取。
- 精确拉取生产/开发/严格保存态代码及历史版本；同名任务和多产出节点不自动选首项。
- SQL 审查与本地修改；inline 对无法保证语义的输入拒绝生成，compare 先验证键非 NULL 且唯一。
- PyODPS3 已有日志/实例读取、单次保存态调试，以及 `repair_pyodps3.py init → prepare → run/resume → report` 修复会话。Codex 根据日志修改本地候选，在当前授权范围和运行预算内验证、恢复；默认最多 3 次提交，提交不确定先恢复，不重发。
- DI 源/目标/写入模式与按位置列映射；缺列信息显示不足以判断，多 reader/writer 不宣称完整映射通过。

SQL 查询通道保持只读。拉回的任务代码只供审查，不能直接执行或包装为 Python 绕过查询校验。PyODPS3 可能产生实际写入，先核实目标、分区、参数和授权，不能以 test/Dev 命名代替。代码只交付本地，不更新、保存、提交或发布原 DataWorks 节点。运行验证与业务数据验证分别报告。

## 配置与依赖

本项目路径为 `D:\AllForCareer\数仓问题排查\.agents\skills\maxcompute-query`，Python 固定为 `D:\PythonVenv\Scripts\python.exe -X utf8 -B`。PowerShell 文本操作使用 UTF-8。已有环境版本记录在 [requirements-runtime.txt](requirements-runtime.txt) 与 [requirements-test.txt](requirements-test.txt)；仅需要复现环境时按指定环境管理依赖，不为离线检查升级 SDK。

`runtime_config.py` 按环境变量 → 可选 `scripts/config.py` 的非密钥配置 → 安全空默认值加载。可以参考 [config.example.py](scripts/config.example.py)，但无需为 `--help`、离线日志或 SQL 文本工具创建 config.py 或提供云 SDK/凭证。实际连接前才校验项目与端点，缺失时清晰报错。

凭证只读取 `ALIYUN_ACCESS_KEY_ID` / `ALIYUN_ACCESS_KEY_SECRET`，回退 `ODPS_ACCESS_ID` / `ODPS_SECRET` 环境变量；不在配置文件保存密钥。`ODPS_PROJECT/ODPS_ENDPOINT/ODPS_TUNNEL_ENDPOINT` 和 `DATAWORKS_ENDPOINT/DATAWORKS_PROJECT_ID/DATAWORKS_ODPS_PROJECT_NAME` 可按实际环境设置。已有忽略的本地 config.py 保留，不自动覆盖；输出原始证据保存在本地，展示时脱敏。

## 文件与参考

| 文件 | 用途 |
|---|---|
| [mc_query.py](scripts/mc_query.py) | 查询、分区检查、导出与 UDF 读取 |
| [fetch_task_sql.py](scripts/fetch_task_sql.py) | 代码、版本、精确 file/node 选择 |
| [build_validation_sql.py](scripts/build_validation_sql.py) | 离线 inline/compare/变量清点 |
| [debug_pyodps3.py](scripts/debug_pyodps3.py) | 日志、保存态与单次调试 |
| [repair_pyodps3.py](scripts/repair_pyodps3.py) | 本地候选、轮次预算、恢复与报告 |
| [pyodps3_runtime.py](scripts/pyodps3_runtime.py) | 共享锁、API、请求与参数 |
| [di_task.py](scripts/di_task.py) | DI 纯文本解析与列映射 |
| [查询参考](references/maxcompute_sql.md) | SQL、命令、导出、错误与验证 |
| [取码参考](references/fetch_task_sql.md) | 基础取码、保存态、版本与 DI |
| [调试参考](references/pyodps3_debugging.md) | 运行标识、日志和完整修复会话 |

## 离线验证

```powershell
Set-Location -LiteralPath 'D:\AllForCareer\数仓问题排查\.agents\skills\maxcompute-query'
& 'D:\PythonVenv\Scripts\python.exe' -X utf8 -B -m pytest scripts -q -p no:cacheprovider
& 'D:\PythonVenv\Scripts\python.exe' -X utf8 -B 'D:\AllForCareer\数仓问题排查\.agents\skills\maxcompute-query\scripts\test_di_summary.py'
```

测试使用纯函数、合成输入、模拟 API 和临时目录，不证明真实云端契约或业务数据通过。原有可直接运行的测试入口保留，关键断言也纳入 pytest。26 个场景见 [evals.json](evals/evals.json)：live 用真实证据，不固定最新数据数字或要求非空；offline 使用明确合成附件/模拟响应。人工场景断言和自动化回归分别记录，不能把文档数量当已执行评测。

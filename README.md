# maxcompute-query

一个 Codex / Agent Skill：通过 MaxCompute (ODPS) 只读 SQL 排查数仓数据，拉取 DataWorks 代码，并调试 PyODPS3 保存态及分析输出、报错和执行日志。

## 能力

- 探查表结构、分区、字段分布
- 编写并执行只读 SQL，为结论取数佐证
- 读取**自定义函数（UDF/UDTF）的注册信息与实现源码**（`func` / `list-functions` / `resource`）：Python UDF 从 `.py` 资源读出完整源码、嵌入式/SQL 函数读 `code`、Java UDF（jar 二进制）标注无源码可读——审查任务 SQL 时遇到未知函数不必当黑盒
- 审查 / 重构在线数仓 SQL 任务（粒度、去重、JOIN 膨胀、分区过滤等）
- 从 DataWorks 拉取生产、开发、严格保存态代码及历史版本（`scripts/fetch_task_sql.py`）
- PyODPS3 调试（`scripts/debug_pyodps3.py`）：保存态检查、经用户授权运行一次、状态与日志收集、离线日志分析；不自动修改或发布节点、不自动修复重跑。详见 [调试说明](references/pyodps3_debugging.md)。
- 读取 / 解读**数据集成（离线同步）任务**：自动给出源表 → 目标表、写入模式、reader/writer 列按位置映射对照（含错位审查）

## 目录结构

```
SKILL.md                    技能说明（触发条件、工作流）
scripts/
  config.example.py         连接配置模板（密钥走环境变量；config.py 仅存非密钥配置）
  mc_query.py               执行只读 SQL / 探查表结构 / 读取 UDF 源码（func、list-functions、resource）
  fetch_task_sql.py         拉取严格保存态 / 线上代码 / 历史版本，并解读 DI 同步任务
  debug_pyodps3.py          PyODPS3 保存态检查、运行、状态、日志和离线诊断
  di_task.py                数据集成同步配置解析（源/目标/写入模式/列映射审查）
  build_validation_sql.py   生成校验 SQL
references/
  maxcompute_sql.md         MaxCompute SQL 参考
  fetch_task_sql.md         保存态 / 历史版本 / 数据集成同步任务
  pyodps3_debugging.md      PyODPS3 运行与诊断流程、权限边界、官方 API
agents/                     子代理（任务分析、校验）
evals/                      评测用例
```

## 配置

**AK/SK 只来自环境变量，脚本与 `config.py` 都不再保存明文密钥。** `scripts/config.py`
仅保存非密钥的 project/endpoint 等环境配置（仍在 `.gitignore` 中，按需本地保留）。

使用前设置凭证环境变量（持久化到用户环境）：

```powershell
# Windows PowerShell（setx 写入用户级环境变量，需新开终端才生效）
setx ALIYUN_ACCESS_KEY_ID     "<your-access-key-id>"
setx ALIYUN_ACCESS_KEY_SECRET "<your-access-key-secret>"
```

```bash
# Linux / macOS（写入 shell 启动文件以持久化）
export ALIYUN_ACCESS_KEY_ID=...
export ALIYUN_ACCESS_KEY_SECRET=...
```

未设凭证时脚本会在连接前以「未配置阿里云凭证…」清晰报错。project/endpoint 如需覆盖默认值，
同样用环境变量（`ODPS_PROJECT` / `ODPS_ENDPOINT` / `DATAWORKS_PROJECT_ID` 等）或改本地
`config.py`（复制自 `config.example.py`）。

## 安装为 Skill

本项目技能目录为：

```
<project>/.agents/skills/maxcompute-query/
```

本项目调试及测试统一使用 `D:\PythonVenv\Scripts\python.exe`。SQL 查询通道保持只读；PyODPS3 运行可能执行脚本中的写入，必须先核实保存态的实际目标及用户授权。

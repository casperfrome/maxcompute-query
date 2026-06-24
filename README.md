# maxcompute-query

一个 Claude Code / Agent Skill：通过辅助脚本探查表结构、编写并执行 MaxCompute (ODPS) 只读 SQL 来排查数仓/数据问题，并用真实数据为结论佐证。

## 能力

- 探查表结构、分区、字段分布
- 编写并执行只读 SQL，为结论取数佐证
- 审查 / 重构在线数仓 SQL 任务（粒度、去重、JOIN 膨胀、分区过滤等）
- 从 DataWorks 拉取任务的线上 SQL 及历史版本（`scripts/fetch_task_sql.py`）
- 读取 / 解读**数据集成（离线同步）任务**：自动给出源表 → 目标表、写入模式、reader/writer 列按位置映射对照（含错位审查）

## 目录结构

```
SKILL.md                    技能说明（触发条件、工作流）
scripts/
  config.example.py         连接配置模板（密钥走环境变量；config.py 仅存非密钥配置）
  mc_query.py               执行只读 SQL / 探查表结构
  fetch_task_sql.py         从 DataWorks 拉取任务 SQL / 历史版本，并解读数据集成离线同步任务
  di_task.py                数据集成同步配置解析（源/目标/写入模式/列映射审查）
  build_validation_sql.py   生成校验 SQL
references/
  maxcompute_sql.md         MaxCompute SQL 参考
  fetch_task_sql.md         拉历史版本 / 读数据集成同步任务（进阶用法）
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

将本目录放到 Claude Code 的 skills 目录下，例如：

```
<project>/.claude/skills/maxcompute-query/
```

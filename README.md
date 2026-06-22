# maxcompute-query

一个 Claude Code / Agent Skill：通过辅助脚本探查表结构、编写并执行 MaxCompute (ODPS) 只读 SQL 来排查数仓/数据问题，并用真实数据为结论佐证。

## 能力

- 探查表结构、分区、字段分布
- 编写并执行只读 SQL，为结论取数佐证
- 审查 / 重构在线数仓 SQL 任务（粒度、去重、JOIN 膨胀、分区过滤等）
- 从 DataWorks 拉取任务的线上 SQL 及历史版本（`scripts/fetch_task_sql.py`）

## 目录结构

```
SKILL.md                    技能说明（触发条件、工作流）
scripts/
  config.example.py         连接配置模板（复制为 config.py 后填入凭证）
  mc_query.py               执行只读 SQL / 探查表结构
  fetch_task_sql.py         从 DataWorks 拉取任务 SQL 与历史版本
  build_validation_sql.py   生成校验 SQL
references/maxcompute_sql.md  MaxCompute SQL 参考
agents/                     子代理（任务分析、校验）
evals/                      评测用例
```

## 配置

凭证不随仓库提交（`scripts/config.py` 已在 `.gitignore` 中）。使用前二选一：

1. 复制模板并填入凭证：
   ```bash
   cp scripts/config.example.py scripts/config.py
   # 编辑 scripts/config.py，填入 ACCESS_ID / SECRET / project / endpoint
   ```
2. 或仅通过环境变量提供（推荐）：
   ```bash
   export ALIYUN_ACCESS_KEY_ID=...
   export ALIYUN_ACCESS_KEY_SECRET=...
   export ODPS_PROJECT=...
   export ODPS_ENDPOINT=...
   ```

## 安装为 Skill

将本目录放到 Claude Code 的 skills 目录下，例如：

```
<project>/.claude/skills/maxcompute-query/
```

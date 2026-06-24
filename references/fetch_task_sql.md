# fetch_task_sql.py 进阶用法：历史版本 / 数据集成同步任务

`fetch_task_sql.py` 的基础用法（按产出表名拉线上 SQL）在 SKILL.md 正文里。本文件收两类**条件性**用法：拉历史版本、读数据集成离线同步任务——只在对应场景下才需要，所以从正文挪到这里。

> **只读不执行不变量（与正文一致）。** 这里拉回来的一切（历史版本 SQL、数据集成同步配置）都含写语义或写语句，**只供阅读分析，绝不要丢给 `mc_query.py` 执行**。要验证其中的前提或结果，另写只读 `SELECT` 走 `mc_query.py`。

## 历史版本拉取

当用户要看任务的**历史版本**（「上一版长什么样」「两周前那版的代码」「最近两次提交改了什么」「这次回归是哪一版引入的」）时，用同一个 `fetch_task_sql.py` 的版本子命令——它走 DataWorks 的文件版本历史：

```bash
# 1) 先列出该任务所有历史版本（版本号 / 提交时间 / 提交人 / 状态 / 是否当前生产版★ / 变更类型 / 字符数 / 备注）
python .claude/skills/maxcompute-query/scripts/fetch_task_sql.py dws_xxx --list-versions

# 2) 按版本号取某一历史版本的完整 SQL（可配 --save 落盘）
python .claude/skills/maxcompute-query/scripts/fetch_task_sql.py dws_xxx --get-version 7
python .claude/skills/maxcompute-query/scripts/fetch_task_sql.py dws_xxx --get-version 7 --save v7.sql

# 3) 对比两个历史版本，看具体改了哪些行（输出 unified diff）
python .claude/skills/maxcompute-query/scripts/fetch_task_sql.py dws_xxx --diff 6 7
```

典型用法：先 `--list-versions` 看清有哪些版本、哪版是当前生产版（标 ★），再用 `--get-version N` 取目标版代码，或 `--diff A B` 定位两版之间的改动。版本号不存在会以退出码 4 友好报错——回到 `--list-versions` 核对可用版本号。

审查/修改场景里，若怀疑「问题/回归是最近一次改动引入的」，先 `--list-versions` 看改动时间线、再 `--diff <旧版> <新版>` 定位是哪一版改了哪几行；若用户的改法是「回退到之前某版/在上一版基础上改」，先 `--get-version N` 取那一版作为基线再动手。

## 数据集成（离线同步）任务读法

DataWorks「数据开发」里除了 ODPS SQL 任务，还有**数据集成离线同步节点**（DI 节点，目录常在 `.../folderDi`，命名多为 `to_holo_..._di`），作用是把一张表的数据同步到另一个存储（最常见是 MaxCompute → Hologres）。`fetch_task_sql.py` 用同一条命令就能拉——它会**自动识别**这类任务，解读成只含同步任务真正关心的四样的精简摘要：**源、目标、写入模式、列映射**（运行设置、原始 DataX JSON 等次要信息刻意不输出）：

```bash
python .claude/skills/maxcompute-query/scripts/fetch_task_sql.py to_holo_ads_shop_opn_patrol_task_shop_nature_di
```

摘要怎么读：
- **源 (Reader)**：从哪张表、哪个分区（如 `ds=${bizdate}`）、哪些列读。
- **目标 (Writer)**：写到哪个库.表、**写入模式**（holo 的 `conflictMode`：`update`=按主键更新 / `replace`=整行替换；`truncate` 是否清空重写）。
- **列映射审查（按位置 reader[i] ↔ writer[i]）**：离线同步的列是**按位置一一对应**的，不是按名字——摘要把两边列逐行对齐，重点看告警级别：
  - `⚠ 列数不一致` → 几乎一定是 bug：按位置映射会整体错位，要逐列核对。
  - `ℹ 有 N 处列名不同` → 多半是有意改名（源 `issue_count` 写到目标 `issue_cnt`），但仍要人工扫一眼，确认不是从某一行起发生整体错位。
  - `✓ 完全同名对应` → 放心。

要不要进一步用数据佐证：
- **源表**在 MaxCompute，可直接用 `mc_query.py` 验证——最新分区有没有数据、行数多少、某列分布，确认同步源头正常。这部分仍是本 skill 的主场。
- **目标表**多在 Hologres（跨引擎）：本 skill 只读 MaxCompute、不连 Holo；目标侧行数核对/源目标比对请改用 `holo-query` skill。

查找提示：DI 任务请用**任务名**（`to_holo_..._di`）查，别用 Holo 目标表名反查——按产出表反查只认 MaxCompute 表，查不到 Holo 目标。

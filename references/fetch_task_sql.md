# fetch_task_sql.py：保存态 / 历史版本 / 数据集成同步任务

`fetch_task_sql.py` 的基础用法（按产出表名拉线上 SQL）在 SKILL.md 正文里。本文件说明严格保存态取码、历史版本和数据集成离线同步任务；按当前场景阅读对应部分。

> 取码命令本身不执行任务。取回的 SQL 和 DI 配置只供阅读分析，不能直接传给 `mc_query.py`；验证时另写只读查询。用户授权运行 PyODPS3 保存态时，使用 [PyODPS3 调试入口](pyodps3_debugging.md)，不在本地 `exec`、导入或执行取回的 Python。

## 严格保存态取码

用户说“保存态”“已保存未提交”或要求测试当前编辑内容时，指定 `--source saved`。它通过 DataWorks 文件 ID 获取当前保存内容，保留文件 ID、类型、修改时间、提交状态和 UTF-8 原文 SHA256；`1221` 识别为 PyODPS3。保存态不是生产发布版，也不是最近提交版本。

```powershell
# 有文件 ID 时精确读取；按文件 ID 取码无需填写任务名
& 'D:\PythonVenv\Scripts\python.exe' .agents/skills/maxcompute-query/scripts/fetch_task_sql.py --source saved --file-id 505649513 --save saved.py

# 只有名称时使用精确名称；多个同名候选必须按 ID 消歧
& 'D:\PythonVenv\Scripts\python.exe' .agents/skills/maxcompute-query/scripts/fetch_task_sql.py test_dwd_com_sys_oa_doc_detail_log_df_pyodps3 --source saved
```

保存态缺失、类型不符或存在多个合理候选时，报告原问题；不回退生产代码，不自行挑第一个候选。`--source saved` 不与历史版本选择混用。需要保留运行配置、检查写入目标或运行时，用 `debug_pyodps3.py inspect/run-saved` 生成完整证据包。上面的文件 ID 是本轮测试对象示例，不是其他任务的默认值。

## 历史版本拉取

当用户要看任务的**历史版本**（「上一版长什么样」「两周前那版的代码」「最近两次提交改了什么」「这次回归是哪一版引入的」）时，用同一个 `fetch_task_sql.py` 的版本子命令——它走 DataWorks 的文件版本历史：

```powershell
# 1) 先列出该任务所有历史版本（版本号 / 提交时间 / 提交人 / 状态 / 是否当前生产版★ / 变更类型 / 字符数 / 备注）
& 'D:\PythonVenv\Scripts\python.exe' .agents/skills/maxcompute-query/scripts/fetch_task_sql.py dws_xxx --list-versions

# 2) 按版本号取某一历史版本的完整 SQL（可配 --save 落盘）
& 'D:\PythonVenv\Scripts\python.exe' .agents/skills/maxcompute-query/scripts/fetch_task_sql.py dws_xxx --get-version 7
& 'D:\PythonVenv\Scripts\python.exe' .agents/skills/maxcompute-query/scripts/fetch_task_sql.py dws_xxx --get-version 7 --save v7.sql

# 3) 对比两个历史版本，看具体改了哪些行（输出 unified diff）
& 'D:\PythonVenv\Scripts\python.exe' .agents/skills/maxcompute-query/scripts/fetch_task_sql.py dws_xxx --diff 6 7
```

典型用法：先 `--list-versions` 看清有哪些版本、哪版是当前生产版（标 ★），再用 `--get-version N` 取目标版代码，或 `--diff A B` 定位两版之间的改动。版本号不存在会以退出码 4 友好报错——回到 `--list-versions` 核对可用版本号。

审查/修改场景里，若怀疑「问题/回归是最近一次改动引入的」，先 `--list-versions` 看改动时间线、再 `--diff <旧版> <新版>` 定位是哪一版改了哪几行；若用户的改法是「回退到之前某版/在上一版基础上改」，先 `--get-version N` 取那一版作为基线再动手。

## 数据集成（离线同步）任务读法

DataWorks「数据开发」里除了 ODPS SQL 任务，还有**数据集成离线同步节点**（DI 节点，目录常在 `.../folderDi`，命名多为 `to_holo_..._di`），作用是把一张表的数据同步到另一个存储（最常见是 MaxCompute → Hologres）。`fetch_task_sql.py` 用同一条命令就能拉——它会**自动识别**这类任务，解读成只含同步任务真正关心的四样的精简摘要：**源、目标、写入模式、列映射**（运行设置、原始 DataX JSON 等次要信息刻意不输出）：

```powershell
& 'D:\PythonVenv\Scripts\python.exe' .agents/skills/maxcompute-query/scripts/fetch_task_sql.py to_holo_ads_shop_opn_patrol_task_shop_nature_di
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

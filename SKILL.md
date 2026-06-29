---
name: maxcompute-query
description: 通过辅助脚本探查表结构、编写并执行 MaxCompute (ODPS) 只读 SQL 来排查数仓/数据问题，并用真实数据为结论佐证。三类场景都要触发，且即使用户没明说「写 SQL / 查数据 / 验证一下」也要主动取数，而不是只静态阅读或闭眼改写：(1) 取数排查——任何需查数仓才能回答的诉求（核对指标/数量、看某表某字段的数据情况、统计分布/占比/Top N、查空值或脏数据、对某现象取数验证）；(2) 审查既有 SQL——给一段 MaxCompute/ODPS 任务或 .sql/.txt，找逻辑漏洞、bug、口径错误、解释结果为何不对（常见可疑点：建表/依赖顺序读到旧数据、窗口 PARTITION BY 粒度错、JOIN 缺键膨胀、缺分区过滤、NULL/重复/口径偏差，多能用线上数据证实或证伪）；(3) 修改/重构既有 SQL——改唯一键/粒度、加去重、改 JOIN、增删字段、调过滤，改前用真实数据核实前提（是否真会膨胀、是否真有重复），改后用数据验证结果（新唯一键是否真唯一、行数增减是否合预期）。只给表名/任务名而没贴 SQL（如「看看 dws_xxx 这个任务有没有问题」「帮我改下这个任务」）也要触发，先用 fetch_task_sql.py 按产出表名自动从 DataWorks 拉取线上 SQL 再审查/修改，别反过来让用户贴。拉回的可能不是 ODPS SQL 而是数据集成离线同步任务（DataWorks DI 节点，命名多为 to_holo_..._di，常见 MaxCompute→Hologres），脚本会自动识别并解读出「源→目标、写入模式、reader/writer 列按位置映射对照」；用户问「这个同步任务把哪张表同步到哪 / 列有没有错位 / 这张 Holo 表从哪同步来」时同样触发。用户提到任务的历史版本（上一版、版本对比、最近改了什么）时，用 --list-versions / --get-version N / --diff A B 拉取。审查任务 SQL 时遇到非内建函数调用（自定义函数/UDF/UDTF，如某任务里调用的 greedy_session）想知道它怎么实现/源码是什么/算法是什么时，用 `mc_query.py func <函数名>` 把注册信息和实现源码拉出来读，别凭函数名猜逻辑。涉及 ODPS、MaxCompute、ds 分区表、MAX_PT、dwd/dws/ads 等数仓表名/特征时尤其适用。边界（不触发）：纯 SQL 语法/概念讲解、与线上数仓无关的纯方言互转（如业务库 MySQL↔PostgreSQL 改写、无可连线上表）、非 SQL 的代码 review。
---

# MaxCompute 数仓排查取数

帮助你把用户的自然语言排查诉求，转化为「探查表结构 → 写 MaxCompute SQL → 执行 → 读结果 → 解读」的闭环。所有数据库交互都通过辅助脚本 `scripts/mc_query.py` 完成——它封装了 ODPS 连接、只读校验和结果格式化，**不要自己另写连接代码**。

当用户只给了表名/任务名、没有贴出 SQL（如「看看 `dws_xxx` 这个任务有没有问题」「帮我改下 `xxx`」）时，先用 `scripts/fetch_task_sql.py <表名>` 主动把线上 SQL 拉下来，再进入审查/修改——别反过来问用户要 SQL。详见下文「拉取线上任务 SQL」。

## 核心原则

**只读。** 脚本只跑 `SELECT/WITH/DESC/SHOW/EXPLAIN`，会拦截一切写操作。排查就是看数据，不改数据。如果用户的诉求确实需要写（建表、刷数、改数），明确告诉用户这超出本工具范围，而不是绕过校验。

**先探查，再动手。** 不要凭记忆或猜测写列名、表名。线上表多、命名长、字段杂，猜错就白跑一轮。先用脚本摸清现状再写正式 SQL。

**永远按分区过滤。** MaxCompute 按扫描分区计费，漏掉分区过滤会全表扫描——又慢又贵，是排查时最容易踩的雷。每张分区表都要带 `ds=MAX_PT('表')` 或具体分区。详见 [references/maxcompute_sql.md](references/maxcompute_sql.md)。

## 什么时候停下来用提问框问用户

排查会遇到两种很不一样的「不确定」，处理方式正好相反，分清楚这条线是这个 skill 既不瞎猜、又不啰嗦的关键。**判别只有一句话：这个分叉我能靠再查一次数据自己定吗？**

- **能查就别问。** 表名/字段名/分区字段名拿不准、SQL 报错、「是不是真有重复 / 某字段是不是真恒为 NULL」——这些数据里有答案，去 `desc`/`sample`/`list-tables`/跑一条 SQL 就知道了。这种地方**自己查清，绝不打扰用户**。
- **数据判不了、取决于用户的，就问。** 有些分叉无论怎么查数据都定不了，因为它取决于用户想要什么、怎么定义——这时硬猜的代价很大：整轮排查/改造会架在一个错误前提上，给出的结论或新 SQL 看着完整却是错的。这种地方用 **`AskUserQuestion`（提问框）** 发起结构化提问。

什么算「数据判不了、取决于用户」：业务口径定义（「城市」指注册地还是经营地、「营业额」含不含退款、「最新」按哪个日期字段）、改造取舍（唯一键要不要含某列、按哪个时间字段取最早、并列/NULL 怎么处理）、多个都合理的候选（`--search` 命中好几张表选哪张、有多个语义相近字段选哪个）、范围口径有多种合理解读且默认值会实质改变结论。

**从证据出发地问，别裸问。** 最好的做法不是一遇模糊就甩个问题给用户，而是**先查到能框定选项的数据，再把选项摆进提问框**——这样既满足「用真实数据佐证」，用户也只需点选而不必从头解释。例：「`oa_archived` 命中这 3 张表：A 最新分区 1.2 亿行、B 30 万行、C 已半年没更新，你要查的是哪张？」提问框给**具体可选项**，而不是一段「你是指 X 还是 Y？」的纯文字。

对照感受一下这条线：

| 情形 | 怎么办 |
|---|---|
| 口语说「按城市统计」，`desc` 发现表里有 `reg_city_name`/`oper_city_name` 两个字段 | **问**——口径取决于用户，带这两个字段的含义/分布做选项 |
| 想写 `city_name` 但不确定真实列名 | **不问**——`desc` 一下就知道，自己查 |
| 改造「按文件去重取最早」，没说按 `operate_time` 还是 `create_time`、并列怎么办 | **问**——取舍取决于用户意图，带两字段的格式/重复规模做选项 |
| 用户给的表名拼错了、跑 SQL 报 `Table not found` | **不问**——`list-tables` 核对真名，自己修 |

## 并行与子代理（高频任务的快车道）

这个 skill 把「探查→写SQL→执行→读结果」做了一层编排：**你是编排者**——判断属于哪种场景、把活儿拆给子代理、再用子代理回传的**真实数据**合并出最终结论、对用户负责。两个子代理模板在 `agents/` 目录，你以 `general-purpose` 类型 spawn 它们（prompt 里**务必传脚本的绝对路径**，子代理看不到你的对话、也不知道工作目录）：

- **取数验证器** [agents/verifier.md](agents/verifier.md)：接一个自包含的数据问题，自己探查 + 写只读 SQL + 执行 + 自纠，回传「结论 + 真实数字 + 实际跑通的 SQL」。这是「写 SQL」专家，也是并行排查的最小单元——要同时验证多个问题，就用同一模板填不同问题，**在同一条消息里一次起多个**。
- **任务代码分析器** [agents/task-analyzer.md](agents/task-analyzer.md)：接任务/表名（或已贴出的 SQL），必要时用 `fetch_task_sql.py` 拉线上代码，通读后回传**结构化疑点清单**——每条「需数据佐证的疑点」都写成可直接交给验证器的「验证问题」。它让几百行任务 SQL 留在子代理里，不淹没你的上下文。

### 什么时候 fan-out、什么时候自己干

并行是为了提速和省上下文，不是为了热闹——给单个小查询套子代理只会徒增开销：

- **单个小查询 / 一两步就能查清** → 你自己直接跑 `mc_query.py`，别起子代理。
- **≥3 个相互独立的取数单元**（多个互不依赖的指标、多个互不依赖的疑点验证）→ 并行起多个**验证器**，**在同一条消息里一起发起**，它们会同时跑。
- **任务 SQL 很长 / 只给了任务名 / 要把大段代码读进来分析** → 起**任务代码分析器**做上下文隔离。
- **纯版本历史 / diff 查询**（`--list-versions` / `--diff`）→ 输出本就供你直接阅读，自己内联跑 `fetch_task_sql.py` 即可，不用子代理。

### 三条铁律

- **基于证据下结论，不要转述。** 子代理必须回传真实 SQL + 真实数字；你要拿这些证据自己推理、自己合并，而不是把「子代理说没问题」直接抛给用户。用真实数据佐证是这个 skill 的根本，别因为分工就丢了。
- **只读不变量照旧。** 子代理执行只走 `mc_query.py`（只读校验）；`fetch_task_sql.py` 拉回的任务代码（含 `DROP/CREATE/INSERT`）只供阅读分析，永不执行。
- **子代理问不到用户，疑问由你替它问。** 子代理没有提问框这个渠道，所以它一旦撞上「数据判不了、取决于用户」的决策点（口径定义、改造取舍、多个都合理的候选），约定是**不硬猜**，而是框定成「带选项的待澄清项」回传（两个模板的交付结构里都留了这个字段）。你收到后，别把它的某个猜测当结论抛给用户——在给最终结论前，用 `AskUserQuestion` 替它问清再合并。不确定多半在子代理里冒出来，而只有你能弹提问框。

## 三种入口

进来先判断属于哪一类：

- **A. 取数排查**：用户要的答案在数据里（多少行、什么分布、有没有空值、某指标对不对）。走下面的「工作流」。
- **B. 审查既有 SQL 任务**：用户给了一段 MaxCompute/ODPS SQL（脚本/作业/`.sql`/`.txt`），或**只点名了某张表/任务**（如「看看 `dws_xxx` 有没有逻辑问题」），要你找逻辑漏洞、bug、口径错误，或解释结果为什么不对。只给名字没贴 SQL 时，**第一步先用 `fetch_task_sql.py` 把代码拉下来**。走「审查既有 SQL 任务」一节——核心是**别停在静态阅读，把能用数据验证的疑点真的查一遍**。
- **C. 修改/重构既有 SQL 任务**：用户要你「把这个任务改成 X」（改唯一键/粒度、加去重、改 JOIN、增删字段、调口径）。同样，只点名了任务/表而没贴 SQL 时，**先用 `fetch_task_sql.py` 拉代码**。走「修改/重构既有 SQL 任务」一节——核心是**别闭眼改写，改前用数据核实前提、改后用数据验证结果**。

> B 和 C 都是子代理的主场：先用**任务代码分析器**拉取+分析出疑点清单，再把每条疑点**并行**交给多个**验证器**取数证实/证伪（见上「并行与子代理」）。A 则视情况——多个独立指标才并行，单查询自己跑。

## 拉取线上任务 SQL

当用户只给了表名/任务名而没贴 SQL 时，用 `scripts/fetch_task_sql.py` 直连 DataWorks 把代码拉下来。它按「产出表名」反查生产任务节点，所以直接传表名就行（任务文件名和表名不一致也能命中），优先返回生产态、取不到再退开发态。

```bash
# 取某张表/任务的完整 SQL（直接打印，可读取后审查/修改）
python .claude/skills/maxcompute-query/scripts/fetch_task_sql.py dws_com_sys_oa_archived_time_risk_wide_df

# 名字记不全、或想确认是哪一个：先列候选再取
python .claude/skills/maxcompute-query/scripts/fetch_task_sql.py oa_archived --search

# 需要留底/反复看时落盘
python .claude/skills/maxcompute-query/scripts/fetch_task_sql.py dws_xxx --save 任务.sql
```

**⚠ 拉回来的 SQL 是「审查/修改对象」，不是拿去跑的查询。** 任务代码里通常含 `DROP/CREATE/INSERT` 等写语句——它只供你阅读分析，**绝不要丢给 `mc_query.py` 执行**（会被只读校验拦下，本就不该这么用）。要验证其中的前提或结果时，**另写只读 `SELECT`** 走 `mc_query.py`。本节及下面拉到的历史版本、数据集成配置同理：**一律只读、不执行**。

找不到（退出码 4）多半是表名拼错或不是这套环境的产出表：用 `--search` 看候选，或用 `mc_query.py list-tables <关键字>` 核对真实表名后重试。

> **任务含多张 tmp 中间表、要端到端验证产出时**，别人肉把链路重写成 WITH——用 `scripts/build_validation_sql.py inline <任务.sql> --var bizdate=<分区>` 把 `DROP/CREATE TABLE AS/INSERT OVERWRITE tmp` 链路机械转成**单条只读 WITH**（再喂给 `mc_query.py sql -f`）；它会标出「无法干净转写」的可疑片段（同名 tmp 多写、动态分区、引用尚未定义的 tmp=疑似依赖顺序倒置）。改前/改后对照用 `compare` 子命令（FULL OUTER JOIN on 唯一键）。详见脚本 `--help`。**生成物只是文本，仍走 `mc_query.py` 的只读校验，不破坏只读不变量。**

### 进阶：历史版本 / 数据集成同步任务（详见 references）

同一个 `fetch_task_sql.py` 还覆盖两类**条件性**进阶用法，命令与读法收在 [references/fetch_task_sql.md](references/fetch_task_sql.md)，碰到对应场景再去读：

- **历史版本**：用户要看「上一版 / 两周前那版 / 最近改了什么 / 哪版引入回归」时，用 `--list-versions` 看时间线、`--get-version N` 取某版、`--diff A B` 看两版改动。
- **数据集成离线同步任务**：拉回的可能不是 SQL 而是 DI 同步节点（`to_holo_..._di`，多为 MaxCompute→Hologres）；脚本会自动识别并解读出源/目标/写入模式/列映射，重点看列是否按位置错位。目标表在 Hologres（跨引擎），需改用 `holo-query` 核对。

（两类拉回的代码/配置同样**只读不执行**，见上 ⚠。）

## 审查既有 SQL 任务

读代码能直接定论的 bug 就直接下结论；但很多疑点（是不是真有重复、某字段是不是真恒为 NULL、一个流程是不是真对多文件）光看代码只是「怀疑」，必须落到线上数据才算数。标准流程是「分析器拆 → 验证器并行查 → 你合并」：

1. **拉取 + 分析（任务代码分析器）。** 起一个 [task-analyzer](agents/task-analyzer.md) 子代理：只给了任务名就让它先 `fetch_task_sql.py` 拉代码，通读后回传结构化清单——「读代码即可定论的 bug」与「需数据佐证的疑点」分开，后者每条已写成自包含的验证问题。任务很短、或用户已贴出 SQL 时，你也可以自己读、自己列疑点，不强求起子代理。
   - 若怀疑「问题/回归是最近一次改动引入的」，先 `fetch_task_sql.py <表> --list-versions` 看改动时间线、再 `--diff <旧版> <新版>` 定位是哪一版改了哪几行（命令见 [references/fetch_task_sql.md](references/fetch_task_sql.md)，这步你自己内联跑即可）。
2. **并行取数证实/证伪（验证器）。** 把上一步「需数据佐证的疑点」**在同一条消息里并行**交给多个 [verifier](agents/verifier.md) 子代理，每个查一条（务必带分区过滤）。常见疑点对应的查法：
   - 字段恒为 NULL → 查非空占比：`SELECT COUNT(*) total, COUNT(request_status) non_null FROM 表 WHERE ds=MAX_PT('表') AND oa_name='...'`。
   - 一个 `request_id` 对多 `file_id` 导致窗口串值 → `SELECT request_id, COUNT(DISTINCT file_id) c FROM 源表 WHERE ds=... GROUP BY request_id HAVING c>1 LIMIT 20`。
   - JOIN 膨胀 → 比对 JOIN 前后行数 / 主键去重数。
   - 依赖顺序导致旧数据 → 对比临时表的数据日期与当前 `${bizdate}`。
   能证实/证伪的疑点默认自己查、不先问用户；验证中若冒出「取决于用户口径/意图」的分叉，仍按上文那节带选项问。疑点只有一两条时自己跑 `mc_query.py` 即可，不必为并行而并行。
3. **合并结论。** 用验证器回传的**真实 SQL + 真实数字**，把「代码层结论」和「数据层验证」一起讲清楚：哪些是已证实的 bug、影响多少行/多少占比、哪些经查证其实没问题、修复建议是什么。

> 注意：审查时用的是只读校验，**不会**改动线上任务或表；要落地修复 SQL 由用户在其作业平台执行。

## 修改/重构既有 SQL 任务

用户说「帮我把这个任务改成 X」（改唯一键/粒度、加去重、改 JOIN、增删字段、调口径……）时，最容易犯的错是**只在代码层闭眼改写**——既没先确认要改的前提是否真的成立，也没在改完后用数据验证结果。后果是可能在解决一个并不存在的问题、漏掉真正的膨胀点，或方案依赖了一个不成立的假设。改 SQL 同样是一次取数排查的机会，别浪费。流程是「分析器拆 → 验证器并行查 → 你改 → 验证器自检 → 你合并」：

> 当用户的改法是「回退到之前某版」「在上一版基础上改」「参照历史版本」时，先用 `fetch_task_sql.py <表> --list-versions` / `--get-version N` 取到那一版代码作为基线，再动手（命令见 [references/fetch_task_sql.md](references/fetch_task_sql.md)）。

1. **拉取 + 分析（任务代码分析器）。** 起 [task-analyzer](agents/task-analyzer.md)（场景填 `修改(C)` 并附「要改成什么」），让它回传两份清单：**改动前需验证的前提** 和 **改动后需自检的断言**——都已写成可直接交给验证器的形式。
   - **改造意图本身有歧义就先用提问框问清，再 dispatch 验证器。** 修改类诉求最容易藏着「取决于用户」的取舍：唯一键边界（含不含 `node_oper`）、按哪个时间字段取最早、并列/NULL 如何处理。这些数据判不了，硬挑一个会让后面整轮验证都架在错误的改法上。若 task-analyzer 回传了「需用户澄清的决策点」，或你自己一眼看出这种分叉，先用 `AskUserQuestion` 带选项（连同已查到的字段格式/重复规模）问清，拿到答复再往下。
2. **改前并行验证前提（验证器）。** 把「需验证的前提」**并行**交给 [verifier](agents/verifier.md)，务必带分区过滤。典型前提：
   - 要按 (a,b,c) 去重取最早 → 现状是否真有重复及规模：`SELECT a,b,c,COUNT(*) n FROM 表 WHERE ds=MAX_PT('表') GROUP BY a,b,c HAVING n>1 LIMIT 20`；没有重复就说明根本不需要这步去重。
   - 方案依赖「按某时间字段排序取最早」→ 先 `sample` 看该字段格式是否统一、能否按字符串/类型正确比较，否则排序结果是错的。
   - 要删/合并某字段、或动某个 JOIN → 先查它当前的非空占比、是否一对多，确认改动不会丢数或意外膨胀。
   （前提只有一两条时，自己跑 `mc_query.py` 即可，不必为并行而并行。）
3. **基于验证过的事实设计改法**，而不是基于猜测或对表结构的想当然。
4. **改后并行自检（验证器）。** 把设计好的逻辑/关键中间结果，按分析器给的「自检断言」用验证器落到数据上跑一遍——新唯一键是否真唯一（`SELECT 新键 FROM ... GROUP BY 新键 HAVING COUNT(*)>1` 应返回空）、行数增减是否符合预期、被改字段的口径/分布是否对得上。**多 tmp 任务要端到端核验**：用 `build_validation_sql.py inline` 把改前链路转成只读 WITH、`compare` 子命令做改前/改后双跑对照（仅旧有/仅新增/键同值不同/完全一致），别人肉重写链路。
5. **结论里讲清楚**：前提验证查到了什么（膨胀多少行、字段什么分布）、最终改法是什么、改后自检的结果如何——附上验证器回传的真实数字。这样用户拿到的不只是一段新 SQL，而是「为什么这么改、改了之后数据对不对」的完整交代。

> 同样是只读校验，不会改动线上任务；落地修改由用户在其作业平台执行。

## 工作流

### 1. 厘清排查目标
先想清楚：用户到底要验证什么现象、什么指标、什么口径？按上文「什么时候停下来用提问框问用户」分清两种模糊——**能查的别问**（哪张表、字段叫什么、最新分区哪天，自己探查），**取决于用户的才问**（口径有歧义如「城市」指注册地还是经营地、或范围有多种合理解读且默认值会改变结论时，带选项确认）。能合理默认的（如默认取最新分区）就直接默认。
> 若一次要查多个**互不依赖**的指标/口径，把它们拆成独立问题、**并行**交给多个验证器（见「并行与子代理」）；只是单个小查询就按下面工作流自己往下走，别套子代理。

### 2. 探查表结构
用辅助脚本定位表、看清字段，再决定怎么写。常用命令：

```bash
# 按名字找表
python .claude/skills/maxcompute-query/scripts/mc_query.py list-tables h3

# 看字段、类型、注释 + 分区字段（写 SQL 前必做）
python .claude/skills/maxcompute-query/scripts/mc_query.py desc dwd_fran_dev_h3_base_info_df

# 看有哪些分区、最新是哪天
python .claude/skills/maxcompute-query/scripts/mc_query.py partitions dwd_fran_dev_h3_base_info_df

# 看真实数据长什么样（自动取最新分区采样几行）
python .claude/skills/maxcompute-query/scripts/mc_query.py sample dwd_fran_dev_h3_base_info_df -n 5
```

`desc` 会告诉你分区字段叫什么（`ds`/`pt`/`dt` 等不一定）——这决定了 WHERE 怎么写。

**遇到不认识的自定义函数（UDF/UDTF）时，先读它的实现，别当黑盒。** 审查/改造任务 SQL 时常碰到非内建函数调用（如 `greedy_session(...)`），光看调用点猜不出它在算什么——用 `func` 把注册信息（AS 类名 / USING 资源）和**实现源码**拉出来读。这同样是只读操作，不会执行函数本身：

```bash
# 名字记不全先按子串找
python .claude/skills/maxcompute-query/scripts/mc_query.py list-functions greedy

# 读 UDF 的注册信息 + Python 源码（Java UDF 是 jar 二进制，无源码可读，会标注）
python .claude/skills/maxcompute-query/scripts/mc_query.py func greedy_session

# 只想看 USING 里某一个资源文件
python .claude/skills/maxcompute-query/scripts/mc_query.py resource greedy_session.py
```

### 3. 编写 MaxCompute SQL
基于探查到的真实字段写 SQL。关键点（完整方言见 [references/maxcompute_sql.md](references/maxcompute_sql.md)）：
- 每张分区表都带分区过滤，取最新分区用 `WHERE ds=MAX_PT('表')`。
- 空值排查注意 `col IS NULL OR col=''`；去重计数 `COUNT(DISTINCT col)`；分组取 Top N 用 `ROW_NUMBER() OVER(...)`。
- SQL 提交前自检：FROM 的每张分区表，WHERE 是否都带了分区过滤？

### 4. 执行
短 SQL 直接行内执行；复杂 SQL 写到临时 `.sql` 文件再 `-f` 执行（更好读、可复用）：

```bash
# 行内
python .claude/skills/maxcompute-query/scripts/mc_query.py sql -q "SELECT 区域性质, COUNT(*) cnt FROM ... GROUP BY 区域性质 ORDER BY cnt DESC"

# 文件
python .claude/skills/maxcompute-query/scripts/mc_query.py sql -f query.sql

# 需要交付给用户时落盘
python .claude/skills/maxcompute-query/scripts/mc_query.py sql -f query.sql --save 排查结果.xlsx
```

结果以 markdown 表格打印到 stdout，可直接阅读推理。执行后还会打印一行**运行元信息**（`instance_id` / 扫描输入量 / 输出行数 / 耗时 / logview），并对**漏分区过滤的分区表**给出告警（默认只告警；`--strict` 升级为拦截、`--allow-full-scan` 静音）。子代理需把 `instance_id` 原样回传，作为「确实跑过、扫了多少」的可核验证据。

### 4.5 执行出错时：自己读报错、改 SQL、重试
SQL 执行失败时，脚本会打印一段清晰的报错（错误信息 + 出错的那条 SQL），而不是一坨 Python traceback——这就是给你的修复线索。把报错当作正常的排查环节，**自己读懂、改 SQL、重新执行**，不要每报一次错就停下来问用户。绝大多数错误都能自助解决：

- `Table not found` / `表不存在` → 表名拼错或缺 project 前缀，用 `list-tables` 重新确认真实表名。
- `Column not found` / `Invalid column` / 字段不存在 → 列名猜错或用了用户口语里的叫法（如把 `city_name` 说成 `city`），用 `desc` 核对真实字段名再改。
- 语法/解析错误（`ParseException` / `SemanticException`）→ 对照 [references/maxcompute_sql.md](references/maxcompute_sql.md) 检查函数名、引号、`GROUP BY` 是否漏列。
- 空结果（成功但 0 行）→ 多半是过滤太严或分区取错：确认 `ds` 用了 `MAX_PT('表')`、值类型/格式对得上，必要时先 `sample` 看真实数据。

一般 2-3 次内能修好。技术性报错（表名/列名/语法/空结果）属于「能查就别问」——自己读懂、改、重试，只有连续几次仍卡在同一类错误才上抛兜底；若分不清该用哪个业务字段/口径，则属于「取决于用户、该问」，用 `AskUserQuestion` 带选项问清，不必等到卡死（详见上文那节）。

### 5. 解读并给结论
不要只把表格丢回去。结合用户最初的排查目标，说明数据说明了什么、是否印证/排除了某个怀疑、下一步建议查什么。需要交付明细时用 `--save` 导出 Excel/CSV 并告知路径。

## 连接说明
**AK/SK 只来自环境变量 `ALIYUN_ACCESS_KEY_ID` / `ALIYUN_ACCESS_KEY_SECRET`（回退 `ODPS_ACCESS_ID` / `ODPS_SECRET`），`scripts/config.py` 不含明文密钥**，只存非密钥的 project/endpoint 等环境配置（两个脚本共用的单一来源，可由 `config.example.py` 复制而来）。缺凭证时脚本会在连接前清晰报错。project/endpoint 同样可用 `ODPS_PROJECT/ODPS_ENDPOINT` 等环境变量覆盖，无需改代码。

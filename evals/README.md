# 评测运行方式

[evaluations](evals.json) 保留 ID 0–25 共 26 个场景，mode 明确 live/offline。live 依赖当前授权只读环境，记录 SQL、实际分区和 instance_id；无环境不计通过，不固定最新行数或倍率，条件正确时零条是有效结果。offline 使用内联日志、模拟 API 和本目录明确合成附件，不访问线上或执行取回 Python。

fixtures/review_task.sql 与 dedup_task.sql 替换原未提交的历史附件，operation_rows.json 是同一合成数据，均无真实云表含义。historical_join_observation.json 仅归档原评测描述的历史数字，未在本轮重新验证；不得作为 live 预期值。

pytest 文档验收检查 A/B/C/D 参考路由、链接、附件、模式及关键工具契约；这些结构检查不是实际模型行为评测。26 个 manual 断言仍需按场景独立执行和记录，不因 pytest 全过就宣称 26 个任务完成。

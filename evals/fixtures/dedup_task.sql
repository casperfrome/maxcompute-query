-- SYNTHETIC FIXTURE: offline review/edit only; no actual cloud tables exist.
-- Current grain is raw operations; user will choose the intended deduplication policy.
CREATE TABLE tmp_operations AS
SELECT request_id, file_id, node_oper, operate_time, create_time, event_id
FROM fixture_project.operation_log WHERE ds='20260911';
INSERT OVERWRITE TABLE fixture_project.operation_output SELECT * FROM tmp_operations;

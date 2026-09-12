-- SYNTHETIC FIXTURE: offline review only; never submit this task to a cloud project.
-- Deliberate dependency inversion: tmp_read consumes tmp_created before this run creates it.
DROP TABLE IF EXISTS tmp_read;
CREATE TABLE tmp_read AS SELECT request_id, file_id, node_oper, operate_time FROM tmp_created;
DROP TABLE IF EXISTS tmp_created;
CREATE TABLE tmp_created AS
SELECT request_id, file_id, node_oper, operate_time
FROM fixture_project.operation_log WHERE ds='20260911';
INSERT OVERWRITE TABLE fixture_project.review_output SELECT * FROM tmp_read;

"""Offline transformation invariants; pytest collects every assertion.
Direct entry remains available with the configured Python interpreter.
"""
import sys
import pytest
from mc_query import assert_readonly, extract_table_refs
from build_validation_sql import inline_task, build_compare, split_statements, find_vars, apply_vars

LINEAR = """
DROP TABLE IF EXISTS tmp_a;
CREATE TABLE tmp_a AS SELECT id, name FROM src_x WHERE ds='${bizdate}';
DROP TABLE IF EXISTS tmp_b;
CREATE TABLE tmp_b LIFECYCLE 7 AS
SELECT a.id, a.name, b.amt FROM tmp_a a LEFT JOIN src_y b ON a.id=b.id
WHERE b.ds='${bizdate}';
INSERT OVERWRITE TABLE final_out SELECT id, name, amt FROM tmp_b WHERE amt>0;
"""


def test_linear_and_target():
    source = apply_vars(LINEAR, {'bizdate': '20260620'})
    sql, warnings, ctes, target = inline_task(source, project='p')
    assert_readonly(sql)
    assert len(ctes) == 2 and target == 'final_out' and not warnings
    assert sql.index(ctes[0] + ' AS (') < sql.index(ctes[1] + ' AS (')
    sql, _, ctes, _ = inline_task(source, target='tmp_a', project='p')
    assert_readonly(sql)
    assert len(ctes) == 1 and sql.endswith('SELECT * FROM ' + ctes[0])
    assert 'src_y' not in sql
    with pytest.raises(ValueError, match='tmp_missing.*tmp_a.*tmp_b'):
        inline_task(source, target='tmp_missing', project='p')


def test_variables_cannot_be_emitted_without_values():
    assert find_vars(LINEAR) == ['bizdate']
    with pytest.raises(ValueError, match='未代入变量'):
        inline_task(LINEAR)


def test_qualified_temporary_and_physical_references():
    source = 'CREATE TABLE tmp AS SELECT id FROM source.t; SELECT p.tmp.id FROM p.tmp'
    sql, *_ = inline_task(source, project='p')
    assert 'p.tmp' not in sql and 'source.t' in sql
    compared = build_compare(sql, sql, ['id'], [])
    assert_readonly(compared)
    assert 'source.t' in compared and 'source.o_t' not in compared


@pytest.mark.parametrize('source', [
    'CREATE TABLE a AS SELECT * FROM b; CREATE TABLE b AS SELECT 1; SELECT * FROM a',
    'CREATE TABLE a(id BIGINT); INSERT OVERWRITE TABLE a SELECT 1; SELECT * FROM a',
    'CREATE TABLE a AS SELECT 1; INSERT INTO TABLE a SELECT 2; SELECT * FROM a',
    "INSERT OVERWRITE TABLE result PARTITION(ds='20260101') SELECT 1",
])
def test_unsafe_transforms_are_errors(source):
    with pytest.raises(ValueError):
        inline_task(source)


def test_direct_output_and_statement_tokenization():
    sql, warnings, ctes, target = inline_task('INSERT OVERWRITE TABLE result SELECT a,b FROM src')
    assert_readonly(sql)
    assert not warnings and not ctes and target == 'result'
    assert len(split_statements("SELECT ';' AS a WHERE x='a;b' -- ; comment\n; SELECT 2;")) == 2


def test_scope_table_extraction():
    names, refs = extract_table_refs("WITH c AS (SELECT id FROM real_a) SELECT c.id FROM c LEFT JOIN real_b b ON c.id=b.id")
    assert names == {'c'} and set(refs) == {'real_a', 'real_b'}


def test_compare_readonly_and_multikey():
    source = "WITH t AS (SELECT id,amt FROM s) SELECT id AS k, SUM(amt) amt FROM t GROUP BY id"
    sql = build_compare(source, source, ['k'], ['amt'])
    assert_readonly(sql)
    assert 'o_t AS (' in sql and 'n_t AS (' in sql and '键同值不同' in sql
    sql = build_compare('SELECT a,b FROM x', 'SELECT a,b FROM y', ['a', 'b'], [])
    assert_readonly(sql)
    assert 'o.`a` = n.`a` AND o.`b` = n.`b`' in sql and '键同值不同' not in sql


def main():
    return pytest.main([__file__, '-q', '-p', 'no:cacheprovider'])


if __name__ == '__main__':
    sys.exit(main())

"""Behavior regressions for SQL transformations and per-relation partition checks."""
import sqlite3
from types import SimpleNamespace as NS

import pytest

import build_validation_sql as b
import mc_query as m


def evaluate(sql):
    with sqlite3.connect(':memory:') as db:
        return db.execute(sql).fetchall()


def test_inline_does_not_replace_other_project_table():
    sql, *_ = b.inline_task('CREATE TABLE tmp_x AS SELECT 1 AS id; '
                           'INSERT OVERWRITE TABLE p.out SELECT * FROM other.tmp_x;')
    assert 'other.tmp_x' in sql


@pytest.mark.parametrize('sql', [
    'CREATE TABLE tmp_a AS SELECT 1 AS id; CREATE TABLE tmp_b AS SELECT * FROM tmp_a; '
    'INSERT OVERWRITE TABLE tmp_a SELECT 2 AS id; INSERT OVERWRITE TABLE out SELECT * FROM tmp_b;',
    'CREATE TABLE tmp_a(id BIGINT); INSERT INTO TABLE tmp_a SELECT 1; SELECT * FROM tmp_a;',
    "INSERT OVERWRITE TABLE out PARTITION(ds='20260101') SELECT 1;",
    'CREATE TABLE tmp_a AS SELECT * FROM tmp_b; CREATE TABLE tmp_b AS SELECT 1; SELECT * FROM tmp_a;',
    'INSERT OVERWRITE TABLE out1 SELECT 1; INSERT OVERWRITE TABLE out2 SELECT 2;',
    'SET odps.sql.type.system.odps2=true; SELECT 1;',
])
def test_inline_refuses_non_equivalent_transforms(sql):
    with pytest.raises(ValueError):
        b.inline_task(sql)


def test_compare_cte_column_qualifier_is_renamed():
    q = 'WITH a AS (SELECT 1 AS id) SELECT a.id FROM a'
    assert evaluate(b.build_compare(q, q, ['id'], [])) == [('键匹配(未比较值，--measure 可加上)', 1)]


def test_compare_does_not_rename_unqualified_column_named_like_cte():
    q = 'WITH a AS (SELECT 1 AS a) SELECT a FROM a'
    assert evaluate(b.build_compare(q, q, ['a'], []))[0][1] == 1


def test_compare_preserves_cte_column_list_and_explicit_alias():
    q = 'WITH a(id) AS (SELECT 1) SELECT x.id FROM a AS x'
    assert evaluate(b.build_compare(q, q, ['id'], []))[0][1] == 1


@pytest.mark.parametrize('old,new,label,count', [
    ('SELECT NULL AS id', 'SELECT 1 AS id WHERE 0', '旧侧NULL键行数', 1),
    ('SELECT 1 AS id', 'SELECT NULL AS id', '新侧NULL键行数', 1),
    ('SELECT 1 AS id UNION ALL SELECT 1', 'SELECT 1 AS id', '旧侧重复键组数', 1),
    ('SELECT 1 AS id', 'SELECT 1 AS id UNION ALL SELECT 1', '新侧重复键组数', 1),
])
def test_compare_invalid_keys_only_reports_key_diagnostics(old, new, label, count):
    result = evaluate(b.build_compare(old, new, ['id'], []))
    assert result == [('校验失败:' + label, count)]


def test_compare_valid_changes_and_null_measures():
    old = 'SELECT 1 id, NULL v UNION ALL SELECT 2, 5 UNION ALL SELECT 4, 7'
    new = 'SELECT 1 id, 9 v UNION ALL SELECT 3, 5 UNION ALL SELECT 4, 7'
    rows = dict(evaluate(b.build_compare(old, new, ['id'], ['v'])))
    assert rows == {'仅旧有(旧有新无)': 1, '仅新增(新有旧无)': 1, '键同值不同': 1, '完全一致': 1}


class Tables:
    project = 'p'

    def exist_table(self, name):
        return True

    def get_table(self, name):
        return NS(table_schema=NS(partitions=[NS(name='ds')]))


@pytest.mark.parametrize('sql', [
    'SELECT ds FROM p.t',
    "SELECT * FROM p.a a JOIN p.b b ON a.id=b.id WHERE a.ds='20260101'",
    'SELECT * FROM `p`.`t`',
    "SELECT * FROM p.t WHERE ds='20260101' OR id=1",
    "SELECT * FROM p.a a LEFT JOIN p.b b ON a.ds='20260101' AND b.ds='20260101'",
    'SELECT * FROM p.a a JOIN p.b b ON a.ds=b.ds',
])
def test_missing_partition_filter_cannot_pass(sql):
    assert m.check_partition_filters(Tables(), sql)


@pytest.mark.parametrize('sql', [
    "SELECT * FROM p.t WHERE ds='20260101'",
    "SELECT * FROM `p`.`t` t WHERE t.`ds` IN ('20260101','20260102')",
    "SELECT * FROM p.t WHERE ds='20260101' OR ds='20260102'",
    "SELECT * FROM p.t WHERE ds BETWEEN '20260101' AND '20260102'",
    "WITH a AS (SELECT id FROM p.t WHERE ds='20260101') SELECT * FROM a",
    "SELECT * FROM p.a a LEFT JOIN p.b b ON a.id=b.id AND b.ds='20260101' WHERE a.ds='20260101'",
])
def test_supported_partition_predicates_are_accepted(sql):
    assert m.check_partition_filters(Tables(), sql) == []


def test_metadata_failure_is_not_a_pass():
    class Broken(Tables):
        def get_table(self, name):
            raise RuntimeError('metadata unavailable')
    assert m.check_partition_filters(Broken(), "SELECT * FROM p.t WHERE ds='20260101'")


def test_inline_cli_failure_preserves_existing_output(tmp_path):
    src = tmp_path / 'input.sql'
    dst = tmp_path / 'output.sql'
    src.write_text('SET unsupported=1; SELECT 1', encoding='utf-8')
    dst.write_text('keep me', encoding='utf-8')
    args = NS(file=str(src), save=str(dst), var=None, list_vars=False, target=None, project=None)
    with pytest.raises(ValueError):
        b.cmd_inline(args)
    assert dst.read_text(encoding='utf-8') == 'keep me'


def test_generated_inline_names_do_not_capture_physical_tables():
    source = 'CREATE TABLE p.a AS SELECT 1 id; SELECT a.id FROM a;'
    transformed, *_ = b.inline_task(source, project='p2')
    with sqlite3.connect(':memory:') as db:
        db.execute('CREATE TABLE a(id INTEGER)')
        db.execute('INSERT INTO a VALUES (9)')
        assert db.execute(transformed).fetchall() == [(9,)]


def test_compare_generated_names_do_not_capture_other_side_tables():
    old = 'WITH a AS (SELECT 1 id) SELECT a.id FROM a'
    new = 'SELECT id FROM o_a'
    sql = b.build_compare(old, new, ['id'], [])
    with sqlite3.connect(':memory:') as db:
        db.execute('CREATE TABLE o_a(id INTEGER)')
        db.execute('INSERT INTO o_a VALUES (2)')
        assert dict(db.execute(sql).fetchall()) == {'仅旧有(旧有新无)': 1, '仅新增(新有旧无)': 1}


def test_compare_both_empty_is_valid_empty_result():
    q = 'SELECT 1 id WHERE 0'
    assert evaluate(b.build_compare(q, q, ['id'], [])) == []


def test_inline_project_identity_is_explicit():
    source = 'CREATE TABLE tmp AS SELECT 1 id; SELECT p.tmp.id FROM p.tmp'
    sql, *_ = b.inline_task(source, project='p')
    assert evaluate(sql) == [(1,)]
    sql, *_ = b.inline_task(source, project='')
    assert 'p.tmp' in sql


def test_compare_nested_scope_and_shadowed_cte():
    q = 'WITH a AS (SELECT 1 id) SELECT a.id FROM a WHERE EXISTS (WITH a AS (SELECT 2 id) SELECT a.id FROM a WHERE a.id=2)'
    assert evaluate(b.build_compare(q, q, ['id'], []))[0][1] == 1


def test_compare_semicolon_before_trailing_comment():
    q = 'SELECT 1 AS id; -- trailing comment'
    assert evaluate(b.build_compare(q, q, ['id'], []))[0][1] == 1


def test_compare_quoted_reserved_column():
    q = 'SELECT 1 AS `select`'
    assert evaluate(b.build_compare(q, q, ['`select`'], []))[0][1] == 1


def test_cte_rewrite_preserves_project_udf():
    from sql_utils import Query
    q = 'WITH a AS (SELECT 1 id) SELECT a.func(id) AS id FROM a'
    rewritten = Query(q).rewrite(cte_prefix='o_')
    assert 'SELECT a.func(id)' in rewritten and 'FROM o_a' in rewritten


def test_cte_rewrite_handles_struct_qualifiers():
    from sql_utils import Query
    q = "WITH a AS (SELECT named_struct('id',1) AS s) SELECT a.s.id FROM a"
    rewritten = Query(q).rewrite(cte_prefix='o_')
    assert 'SELECT o_a.s.id' in rewritten and 'FROM o_a' in rewritten


@pytest.mark.parametrize('query', ["SELECT 1 INTO TABLE out", 'SELECT 1; DELETE FROM x', 'SELECT'])
def test_uninterpretable_or_writing_queries_are_not_emitted(query):
    with pytest.raises(ValueError):
        b.build_compare(query, 'SELECT 1 id', ['id'], [])


@pytest.mark.parametrize('query,status', [('SELECT * FROM p.t', '缺失'), ('SELECT * FROM p.t LATERAL VIEW f(x) t AS v', '未知')])
def test_strict_partition_blocks_before_execution(monkeypatch, capsys, query, status):
    monkeypatch.setattr(m, 'get_odps', lambda: Tables())
    def forbidden(*args):
        raise AssertionError('query must not be executed')
    monkeypatch.setattr(m, 'execute_or_report', forbidden)
    args = m.build_parser().parse_args(['sql', '-q', query, '--strict'])
    with pytest.raises(SystemExit) as error:
        m.cmd_sql(args)
    assert error.value.code == 3 and status in capsys.readouterr().err


@pytest.mark.parametrize('strict,fullscan', [(False, False), (True, True)])
def test_default_warning_and_explicit_fullscan_preserve_execution(monkeypatch, strict, fullscan):
    monkeypatch.setattr(m, 'get_odps', lambda: Tables())
    calls = []
    monkeypatch.setattr(m, 'execute_or_report', lambda *args: (calls.append(args) or object(), {}))
    monkeypatch.setattr(m, 'df_to_markdown', lambda *args, **kw: 'empty valid result')
    monkeypatch.setattr(m, 'format_run_meta', lambda *_: 'mock')
    args = NS(query='SELECT * FROM p.t', file=None, allow_full_scan=fullscan, strict=strict, save=None, max_rows=10)
    m.cmd_sql(args)
    assert len(calls) == 1


@pytest.mark.parametrize('script,args', [
    ('mc_query.py', ['--help']), ('build_validation_sql.py', ['--help']), ('di_task.py', ['--help']),
    ('build_validation_sql.py', ['inline', 'vars.sql', '--list-vars']),
    ('build_validation_sql.py', ['inline', 'query.sql']),
    ('build_validation_sql.py', ['compare', 'query.sql', 'query.sql', '--key', 'id']),
])
def test_sql_offline_commands_without_sdk_config_or_credentials(tmp_path, script, args):
    import os
    import shutil
    import subprocess
    import sys
    from pathlib import Path
    for file in Path(__file__).parent.glob('*.py'):
        if file.name != 'config.py':
            shutil.copyfile(file, tmp_path / file.name)
    (tmp_path / 'vars.sql').write_text("SELECT '${bizdate}'", encoding='utf8')
    (tmp_path / 'query.sql').write_text('SELECT 1 AS id', encoding='utf8')
    runner = """import sys,runpy
class Block:
 def find_spec(self, fullname, path=None, target=None):
  if fullname.startswith(('odps','alibabacloud','Tea')): raise ImportError('SDK forbidden')
sys.meta_path.insert(0,Block())
sys.argv=sys.argv[1:]
runpy.run_path(sys.argv[0],run_name='__main__')
"""
    env = {k:v for k,v in os.environ.items() if not k.startswith(('ALIYUN_', 'ODPS_', 'DATAWORKS_'))}
    proc = subprocess.run([sys.executable, '-X', 'utf8', '-B', '-c', runner, script, *args],
        cwd=tmp_path, env=env, capture_output=True, text=True, encoding='utf8')
    assert proc.returncode == 0, proc.stderr


def test_unresolved_variable_cli_is_atomic(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    source, output = tmp_path / 'source.sql', tmp_path / 'existing.sql'
    source.write_text("SELECT '${date}' AS id", encoding='utf8')
    output.write_text('original', encoding='utf8')
    proc = subprocess.run([sys.executable, '-X', 'utf8', '-B', str(Path(b.__file__)),
        'inline', str(source), '--save', str(output)], capture_output=True, text=True, encoding='utf8')
    assert proc.returncode == 3 and proc.stdout == '' and '未代入变量' in proc.stderr
    assert output.read_text(encoding='utf8') == 'original'


def test_target_does_not_hide_unsupported_writes():
    with pytest.raises(ValueError):
        b.inline_task('CREATE TABLE a AS SELECT 1 id; INSERT INTO TABLE a SELECT 2; SELECT * FROM a', target='a')


def test_parenthesized_top_query_is_explicitly_rejected():
    with pytest.raises(ValueError):
        b.build_compare('(SELECT 1 AS id)', 'SELECT 1 AS id', ['id'], [])


def test_inline_retains_comments_inside_qualified_names():
    source = 'CREATE TABLE p.a AS SELECT 1 id; SELECT p /* qualifier */ .a.id FROM p /* relation */ .a'
    sql, *_ = b.inline_task(source, project='p')
    assert '/* qualifier */' in sql and '/* relation */' in sql
    assert evaluate(sql) == [(1,)]


@pytest.mark.parametrize('placeholder', ['${bizdate-1}', '${}', '${unclosed'])
def test_nonstandard_unexpanded_placeholders_cannot_be_emitted(placeholder):
    with pytest.raises(ValueError, match='变量'):
        b.inline_task("SELECT '" + placeholder + "' AS id")


def test_output_publish_failure_preserves_existing_file(tmp_path, monkeypatch):
    import os
    output = tmp_path / 'existing.sql'
    output.write_text('original', encoding='utf8')
    def fail(*args):
        raise OSError('simulated publish failure')
    monkeypatch.setattr(os, 'replace', fail)
    with pytest.raises(OSError):
        b._emit('SELECT 1 id', output)
    assert output.read_text(encoding='utf8') == 'original'
    assert list(tmp_path.iterdir()) == [output]

"""CLI integration contracts for resumable, bounded SQL result consumption."""
import sys
from types import SimpleNamespace as NS

import pytest

import mc_query as m


def test_sql_parser_recovery_and_defaults():
    args = m.build_parser().parse_args(['sql', '--instance-id', 'abc', '--project', 'p'])
    assert args.instance_id == 'abc' and args.project == 'p'
    assert args.wait_timeout == 600 and args.batch_size == 10000 and args.max_rows == 200
    with pytest.raises(SystemExit):
        m.build_parser().parse_args(['sql', '--instance-id', 'abc', '-q', 'SELECT 1'])


@pytest.mark.parametrize('flag', ['--wait-timeout', '--max-rows', '--batch-size'])
@pytest.mark.parametrize('value', ['0', '-1', 'not-a-number'])
def test_sql_parser_rejects_invalid_limits(flag, value):
    with pytest.raises(SystemExit):
        m.build_parser().parse_args(['sql', '-q', 'SELECT 1', flag, value])


def test_new_query_passes_bounded_options_to_backend(monkeypatch):
    client = object()
    monkeypatch.setattr(m, 'get_odps', lambda: client)
    monkeypatch.setattr(m, 'check_partition_filters', lambda *_: [])
    calls = []
    monkeypatch.setattr(m, 'execute_or_report', lambda *args, **kwargs: (calls.append((args, kwargs)) or 'raw result', {}))
    args = m.build_parser().parse_args(['sql', '-q', 'SELECT 1', '--max-rows', '7'])
    m.cmd_sql(args)
    positional, options = calls[0]
    assert positional == (client, 'SELECT 1')
    assert options['max_rows'] == 7 and options['wait_timeout'] == 600


def test_resume_never_reads_file_or_rechecks_partition(monkeypatch, capsys):
    client = object()
    projects, calls = [], []
    monkeypatch.setattr(m, 'get_odps', lambda **kw: (projects.append(kw) or client))
    monkeypatch.setattr(m, 'check_partition_filters', lambda *_: pytest.fail('resume must not replan SQL'))
    monkeypatch.setattr(m, 'execute_or_report', lambda *args, **kw: (calls.append((args, kw)) or 'existing result', {}))
    args = m.build_parser().parse_args(['sql', '--instance-id', 'known', '--project', 'original'])
    m.cmd_sql(args)
    assert projects == [{'project': 'original'}]
    assert calls[0][1]['instance_id'] == 'known' and calls[0][0] == (client, None)
    assert 'existing result' in capsys.readouterr().out


def test_preview_footer_distinguishes_remote_and_downloaded_rows():
    text = m.format_run_meta(dict(instance_id='i', project='p', total_rows=1000000,
        downloaded_rows=7, displayed_rows=7, truncated=True, restricted=False, elapsed_s=1))
    assert '总行数=1000000' in text and '下载行数=7' in text and '展示行数=7' in text
    assert '截断=是' in text


def test_unknown_total_and_signed_logview_are_not_misrepresented():
    text = m.format_run_meta(dict(instance_id='i', total_rows=None, downloaded_rows=7,
        displayed_rows=7, restricted=True, truncated=None, logview='https://example.com/?token=private-secret'))
    assert '总行数=未知' in text and 'private-secret' not in text and '受限=是' in text


def test_sample_is_bounded(monkeypatch):
    client = NS(exist_table=lambda _: True, get_table=lambda _: NS(table_schema=NS(partitions=[])))
    monkeypatch.setattr(m, 'get_odps', lambda: client)
    calls = []
    monkeypatch.setattr(m, 'execute_or_report', lambda *args, **kw: (calls.append(kw) or 'sample', {}))
    m.cmd_sample(NS(table='p.t', n=3))
    assert calls[0]['max_rows'] == 3


def test_project_override_does_not_mutate_shared_config(monkeypatch):
    import runtime_config as config
    monkeypatch.setattr(config, 'ODPS_PROJECT', '')
    monkeypatch.setattr(config, 'ODPS_ENDPOINT', 'https://endpoint.example.com')
    monkeypatch.setattr(config, 'ACCESS_ID', 'test')
    monkeypatch.setattr(config, 'SECRET', 'test')
    created = []
    monkeypatch.setitem(sys.modules, 'odps', NS(ODPS=lambda **kw: created.append(kw) or object()))
    m.get_odps(project='chosen')
    assert created[0]['project'] == 'chosen' and config.ODPS_PROJECT == ''


def test_invalid_output_extension_prevents_cloud_connection(monkeypatch):
    monkeypatch.setattr(m, 'get_odps', lambda **kw: pytest.fail('invalid export must fail offline'))
    args = m.build_parser().parse_args(['sql', '-q', 'SELECT 1', '--save', 'wrong.json'])
    with pytest.raises(ValueError, match='csv|xlsx'):
        m.cmd_sql(args)


def test_udf_metadata_reading_survives_execution_refactor(monkeypatch, capsys):
    function = NS(name='fn', class_type='f', resources=[], code=None)
    client = NS(get_function=lambda _: function, exist_function=lambda _: True)
    monkeypatch.setattr(m, 'get_odps', lambda: client)
    m.cmd_func(NS(name='fn', save=None))
    assert 'fn' in capsys.readouterr().out


@pytest.mark.parametrize('flag', ['--instance-id', '--project'])
def test_blank_identity_is_rejected_by_parser(flag):
    argv = ['sql', flag, '  ']
    if flag == '--project':
        argv += ['-q', 'SELECT 1']
    with pytest.raises(SystemExit):
        m.build_parser().parse_args(argv)


def test_empty_file_argument_is_a_local_input_error(monkeypatch):
    monkeypatch.setattr(m, 'get_odps', lambda **kw: pytest.fail('invalid input must not connect'))
    args = m.build_parser().parse_args(['sql', '-f', ''])
    with pytest.raises((ValueError, OSError)):
        m.cmd_sql(args)

"""Offline documentation integrity; these checks do not claim agent behavior or live results."""
import json
from pathlib import Path
import re

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_skill_size_and_host_independent_templates():
    text = (ROOT / 'SKILL.md').read_text(encoding='utf8')
    assert 7500 <= len(text) <= 9500
    for path in [ROOT / 'SKILL.md', *(ROOT / 'agents').glob('*.md')]:
        content = path.read_text(encoding='utf8')
        assert '.claude' not in content and 'AskUserQuestion' not in content and 'general-purpose' not in content
        assert not re.search(r'(?m)^\s*(?:`|&)??python\s', content)


@pytest.mark.parametrize('route,reference', [('A', 'references/maxcompute_sql.md'),
    ('B', 'references/fetch_task_sql.md'), ('C', 'references/maxcompute_sql.md'),
    ('D', 'references/pyodps3_debugging.md')])
def test_abcd_routes_link_to_working_procedures(route, reference):
    text = (ROOT / 'SKILL.md').read_text(encoding='utf8')
    row = next(line for line in text.splitlines() if line.startswith('| ' + route + ' '))
    assert reference in row
    assert (ROOT / reference).is_file()


def test_relative_markdown_links_resolve():
    for path in [ROOT / 'SKILL.md', ROOT / 'README.md', *(ROOT / 'references').glob('*.md'), *(ROOT / 'agents').glob('*.md')]:
        for target in re.findall(r'\]\(([^)]+)\)', path.read_text(encoding='utf8')):
            if '://' in target or target.startswith('#'):
                continue
            assert (path.parent / target.split('#', 1)[0]).is_file(), (path, target)


def test_all_26_evaluations_have_explicit_modes_and_real_attachments():
    data = json.loads((ROOT / 'evals/evals.json').read_text(encoding='utf8'))
    assert {item['id'] for item in data['evals']} == set(range(26))
    for item in data['evals']:
        assert item['mode'] in ('live', 'offline')
        for name in item['files']:
            assert (ROOT / name).is_file(), (item['id'], name)
        if item['mode'] == 'live':
            checks = item['expected_output'] + str(item['assertions'])
            assert '返回了非空结果' not in checks and '约 5 倍' not in checks and '521357' not in checks


def test_reference_records_fail_closed_validation_and_group_review():
    sql = (ROOT / 'references/maxcompute_sql.md').read_text(encoding='utf8')
    logs = (ROOT / 'references/pyodps3_debugging.md').read_text(encoding='utf8')
    assert 'diff_type' in sql and 'cnt' in sql and '--list-vars' in sql
    assert 'ExceptionGroup' in logs and 'needs_diagnosis' in logs

#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Conservative read-only SQL generation. No service connections."""
import argparse
import os
import tempfile
import re
import sys
from pathlib import Path
from sql_utils import Query, tokenize, identifier_path, is_identifier, quote_identifier, table_identity

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass


def split_statements(sql):
    statements, start = [], 0
    for token in tokenize(sql):
        if token.text == ';':
            part = sql[start:token.start].strip()
            if tokenize(part):
                statements.append(part)
            start = token.end
    part = sql[start:].strip()
    if tokenize(part):
        statements.append(part)
    return statements


def find_vars(sql):
    return list(dict.fromkeys(re.findall(r'\$\{([^{}]*)\}', sql)))


def apply_vars(sql, var_map):
    for name, value in var_map.items():
        sql = sql.replace('$' + '{' + name + '}', value)
    return sql


def _query(sql):
    if '${' in sql:
        raise ValueError('仍有未代入变量或不支持的变量表达式：' + ', '.join(find_vars(sql)))
    model = Query(sql)
    if model.tokens[0].word not in ('SELECT', 'WITH'):
        raise ValueError('顶层查询必须直接以 SELECT/WITH 开始')
    return model


def _name(text):
    tokens = tokenize(text)
    parts, end, _ = identifier_path(tokens, 0, len(tokens))
    if end != len(tokens) or len(parts) > 3:
        raise ValueError('需要明确表名：' + text)
    return parts


def _create(stmt):
    tokens = tokenize(stmt)
    if len(tokens) < 3 or [t.word for t in tokens[:2]] != ['CREATE', 'TABLE']:
        raise ValueError('仅支持 CREATE TABLE AS SELECT')
    if tokens[2].word == 'IF':
        raise ValueError('CREATE IF NOT EXISTS 依赖物理表状态，无法保证等价')
    parts, i, _ = identifier_path(tokens, 2, len(tokens))
    tail = tokens[i:]
    if len(tail) >= 2 and tail[0].word == 'LIFECYCLE' and tail[1].kind == 'number':
        tail = tail[2:]
    if not tail or tail[0].word != 'AS' or len(tail) < 2:
        raise ValueError('显式列类型或其他建表选项暂不支持；请提供 CTAS 链路')
    body = stmt[tail[0].end:].strip()
    _query(body)
    return parts, body


def inline_task(sql, target=None, project=None):
    if project is None:
        import runtime_config as config
        project = config.ODPS_PROJECT or None
    statements = split_statements(sql)
    creates = {}
    for stmt in statements:
        if tokenize(stmt)[0].word == 'CREATE':
            parts, body = _create(stmt)
            key = table_identity(parts, project)
            if key in creates:
                raise ValueError('同一中间表被重复创建，不能保证等价')
            creates[key] = parts
    requested = table_identity(_name(target), project) if target else None
    if requested is not None and requested not in creates:
        available = ', '.join('.'.join(parts) for parts in creates.values())
        raise ValueError(f'--target {target} 不在中间表列表中。可用：{available}')
    definitions, mapping, dropped = [], {}, set()
    used = {t.name.lower() for t in tokenize(sql) if is_identifier(t)}
    final_body, final_target = None, None

    def convert(body):
        model = _query(body)
        for _, rel in model.physical:
            key = table_identity(rel.parts, project)
            if key in creates and key not in mapping:
                raise ValueError('依赖顺序倒置或中间表尚未写入：' + '.'.join(rel.parts))
        return model.rewrite(mapping=mapping, project=project)

    for stmt in statements:
        tokens = tokenize(stmt)
        first = tokens[0].word
        if first == 'DROP':
            i = 1
            if i >= len(tokens) or tokens[i].word != 'TABLE':
                raise ValueError('只支持中间表创建前的 DROP TABLE')
            i += 1
            if i + 1 < len(tokens) and tokens[i].word == 'IF' and tokens[i + 1].word == 'EXISTS':
                i += 2
            parts, i, _ = identifier_path(tokens, i, len(tokens))
            key = table_identity(parts, project)
            if i != len(tokens) or key not in creates or key in mapping or key in dropped:
                raise ValueError('DROP 不是创建前的单次清理，无法保证等价')
            dropped.add(key)
        elif first == 'CREATE':
            parts, body = _create(stmt)
            key = table_identity(parts, project)
            converted = convert(body)
            base = '__inline_' + parts[-1]
            name, suffix = base, 1
            while name.lower() in used:
                suffix += 1
                name = base + '__' + str(suffix)
            used.add(name.lower())
            mapping[key] = name
            definitions.append((key, name, converted))
        elif first == 'INSERT':
            if len(tokens) < 4 or [t.word for t in tokens[:3]] != ['INSERT', 'OVERWRITE', 'TABLE']:
                raise ValueError('仅支持 INSERT OVERWRITE TABLE；追加写入不支持')
            parts, i, _ = identifier_path(tokens, 3, len(tokens))
            if i >= len(tokens) or tokens[i].word not in ('SELECT', 'WITH'):
                raise ValueError('分区写入、目标列清单或该 INSERT 形式暂不支持')
            if table_identity(parts, project) in creates:
                raise ValueError('中间表多次写入，无法保证转写等价')
            if final_body is not None:
                raise ValueError('多个输出目标，无法生成单一对照结果')
            final_body = convert(stmt[tokens[i].start:])
            final_target = '.'.join(parts)
        elif first in ('SELECT', 'WITH'):
            if final_body is not None:
                raise ValueError('多个最终输出查询，无法保证等价')
            final_body = convert(stmt)
        else:
            raise ValueError('无法可靠转写语句：' + (first or tokens[0].text))
    if requested is not None:
        index = next(i for i, (key, _, _) in enumerate(definitions) if key == requested)
        definitions = definitions[:index + 1]
        final_body = 'SELECT * FROM ' + quote_identifier(mapping[requested])
        final_target = None
    if final_body is None:
        raise ValueError('未找到最终输出；可用 --target 指定已写入的中间表')
    if Query(final_body).top_definitions and definitions:
        raise ValueError('中间表链路的最终查询包含独立 WITH，暂不合并作用域')
    if definitions:
        head = ',\n'.join(quote_identifier(name) + ' AS (\n' + body + '\n)' for _, name, body in definitions)
        result = 'WITH\n' + head + '\n' + final_body
    else:
        result = final_body
    _query(result)
    return result, [], [name for _, name, _ in definitions], final_target


def parse_with_query(sql):
    model = _query(sql)
    return [(d.token.name, sql[d.body_start:d.body_end].strip()) for d in model.top_definitions], sql[model.final_start:].strip().rstrip(';')


def _lift(sql, prefix, used):
    _query(sql)
    for token in reversed(tokenize(sql)):
        if token.text != ';':
            break
        sql = sql[:token.start] + sql[token.end:]
    rewritten = _query(sql).rewrite(cte_prefix=prefix, reserved_names=used)
    model = _query(rewritten)
    return [rewritten[d.token.start:d.end] for d in model.top_definitions], rewritten[model.final_start:].strip().rstrip(';')


def build_compare(old_sql, new_sql, keys, measures):
    if not keys:
        raise ValueError('至少提供一个唯一键列')
    keys, measures = list(keys), list(measures)
    for name in keys + measures:
        tokens = tokenize(name)
        if len(tokens) != 1 or not is_identifier(tokens[0]):
            raise ValueError('比较列必须是单个列名：' + name)
    normalized = [tokenize(k)[0].name.lower() for k in keys]
    if len(set(normalized)) != len(keys):
        raise ValueError('唯一键列不能重复')
    key_sql = [quote_identifier(tokenize(k)[0].name, force=True) for k in keys]
    measure_sql = [quote_identifier(tokenize(m)[0].name, force=True) for m in measures]
    used = {t.name.lower() for t in tokenize(old_sql + '\n' + new_sql) if is_identifier(t)}
    used.update(normalized + [tokenize(k)[0].name.lower() for k in measures])

    def fresh(base):
        name, number = base, 0
        while name.lower() in used:
            number += 1
            name = base + '_' + str(number)
        used.add(name.lower())
        return name

    old, new, issues, gate = [fresh(s) for s in ('__old', '__new', '__key_issues', '__key_gate')]
    valid_old, valid_new = fresh('__old_valid'), fresh('__new_valid')
    marker = fresh('__validation_present')
    o_defs, o_final = _lift(old_sql, 'o_', used)
    n_defs, n_final = _lift(new_sql, 'n_', used)
    all_defs = o_defs + [f'{old} AS (\n{o_final}\n)'] + n_defs + [f'{new} AS (\n{n_final}\n)']
    nulls = ' OR '.join(k + ' IS NULL' for k in key_sql)
    key_list = ', '.join(key_sql)
    checks = []
    for table, side in ((old, '旧侧'), (new, '新侧')):
        checks.extend([
            f"SELECT '校验失败:{side}NULL键行数' AS diff_type, COUNT(*) AS cnt FROM {table} WHERE {nulls}",
            f"SELECT '校验失败:{side}重复键组数' AS diff_type, COUNT(*) AS cnt FROM "
            f"(SELECT {key_list} FROM {table} GROUP BY {key_list} HAVING COUNT(*)>1) duplicate_keys",
        ])
    all_defs.append(issues + ' AS (\n' + '\nUNION ALL\n'.join(checks) + '\n)')
    all_defs.append(f'{gate} AS (SELECT SUM(cnt) AS errors FROM {issues})')
    projections = list(dict.fromkeys(key_sql + measure_sql))
    for source, name in ((old, valid_old), (new, valid_new)):
        cols = ', '.join('s.' + col for col in projections)
        all_defs.append(f'{name} AS (SELECT {cols}, 1 AS {marker} FROM {source} s '
                        f'CROSS JOIN {gate} g WHERE g.errors=0)')
    on = ' AND '.join('o.' + k + ' = n.' + k for k in key_sql)
    if measure_sql:
        differences = ' OR '.join(
            f'((o.{m} IS NULL AND n.{m} IS NOT NULL) OR '
            f'(o.{m} IS NOT NULL AND n.{m} IS NULL) OR o.{m} <> n.{m})' for m in measure_sql)
        value_when = f"WHEN {differences} THEN '键同值不同'\n"
        equal = '完全一致'
    else:
        value_when, equal = '', '键匹配(未比较值，--measure 可加上)'
    return ('WITH\n' + ',\n'.join(all_defs) + f"""
SELECT diff_type, cnt FROM {issues} WHERE cnt>0
UNION ALL
SELECT diff_type, COUNT(*) AS cnt FROM (
  SELECT CASE
    WHEN o.{marker} IS NULL THEN '仅新增(新有旧无)'
    WHEN n.{marker} IS NULL THEN '仅旧有(旧有新无)'
    {value_when}ELSE '{equal}'
  END AS diff_type
  FROM {valid_old} o FULL OUTER JOIN {valid_new} n ON {on}
) compared
GROUP BY diff_type
ORDER BY cnt DESC, diff_type""")


def _collect_vars(pairs):
    result = {}
    for pair in pairs or []:
        key, sep, value = pair.partition('=')
        if not sep or not re.fullmatch(r'\w+', key):
            raise ValueError('--var 需要 name=value')
        if key in result and result[key] != value:
            raise ValueError('--var 重复且值冲突：' + key)
        result[key] = value
    return result


def _emit(sql, save):
    _query(sql)
    if save:
        target = Path(save)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='\n',
                    dir=target.parent, prefix='.' + target.name + '.', suffix='.tmp', delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(sql)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        print(f'已写出 {len(sql)} 字符至 {save}', file=sys.stderr)
    else:
        print(sql)


def cmd_inline(args):
    sql = Path(args.file).read_text(encoding='utf-8-sig')
    if args.list_vars:
        print('变量占位符：' + ', '.join(find_vars(sql)))
        return
    sql = apply_vars(sql, _collect_vars(args.var))
    result, _, _, _ = inline_task(sql, target=args.target, project=getattr(args, 'project', None))
    _emit(result, args.save)


def cmd_compare(args):
    variables = _collect_vars(args.var)
    old = apply_vars(Path(args.old).read_text(encoding='utf-8-sig'), variables)
    new = apply_vars(Path(args.new).read_text(encoding='utf-8-sig'), variables)
    keys = [k.strip() for k in args.key.split(',') if k.strip()]
    measures = [m.strip() for m in (args.measure or '').split(',') if m.strip()]
    _emit(build_compare(old, new, keys, measures), args.save)


def build_parser():
    parser = argparse.ArgumentParser(description='生成保守的只读验证 SQL；不连接云服务')
    sub = parser.add_subparsers(dest='cmd', required=True)
    inline = sub.add_parser('inline', help='可靠的单次写入 CTAS 链路 → WITH')
    inline.add_argument('file')
    inline.add_argument('--target', help='只验证到已写入的中间表')
    inline.add_argument('--project', help='非限定表名所属项目；默认已有 ODPS_PROJECT，未知不猜测')
    inline.add_argument('--list-vars', action='store_true')
    inline.set_defaults(func=cmd_inline)
    compare = sub.add_parser('compare', help='先校验非空唯一键，再对比结果')
    compare.add_argument('old')
    compare.add_argument('new')
    compare.add_argument('--key', required=True)
    compare.add_argument('--measure')
    compare.set_defaults(func=cmd_compare)
    for command in (inline, compare):
        command.add_argument('--var', action='append', help='name=value，可重复')
        command.add_argument('--save', help='UTF-8 SQL 输出文件')
    return parser


def main():
    args = build_parser().parse_args()
    try:
        args.func(args)
    except (ValueError, OSError) as error:
        print('[无法生成验证 SQL] ' + str(error), file=sys.stderr)
        sys.exit(3)


if __name__ == '__main__':
    main()

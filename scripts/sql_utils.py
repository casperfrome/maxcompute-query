"""Conservative SQL structure analysis. Standard library only; never connects.

Tokens retain source offsets. SELECT/WITH, subqueries and ANSI joins are supported.
Unrecognized relation syntax is an error, not a guess about table identity.
"""
from dataclasses import dataclass, field
import re


@dataclass(frozen=True)
class Token:
    text: str
    start: int
    end: int
    kind: str

    @property
    def name(self):
        return self.text[1:-1].replace('``', '`') if self.kind == 'quoted' else self.text

    @property
    def word(self):
        return self.text.upper() if self.kind == 'word' else ''


def tokenize(sql):
    result, i = [], 0
    while i < len(sql):
        start = i
        if sql[i].isspace():
            i += 1
            continue
        if sql.startswith('--', i):
            end = sql.find('\n', i)
            i = len(sql) if end < 0 else end
            continue
        if sql.startswith('/*', i):
            end = sql.find('*/', i + 2)
            if end < 0:
                raise ValueError('SQL 块注释未闭合')
            i = end + 2
            continue
        if sql[i] in "'\"`":
            quote = sql[i]
            i += 1
            while i < len(sql):
                if sql[i] == '\\' and quote != '`':
                    i += 2
                elif sql[i] == quote:
                    if sql[i:i + 2] == quote * 2:
                        i += 2
                    else:
                        i += 1
                        break
                else:
                    i += 1
            else:
                raise ValueError('SQL 引号未闭合')
            result.append(Token(sql[start:i], start, i, 'quoted' if quote == '`' else 'string'))
            continue
        match = re.match(r'(?:[^\W\d]|_)\w*', sql[i:], re.UNICODE)
        if match:
            i += len(match.group())
            kind = 'word'
        else:
            match = re.match(r'\d+(?:\.\d+)?(?:[eE][+-]?\d+)?', sql[i:])
            if match:
                i += len(match.group())
                kind = 'number'
            else:
                op = next((x for x in ('<=>', '<=', '>=', '<>', '!=', '||') if sql.startswith(x, i)), None)
                i += len(op) if op else 1
                kind = 'symbol'
        result.append(Token(sql[start:i], start, i, kind))
    return result


def is_identifier(token):
    return token.kind in ('word', 'quoted')


def quote_identifier(name, force=False):
    return name if not force and re.fullmatch(r'[A-Za-z_]\w*', name) else '`' + name.replace('`', '``') + '`'


def identifier_path(tokens, index, end):
    if index >= end or not is_identifier(tokens[index]):
        raise ValueError('需要明确的 SQL 标识符')
    indices = [index]
    index += 1
    while index < end and tokens[index].text == '.':
        if index + 1 >= end or not is_identifier(tokens[index + 1]):
            break
        indices.append(index + 1)
        index += 2
    return tuple(tokens[n].name for n in indices), index, indices


def table_identity(parts, project=None):
    parts = tuple(p.lower() for p in parts)
    if len(parts) == 1 and project:
        return (str(project).lower(),) + parts
    return parts


def apply_edits(sql, edits):
    unique = {}
    for start, end, text in edits:
        if (start, end) in unique and unique[start, end] != text:
            raise ValueError('SQL 标识符映射冲突')
        unique[start, end] = text
    previous = len(sql) + 1
    for (start, end), text in sorted(unique.items(), reverse=True):
        if end > previous:
            raise ValueError('SQL 标识符映射范围重叠')
        sql = sql[:start] + text + sql[end:]
        previous = start
    return sql


@dataclass(eq=False)
class Definition:
    token: Token
    body_start: int
    body_end: int
    end: int


@dataclass(eq=False)
class Relation:
    parts: tuple
    start: int
    end: int
    alias: str
    explicit_alias: bool
    definition: object = None
    derived: bool = False
    predicates: list = field(default_factory=list)


@dataclass
class Scope:
    start: int
    end: int
    parent: object
    relations: list = field(default_factory=list)
    children: list = field(default_factory=list)


_CLAUSES = {'WHERE', 'GROUP', 'HAVING', 'ORDER', 'LIMIT', 'QUALIFY', 'WINDOW', 'DISTRIBUTE', 'SORT', 'CLUSTER'}
_JOIN = {'JOIN', 'LEFT', 'RIGHT', 'FULL', 'INNER', 'CROSS', 'NATURAL', 'SEMI', 'ANTI'}
_RESERVED = _CLAUSES | _JOIN | {'ON', 'USING', 'UNION', 'INTERSECT', 'EXCEPT', 'LATERAL', 'TABLESAMPLE'}


class Query:
    def __init__(self, sql):
        self.sql = sql
        self.tokens = tokenize(sql)
        while self.tokens and self.tokens[-1].text == ';':
            self.tokens.pop()
        if not self.tokens or any(t.text == ';' for t in self.tokens):
            raise ValueError('需要单条 SELECT/WITH 查询')
        forbidden = {'INSERT', 'UPDATE', 'DELETE', 'DROP', 'ALTER', 'CREATE', 'TRUNCATE',
                     'MERGE', 'RENAME', 'GRANT', 'REVOKE', 'UNLOAD', 'MSCK', 'PURGE', 'RESTORE', 'INTO', 'TRANSFORM'}
        for i, token in enumerate(self.tokens):
            if token.word in forbidden and not (i + 1 < len(self.tokens) and self.tokens[i + 1].text == '('):
                raise ValueError('查询含写入或无法解释的关键字：' + token.word)
        self.pairs, stack = {}, []
        for i, token in enumerate(self.tokens):
            if token.text == '(':
                stack.append(i)
            elif token.text == ')':
                if not stack:
                    raise ValueError('SQL 括号不匹配')
                opening = stack.pop()
                self.pairs[opening] = i
        if stack:
            raise ValueError('SQL 括号未闭合')
        self.scopes, self.definitions = [], []
        self.top_definitions, self.final_start = self._query(0, len(self.tokens), {}, None)

    def _top(self, start, end):
        i = start
        while i < end:
            yield i
            i = self.pairs[i] + 1 if self.tokens[i].text == '(' else i + 1

    def _query(self, start, end, inherited, parent):
        tokens = self.tokens
        while start < end and tokens[start].text == '(' and self.pairs[start] == end - 1:
            start, end = start + 1, end - 1
        if start >= end:
            raise ValueError('查询为空')
        visible, definitions = dict(inherited), []
        if tokens[start].word == 'WITH':
            start += 1
            if start < end and tokens[start].word == 'RECURSIVE':
                raise ValueError('暂不支持递归 CTE')
            while True:
                if start >= end or not is_identifier(tokens[start]):
                    raise ValueError('无法解析 CTE 名称')
                name_token = tokens[start]
                name = name_token.name.lower()
                if any(d.token.name.lower() == name for d in definitions):
                    raise ValueError('同一作用域的 CTE 重名')
                start += 1
                if start < end and tokens[start].text == '(':
                    columns_end = self.pairs[start]
                    if any(not is_identifier(t) and t.text != ',' for t in tokens[start + 1:columns_end]):
                        raise ValueError('无法解析 CTE 列清单')
                    start = columns_end + 1
                if start + 1 >= end or tokens[start].word != 'AS' or tokens[start + 1].text != '(':
                    raise ValueError('无法解析 CTE AS 查询')
                opening = start + 1
                closing = self.pairs[opening]
                definition = Definition(name_token, tokens[opening].end, tokens[closing].start, tokens[closing].end)
                self._query(opening + 1, closing, visible, parent)
                definitions.append(definition)
                self.definitions.append(definition)
                visible[name] = definition
                start = closing + 1
                if start >= end or tokens[start].text != ',':
                    break
                start += 1
        if start >= end:
            raise ValueError('CTE 后没有查询')
        final_start = tokens[start].start
        boundaries = [i for i in self._top(start, end) if tokens[i].word in ('UNION', 'INTERSECT', 'EXCEPT')]
        for boundary in boundaries + [end]:
            if start >= boundary:
                raise ValueError('集合查询缺少 SELECT')
            self._select(start, boundary, visible, parent)
            start = boundary + 1
            if start < end and tokens[start].word in ('ALL', 'DISTINCT'):
                start += 1
        return definitions, final_start

    def _select(self, start, end, visible, parent):
        tokens = self.tokens
        if tokens[start].word != 'SELECT':
            raise ValueError('只支持可解析的 SELECT/WITH 查询')
        if start + 1 >= end or tokens[start + 1].word == 'FROM':
            raise ValueError('SELECT 缺少投影表达式')
        scope = Scope(start, end, parent)
        self.scopes.append(scope)
        if parent is not None:
            parent.children.append(scope)
        def nested(lo, hi):
            for i in self._top(lo, hi):
                if tokens[i].text == '(':
                    close = self.pairs[i]
                    if i + 1 < close and tokens[i + 1].word in ('SELECT', 'WITH'):
                        self._query(i + 1, close, visible, scope)
                    else:
                        nested(i + 1, close)
        nested(start, end)
        top = list(self._top(start, end))
        if any(tokens[i].word in ('LATERAL', 'TABLESAMPLE') for i in top):
            raise ValueError('暂不支持 LATERAL/TABLESAMPLE 关系分析')
        from_positions = [i for i in top if tokens[i].word == 'FROM']
        if len(from_positions) > 1:
            raise ValueError('同一 SELECT 中出现多个 FROM')
        if from_positions:
            first = from_positions[0] + 1
            finish = next((i for i in top if i >= first and tokens[i].word in _CLAUSES), end)
            self._sources(scope, first, finish, visible)
        where = next((i for i in top if tokens[i].word == 'WHERE'), None)
        if where is not None:
            finish = next((i for i in top if i > where and tokens[i].word in _CLAUSES), end)
            for relation in scope.relations:
                relation.predicates.append((where + 1, finish))

    def _sources(self, scope, start, end, visible):
        tokens = self.tokens
        join_kind = 'FIRST'
        while start < end:
            first = start
            derived = tokens[start].text == '('
            if derived:
                close = self.pairs[start]
                if start + 1 >= close or tokens[start + 1].word not in ('SELECT', 'WITH'):
                    raise ValueError('暂不支持括号 JOIN 或表函数')
                parts, last = (), close + 1
            else:
                parts, last, _ = identifier_path(tokens, start, end)
                if len(parts) > 3 or (last < end and tokens[last].text == '('):
                    raise ValueError('暂不支持该关系名或表函数')
            source_end = tokens[last - 1].end
            explicit = False
            alias = parts[-1] if parts else ''
            if last < end and tokens[last].word == 'AS':
                last += 1
                if last >= end or not is_identifier(tokens[last]):
                    raise ValueError('缺少关系别名')
                explicit, alias, last = True, tokens[last].name, last + 1
            elif last < end and is_identifier(tokens[last]) and tokens[last].word not in _RESERVED:
                explicit, alias, last = True, tokens[last].name, last + 1
            if derived and not alias:
                raise ValueError('子查询必须有明确别名')
            if any(r.alias.lower() == alias.lower() for r in scope.relations):
                raise ValueError('同一查询作用域关系别名不唯一')
            relation = Relation(parts, tokens[first].start, source_end, alias, explicit,
                                visible.get(parts[0].lower()) if len(parts) == 1 else None, derived)
            previous = list(scope.relations)
            scope.relations.append(relation)
            if last < end and tokens[last].word == 'ON':
                pred_start = last + 1
                last = next((i for i in self._top(pred_start, end)
                             if tokens[i].word in _JOIN or tokens[i].text == ','), end)
                eligible = ([relation] if join_kind == 'LEFT' else previous if join_kind == 'RIGHT'
                            else [] if join_kind == 'FULL' else previous + [relation])
                for rel in eligible:
                    rel.predicates.append((pred_start, last))
            elif last < end and tokens[last].word == 'USING':
                last += 1
                if last >= end or tokens[last].text != '(':
                    raise ValueError('无法解析 USING')
                last = self.pairs[last] + 1
            if last == end:
                return
            if tokens[last].text == ',':
                join_kind, start = 'CROSS', last + 1
                continue
            join_kind = 'INNER'
            if tokens[last].word in ('LEFT', 'RIGHT', 'FULL', 'INNER', 'CROSS'):
                join_kind = tokens[last].word
                last += 1
                if last < end and tokens[last].word == 'OUTER':
                    last += 1
            if last >= end or tokens[last].word != 'JOIN':
                raise ValueError('无法解析 FROM/JOIN 中的语法')
            start = last + 1
        raise ValueError('JOIN 后缺少关系')

    @property
    def physical(self):
        return [(scope, rel) for scope in self.scopes for rel in scope.relations
                if not rel.derived and rel.definition is None]

    def rewrite(self, mapping=None, project=None, cte_prefix=None, reserved_names=None):
        mapping = mapping or {}
        used = {t.name.lower() for t in self.tokens if is_identifier(t)}
        used.update(reserved_names or ())
        names = {}
        if cte_prefix:
            for definition in self.definitions:
                base = cte_prefix + definition.token.name
                name, number = base, 0
                while name.lower() in used:
                    number += 1
                    name = base + '_' + str(number)
                names[definition] = name
                used.add(name.lower())
        if reserved_names is not None:
            reserved_names.update(used)
        def replace_name(start, end, name):
            # Qualified names may have comments between components; keep every gap.
            pieces, cursor = [], start
            for token in self.tokens:
                if start <= token.start < end:
                    pieces.append(self.sql[cursor:token.start])
                    cursor = token.end
            pieces.append(self.sql[cursor:end])
            return quote_identifier(name) + ''.join(pieces)

        edits = [(d.token.start, d.token.end, quote_identifier(name)) for d, name in names.items()]
        for scope in self.scopes:
            for rel in scope.relations:
                replacement = names.get(rel.definition) if rel.definition else mapping.get(table_identity(rel.parts, project))
                if replacement is not None and not rel.derived:
                    edits.append((rel.start, rel.end, replace_name(rel.start, rel.end, replacement)))
            i = scope.start
            while i < scope.end:
                token = self.tokens[i]
                if any(self.tokens[c.start].start <= token.start < self.tokens[c.end - 1].end for c in scope.children):
                    i += 1
                    continue
                if is_identifier(token) and (i == 0 or self.tokens[i - 1].text != '.'):
                    parts, last, indices = identifier_path(self.tokens, i, scope.end)
                    star = last + 1 < scope.end and self.tokens[last].text == '.' and self.tokens[last + 1].text == '*'
                    qualifier = parts if star else parts[:-1]
                    function = last < scope.end and self.tokens[last].text == '('
                    if qualifier and not function and not any(r.start <= token.start < r.end for r in scope.relations):
                        owner = scope
                        while owner:
                            matches = []
                            lowered = tuple(p.lower() for p in qualifier)
                            for rel in owner.relations:
                                lengths = []
                                if lowered[0] == rel.alias.lower():
                                    lengths.append(1)
                                physical = tuple(p.lower() for p in rel.parts)
                                if physical and not rel.explicit_alias and lowered[:len(physical)] == physical:
                                    lengths.append(len(physical))
                                if lengths:
                                    matches.append((max(lengths), rel))
                            if matches:
                                matches.sort(key=lambda item: item[0], reverse=True)
                                if len(matches) > 1:
                                    raise ValueError('列限定符存在歧义，无法可靠改写')
                                length, rel = matches[0]
                                replacement = names.get(rel.definition) if rel.definition else mapping.get(table_identity(rel.parts, project))
                                if replacement is not None and not rel.explicit_alias:
                                    qend = self.tokens[indices[length - 1]].end
                                    edits.append((token.start, qend, replace_name(token.start, qend, replacement)))
                                break
                            owner = owner.parent
                    i = last
                else:
                    i += 1
        return apply_edits(self.sql, edits)


def mask_literals(sql):
    """Same-length mask for callers doing source slicing; no SDK dependency."""
    out = [' '] * len(sql)
    for token in tokenize(sql):
        if token.kind in ('string', 'quoted'):
            out[token.start] = token.text[0]
            out[token.end - 1] = token.text[-1]
        else:
            out[token.start:token.end] = token.text
    return ''.join(out)


def partition_constrained(query, scope, relation, column, start, end, project=None):
    """Recognize only direct, bounded predicates; do not infer join propagation."""
    tokens = query.tokens
    while start < end and tokens[start].text == '(' and query.pairs[start] == end - 1:
        start, end = start + 1, end - 1
    if start >= end:
        return False
    top = list(query._top(start, end))
    ors = [i for i in top if tokens[i].word == 'OR']
    if ors:
        bounds = [start - 1] + ors + [end]
        return all(partition_constrained(query, scope, relation, column, a + 1, b, project)
                   for a, b in zip(bounds, bounds[1:]))
    ands, between = [], False
    for i in top:
        if tokens[i].word == 'BETWEEN':
            between = True
        elif tokens[i].word == 'AND':
            if between:
                between = False
            else:
                ands.append(i)
    if ands:
        bounds = [start - 1] + ands + [end]
        return any(partition_constrained(query, scope, relation, column, a + 1, b, project)
                   for a, b in zip(bounds, bounds[1:]))

    def column_end(lo, hi):
        if lo >= hi or not is_identifier(tokens[lo]):
            return None
        parts, last, _ = identifier_path(tokens, lo, hi)
        if parts[-1].lower() != column.lower():
            return None
        if len(parts) == 1:
            return last if len(scope.relations) == 1 else None
        qualifier = tuple(p.lower() for p in parts[:-1])
        if qualifier == (relation.alias.lower(),):
            return last
        if not relation.explicit_alias and qualifier == tuple(p.lower() for p in relation.parts):
            return last
        return None

    def value(lo, hi):
        if hi - lo == 1:
            return tokens[lo].kind in ('string', 'number') and tokens[lo].text not in ("''", '""')
        if hi - lo == 2 and tokens[lo].text in ('+', '-') and tokens[lo + 1].kind == 'number':
            return True
        if (hi - lo == 4 and tokens[lo].word == 'MAX_PT' and tokens[lo + 1].text == '('
                and tokens[lo + 2].kind == 'string' and tokens[lo + 3].text == ')'):
            raw = tokens[lo + 2].text[1:-1]
            return table_identity(tuple(raw.split('.')), project) == table_identity(relation.parts, project)
        return False

    middle = column_end(start, end)
    if middle is not None and middle < end:
        op = tokens[middle]
        if op.text in ('=', '<', '<=', '>', '>='):
            return value(middle + 1, end)
        if op.word == 'BETWEEN':
            sep = next((i for i in top if i > middle and tokens[i].word == 'AND'), None)
            return sep is not None and value(middle + 1, sep) and value(sep + 1, end)
        if op.word == 'IN' and middle + 1 < end and tokens[middle + 1].text == '(':
            opening = middle + 1
            if query.pairs[opening] != end - 1:
                return False
            commas = [i for i in query._top(opening + 1, end - 1) if tokens[i].text == ',']
            bounds = [opening] + commas + [end - 1]
            return all(value(a + 1, b) for a, b in zip(bounds, bounds[1:]))
    for i in top:
        if tokens[i].text in ('=', '<', '<=', '>', '>=') and value(start, i):
            return column_end(i + 1, end) == end
    return False

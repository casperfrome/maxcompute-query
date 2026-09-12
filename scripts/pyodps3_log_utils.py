"""Pure text helpers for DataWorks console output. No clients or task execution."""
import html
import re


_LEVEL = r'(?:TRACE|DEBUG|INFO|WARN(?:ING)?|ERROR|CRITICAL|FATAL)'
_TIMESTAMP = re.compile(
    r'^\s*\[?\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?'
    r'(?:Z|[+-]\d{2}:?\d{2})?\]?\s')
_TIMESTAMP_LEVEL = re.compile(r'^' + _LEVEL + r'(?:\s|:)[ ]?')
_WRAPPER = re.compile(r'^\s*' + _LEVEL + r':(?:odps\.)?pyodpswrapper:[ ]?')
_HEADER = re.compile(r'^\s*Traceback \(most recent call last\):\s*$')
_FRAME = re.compile(r'^\s*File ["\'](.+?)["\'], line (\d+)(?:, in (.*))?\s*$')
_EXCEPTION = re.compile(r'^([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)(?::\s?(.*))?$')
_KNOWN_EXCEPTION = re.compile(r'(?:Error|Exception)$|^(?:KeyboardInterrupt|SystemExit|StopIteration|GeneratorExit)$')
_CHAIN = re.compile(r'^(?:During handling of the above exception|The above exception was the direct cause)')
_FOOTER = re.compile(r'^(?:Exit code of the Shell command|Shell run failed|Current task status:|'
                     r'Process exited|Process exit code|\[?日志.{0,8}截断|\[?log.{0,20}truncat)', re.I)


_WRAPPER_NOTICE = '错误信息中似乎不包含 PyODPS 相关的代码，请检查自己的代码逻辑是否正确。'
_SEPARATOR = re.compile(r'={3,}')


def _is_platform_footer(lines, index):
    text = lines[index].strip()
    if _FOOTER.match(text):
        return True
    is_notice = text == _WRAPPER_NOTICE
    if not is_notice and not _SEPARATOR.fullmatch(text):
        return False
    # The wrapper notice and separators can also be user message text. Treat
    # them as a footer only when followed by an explicit platform exit/status.
    following = index + 1
    while following < len(lines) and not lines[following].strip():
        following += 1
    if is_notice and following < len(lines) and _SEPARATOR.fullmatch(lines[following].strip()):
        following += 1
        while following < len(lines) and not lines[following].strip():
            following += 1
    return following < len(lines) and bool(_FOOTER.match(lines[following].strip()))


def normalize_log(raw):
    """Decode a parsing copy; one line of a prefixed stack remains one stack line."""
    text = html.unescape(raw)
    text = re.sub(r'\\([_<>])', r'\1', text)
    if '\n' not in text and '\\n' in text:
        text = text.replace('\\r\\n', '\n').replace('\\n', '\n')
    lines = []
    for line in text.splitlines():
        without_time, count = _TIMESTAMP.subn('', line, count=1)
        # Strip a whole wrapper before a generic level, or WARNING: would be
        # consumed leaving an unrecognizable odps.pyodpswrapper: prefix.
        line = _WRAPPER.sub('', without_time, count=1)
        if count:
            line = _TIMESTAMP_LEVEL.sub('', line, count=1)
        line = _WRAPPER.sub('', line, count=1)
        lines.append(line)
    return '\n'.join(lines)


def extract_tracebacks(normalized, redact):
    """Keep stack blocks and chained exceptions, including message continuations.

    Line ranges refer to the normalized copy. Unframed, known exception lines
    are still useful when the platform has omitted the beginning of a stack.
    """
    lines = normalized.splitlines()
    blocks, exceptions = [], []
    start = None
    frames = []
    block_exceptions = []
    chain_pending = False
    current_exception = None

    def finish(end):
        nonlocal start, frames, block_exceptions, current_exception, chain_pending
        if start is not None:
            for exception in block_exceptions:
                exception['message'] = exception['message'].rstrip('\n')
            text = '\n'.join(lines[start:end]).rstrip()
            if text:
                blocks.append({'start_line': start + 1, 'end_line': start + len(text.splitlines()),
                               'text': redact(text), 'frames': frames,
                               'exceptions': block_exceptions})
        start, frames, block_exceptions = None, [], []
        current_exception, chain_pending = None, False

    for index, line in enumerate(lines):
        stripped = line.strip()
        if _is_platform_footer(lines, index):
            finish(index)
            continue
        if _CHAIN.match(stripped) and start is not None:
            chain_pending, current_exception = True, None
            continue
        if _HEADER.match(line):
            if start is not None and not chain_pending:
                finish(index)
            if start is None:
                start = index
            chain_pending, current_exception = False, None
            continue
        frame = _FRAME.match(line)
        if frame:
            if start is None:
                start = index  # SyntaxError / clipped traceback without a header.
            frames.append({'file': redact(frame.group(1)), 'line': int(frame.group(2)),
                           'function': redact(frame.group(3)) if frame.group(3) else None})
            current_exception = None
            continue
        exception = _EXCEPTION.match(line)
        type_name = exception.group(1).rsplit('.', 1)[-1] if exception else None
        is_exception = exception and (_KNOWN_EXCEPTION.search(type_name) or (
            start is not None and current_exception is None and type_name[:1].isupper()))
        if is_exception:
            if start is None:
                start = index
            current_exception = {'type': type_name, 'message': redact(exception.group(2) or '')}
            exceptions.append(current_exception)
            block_exceptions.append(current_exception)
            continue
        if current_exception is not None:
            # Python exception messages may contain blank paragraphs. Only an
            # explicit footer, chain marker or new stack delimits the block.
            current_exception['message'] += '\n' + redact(line)
    finish(len(lines))
    return blocks, exceptions


def render_log(raw, analysis, output_format, redact, tail=None):
    """Render a display copy. Tail never affects collection or saved evidence."""
    if output_format == 'traceback':
        return '\n\n'.join(block['text'] for block in analysis['tracebacks']) or '未识别到 traceback。'
    text = redact(raw)
    if tail is not None:
        text = '\n'.join(text.splitlines()[-tail:])
    return text

"""Frozen changes on real, pinned repository source. Not upstream change requests."""

from dataclasses import dataclass


PINS = {
    "click": "c06d2d0a6aee6bcc50bd8257be2a4a592f4e75d0",
    "packaging": "3b77a26f5a27473ad3b08194d773f325d018a2d0",
}


@dataclass(frozen=True)
class Case:
    name: str
    repo: str
    paths: tuple[str, str]
    symbols: tuple[str, str]
    request: str
    decision: str
    public: tuple[str, ...]
    hidden: tuple[str, ...]
    preserve: tuple[str, ...]
    reference: tuple[str, str | None]


FIRST_BATCH = (
    Case(
        "terminal-table-width", "click", ("_compat", "formatting"), ("term_len", "measure_table"),
        "Apply the recorded terminal-table width policy in both functions.",
        "term_len strips ANSI escapes, then ignores trailing ASCII spaces. Other characters still count. "
        "measure_table drops only trailing zero-width columns and keeps leading and internal zero-width columns.",
        ("a.term_len('tag   ') == 3", "b.measure_table([('', 'x', '')]) == (0, 1)"),
        ("a.term_len('\x1b[31mcafe\x1b[0m   ') == 4",
         "b.measure_table([(' ', 'xx', '', ' '), ('', 'y', '')]) == (0, 2)",
         "b.measure_table([('', 'xx', '', 'y', '')]) == (0, 2, 0, 1)"),
        ("b.measure_table([]) == ()", "b.measure_table([('x', 'yyy'), ('xx', 'y')]) == (2, 3)",
         r"a.term_len('\x1b[31ma b\x1b[0m') == 3", r"a.term_len('x\t') == 2"),
        ("return len(strip_ansi(x).rstrip(' '))",
         """widths = {}
for row in rows:
    for idx, col in enumerate(row):
        widths[idx] = max(widths.get(idx, 0), term_len(col))
result = [width for _, width in sorted(widths.items())]
while result and result[-1] == 0:
    result.pop()
return tuple(result)"""),
    ),
    Case(
        "wheel-tag-validation", "packaging", ("tags", "utils"), ("parse_tag", "parse_wheel_filename"),
        "Apply the recorded wheel-tag validation policy in both functions.",
        "parse_tag rejects an empty component, including empty alternatives in a compressed group, with ValueError. "
        "parse_wheel_filename wraps a ValueError from parse_tag in InvalidWheelFilename, keeping the original exception as its cause.",
        ("raises(ValueError, a.parse_tag, 'py3.-none-any')",
         "raises(b.InvalidWheelFilename, b.parse_wheel_filename, 'demo-1-py3.-none-any.whl')"),
        ("raises(ValueError, a.parse_tag, 'py3--any')", "raises(ValueError, a.parse_tag, 'py3-none-any..other')",
         "wraps_tag_error('demo-1-py3-none-.whl')", "wraps_tag_error('demo-1-py3-none.any-platform..two.whl')"),
        ("{str(tag) for tag in a.parse_tag('py3.py2-none-any')} == {'py3-none-any', 'py2-none-any'}",
         "wheel_parts('Demo_Name-1.2-4abc-py3-none-any.whl') == ('demo-name', '1.2', (4, 'abc'), {'py3-none-any'})",
         "raises(b.InvalidWheelFilename, b.parse_wheel_filename, 'demo-1-py3-none-any.zip')"),
        ("""interpreters, abis, platforms = [part.split('.') for part in tag.split('-')]
if any('' in group for group in (interpreters, abis, platforms)):
    raise ValueError(f'Empty wheel tag component: {tag!r}')
return frozenset(Tag(interpreter, abi, platform)
                 for interpreter in interpreters for abi in abis for platform in platforms)""",
         None),  # The reference wraps the existing call and preserves the rest of the parser.
    ),
)


CASES = (
    Case(
        "terminal-color-defaults", "click", ("_compat", "globals"),
        ("should_strip_ansi", "resolve_color_default"),
        "Apply the approved default-color policy across the terminal helpers.",
        "should_strip_ansi must strip when neither a stream nor an explicit color is supplied, regardless of stdin. "
        "resolve_color_default must return False when no explicit color or active context exists. "
        "With an active context preserve its color value, including None. Explicit color flags keep taking priority.",
        ("with_tty_stdin() is True", "b.resolve_color_default() is False"),
        ("a.should_strip_ansi(color=True) is False", "current_color(None) is None",
         "current_color(True) is True", "b.resolve_color_default(False) is False"),
        ("a.should_strip_ansi(stream=Stream(True)) is False",
         "a.should_strip_ansi(stream=Stream(False)) is True",
         "a.should_strip_ansi(color=False) is True", "b.resolve_color_default(True) is True"),
        ("""if color is None:
    if stream is None:
        return True
    return not isatty(stream) and not _is_jupyter_kernel_output(stream)
return not color""",
         """if color is not None:
    return color
ctx = get_current_context(silent=True)
return ctx.color if ctx is not None else False"""),
    ),
    Case(
        "project-name-outer-spacing", "packaging", ("utils", "pylock"),
        ("canonicalize_name", "_validate_normalized_name"),
        "Apply the approved project-name spacing policy to canonicalization and lockfile validation.",
        "canonicalize_name and _validate_normalized_name trim only leading and trailing ASCII spaces before existing "
        "normalization or validation. Keep interior whitespace, existing normalization rules, and lockfile case validation.",
        ("a.canonicalize_name(' Foo_Bar ', validate=True) == 'foo-bar'",
         "b._validate_normalized_name(' demo-name ') == 'demo-name'"),
        ("a.canonicalize_name('   A..B  ') == 'a-b'",
         "b._validate_normalized_name('   foo-bar   ') == 'foo-bar'",
         "raises(b.PylockValidationError, b._validate_normalized_name, ' Demo_Name ')"),
        ("a.canonicalize_name('A..B', validate=True) == 'a-b'",
         "not valid_name('A B')", "not valid_name('\tA')",
         "raises(b.PylockValidationError, b._validate_normalized_name, 'demo\t')"),
        ("""name = name.strip(' ')
if validate and not _validate_regex.fullmatch(name):
    raise InvalidName(f'name is invalid: {name!r}')
value = name.lower().replace('_', '-').replace('.', '-')
while '--' in value:
    value = value.replace('--', '-')
return cast('NormalizedName', value)""",
         """name = name.strip(' ')
if not is_normalized_name(name):
    raise PylockValidationError(f'Name {name!r} is not normalized')
return NormalizedName(name)"""),
    ),
)

"""Visible tests and rule checks for the notes-versus-executable-rules repair trial.

For each frozen maintained-notes task (``notes_cases.CASES``):

- ``tests`` stand in for the project's existing test suite. Every arm can run
  them after each attempt. They exercise existing behavior only, so they pass
  on the original function and on the reference change.
- ``checks`` are the executable checks attached to the approved rule in
  ``.trace/RULES.md``. They pass on the reference, and at least one fails on
  the original. Tasks without a historical rule have none.

Both sets were written for this trial, after the frozen tasks, with inputs
chosen to differ from every hidden grader clause; the trial preflight asserts
that no visible test or rule check repeats a hidden clause, and that the
pass/fail properties above hold in the WASI grader.
"""

TESTS = {
    "trace:trace_memory/native.py:_identifier": (
        "_identifier('abc') == 'abc'", "_identifier(None) is None", "_identifier('') is None"),
    "trace:memory/task_library.py:is_commit": (
        "is_commit('a' * 40)", "not is_commit('g' * 40)", "not is_commit(None)"),
    "trace:memory/task_library.py:is_git_path": (
        "is_git_path('src/main.py')", "not is_git_path('a/../b')", "not is_git_path('C:/x')"),
    "trace:memory/session_context.py:_truncate": (
        "_truncate('hello', 10) == ('hello', False)", "_truncate('abcdefghij', 8) == ('abc \u2026 ij', True)"),
    "trace:memory/store.py:clamp_limit": (
        "clamp_limit(20) == 20", "clamp_limit(500) == 50", "clamp_limit('x') == 10"),
    "trace:memory/git_events.py:_new_side_ranges": (
        "_new_side_ranges('@@ -1,3 +1,4 @@') == [(1, 5)]", "_new_side_ranges('no hunks') == []"),
    "trace:trace_memory/obsidian.py:_text": (
        "_text('hello world', 20) == 'hello world'", "_text('a|b', 10) == 'a\u00a6b'"),
    "trace:memory/session_context.py:_apply_budgets": (
        "_apply_budgets([{'text': 'ab'}], 5, 10) == ([{'text': 'ab'}], False)",
        "_apply_budgets([{'text': 'a'}, {'text': 'b'}], 1, 10) == ([{'text': 'a'}], True)"),
    "click:src/click/utils.py:_posixify": (
        "_posixify('Hello World') == 'hello-world'", "_posixify('x-y') == 'x-y'"),
    "click:src/click/utils.py:make_str": (
        "make_str(b'abc') == 'abc'", "make_str(1.5) == '1.5'"),
    "click:src/click/parser.py:_normalize_opt": (
        "_normalize_opt('--Foo', None) == '--Foo'", "_normalize_opt('-Q', C(str.lower)) == '-q'"),
    "click:src/click/formatting.py:iter_rows": (
        "list(iter_rows([('p',)], 3)) == [('p', '', '')]", "list(iter_rows([('m', 'n', 'o')], 3)) == [('m', 'n', 'o')]"),
    "click:src/click/formatting.py:join_options": (
        "join_options(['-v']) == ('-v', False)", "join_options(['--verbose', '-v']) == ('-v, --verbose', False)"),
    "click:src/click/_compat.py:get_best_encoding": (
        "get_best_encoding(S('utf-16')) == 'utf-16'", "get_best_encoding(S('US-ASCII')) == 'utf-8'"),
    "click:src/click/_compat.py:_is_binary_reader": (
        "not _is_binary_reader(S('text'))", "_is_binary_reader(S(b'data'))"),
    "click:src/click/_compat.py:isatty": (
        "isatty(S(True)) is True", "isatty(SimpleNamespace()) is False"),
    "packaging:src/packaging/metadata.py:_parse_keywords": (
        "_parse_keywords('a, b') == ['a', 'b']", "_parse_keywords('x') == ['x']"),
    "packaging:src/packaging/metadata.py:_parse_project_urls": (
        "_parse_project_urls(['Home, https://e.org']) == {'Home': 'https://e.org'}",
        "raises(KeyError, _parse_project_urls, ['A, x', 'A, y'])"),
    "packaging:src/packaging/pylock.py:is_valid_pylock_path": (
        "is_valid_pylock_path(Path('pylock.dev.toml'))", "not is_valid_pylock_path(Path('poetry.lock'))"),
    "packaging:src/packaging/version.py:_validate_epoch": (
        "_validate_epoch(3) == 3", "raises(InvalidVersion, _validate_epoch, 'x')"),
    "packaging:src/packaging/version.py:_validate_release": (
        "_validate_release((1, 2, 3)) == (1, 2, 3)", "raises(InvalidVersion, _validate_release, ())"),
    "packaging:src/packaging/tags.py:_normalize_string": (
        "_normalize_string('abc') == 'abc'", "_normalize_string('x.y') == 'x_y'"),
    "packaging:src/packaging/specifiers.py:_version_join": (
        "_version_join(['1', '2', '0']) == '1!2.0'", "_version_join(['3', '1']) == '3!1'"),
    "packaging:src/packaging/version.py:_validate_post": (
        "_validate_post(3) == ('post', 3)", "raises(InvalidVersion, _validate_post, 'a')"),
}

CHECKS = {
    "trace:trace_memory/native.py:_identifier": (
        "_identifier('b' * 120) is None", "_identifier(' ' * 300 + 'x') == 'x'"),
    "trace:memory/task_library.py:is_commit": (
        "not is_commit('ABCDEF' + 'a' * 34)", "not is_commit('0' * 40)"),
    "trace:memory/task_library.py:is_git_path": (
        "not is_git_path('docs/ readme.md')", "not is_git_path('docs /readme.md')", "is_git_path('my docs/read me.md')"),
    "trace:memory/session_context.py:_truncate": (
        "_truncate('abcdefgh', 3) == ('ab\u2026', True)",
        "len(_truncate('abcdefgh', 5)[0]) == 5 and _truncate('abcdefgh', 5)[0].endswith('\u2026')"),
    "trace:memory/store.py:clamp_limit": (
        "clamp_limit(2.5) == DEFAULT_LIMIT", "clamp_limit(30.0) == 30"),
    "trace:memory/git_events.py:_new_side_ranges": (
        "_new_side_ranges('@@ -5,1 +6,0 @@') == []", r"_new_side_ranges('@@ -1 +2,0 @@\n@@ -3 +3 @@') == [(3, 4)]"),
    "trace:trace_memory/obsidian.py:_text": (
        "len(_text('abcdefgh', 5)) == 5", "_text('abc', -1) == ''"),
    "click:src/click/utils.py:_posixify": (
        "_posixify('my_tool') == 'my-tool'", "_posixify('__Run  It__') == 'run-it'"),
    "click:src/click/utils.py:make_str": (
        "make_str(bytes([233])) == '\u00e9'", "make_str(bytes([128, 65])) == chr(128) + 'A'"),
    "click:src/click/parser.py:_normalize_opt": (
        "_normalize_opt('-V', C({}.get)) == '-V'", "_normalize_opt('--x', C({'x': ''}.get)) == '--'"),
    "click:src/click/formatting.py:iter_rows": (
        "list(iter_rows([('a', 'b', 'c', 'd')], 2)) == [('a', 'b')]", "list(iter_rows([('a', 'b')], -3)) == [()]"),
    "click:src/click/formatting.py:join_options": (
        "join_options(['-x', '-x']) == ('-x', False)", "join_options(['--beta', '--Alpha']) == ('--Alpha, --beta', False)",
        "join_options(['//opt'])[1] is True"),
    "click:src/click/_compat.py:get_best_encoding": (
        "get_best_encoding(S('bogus-encoding-x')) == 'utf-8'", "get_best_encoding(S('Latin-1')) == 'Latin-1'"),
    "click:src/click/_compat.py:_is_binary_reader": (
        "_is_binary_reader(S(bytearray(b'x')))", "_is_binary_reader(S(memoryview(b'x')))"),
    "packaging:src/packaging/metadata.py:_parse_keywords": (
        "_parse_keywords('a,,b') == ['a', 'b']", "_parse_keywords('Py, py, PY') == ['Py']"),
    "packaging:src/packaging/metadata.py:_parse_project_urls": (
        "raises(KeyError, _parse_project_urls, ['Source, a', 'source, b'])",
        "_parse_project_urls(['Src, a', 'Bug, b']) == {'Src': 'a', 'Bug': 'b'}"),
    "packaging:src/packaging/pylock.py:is_valid_pylock_path": (
        "not is_valid_pylock_path(Path('pylock.Dev.toml'))", "not is_valid_pylock_path(Path('pylock.' + 'a' * 17 + '.toml'))",
        "not is_valid_pylock_path(Path('pylock.9x.toml'))"),
    "packaging:src/packaging/version.py:_validate_epoch": (
        "raises(InvalidVersion, _validate_epoch, True)", "raises(InvalidVersion, _validate_epoch, 0.0)", "_validate_epoch(0) == 0"),
    "packaging:src/packaging/version.py:_validate_release": (
        "_validate_release([1, 4]) == (1, 4)", "raises(InvalidVersion, _validate_release, (1, False))"),
    "packaging:src/packaging/tags.py:_normalize_string": (
        "_normalize_string('Linux_X86') == 'linux_x86'", "_normalize_string('a..b') == 'a_b'"),
    "packaging:src/packaging/specifiers.py:_version_join": (
        "_version_join(['0', '4', '2']) == '4.2'", "_version_join(['', '5']) == '5'"),
}

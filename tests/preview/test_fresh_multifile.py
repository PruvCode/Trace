import json

from research.preview import fresh_multifile as fixture


def test_reply_requires_both_functions_and_preserves_signatures():
    original = ("def width(value):\n    return len(value)", "def table(row):\n    return tuple(row)")
    symbols = ("width", "table")
    changes = dict(zip(symbols, original))
    assert fixture.parse_reply(json.dumps(changes), symbols, original) == original
    assert fixture.parse_reply(json.dumps({"width": original[0]}), symbols, original) is None
    changes["table"] = "def table():\n    return ()"
    assert fixture.parse_reply(json.dumps(changes), symbols, original) is None


def test_patch_edits_only_the_selected_top_level_function():
    case = fixture.CASES[0]
    paths = fixture.file_paths(case)
    source = {
        paths[0]: "def term_len(x: str) -> int:\n    return len(x)\n\nSENTINEL = 'kept'\n",
        paths[1]: "def measure_table(rows):\n    return ()\n",
    }
    change = "def term_len(x: str) -> int:\n    return len(x.rstrip(' '))"
    patched = fixture.replacements(source, case, (change, None))
    assert patched[paths[0]] == change + "\n\nSENTINEL = 'kept'\n"
    assert patched[paths[1]] == source[paths[1]]


def test_qualification_retry_reports_the_failed_approved_example(monkeypatch):
    case = fixture.QUALIFY[0]
    first = ("def limit():\n    return 12", "def keep(value):\n    return value <= 12")
    fixed = ("def limit():\n    return 12", "def keep(value):\n    return 0 <= value <= limit()")
    prompts = []

    def ask(model, seed, prompt, symbols, originals):
        prompts.append(prompt)
        return (first if len(prompts) == 1 else fixed), [1, 1, 1]

    monkeypatch.setattr(fixture, "ask", ask)
    monkeypatch.setattr(fixture, "evaluate", lambda grader, source, case: (
        "0 <= value" in source[1] or all("keep(-1)" not in call for call, _ in case.checks)
    ))
    result = fixture.qualify_sample(None, case, "model")
    assert result["passed"] and result["usage"] == [2, 2, 2]
    assert prompts[1].split("Failed approved examples:\n")[1].splitlines()[0] == "handler.keep(-1) == False"

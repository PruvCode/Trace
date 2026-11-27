"""Editor mechanics shared by the maintained-notes coding trial (v2)."""

import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from research.preview.editor import extract_function, retry_feedback, render_excerpt  # noqa: E402

ORIGINAL = "def f(value):\n    return value\n"


def test_strict_extraction_still_accepts_a_single_function():
    candidate, defect = extract_function("```python\ndef f(value):\n    return value\n```", "f", ORIGINAL)
    assert candidate == "def f(value):\n    return value" and defect is None


def test_fallback_extracts_the_function_from_supporting_statements():
    reply = ("```python\nimport re\nLIMIT = 8\nclass Bad(Exception):\n    pass\n\n"
             "def f(value):\n    return value or LIMIT\n```")
    candidate, defect = extract_function(reply, "f", ORIGINAL)
    assert defect == "extra-statements"
    assert candidate == "def f(value):\n    return value or LIMIT"


def test_signature_drift_and_wrong_names_stay_rejected():
    drifted, defect = extract_function("```python\ndef f(value, extra=None):\n    return value\n```", "f", ORIGINAL)
    assert drifted is None and defect == "signature"
    renamed, defect = extract_function("```python\ndef g(value):\n    return value\n```", "f", ORIGINAL)
    assert renamed is None and defect == "no-function"
    prose, defect = extract_function("I cannot do that.", "f", ORIGINAL)
    assert prose is None and defect == "no-block"


def test_retry_feedback_never_mentions_graded_behavior():
    for defect in ("no-block", "no-function", "signature", "extra-statements"):
        message = retry_feedback(defect, "")
        assert "check" not in message.lower() or "checks" not in message.lower()
        assert defect != "signature" or "signature" in message


@pytest.mark.parametrize("reply", [None, [], {"done_reason": "stop"}, {"message": {"content": []}}])
def test_notes_trial_records_invalid_provider_response(monkeypatch, reply):
    from research.preview import notes_trial

    monkeypatch.setattr(notes_trial, "request", lambda *args: reply)
    row = notes_trial.proposal(None, notes_trial.CASES[0], ORIGINAL, "stub", 1, "notes", "",
                               fenced_editor=True, repair=True)
    assert row["reason"] == "invalid-provider-response"
    assert row["attempts"] == 1 and not row["passed"] and row["candidate"] is None


def test_notes_trial_records_invalid_provider_json(monkeypatch):
    from research.preview import notes_trial

    def request(*args):
        raise json.JSONDecodeError("invalid JSON", "", 0)

    monkeypatch.setattr(notes_trial, "request", request)
    row = notes_trial.proposal(None, notes_trial.CASES[0], ORIGINAL, "stub", 1, "notes", "")
    assert row["reason"] == "invalid-provider-response" and not row["passed"]


def test_notes_trial_retries_with_assistant_text_only(monkeypatch):
    from research.preview import notes_trial

    case = SimpleNamespace(repo="stub", path="module.py", symbol="f", request="Return the input.", prelude="")
    candidate = ORIGINAL.strip()
    replies = iter([
        {"message": {"content": "Please clarify."}, "prompt_eval_count": 10, "eval_count": [], "done_reason": "stop"},
        {"message": {"content": f"```python\n{candidate}\n```"}, "prompt_eval_count": 20,
         "eval_count": 3, "done_reason": "stop"},
    ])
    prompts = []

    def request(path, body):
        prompts.append(body["messages"][0]["content"])
        return next(replies)

    monkeypatch.setattr(notes_trial, "request", request)
    monkeypatch.setattr(notes_trial, "grade", lambda grader, case, edit: (edit == candidate, True))
    row = notes_trial.proposal(None, case, ORIGINAL, "stub", 1, "notes", "", fenced_editor=True, repair=True)
    assert row["passed"] and row["candidate"] == candidate and row["attempts"] == 2
    assert row["input_tokens"] == 30 and row["output_tokens"] == 3
    assert prompts[1] == prompts[0] + "\n\nPrevious reply:\nPlease clarify.\n\n" + retry_feedback("no-block", "")


def test_notes_trial_does_not_retry_truncated_output(monkeypatch):
    from research.preview import notes_trial

    replies = iter([{"done_reason": "length", "eval_count": 400}])
    monkeypatch.setattr(notes_trial, "request", lambda *args: next(replies))
    row = notes_trial.proposal(None, notes_trial.CASES[0], ORIGINAL, "stub", 1, "notes", "",
                               fenced_editor=True, repair=True)
    assert row["reason"] == "length" and row["attempts"] == 1 and row["candidate"] is None


def test_excerpt_renders_only_verified_evidenced_decisions():
    verified = {
        "kind": "finding", "text": "Use rule A in helper.", "role": "decision (verified)",
        "evidence": [{"ref": "ev_1", "type": "transcript"}],
        "code": {"path": "pkg/mod.py", "name": "helper"},
        "code_targets": [{"path": "pkg/mod.py", "line": 3, "symbol": "helper"}],
    }
    uncertain = dict(verified, role="decision", uncertain=True, text="Maybe rule B.")
    routine = {"kind": "message", "role": "assistant", "text": "Routine diagnostics pending.", "session": "s1"}
    excerpt = render_excerpt([routine, uncertain, verified])
    assert "rule A" in excerpt and "ev_1" in excerpt and "pkg/mod.py" in excerpt
    assert "rule B" not in excerpt and "Routine diagnostics" not in excerpt


def test_excerpt_is_empty_without_verified_history():
    assert render_excerpt([
        {"kind": "message", "role": "assistant", "text": "hello", "session": "s1"},
        {"kind": "finding", "text": "no evidence", "role": "decision (verified)", "evidence": []},
    ]) == ""


def test_development_replay_cannot_count_as_a_qualified_matrix(monkeypatch):
    from research.preview import notes_trial

    case = notes_trial.CASES[0]
    monkeypatch.setattr(notes_trial, "CASES", (case,))
    monkeypatch.setattr(notes_trial, "MODELS", {"local-model": "digest"})
    monkeypatch.setattr(notes_trial, "SEEDS", (1,))
    rows = [
        dict(type="scored", task=notes_trial.key(case), model="local-model", seed=1,
             arm=arm, passed=True, candidate="edit", regression=False, elapsed_ms=1, input_tokens=1)
        for arm in notes_trial.ARMS
    ]
    assert notes_trial.summarize(rows)["complete_matrix"]
    summary = notes_trial.summarize([dict(row, dev_probe=True) for row in rows])
    assert summary["dev_probe"] and not summary["complete_matrix"]
    with pytest.raises(ValueError, match="must stay separate"):
        notes_trial.summarize([dict(rows[0], dev_probe=True), *rows[1:]])

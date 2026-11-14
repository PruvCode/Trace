"""Editor mechanics shared by the maintained-notes coding trial (v2)."""

from pathlib import Path
import sys

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

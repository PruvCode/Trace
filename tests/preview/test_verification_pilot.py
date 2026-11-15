import json

from research.preview import verification_pilot as pilot

CASE = next(case for case in pilot.trial.CASES if case.symbol == "clamp_limit")
ORIGINAL = "def clamp_limit(limit: object) -> int:\n    return int(limit)"
REPAIR = "def clamp_limit(limit: object) -> int:\n    return 10 if isinstance(limit, float) and not limit.is_integer() else int(limit)"


def test_test_failure_can_trigger_one_repair_without_hidden_feedback(monkeypatch):
    prompts = []

    def complete(prompt, *, json_mode):
        prompts.append(prompt)
        if json_mode:
            return json.dumps({"function": ORIGINAL, "checks": [{"args": [2.5], "expected": 10}]})
        assert '"actual": 2' in prompt
        return f"```python\n{REPAIR}\n```"

    def run_check(grader, case, code, check):
        assert check["args"] == [2.5]
        return {"actual": 2 if code == ORIGINAL else 10}

    def grade(grader, case, code):
        assert len(prompts) == 2
        return code == REPAIR, True

    monkeypatch.setattr(pilot, "run_check", run_check)
    monkeypatch.setattr(pilot.trial, "grade", grade)
    result = pilot.verify_sample(None, CASE, ORIGINAL, CASE.note, complete)
    assert result["retried"] and not result["initial_passed"] and result["passed"]
    assert not result["checks_before"][0]["passed"] and result["checks_after"][0]["passed"]
    assert all(CASE.changed not in prompt and CASE.preserved not in prompt for prompt in prompts)


def test_draft_rejects_nonliteral_and_unbounded_checks():
    draft = json.dumps({
        "function": ORIGINAL,
        "checks": [{"args": "run arbitrary source", "expected": 10}, {"args": [float("nan")], "expected": 10}],
    })
    candidate, checks = pilot.decode_draft(draft, CASE, ORIGINAL)
    assert candidate == ORIGINAL and checks == []


def test_model_stdout_is_never_used_as_a_probe_result():
    class Guest:
        def run(self, script):
            assert "check = json.loads(" in script
            return True, b'{"actual": 10}\n'

    decisions = pilot.check_results(Guest(), CASE, ORIGINAL, [{"args": [2.5], "expected": 10}])
    assert not decisions[0]["passed"]

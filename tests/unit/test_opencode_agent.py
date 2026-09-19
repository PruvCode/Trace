"""Unit tests for the OpenCode adapter (no model, no network, no binary)."""

import json

from agent.opencode_agent import (
    OpenCodeAgent,
    build_opencode_config,
    canonical_tool_name,
    parse_events,
)

SAMPLE_STREAM = [
    '{"type":"step_start","sessionID":"s1"}',
    '{"type":"tool_use","part":{"tool":"read","state":{"status":"completed",'
    '"input":{"filePath":"a.py"},"time":{"start":1000,"end":1010}}}}',
    '{"type":"step_finish","part":{"reason":"tool-calls","tokens":'
    '{"total":100,"input":90,"output":10,"reasoning":0}}}',
    '{"type":"tool_use","part":{"tool":"trace_memory_find_definition",'
    '"state":{"status":"completed","input":{"name":"f"},'
    '"time":{"start":2000,"end":2050}}}}',
    '{"type":"tool_use","part":{"tool":"trace_memory_search_symbols",'
    '"state":{"status":"error","input":{},"time":{"start":3000,"end":3005}}}}',
    '{"type":"step_finish","part":{"reason":"stop","tokens":'
    '{"total":50,"input":40,"output":10,"reasoning":0}}}',
    "not json at all",
]


def test_canonical_tool_name_strips_mcp_prefix():
    assert canonical_tool_name("trace_memory_find_definition") == "find_definition"
    assert canonical_tool_name("trace_memory_search_events") == "search_events"
    assert canonical_tool_name("read") == "read"
    assert canonical_tool_name("trace_memory_bogus") == "trace_memory_bogus"


def test_parse_events_counts_and_usage():
    parsed = parse_events(SAMPLE_STREAM)
    assert parsed["turns"] == 1
    assert parsed["tool_calls"] == 3
    assert parsed["input_tokens"] == 130
    assert parsed["output_tokens"] == 20
    assert parsed["skipped_lines"] == 1
    assert parsed["last_reason"] == "stop"
    names = [r.name for r in parsed["tool_log"]]
    assert names == ["read", "find_definition", "search_symbols"]
    assert [r.ok for r in parsed["tool_log"]] == [True, True, False]
    assert parsed["tool_log"][2].error == "opencode_tool_error"


def test_parse_events_no_usage_stays_unknown():
    parsed = parse_events(['{"type":"step_start"}'])
    assert parsed["input_tokens"] is None
    assert parsed["output_tokens"] is None


def test_build_config_baseline_has_no_mcp(tmp_path):
    # Agent adapter stays permission-only; the memory seam lives in the runner.
    _ = tmp_path
    config = build_opencode_config()
    assert "mcp" not in config
    assert config["permission"]["bash"] == "deny"
    assert config["permission"]["edit"] == "allow"


def test_build_config_reference_points_at_run_db(tmp_path):
    # Reference MCP injection is runner-owned (benchmark seam), using the
    # backend's stdio command so OpenCode reaches the same per-run database.
    import sys

    from benchmark.runner import build_opencode_config_with_memory

    db = tmp_path / "ws" / ".agent-memory" / "memory.db"
    repo = tmp_path / "ws"
    mcp_command = [
        sys.executable,
        "-m",
        "memory.mcp_server",
        "--db",
        db.as_posix(),
        "--repo",
        repo.as_posix(),
    ]
    config = build_opencode_config_with_memory(mcp_command)
    server = config["mcp"]["trace_memory"]
    assert server["type"] == "local"
    assert server["enabled"] is True
    assert "--db" in server["command"] and db.as_posix() in server["command"]
    assert json.dumps(config)  # serializable into opencode.json


def test_resolve_binary_prefers_real_executable(tmp_path, monkeypatch):
    import os

    fake_exe = tmp_path / "opencode.exe"
    fake_exe.write_text("fake", encoding="utf-8")
    monkeypatch.setenv("PATH", str(tmp_path), prepend=os.pathsep)
    agent = OpenCodeAgent(model="opencode/mimo-v2.5-free", opencode_bin="no-such-bin")
    resolved = agent._resolve_binary()
    assert resolved.endswith("opencode.exe")


def test_resolve_binary_missing_is_launch_error(tmp_path, monkeypatch):
    import os

    monkeypatch.setenv("PATH", str(tmp_path))
    agent = OpenCodeAgent(model="opencode/mimo-v2.5-free", opencode_bin="no-such-bin")
    try:
        agent._resolve_binary()
    except FileNotFoundError as exc:
        assert "opencode_not_installed" in str(exc)
    else:
        raise AssertionError("expected FileNotFoundError")


def test_create_agent_selects_opencode(repo_root):
    from benchmark import loader as loader_mod
    from benchmark import runner as runner_mod

    config = loader_mod.load_config(repo_root / "configs" / "baseline_opencode.yaml")
    agent = runner_mod._create_agent(config)
    assert isinstance(agent, OpenCodeAgent)


def test_opencode_configs_fair(repo_root):
    """Same controls for the OpenCode pair; memory is the only difference."""
    from benchmark import fairness as fairness_mod
    from benchmark import loader as loader_mod

    task = loader_mod.load_task(
        repo_root / "tasks" / "A_control" / "task_01_timeout_fix", repo_root
    )
    baseline = loader_mod.load_config(repo_root / "configs" / "baseline_opencode.yaml")
    reference = loader_mod.load_config(
        repo_root / "configs" / "reference_opencode.yaml"
    )
    base_spec = fairness_mod.resolve_run_spec(task, baseline, str(repo_root))
    ref_spec = fairness_mod.resolve_run_spec(task, reference, str(repo_root))
    assert "fair" in fairness_mod.assert_fair(base_spec, ref_spec)
    assert baseline["agent"] == reference["agent"] == "opencode"
    assert baseline["model"] == reference["model"] == "opencode/mimo-v2.5-free"
    assert baseline["backend"] is None
    assert reference["backend"] == "reference"

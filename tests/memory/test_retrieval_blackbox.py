"""Real OpenCode black-box test for automatic cross-session retrieval (5.2).

Proves the full automatic chain with the ACTUAL OpenCode executable:

    OpenCode session 1 (marker established, TRACE captures)
        ↓
    OpenCode session 2, launched normally, no TRACE commands
        ↓
    TRACE plugin retrieves prior context automatically
        ↓
    prior marker becomes model-visible, model uses it

Airtightness controls:
- Session 1 establishes the marker WITHOUT writing it to disk, and the
  test asserts the marker file does not exist before session 2, so the
  model cannot recover it from the working tree.
- The TRACE MCP server is removed from opencode.json for this test, so
  agent-mediated MCP retrieval is impossible; the plugin path is the
  only memory channel (asserted).
- The test never invokes trace context / transcript search / capture.
"""

import json
import time
from pathlib import Path

import pytest

from trace_memory import project as project_mod
from tests.memory.test_441_environment import (
    _get_session_from_opencode_db,
    _query_trace_db,
    _rmtree_with_retry,
    _run_opencode_direct,
    _stop_service_and_wait,
)

MARKER = "MANGO-7"


def _safe(text: str, limit: int = 500) -> str:
    """ASCII-safe snippet for failure messages (Windows console safe)."""
    return str(text or "")[:limit].encode("ascii", "replace").decode("ascii")


def _wait_for(predicate, timeout: float = 40.0, interval: float = 2.0) -> bool:
    """Poll predicate until true or timeout. Returns final outcome."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


def _retrieval_log_entries(project: Path) -> list[dict]:
    log_path = project / ".agent-memory" / "retrieval.log"
    if not log_path.exists():
        return []
    entries = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue
    return entries


@pytest.fixture()
def continuity_project(monkeypatch):
    """TRACE project with plugin installed and MCP stripped (plugin-only)."""
    import tempfile

    monkeypatch.setenv("OPENAI_API_KEY", "test")
    tmp = tempfile.mkdtemp()
    try:
        project = Path(tmp) / "project"
        project.mkdir()
        (project / "main.py").write_text("def hello():\n    return 'world'\n", encoding="utf-8")
        project_mod.init_project(project)

        from memory.opencode_transparent import create_opencode_transparent_adapter

        adapter = create_opencode_transparent_adapter()
        setup = adapter.setup_integration(project)
        assert setup["monitoring"]["status"] == "started"
        assert setup["plugin"]["status"] in ("created", "updated")

        # Strip the MCP server: the plugin path must be the ONLY memory
        # channel, otherwise agent-mediated MCP retrieval could explain
        # the result instead of automatic injection.
        config_path = project / "opencode.json"
        if config_path.exists():
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config.pop("mcp", None)
            config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
            assert "mcp" not in json.loads(config_path.read_text(encoding="utf-8"))

        yield project, adapter
    finally:
        try:
            from memory.opencode_service import get_service

            service = get_service(project)
            if service.is_running():
                service.stop()
                deadline = time.time() + 20.0
                while service.is_running() and time.time() < deadline:
                    time.sleep(0.5)
                time.sleep(2)
        except Exception as e:
            print(f"[continuity teardown] best-effort service stop: {e}")
        _rmtree_with_retry(Path(tmp))


class TestAutomaticContinuity:
    """5.2 black-box: prior memory is automatically model-visible."""

    def test_cross_session_marker_recall(self, continuity_project):
        project, adapter = continuity_project

        # SESSION 1: establish the marker in conversation only.
        prompt1 = (
            f"Our project code word is {MARKER}. "
            "Acknowledge it briefly without creating any files."
        )
        result1 = _run_opencode_direct(project, prompt1, timeout=180)
        assert result1.returncode == 0, (
            f"Session 1 failed: {_safe(result1.stdout)} {_safe(result1.stderr)}"
        )
        session_id_1 = _get_session_from_opencode_db(project)
        assert session_id_1 is not None

        # Wait until TRACE has captured the marker (bounded poll).
        captured = _wait_for(
            lambda: any(
                MARKER in row[0]
                for row in _query_trace_db(
                    project,
                    "SELECT content FROM transcript_messages WHERE session_id = ?",
                    (session_id_1,),
                )
            ),
            timeout=40.0,
        )
        assert captured, "TRACE did not capture the session 1 marker"

        # Contamination guard: marker must not exist on disk (session 1
        # creates no files, so any marker-bearing file could only come
        # from session 2 recalling injected context).
        assert not any(
            MARKER in p.read_text(encoding="utf-8", errors="ignore")
            for p in project.glob("*.txt")
        ), "marker leaked to disk; proof would be contaminated"

        # SESSION 2: normal launch, no marker in prompt, no TRACE commands.
        # The filename is unguessable and exists nowhere on disk: the only
        # way to produce it is recalled TRACE MEMORY context. The write
        # tool forces a multi-step session, which meaningfully exercises
        # the once-per-session injection guard.
        expected_file = project / f"{MARKER}.txt"
        prompt2 = (
            "Create a file named after our project code word from our "
            "previous conversation (with a .txt extension) containing the "
            "word done."
        )
        result2 = _run_opencode_direct(project, prompt2, timeout=180)
        assert result2.returncode == 0, (
            f"Session 2 failed: {_safe(result2.stdout)} {_safe(result2.stderr)}"
        )
        session_id_2 = _get_session_from_opencode_db(project)
        assert session_id_2 is not None
        assert session_id_2 != session_id_1

        # A. Deterministic retrieval proof (no model involved): recompute
        # exactly what the plugin assembled for session 2 and require the
        # marker-bearing block. This fails only on a real retrieval bug.
        from memory import session_context as session_context_mod

        recomputed = session_context_mod.get_session_start_context(
            project_mod.db_path(project), exclude_session_id=session_id_2
        )
        assert MARKER in recomputed["text"], (
            "retrieval layer did not produce marker-bearing context for session 2"
        )
        assert recomputed["text"].startswith("<TRACE MEMORY>")

        # B. Mechanism proof from the plugin sidecar (not MCP, not manual).
        entries = _retrieval_log_entries(project)
        session2_injections = [
            e for e in entries
            if e.get("sessionID") == session_id_2 and e.get("injected") is True
        ]
        assert len(session2_injections) == 1, (
            f"expected exactly one injection for session 2, got {len(session2_injections)}: "
            f"{json.dumps(entries)[:1000]}"
        )
        assert session2_injections[0].get("items", 0) >= 1
        assert session2_injections[0].get("error") in (None, "")
        print(f"injection overhead: {session2_injections[0].get('latencyMs')}ms")

        # C. Model-use proof: the expected filename is unguessable, appears
        # nowhere on disk, and never in prompt 2. Creating it proves the
        # model recalled the marker from injected context. (Reply text is
        # recorded for diagnosis; free-tier phrasing varies, the artifact
        # is exact.)
        marker_in_reply = MARKER in result2.stdout
        print(f"marker_file_exists={expected_file.exists()} marker_in_reply={marker_in_reply}")
        assert expected_file.exists(), (
            "model did not create the marker-named file; automatic retrieval failed"
        )
        content = expected_file.read_text(encoding="utf-8", errors="replace")
        assert "done" in content.lower(), f"unexpected file content: {content[:200]}"

        _stop_service_and_wait(adapter, project)


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])

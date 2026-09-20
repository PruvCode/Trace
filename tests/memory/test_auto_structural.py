"""Tests for automatic structural memory lifecycle (Step 3).

Black-box tests for:
1. Fresh repository: no TRACE DB -> structural DB appears automatically
2. Initial structural content: symbols/definitions/relationships exist
3. Unchanged repository: second check does not rebuild unnecessarily
4. New file: structural memory reflects it automatically
5. Modified file: structural memory reflects changes
6. Deleted file: removed symbols disappear
7. Branch/tree change: stale state is detected
8. Restart: state persists correctly
9. Idempotency: repeated checks do not duplicate structural rows
10. Project isolation: two repositories never share structural memory
11. Non-Git repository: fallback behaves safely
"""

import tempfile
import shutil
from pathlib import Path
import subprocess

import pytest

from memory import store as store_mod
from memory import structural as structural_mod
from trace_memory import project as project_mod


class TestFreshRepositoryAutoInit:
    """Test 1: Fresh repository - structural memory created automatically."""

    def test_fresh_repo_creates_structural_memory(self, tmp_path):
        """When no .agent-memory exists, ensure_structural_memory creates it."""
        project = tmp_path / "myproject"
        project.mkdir()
        (project / "main.py").write_text("def hello():\n    return 'world'\n", encoding="utf-8")

        # No TRACE DB exists yet
        assert not project_mod.is_initialized(project)

        # This should auto-create DB and index
        result = structural_mod.ensure_structural_memory(project)

        # DB should now exist
        assert project_mod.is_initialized(project)
        assert result.get("files", 0) >= 1
        assert result.get("symbols", 0) >= 1

    def test_fresh_repo_has_symbols_and_relationships(self, tmp_path):
        """Fresh repo has symbols, definitions, and relationships."""
        project = tmp_path / "myproject"
        project.mkdir()
        (project / "auth.py").write_text(
            "def login(user):\n    return validate(user)\n\ndef validate(u):\n    return True\n",
            encoding="utf-8",
        )

        structural_mod.ensure_structural_memory(project)

        conn = store_mod.connect(project_mod.db_path(project))
        try:
            symbols = conn.execute("SELECT name, kind FROM symbols").fetchall()
            names = {row["name"] for row in symbols}
            assert "login" in names
            assert "validate" in names

            rels = conn.execute("SELECT src, rel, dst FROM relationships").fetchall()
            # login calls validate
            calls = [(r["src"], r["dst"]) for r in rels if r["rel"] == "calls"]
            assert ("login", "validate") in calls
        finally:
            conn.close()


class TestUnchangedRepositoryNoRebuild:
    """Test 3: Unchanged repository - no unnecessary rebuild."""

    def test_unchanged_repo_returns_fresh_status(self, tmp_path):
        """Second call on unchanged repo returns 'fresh' without reindexing."""
        project = tmp_path / "myproject"
        project.mkdir()
        (project / "main.py").write_text("def hello():\n    return 'world'\n", encoding="utf-8")

        # First call - creates and indexes
        result1 = structural_mod.ensure_structural_memory(project)
        assert result1.get("status") in ("refreshed", None) or "files" in result1

        # Second call - should detect fresh
        result2 = structural_mod.ensure_structural_memory(project)
        assert result2.get("status") == "fresh"

    def test_repeated_checks_no_duplicate_rows(self, tmp_path):
        """Repeated ensure_structural_memory calls don't duplicate rows."""
        project = tmp_path / "myproject"
        project.mkdir()
        (project / "main.py").write_text("def hello():\n    return 'world'\n", encoding="utf-8")

        for _ in range(3):
            structural_mod.ensure_structural_memory(project)

        conn = store_mod.connect(project_mod.db_path(project))
        try:
            count = conn.execute("SELECT COUNT(*) FROM symbols WHERE name = 'hello'").fetchone()[0]
            assert count == 1, f"Expected 1 hello symbol, got {count}"
        finally:
            conn.close()


class TestNewFileAutoRefresh:
    """Test 4: New file - structural memory reflects it automatically."""

    def test_new_file_detected_and_indexed(self, tmp_path):
        """Adding a new Python file triggers automatic reindex."""
        project = tmp_path / "myproject"
        project.mkdir()
        (project / "main.py").write_text("def hello():\n    return 'world'\n", encoding="utf-8")

        structural_mod.ensure_structural_memory(project)

        # Add new file
        (project / "new_module.py").write_text(
            "def new_func():\n    return 42\n",
            encoding="utf-8",
        )

        # Should auto-refresh and include new file
        result = structural_mod.ensure_structural_memory(project)
        assert result.get("status") == "refreshed"

        conn = store_mod.connect(project_mod.db_path(project))
        try:
            symbols = conn.execute("SELECT name FROM symbols").fetchall()
            names = {row["name"] for row in symbols}
            assert "new_func" in names
        finally:
            conn.close()


class TestModifiedFileAutoRefresh:
    """Test 5: Modified file - structural memory reflects changes."""

    def test_modified_function_signature_updated(self, tmp_path):
        """Changing a function signature updates structural memory."""
        project = tmp_path / "myproject"
        project.mkdir()
        (project / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n",
            encoding="utf-8",
        )

        structural_mod.ensure_structural_memory(project)

        # Modify the function
        (project / "calc.py").write_text(
            "def add(a, b, c=0):\n    return a + b + c\n",
            encoding="utf-8",
        )

        result = structural_mod.ensure_structural_memory(project)
        assert result.get("status") == "refreshed"

        conn = store_mod.connect(project_mod.db_path(project))
        try:
            sig = conn.execute("SELECT signature FROM symbols WHERE name = 'add'").fetchone()
            assert "c=0" in sig["signature"]
        finally:
            conn.close()


class TestDeletedFileAutoRefresh:
    """Test 6: Deleted file - removed symbols disappear."""

    def test_deleted_file_symbols_removed(self, tmp_path):
        """Deleting a file removes its symbols from structural memory."""
        project = tmp_path / "myproject"
        project.mkdir()
        (project / "to_delete.py").write_text("def obsolete():\n    pass\n", encoding="utf-8")
        (project / "keeper.py").write_text("def keep():\n    pass\n", encoding="utf-8")

        structural_mod.ensure_structural_memory(project)

        # Delete the file
        (project / "to_delete.py").unlink()

        result = structural_mod.ensure_structural_memory(project)
        assert result.get("status") == "refreshed"

        conn = store_mod.connect(project_mod.db_path(project))
        try:
            symbols = conn.execute("SELECT name FROM symbols").fetchall()
            names = {row["name"] for row in symbols}
            assert "obsolete" not in names
            assert "keep" in names
        finally:
            conn.close()


class TestGitTreeChangeDetection:
    """Test 7: Git tree/branch changes - stale state detected."""

    def test_git_commit_changes_detected(self, tmp_path):
        """New git commit triggers structural refresh."""
        project = tmp_path / "myproject"
        project.mkdir()
        (project / "main.py").write_text("def v1():\n    return 1\n", encoding="utf-8")

        # Initialize git
        subprocess.run(["git", "init", "-b", "main"], cwd=project, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test"], cwd=project, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=project, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=project, capture_output=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=project, capture_output=True)

        structural_mod.ensure_structural_memory(project)

        # Make a change and commit
        (project / "main.py").write_text("def v2():\n    return 2\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=project, capture_output=True)
        subprocess.run(["git", "commit", "-m", "update"], cwd=project, capture_output=True)

        # Should detect git tree change and refresh
        result = structural_mod.ensure_structural_memory(project)
        assert result.get("status") == "refreshed"

        conn = store_mod.connect(project_mod.db_path(project))
        try:
            names = {r["name"] for r in conn.execute("SELECT name FROM symbols").fetchall()}
            assert "v2" in names
            assert "v1" not in names
        finally:
            conn.close()


class TestRestartPersistence:
    """Test 8: Restart - state persists correctly."""

    def test_structural_memory_persists_across_restarts(self, tmp_path):
        """Structural memory survives process restart."""
        project = tmp_path / "myproject"
        project.mkdir()
        (project / "main.py").write_text("def hello():\n    return 'world'\n", encoding="utf-8")

        structural_mod.ensure_structural_memory(project)

        # Simulate restart by creating new connection
        conn = store_mod.connect(project_mod.db_path(project))
        try:
            symbols = conn.execute("SELECT name FROM symbols WHERE name = 'hello'").fetchall()
            assert len(symbols) == 1
        finally:
            conn.close()

        # Call again - should be fresh
        result = structural_mod.ensure_structural_memory(project)
        assert result.get("status") == "fresh"


class TestProjectIsolation:
    """Test 10: Project isolation - two repos never share memory."""

    def test_two_projects_isolated(self, tmp_path):
        """Two different projects have separate structural memory."""
        project_a = tmp_path / "project_a"
        project_b = tmp_path / "project_b"
        project_a.mkdir()
        project_b.mkdir()

        (project_a / "a.py").write_text("def func_a():\n    return 'A'\n", encoding="utf-8")
        (project_b / "b.py").write_text("def func_b():\n    return 'B'\n", encoding="utf-8")

        structural_mod.ensure_structural_memory(project_a)
        structural_mod.ensure_structural_memory(project_b)

        # Check project A only has func_a
        conn_a = store_mod.connect(project_mod.db_path(project_a))
        try:
            names_a = {r["name"] for r in conn_a.execute("SELECT name FROM symbols").fetchall()}
            assert "func_a" in names_a
            assert "func_b" not in names_a
        finally:
            conn_a.close()

        # Check project B only has func_b
        conn_b = store_mod.connect(project_mod.db_path(project_b))
        try:
            names_b = {r["name"] for r in conn_b.execute("SELECT name FROM symbols").fetchall()}
            assert "func_b" in names_b
            assert "func_a" not in names_b
        finally:
            conn_b.close()


class TestNonGitRepositoryFallback:
    """Test 11: Non-Git repository - fallback behaves safely."""

    def test_non_git_repo_uses_mtime_fallback(self, tmp_path):
        """Non-git repo uses file mtimes for staleness detection."""
        project = tmp_path / "myproject"
        project.mkdir()
        (project / "main.py").write_text("def hello():\n    return 'world'\n", encoding="utf-8")
        # No git init

        structural_mod.ensure_structural_memory(project)

        # Should work without git
        conn = store_mod.connect(project_mod.db_path(project))
        try:
            symbols = conn.execute("SELECT name FROM symbols WHERE name = 'hello'").fetchall()
            assert len(symbols) == 1
        finally:
            conn.close()

        # Second call should be fresh (mtime-based)
        result = structural_mod.ensure_structural_memory(project)
        assert result.get("status") == "fresh"

    def test_non_git_repo_detects_file_change_via_mtime(self, tmp_path):
        """Non-git repo detects file changes via mtime."""
        project = tmp_path / "myproject"
        project.mkdir()
        (project / "main.py").write_text("def v1():\n    return 1\n", encoding="utf-8")

        structural_mod.ensure_structural_memory(project)

        # Modify file (mtime changes)
        import time
        time.sleep(0.01)  # Ensure mtime changes
        (project / "main.py").write_text("def v2():\n    return 2\n", encoding="utf-8")

        result = structural_mod.ensure_structural_memory(project)
        assert result.get("status") == "refreshed"

        conn = store_mod.connect(project_mod.db_path(project))
        try:
            names = {r["name"] for r in conn.execute("SELECT name FROM symbols").fetchall()}
            assert "v2" in names
            assert "v1" not in names
        finally:
            conn.close()


class TestProductWorkflow:
    """Test 3F: Product-level black-box workflow test."""

    def test_full_workflow_fresh_then_modified(self, tmp_path):
        """
        Black-box product test:
        1. Create fresh repository
        2. TRACE installed/configured (simulated by calling ensure_structural_memory)
        3. No manual trace init/index
        4. Structural memory automatically appears
        5. Modify repository
        6. Reopen/continue workflow
        7. No manual trace index
        8. Structural memory reflects new repository state
        """
        project = tmp_path / "myproject"
        project.mkdir()

        # Step 1-4: Fresh repo, no manual init, structural memory appears
        (project / "auth.py").write_text(
            "def login(user):\n    return validate(user)\n\ndef validate(u):\n    return True\n",
            encoding="utf-8",
        )

        # This is what happens automatically when agent starts
        result = structural_mod.ensure_structural_memory(project)

        assert project_mod.is_initialized(project)
        assert result.get("files", 0) >= 1
        assert result.get("symbols", 0) >= 2

        # Verify we can query structural memory
        conn = store_mod.connect(project_mod.db_path(project))
        try:
            callers = store_mod.find_callers(conn, "validate")
            assert any(c["caller"] == "login" for c in callers)
        finally:
            conn.close()

        # Step 5-8: Modify repo, continue workflow, no manual index
        (project / "auth.py").write_text(
            "def login(user):\n    return validate(user, strict=True)\n\ndef validate(u, strict=False):\n    return True\n",
            encoding="utf-8",
        )

        # This is what happens automatically on next agent interaction
        result = structural_mod.ensure_structural_memory(project)

        # Should have refreshed
        assert result.get("status") == "refreshed"

        # Verify updated structure
        conn = store_mod.connect(project_mod.db_path(project))
        try:
            sig = conn.execute(
                "SELECT signature FROM symbols WHERE name = 'validate'"
            ).fetchone()
            assert "strict=False" in sig["signature"]
        finally:
            conn.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
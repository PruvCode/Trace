"""Architecture guardrails for Phase 1.

- No memory-implementation imports anywhere (SQLite/FTS/tree-sitter/MCP/...).
- No shell=True anywhere (Windows safety + injection surface).
- Dependency direction: only runner.py may import sibling benchmark modules,
  agent code, and workspace helpers. agent/* never imports benchmark/*.
- The agent contract stays vendor-neutral (no provider SDK imports).
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

MEMORY_TOKENS = [
    "sqlite",
    "tree_sitter",
    "tree-sitter",
    "mcp",
    "fts5",
    "embedding",
    "neo4j",
    "kuzu",
    "memorybackend",
    "memory/backend",
]
# NOTE: "episodic"/"structural" are intentionally absent: they appear only as
# the plan-mandated task category labels (B_structural, C_episodic) in the
# loader's CATEGORIES set, not as implementation. The memory-package and
# backend-class assertions below cover the real implementation risk.

SCOPED_FILES = sorted(
    [p for p in (REPO_ROOT / "benchmark").rglob("*.py")]
    + [p for p in (REPO_ROOT / "agent").rglob("*.py")]
)


def test_no_memory_implementation_imports():
    violations = []
    for path in SCOPED_FILES:
        lowered = path.read_text(encoding="utf-8").lower()
        for token in MEMORY_TOKENS:
            if token in lowered:
                violations.append(f"{path.name}: {token}")
    assert violations == []


def test_no_shell_true():
    violations = []
    for path in SCOPED_FILES + sorted((REPO_ROOT / "scripts").rglob("*.py")):
        if "shell=true" in path.read_text(encoding="utf-8").lower().replace(" ", ""):
            violations.append(str(path))
    assert violations == []


def test_runner_is_sole_orchestrator():
    """Sibling modules must not import agent code or other benchmark modules."""
    import re

    offenders = []
    for path in (REPO_ROOT / "benchmark").rglob("*.py"):
        if path.name in ("runner.py", "__init__.py"):
            continue
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"^\s*(?:from|import)\s+([\w.]+)", text, re.M):
            module = match.group(1)
            if module.startswith("agent"):
                offenders.append(f"{path.name} imports {module}")
            if module.startswith("benchmark.") and module != "benchmark.schemas":
                offenders.append(f"{path.name} imports {module}")
    assert offenders == []


def test_agent_never_imports_benchmark():
    import re

    offenders = []
    for path in (REPO_ROOT / "agent").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"^\s*(?:from|import)\s+([\w.]+)", text, re.M):
            if match.group(1).startswith("benchmark"):
                offenders.append(f"{path.name} imports benchmark")
    assert offenders == []


def test_agent_contract_vendor_neutral():
    text = (REPO_ROOT / "agent" / "interface.py").read_text(encoding="utf-8").lower()
    for vendor in ("openai", "anthropic", "google.generativeai", "boto3"):
        assert vendor not in text


def test_benchmark_never_imports_memory():
    """FTS5/Tree-sitter/SQLite stay behind memory/ + MCP; benchmark sees tools.

    Import-line scan (docstrings may say the word "memory" legitimately).
    """
    import re

    offenders = []
    for package in ("benchmark", "agent"):
        for path in (REPO_ROOT / package).rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for match in re.finditer(r"^\s*(?:from|import)\s+([\w.]+)", text, re.M):
                module = match.group(1)
                if module == "memory" or module.startswith("memory."):
                    offenders.append(f"{path.name} imports {module}")
                if module in ("sqlite3", "tree_sitter") or module.startswith(
                    ("tree_sitter", "mcp")
                ):
                    offenders.append(f"{path.name} imports {module}")
    assert offenders == []


def test_no_unapproved_phase_artifacts_yet():
    """Proves no functionality beyond the approved Phase 3.1 scope exists.

    Evolved per milestone: 3.1 legitimized memory/episodic.py, 3.2
    legitimizes memory/git_events.py; MCP episodic tools (3.3) and any
    Phase 4 agent/transcript work remain rejected until their milestone.
    """
    for rel in (
        "memory/reference.py",
        "memory/transcripts.py",
        "agent/llm_agent.py",
        "agent/prompts.py",
    ):
        assert not (REPO_ROOT / rel).exists(), rel
    hits = []
    for path in SCOPED_FILES:
        text = path.read_text(encoding="utf-8")
        for token in ("MemoryBackend", "memory.db", "git_change", "transcript/"):
            if token in text:
                hits.append(f"{path.name}: {token}")
    assert hits == []

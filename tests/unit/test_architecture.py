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
    # Narrow seam exemptions (each covered by dedicated behavior tests):
    # - benchmark/mcp_bridge.py wraps the MCP client library (its whole job).
    # - benchmark/runner.py orchestrates the bridge/backend lifecycle and
    #   counts memory tool calls. Neither may touch sqlite/tree-sitter/etc.
    TOKEN_EXEMPTIONS = {
        "mcp_bridge.py": {"mcp"},
        "runner.py": {"mcp"},
    }
    violations = []
    for path in SCOPED_FILES:
        lowered = path.read_text(encoding="utf-8").lower()
        for token in MEMORY_TOKENS:
            if token in lowered and token not in TOKEN_EXEMPTIONS.get(path.name, set()):
                violations.append(f"{path.name}: {token}")
    assert violations == []


def test_no_shell_true():
    violations = []
    for path in SCOPED_FILES + sorted((REPO_ROOT / "scripts").rglob("*.py")):
        if "shell=true" in path.read_text(encoding="utf-8").lower().replace(" ", ""):
            violations.append(str(path))
    assert violations == []


def test_runner_is_sole_orchestrator():
    """Sibling modules must not import agent code or other benchmark modules.

    One documented exception: benchmark/mcp_bridge.py translates transport
    results into the agent's ToolDef representation (types only); it drives
    no agent behavior. The runner remains the sole orchestrator.
    """
    import re

    offenders = []
    for path in (REPO_ROOT / "benchmark").rglob("*.py"):
        if path.name in ("runner.py", "__init__.py"):
            continue
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"^\s*(?:from|import)\s+([\w.]+)", text, re.M):
            module = match.group(1)
            if module.startswith("agent"):
                if not (path.name == "mcp_bridge.py" and module == "agent.interface"):
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
    """Only the runner may touch the memory seam modules (backend selection).

    Import-line scan (docstrings may say the word "memory" legitimately).
    """
    import re

    # Exact seam allowlist: runner orchestrates backend lifecycles without
    # touching internals (sqlite/tree-sitter/structural/episodic stay banned).
    SEAM_MODULES = {"memory.interface", "memory.baseline", "memory.reference"}
    offenders = []
    for package in ("benchmark", "agent"):
        for path in (REPO_ROOT / package).rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for match in re.finditer(r"^\s*(?:from|import)\s+([\w.]+)", text, re.M):
                module = match.group(1)
                if module == "memory" or module.startswith("memory."):
                    if not (path.name == "runner.py" and module in SEAM_MODULES):
                        offenders.append(f"{path.name} imports {module}")
                if module in ("sqlite3", "tree_sitter") or module.startswith(
                    ("tree_sitter", "mcp")
                ):
                    if path.name != "mcp_bridge.py":
                        offenders.append(f"{path.name} imports {module}")
    assert offenders == []


def test_memory_benchmark_edge_minimal():
    """The only memory->benchmark edge is reference->mcp_bridge (documented).

    The backend owns the child process lifecycle; the transport lives in the
    runner-owned bridge module. No other memory module may reach into
    benchmark/ (and memory never touches agent/benchmark internals).
    """
    import re

    edges = []
    for path in (REPO_ROOT / "memory").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(
            r"^\s*from\s+([\w.]+)\s+import\s+([\w]+)", text, re.M
        ):
            module, name = match.group(1), match.group(2)
            if module == "benchmark":
                edges.append(f"{path.name} imports benchmark.{name}")
                continue
            if module.startswith("benchmark."):
                edges.append(f"{path.name} imports {module}")
        for match in re.finditer(r"^\s*import\s+([\w.]+)", text, re.M):
            module = match.group(1)
            if module == "benchmark" or module.startswith("benchmark."):
                edges.append(f"{path.name} imports {module}")
    assert edges == ["reference.py imports benchmark.mcp_bridge"]


def test_no_unapproved_phase_artifacts_yet():
    """Proves no functionality beyond the approved Phase 3.1 scope exists.

    Evolved per milestone: 3.1 legitimized memory/episodic.py, 3.2
    legitimized memory/git_events.py, 4.1 legitimized agent/llm_agent.py,
    4.2 legitimizes memory/reference.py + benchmark/mcp_bridge.py;
    transcripts, prompts, and any Phase 5 work remain rejected.
    """
    for rel in (
        "memory/transcripts.py",
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

"""Baseline backend: zero memory tools, lifecycle no-ops."""

from memory.baseline import NullBackend
from memory.interface import MCPConfig, MemoryBackend


def test_null_backend_exposes_nothing(tmp_path):
    backend = NullBackend()
    assert isinstance(backend, MemoryBackend)
    assert backend.name == "null"
    config = backend.setup(tmp_path)
    assert isinstance(config, MCPConfig)
    assert config.command == []
    assert config.tools == []
    assert backend.reset() is None
    assert backend.teardown() is None

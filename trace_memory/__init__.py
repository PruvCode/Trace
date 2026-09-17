"""TRACE: persistent local memory for coding agents, with measurement.

The ``trace_memory`` package is the user-facing CLI layer for daily use
(the ``trace`` command: ``trace init`` / ``trace status`` / ``trace report``).
It is a thin wrapper over the existing ``memory`` package (SQLite + FTS5
storage, tree-sitter structural index, episodic events, Git-derived
history).

It never imports the benchmark engine; the benchmark remains standalone
research infrastructure (``python -m benchmark.runner``).
"""

__version__ = "0.1.0"

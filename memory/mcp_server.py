"""Reference structural-memory MCP server (stdio, read-only, 3 tools).

The agent's only path to structural memory. Each tool opens a short-lived
connection to the workspace-local database, so there is no shared handle and
no cross-run state. Errors (missing DB, bad args) are raised to the client,
never swallowed.

Usage:
    python -m memory.mcp_server --db <workspace>/.agent-memory/memory.db
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from memory import store as store_mod

server = MCPServer("trace-reference-memory")

_DB_PATH: Path | None = None


def _connect() -> sqlite3.Connection:
    if _DB_PATH is None or not _DB_PATH.exists():
        raise RuntimeError(
            "structural database not found; index the workspace first"
            " (memory.structural.index_workspace)"
        )
    return store_mod.connect(_DB_PATH)


@server.tool()
def find_definition(name: str, limit: int = 10) -> list[dict]:
    """Find where a symbol is defined: name, kind, file, line, signature."""
    conn = _connect()
    try:
        return store_mod.find_definition(conn, name, limit)
    finally:
        conn.close()


@server.tool()
def find_callers(name: str, limit: int = 10) -> list[dict]:
    """Find call sites of a symbol: caller function, file, line."""
    conn = _connect()
    try:
        return store_mod.find_callers(conn, name, limit)
    finally:
        conn.close()


@server.tool()
def search_symbols(query: str, limit: int = 10) -> list[dict]:
    """Lexical symbol search over names, kinds, files, and signatures."""
    conn = _connect()
    try:
        return store_mod.search_symbols(conn, query, limit)
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    global _DB_PATH
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=None)
    args = parser.parse_args(argv)
    _DB_PATH = args.db
    if not _DB_PATH.exists():
        print(f"error: database not found: {_DB_PATH}", file=sys.stderr)
        return 2
    server.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())

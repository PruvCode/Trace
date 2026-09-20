"""Reference memory MCP server (stdio): 3 structural + 3 episodic + 3 transcript tools.

The agent's only path to memory. Each tool opens a short-lived connection to
the workspace-local database, so there is no shared handle and no cross-run
state. Errors (missing DB/repo, invalid events, bad args) are raised to the
client, never swallowed.

Usage:
    python -m memory.mcp_server --db <workspace>/.agent-memory/memory.db
        [--root <workspace>] [--repo <git repo for get_git_history>]
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import asdict
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from memory import episodic as episodic_mod
from memory import git_events as git_events_mod
from memory import store as store_mod
from memory import transcript as transcript_mod

server = MCPServer("trace-reference-memory")

_DB_PATH: Path | None = None
_REPO_PATH: Path | None = None
_ROOT_PATH: Path | None = None


def _project_root() -> Path | None:
    """Get project root (from --root arg, fallback to DB path derivation)."""
    if _ROOT_PATH is not None:
        return _ROOT_PATH
    if _DB_PATH is None:
        return None
    # DB is at <project>/.agent-memory/memory.db
    return _DB_PATH.parent.parent


def _ensure_structural_fresh() -> None:
    """Ensure structural memory is fresh before structural queries."""
    root = _project_root()
    if root is not None and root.exists():
        from memory import structural as structural_mod
        structural_mod.ensure_structural_memory(root)


def _connect() -> sqlite3.Connection:
    if _DB_PATH is None or not _DB_PATH.exists():
        raise RuntimeError(
            "structural database not found; index the workspace first"
            " (memory.structural.index_workspace)"
        )
    _ensure_structural_fresh()
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


@server.tool()
def record_event(
    type: str,
    repo: str,
    source: str = "agent",
    timestamp: str | None = None,
    commit: str | None = None,
    file: str | None = None,
    symbol: str | None = None,
    payload: dict | None = None,
) -> dict:
    """Record one episodic event (investigation/attempt/decision/observation).

    Agent-reported events are claims, not ground truth; use source="git"
    only for deterministic derivations. Invalid input raises to the client.
    """
    conn = _connect()
    try:
        event_id = episodic_mod.record_event(
            conn,
            type=type,
            source=source,
            timestamp=timestamp,
            repo=repo,
            commit=commit,
            file=file,
            symbol=symbol,
            payload=payload,
        )
        return {"id": event_id}
    finally:
        conn.close()


@server.tool()
def search_events(
    type: str | None = None,
    query: str | None = None,
    symbol: str | None = None,
    file: str | None = None,
    commit: str | None = None,
    limit: int = 10,
) -> list[dict]:
    """Search episodic events by type, text, symbol, file, or commit. Bounded."""
    conn = _connect()
    try:
        return [
            asdict(event)
            for event in episodic_mod.search_events(
                conn,
                type=type,
                query=query,
                symbol=symbol,
                file=file,
                commit=commit,
                limit=limit,
            )
        ]
    finally:
        conn.close()


def _repo() -> Path:
    if _REPO_PATH is None or not (_REPO_PATH / ".git").exists():
        raise RuntimeError(
            "git repo not configured; start the server with --repo <git repo>"
        )
    return _REPO_PATH


@server.tool()
def get_git_history(
    path: str | None = None,
    symbol: str | None = None,
    limit: int = 10,
) -> list[dict]:
    """Deterministic Git facts: sha, subject, message, files, changed symbols."""
    return git_events_mod.get_git_history(_repo(), path, symbol, limit)


@server.tool()
def record_transcript_message(
    session_id: str,
    role: str,
    content: str,
    timestamp: str | None = None,
    metadata: dict | None = None,
) -> dict:
    """Record one transcript message (user/assistant/system/tool)."""
    conn = _connect()
    try:
        msg_id = transcript_mod.record_message(
            conn,
            session_id=session_id,
            role=role,
            content=content,
            timestamp=timestamp,
            metadata=metadata,
        )
        return {"id": msg_id}
    finally:
        conn.close()


@server.tool()
def search_transcripts(
    session_id: str | None = None,
    role: str | None = None,
    query: str | None = None,
    limit: int = 10,
) -> list[dict]:
    """Search transcript messages by session, role, or full-text query. Bounded."""
    conn = _connect()
    try:
        return [
            asdict(msg)
            for msg in transcript_mod.search_messages(
                conn,
                session_id=session_id,
                role=role,
                query=query,
                limit=limit,
            )
        ]
    finally:
        conn.close()


@server.tool()
def get_transcript_session(
    session_id: str,
    limit: int = 50,
) -> list[dict]:
    """Retrieve all messages for a session, ordered by insertion. Bounded."""
    conn = _connect()
    try:
        return [
            asdict(msg)
            for msg in transcript_mod.get_session_messages(conn, session_id, limit)
        ]
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    global _DB_PATH, _REPO_PATH, _ROOT_PATH
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=None)
    parser.add_argument(
        "--repo",
        type=Path,
        default=None,
        help="git repo for get_git_history (defaults to --root)",
    )
    args = parser.parse_args(argv)
    _DB_PATH = args.db
    _ROOT_PATH = args.root
    _REPO_PATH = args.repo if args.repo is not None else args.root
    if not _DB_PATH.exists():
        print(f"error: database not found: {_DB_PATH}", file=sys.stderr)
        return 2
    server.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())

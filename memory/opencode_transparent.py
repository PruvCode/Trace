"""OpenCode transparent capture adapter - monitors OpenCode's SQLite database for session data.

This adapter provides TRUE transparent capture by reading from OpenCode's own
database (~/.local/share/opencode/opencode.db) instead of wrapping the opencode command.
The user simply runs `opencode` normally, and this adapter captures the session data
by monitoring OpenCode's database in real-time.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Iterator, Optional

from memory.capture import (
    CaptureAdapter,
    CaptureConfig,
    CapturedEvent,
    CapturedMessage,
    SessionInfo,
)
from memory import store as store_mod
from memory import transcript as transcript_mod
from memory import episodic as episodic_mod
from trace_memory import project as project_mod

MEMORY_TOOL_NAMES = frozenset({
    "find_definition",
    "find_callers",
    "search_symbols",
    "record_event",
    "search_events",
    "get_git_history",
    "record_transcript_message",
    "search_transcripts",
    "get_transcript_session",
})

# Expected OpenCode database schema version we validate against
OPENCODE_SCHEMA_VERSION = 1


class OpenCodeSchemaError(Exception):
    """Raised when OpenCode database schema is not compatible."""
    pass


class OpenCodeDatabaseMonitor:
    """Monitors OpenCode's SQLite database for new sessions and messages.

    Features:
    - Incremental processing using ROWID-based cursors for efficiency
    - Idempotent processing with stable source identifiers
    - Restart recovery with catch-up logic
    - Graceful DB failure handling with exponential backoff
    - Schema validation on startup
    - Deterministic lifecycle: start/stop with proper thread cleanup
    """

    def __init__(self, opencode_db_path: Path, trace_db_path: Path, project_name: str, config: CaptureConfig):
        self.opencode_db_path = opencode_db_path
        self.trace_db_path = trace_db_path
        self.project_name = project_name
        self.config = config
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        
        # Incremental cursors - use ROWID for reliable incremental processing
        self._last_session_rowid = 0
        self._last_message_rowid = 0
        self._last_part_rowid = 0
        
        # Processed IDs for idempotency (fallback if ROWID not available)
        self._known_sessions: set[str] = set()
        self._processed_message_ids: set[str] = set()
        self._processed_part_ids: set[str] = set()
        
        # Backoff state for DB failures
        self._consecutive_failures = 0
        self._base_backoff = 1.0  # seconds
        self._max_backoff = 30.0  # seconds
        
        # Schema validated flag
        self._schema_validated = False

    def _validate_schema(self):
        """Validate OpenCode database schema compatibility."""
        if self._schema_validated:
            return
            
        try:
            with sqlite3.connect(f"file:{self.opencode_db_path}?mode=ro", uri=True) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                
                # Check required tables exist
                required_tables = {"session", "message", "part", "session_input", "session_message"}
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ({})".format(
                    ",".join("?" * len(required_tables))), tuple(required_tables))
                found_tables = {row[0] for row in cursor.fetchall()}
                missing = required_tables - found_tables
                if missing:
                    raise OpenCodeSchemaError(f"Missing required tables: {missing}")
                
                # Check session table has required columns
                cursor.execute("PRAGMA table_info(session)")
                session_cols = {row[1] for row in cursor.fetchall()}
                required_session_cols = {"id", "time_created"}
                if not required_session_cols.issubset(session_cols):
                    raise OpenCodeSchemaError(f"Session table missing columns: {required_session_cols - session_cols}")
                
                # Check message table has required columns
                cursor.execute("PRAGMA table_info(message)")
                message_cols = {row[1] for row in cursor.fetchall()}
                required_message_cols = {"id", "session_id", "time_created"}
                if not required_message_cols.issubset(message_cols):
                    raise OpenCodeSchemaError(f"Message table missing columns: {required_message_cols - message_cols}")
                
                # Check part table has required columns
                cursor.execute("PRAGMA table_info(part)")
                part_cols = {row[1] for row in cursor.fetchall()}
                required_part_cols = {"id", "message_id", "session_id", "time_created"}
                if not required_part_cols.issubset(part_cols):
                    raise OpenCodeSchemaError(f"Part table missing columns: {required_part_cols - part_cols}")
                
                self._schema_validated = True
                
        except OpenCodeSchemaError:
            raise
        except Exception as e:
            raise OpenCodeSchemaError(f"Schema validation failed: {e}")

    def start(self):
        """Start monitoring in a background thread."""
        if self._running:
            return
        # Validate schema before starting
        self._validate_schema()
        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()

    def stop(self):
        """Stop monitoring and wait for thread to terminate."""
        self._running = False
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10.0)
            if self._thread.is_alive():
                print(f"[OpenCode monitor] Warning: Thread did not terminate within timeout")
            self._thread = None

    def _get_backoff_delay(self) -> float:
        """Calculate exponential backoff delay."""
        delay = min(self._base_backoff * (2 ** self._consecutive_failures), self._max_backoff)
        return delay

    def _monitor_loop(self):
        """Main monitoring loop - polls OpenCode database for changes."""
        while self._running:
            try:
                self._check_for_new_sessions()
                self._check_for_new_messages()
                self._check_for_new_parts()
                self._consecutive_failures = 0  # Reset on success
            except sqlite3.OperationalError as e:
                if "database is locked" in str(e).lower():
                    # Database locked - retry with backoff
                    delay = self._get_backoff_delay()
                    print(f"[OpenCode monitor] Database locked, backing off for {delay:.1f}s")
                    if self._stop_event.wait(timeout=delay):
                        break
                    self._consecutive_failures += 1
                else:
                    print(f"[OpenCode monitor] Database error: {e}")
                    if self._stop_event.wait(timeout=self._get_backoff_delay()):
                        break
                    self._consecutive_failures += 1
            except Exception as e:
                print(f"[OpenCode monitor] Error: {e}")
                if self._stop_event.wait(timeout=self._get_backoff_delay()):
                    break
                self._consecutive_failures += 1
            else:
                # Normal poll interval - use interruptible wait
                if self._stop_event.wait(timeout=1.0):
                    break

    def _check_for_new_sessions(self):
        """Detect new sessions in OpenCode database using ROWID-based incremental processing."""
        try:
            with sqlite3.connect(f"file:{self.opencode_db_path}?mode=ro", uri=True) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                
                # Use ROWID for reliable incremental processing
                cursor.execute("""
                    SELECT rowid, id, project_id, workspace_id, parent_id, slug, directory, path, title,
                           version, share_url, summary_additions, summary_deletions,
                           summary_files, summary_diffs, metadata, cost, tokens_input,
                           tokens_output, tokens_reasoning, tokens_cache_read,
                           tokens_cache_write, revert, permission, agent, model,
                           time_created, time_updated, time_compacting, time_archived
                    FROM session
                    WHERE rowid > ?
                    ORDER BY rowid ASC
                """, (self._last_session_rowid,))

                for row in cursor.fetchall():
                    rowid = row["rowid"]
                    session_id = row["id"]
                    # Update cursor
                    self._last_session_rowid = max(self._last_session_rowid, rowid)
                    
                    # Idempotency check with stable source identifier
                    if session_id not in self._known_sessions:
                        self._known_sessions.add(session_id)
                        self._process_new_session(dict(row))

        except Exception as e:
            print(f"[OpenCode monitor] Session check error: {e}")

    def _check_for_new_messages(self):
        """Detect new messages in OpenCode database using ROWID-based incremental processing."""
        try:
            with sqlite3.connect(f"file:{self.opencode_db_path}?mode=ro", uri=True) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT rowid, id, session_id, time_created, time_updated, data
                    FROM message
                    WHERE rowid > ?
                    ORDER BY rowid ASC
                """, (self._last_message_rowid,))

                for row in cursor.fetchall():
                    rowid = row["rowid"]
                    msg_id = row["id"]
                    # Update cursor
                    self._last_message_rowid = max(self._last_message_rowid, rowid)
                    
                    # Idempotency check with stable source identifier
                    if msg_id not in self._processed_message_ids:
                        self._processed_message_ids.add(msg_id)
                        self._process_message(dict(row))

        except Exception as e:
            print(f"[OpenCode monitor] Message check error: {e}")

    def _check_for_new_parts(self):
        """Detect new message parts (tool calls, text, reasoning, etc.) in OpenCode database."""
        try:
            with sqlite3.connect(f"file:{self.opencode_db_path}?mode=ro", uri=True) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT rowid, id, message_id, session_id, time_created, time_updated, data
                    FROM part
                    WHERE rowid > ?
                    ORDER BY rowid ASC
                """, (self._last_part_rowid,))

                for row in cursor.fetchall():
                    rowid = row["rowid"]
                    part_id = row["id"]
                    # Update cursor
                    self._last_part_rowid = max(self._last_part_rowid, rowid)
                    
                    # Idempotency check with stable source identifier
                    if part_id not in self._processed_part_ids:
                        self._processed_part_ids.add(part_id)
                        self._process_part(dict(row))

        except Exception as e:
            print(f"[OpenCode monitor] Part check error: {e}")

    def _process_new_session(self, session: dict):
        """Process a newly detected session."""
        session_id = session["id"]
        # Create a system message for session start
        self._persist_transcript_message(
            session_id=session_id,
            role="system",
            content=f"Session started: {session.get('title', 'Untitled')}",
            metadata={
                "agent": "opencode",
                "session_slug": session.get("slug"),
                "directory": session.get("directory"),
                "model": session.get("model"),
                "agent_type": session.get("agent"),
            }
        )

    def _process_message(self, message: dict):
        """Process a message from OpenCode's message table."""
        # Message data contains role, content, etc. in the data field
        message_data = json.loads(message["data"]) if isinstance(message["data"], str) else message["data"]
        
        role = message_data.get("role", "assistant")
        content = message_data.get("content", "")
        
        if content:
            # Determine session_id from the message
            session_id = message.get("session_id", "")
            if session_id:
                self._persist_transcript_message(
                    session_id=session_id,
                    role=role,
                    content=content,
                    metadata={"message_id": message["id"]}
                )

    def _parent_message_role(self, message_id: str) -> str | None:
        """Look up the role of a parent message in OpenCode's database.

        OpenCode stores message content in ``part`` rows; the ``message``
        row carries the role (``user`` vs ``assistant``). Returns None when
        the role cannot be determined; callers fall back to ``assistant``.
        """
        try:
            with sqlite3.connect(f"file:{self.opencode_db_path}?mode=ro", uri=True) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT data FROM message WHERE id = ?", (message_id,))
                row = cursor.fetchone()
                if not row:
                    return None
                data = json.loads(row[0]) if isinstance(row[0], str) else row[0]
                role = data.get("role") if isinstance(data, dict) else None
                return role if role in ("user", "assistant", "system") else None
        except Exception:
            return None

    def _process_part(self, part: dict):
        """Process a message part from OpenCode."""
        part_data = json.loads(part["data"]) if isinstance(part["data"], str) else part["data"]
        part_type = part_data.get("type")

        session_id = part["session_id"]

        if part_type == "text":
            # Text content - could be user or assistant message
            text_content = part_data.get("text", "")
            if text_content:
                # Attribute user vs assistant via the parent message row.
                role = self._parent_message_role(part["message_id"]) or "assistant"
                self._persist_transcript_message(
                    session_id=session_id,
                    role=role,
                    content=text_content,
                    metadata={"part_type": "text", "part_id": part["id"]}
                )

        elif part_type == "tool":
            # Tool call
            tool_name = part_data.get("tool")
            call_id = part_data.get("callID")
            state = part_data.get("state", {})
            status = state.get("status")
            input_data = state.get("input")
            output_data = state.get("output")

            if tool_name and tool_name in MEMORY_TOOL_NAMES:
                # This is a TRACE memory tool call - record as episodic event
                self._persist_memory_tool_event(
                    session_id=session_id,
                    tool_name=tool_name,
                    input_data=input_data,
                    output_data=output_data,
                    status=status,
                )

        elif part_type == "step-finish":
            # Step finished - could contain tokens info
            reason = part_data.get("reason")
            tokens = part_data.get("tokens")
            if reason:
                self._persist_transcript_message(
                    session_id=session_id,
                    role="assistant",
                    content=reason,
                    metadata={"part_type": "step-finish", "tokens": tokens}
                )

    def _persist_transcript_message(self, session_id: str, role: str, content: str, metadata: dict):
        """Persist a transcript message to TRACE database."""
        try:
            conn = store_mod.connect(self.trace_db_path)
            try:
                # Sanitize content
                if self.config.redact_secrets:
                    content = self._sanitize_content(content)
                content = content[:self.config.max_message_length]

                transcript_mod.record_message(
                    conn,
                    session_id=session_id,
                    role=role,
                    content=content,
                    metadata=metadata,
                )
            finally:
                conn.close()
        except Exception as e:
            print(f"[OpenCode monitor] Failed to persist transcript: {e}")

    def _persist_memory_tool_event(self, session_id: str, tool_name: str, input_data: dict, output_data: dict, status: str):
        """Persist a memory tool call as an episodic event."""
        try:
            conn = store_mod.connect(self.trace_db_path)
            try:
                content = f"Memory tool: {tool_name}"
                if input_data:
                    content += f" | input: {json.dumps(input_data)}"
                if output_data:
                    content += f" | output: {json.dumps(output_data)[:500]}"

                metadata = {
                    "tool": tool_name,
                    "input": input_data,
                    "output": output_data,
                    "status": status,
                }

                episodic_mod.record_event(
                    conn,
                    type="observation",
                    source="agent",
                    repo=self.project_name,
                    symbol=input_data.get("symbol") if isinstance(input_data, dict) else None,
                    file=input_data.get("file") if isinstance(input_data, dict) else None,
                    payload=metadata,
                )
            finally:
                conn.close()
        except Exception as e:
            print(f"[OpenCode monitor] Failed to persist tool event: {e}")

    def _sanitize_content(self, content: str) -> str:
        """Redact sensitive information from content (shared rules).

        Best-effort secret redaction; this is not guaranteed to detect every secret.
        Covers common patterns for API keys, tokens, passwords, and credentials.
        """
        if not self.config.redact_secrets:
            return content
        from memory import redact as redact_mod
        return redact_mod.redact_text(content)


# Class-level storage for monitor instances per project path
_MONITORS: dict[str, OpenCodeDatabaseMonitor] = {}


def _get_monitor_key(project_path: Path) -> str:
    """Generate a unique key for a project path."""
    return str(project_path.resolve())


def _get_monitor(project_path: Path) -> OpenCodeDatabaseMonitor:
    """Get or create a monitor for the given project path (singleton per project)."""
    key = _get_monitor_key(project_path)
    if key not in _MONITORS:
        opencode_db_path = Path.home() / ".local" / "share" / "opencode" / "opencode.db"
        trace_db_path = project_mod.db_path(project_path)
        _MONITORS[key] = OpenCodeDatabaseMonitor(
            opencode_db_path=Path.home() / ".local" / "share" / "opencode" / "opencode.db",
            trace_db_path=project_mod.db_path(project_path),
            project_name=project_path.name,
            config=CaptureConfig(),
        )
    return _MONITORS[key]


class OpenCodeTransparentAdapter(CaptureAdapter):
    """Transparent capture adapter for OpenCode - monitors OpenCode's database directly."""

    def __init__(self, config: Optional[CaptureConfig] = None):
        self.config = config or CaptureConfig()
        self._project_path: Optional[Path] = None
        self._trace_db_path: Optional[Path] = None

    @property
    def agent_name(self) -> str:
        return "opencode"

    @property
    def agent_version(self) -> str:
        return ">=1.0.0"

    def is_available(self) -> bool:
        # Check if OpenCode is installed AND its database exists
        from shutil import which
        if which("opencode") is None:
            return False
        # Check if OpenCode data directory exists
        opencode_data = Path.home() / ".local" / "share" / "opencode" / "opencode.db"
        return opencode_data.exists()

    def _get_opencode_db_path(self) -> Path:
        """Get the path to OpenCode's database."""
        return Path.home() / ".local" / "share" / "opencode" / "opencode.db"

    def _get_trace_db_path(self, project_path: Path) -> Path:
        """Get the TRACE database path for a project."""
        return project_mod.db_path(project_path)

    def capture_session(
        self,
        project_path: Path,
        prompt: str,
        model: str | None = None,
        session_id: str | None = None,
        config: CaptureConfig | None = None,
    ) -> Iterator[CapturedMessage | CapturedEvent]:
        """
        This method is NOT used for transparent capture.
        Transparent capture happens automatically via the database monitor.
        This method exists to satisfy the interface but should not be called.
        """
        raise NotImplementedError(
            "Transparent capture does not use capture_session. "
            "Start the monitor with start_monitoring() instead."
        )

    def start_monitoring(self, project_path: Path) -> dict:
        """Start persistent OpenCode capture service for the given project."""
        project_path = project_path.resolve()
        self._project_path = project_path

        # Ensure TRACE is initialized for this project
        if not project_mod.is_initialized(project_path):
            project_mod.init_project(project_path)

        self._trace_db_path = self._get_trace_db_path(project_path)
        opencode_db_path = self._get_opencode_db_path()

        if not opencode_db_path.exists():
            return {
                "status": "error",
                "message": f"OpenCode database not found at {opencode_db_path}. Run OpenCode at least once first."
            }

        # Use persistent service
        from memory import opencode_service as opencode_service_mod
        service = opencode_service_mod.get_service(project_path)
        return service.start()

    def stop_monitoring(self, project_path: Path) -> dict:
        """Stop persistent OpenCode capture service."""
        project_path = project_path.resolve()
        from memory import opencode_service as opencode_service_mod
        service = opencode_service_mod.get_service(project_path)
        return service.stop()

    def get_monitoring_status(self, project_path: Path) -> dict:
        """Get current monitoring status from persistent service."""
        project_path = project_path.resolve()
        from memory import opencode_service as opencode_service_mod
        service = opencode_service_mod.get_service(project_path)
        return service.status()

    def setup_integration(self, project_path: Path) -> dict:
        """Set up OpenCode integration for automatic capture (one-time setup)."""
        project_path = project_path.resolve()

        # Ensure TRACE is initialized
        if not project_mod.is_initialized(project_path):
            project_mod.init_project(project_path)

        # Create OpenCode config with TRACE MCP server
        result = self._create_opencode_config(project_path)

        # Install project-local plugin for automatic session-start retrieval
        from memory import opencode_plugin as opencode_plugin_mod
        plugin_result = opencode_plugin_mod.install_plugin(project_path)

        # Also start persistent service
        monitor_result = self.start_monitoring(project_path)

        return {
            "setup": result,
            "plugin": plugin_result,
            "monitoring": monitor_result,
        }

    def _create_opencode_config(self, project_path: Path) -> dict:
        """Create or update opencode.json with TRACE MCP server.

        TRACE only configures its own ``trace-memory`` MCP server entry.
        Any existing user configuration is preserved untouched.

        TRACE must NOT write a ``permission`` block: custom permission
        restrictions change the OpenCode free-tier request path and cause
        ``opencode run`` to fail with 403 FreeTierError
        ("OpenCode's free tier can only be used from within OpenCode").
        """
        import json
        config_path = project_path / "opencode.json"
        mcp_command = project_mod.mcp_server_command(project_path)

        existed = config_path.exists()
        opencode_config: dict = {}
        if existed:
            try:
                loaded = json.loads(config_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    opencode_config = loaded
            except (ValueError, OSError):
                # Unparseable config cannot be preserved; fall through
                # and replace it with a minimal TRACE-compatible config.
                opencode_config = {}

        if "$schema" not in opencode_config:
            opencode_config["$schema"] = "https://opencode.ai/config.json"

        mcp = opencode_config.get("mcp")
        if not isinstance(mcp, dict):
            mcp = {}
            opencode_config["mcp"] = mcp
        mcp["trace-memory"] = {
            "type": "local",
            "enabled": True,
            "command": mcp_command,
        }

        config_path.write_text(json.dumps(opencode_config, indent=2), encoding="utf-8")

        return {
            "status": "updated" if existed else "created",
            "config_path": str(config_path),
            "message": "OpenCode config created/updated with TRACE MCP server",
        }

    def get_recent_context(
        self,
        project_path: Path,
        query: str,
        limit: int = 10,
    ) -> list[CapturedMessage]:
        """Retrieve relevant historical context from TRACE transcript memory."""
        project_path = project_path.resolve()
        if not project_mod.is_initialized(project_path):
            return []

        db_path = project_mod.db_path(project_path)
        conn = store_mod.connect(db_path)
        try:
            results = transcript_mod.search_messages(
                conn,
                query=query,
                limit=limit,
            )
            return [
                CapturedMessage(
                    role=r.role,
                    content=r.content,
                    timestamp=r.timestamp,
                    session_id=r.session_id,
                    metadata=r.metadata,
                )
                for r in results
            ]
        finally:
            conn.close()

    # Automatic retrieval is handled via MCP - when OpenCode starts with the
    # TRACE MCP server configured, it can call search_transcripts/get_transcript_session
    # automatically. This is configured in the opencode.json created by setup_integration.


def create_opencode_transparent_adapter(config: Optional[CaptureConfig] = None) -> OpenCodeTransparentAdapter:
    """Factory function to create OpenCode transparent adapter."""
    return OpenCodeTransparentAdapter(config)
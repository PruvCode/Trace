"""Persistent OpenCode capture service - runs as detached subprocess.

This module provides a long-running service that monitors OpenCode's database
independently of the CLI process. The service:

1. Runs as a detached background process
2. Persists cursors to TRACE database for restart recovery
3. Exposes health/status via PID file and status file
4. Handles graceful shutdown
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from memory import store as store_mod
from memory import transcript as transcript_mod
from memory import episodic as episodic_mod


@dataclass
class CaptureConfig:
    """Configuration for automatic capture behavior."""
    enabled: bool = True
    capture_transcripts: bool = True
    capture_events: bool = True
    capture_tool_calls: bool = True
    redact_secrets: bool = True
    max_message_length: int = 50000
    session_timeout_seconds: int = 3600


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


class OpenCodeSchemaError(Exception):
    """Raised when OpenCode database schema is not compatible."""
    pass


SERVICE_DIR_NAME = ".trace-service"
PID_FILE = "opencode_monitor.pid"
STATUS_FILE = "opencode_monitor.status"


class OpenCodeCaptureService:
    """Persistent capture service for OpenCode database monitoring."""

    def __init__(
        self,
        project_path: Path,
        opencode_db_path: Path,
        trace_db_path: Path,
        project_name: str,
        config: CaptureConfig,
    ):
        self.project_path = project_path.resolve()
        self.opencode_db_path = opencode_db_path
        self.trace_db_path = trace_db_path
        self.project_name = project_name
        self.config = config

        self._running = False
        self._stop_event = None

        # Service directory for PID/status files
        self._service_dir = self.project_path / SERVICE_DIR_NAME
        self._pid_file = self._service_dir / PID_FILE
        self._status_file = self._service_dir / STATUS_FILE

    def _get_service_dir(self) -> Path:
        return self._service_dir

    def _write_pid(self, pid: int) -> None:
        """Write PID file."""
        self._service_dir.mkdir(parents=True, exist_ok=True)
        self._pid_file.write_text(str(pid))

    def _remove_pid(self) -> None:
        """Remove PID file."""
        try:
            self._pid_file.unlink(missing_ok=True)
        except Exception:
            pass

    def _write_status(self, status: dict) -> None:
        """Write status file."""
        self._service_dir.mkdir(parents=True, exist_ok=True)
        self._status_file.write_text(json.dumps(status, indent=2))

    def _read_status(self) -> dict | None:
        """Read status file."""
        try:
            return json.loads(self._status_file.read_text())
        except Exception:
            return None

    def _load_cursors(self) -> tuple[int, int, int]:
        """Load persisted cursors from TRACE database."""
        try:
            conn = store_mod.connect(self.trace_db_path)
            cursors = store_mod.get_all_monitor_cursors(conn, self.project_name)
            conn.close()
            return (
                cursors.get("session", 0),
                cursors.get("message", 0),
                cursors.get("part", 0),
            )
        except Exception:
            return 0, 0, 0

    def _reconstruct_project_sessions(self) -> set[str]:
        """Reconstruct project_sessions by querying OpenCode DB for sessions in this project.
        
        This reconstructs the set of session IDs that belong to this project,
        which is needed for proper message/part filtering after service restart.
        """
        project_sessions = set()
        project_dir = str(self.project_path.resolve()).replace("\\", "/")
        try:
            with sqlite3.connect(f"file:{self.opencode_db_path}?mode=ro", uri=True) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT id FROM session
                    WHERE directory = ?
                """, (project_dir,))
                for row in cursor.fetchall():
                    project_sessions.add(row["id"])
        except Exception as e:
            print(f"[OpenCode service] Failed to reconstruct project sessions: {e}")
        return project_sessions

    def _save_cursors(
        self, session_rowid: int, message_rowid: int, part_rowid: int
    ) -> None:
        """Save cursors to TRACE database."""
        try:
            conn = store_mod.connect(self.trace_db_path)
            store_mod.set_monitor_cursor(
                conn, self.project_name, "session", session_rowid
            )
            store_mod.set_monitor_cursor(
                conn, self.project_name, "message", message_rowid
            )
            store_mod.set_monitor_cursor(
                conn, self.project_name, "part", part_rowid
            )
            conn.close()
        except Exception as e:
            print(f"[OpenCode service] Failed to save cursors: {e}")

    def _initialize_cursors(self) -> tuple[int, int, int]:
        """Initialize cursors to current max ROWID to avoid re-processing existing data."""
        try:
            with sqlite3.connect(f"file:{self.opencode_db_path}?mode=ro", uri=True) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT COALESCE(MAX(rowid), 0) FROM session")
                session_rowid = cursor.fetchone()[0]
                cursor.execute("SELECT COALESCE(MAX(rowid), 0) FROM message")
                message_rowid = cursor.fetchone()[0]
                cursor.execute("SELECT COALESCE(MAX(rowid), 0) FROM part")
                part_rowid = cursor.fetchone()[0]
            return session_rowid, message_rowid, part_rowid
        except Exception as e:
            print(f"[OpenCode service] Failed to initialize cursors: {e}")
            return 0, 0, 0

    def _validate_schema(self) -> None:
        """Validate OpenCode database schema."""
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

        except OpenCodeSchemaError:
            raise
        except Exception as e:
            raise OpenCodeSchemaError(f"Schema validation failed: {e}")

    def start(self) -> dict:
        """Start the persistent capture service."""
        # Check if already running
        if self.is_running():
            return {"status": "already_running", "message": "Service already running"}

        # Validate schema
        try:
            self._validate_schema()
        except OpenCodeSchemaError as e:
            return {"status": "error", "message": f"Schema validation failed: {e}"}

        # Load persisted cursors
        session_rowid, message_rowid, part_rowid = self._load_cursors()

        # If cursors are 0 (first run), initialize to current max ROWID to avoid re-processing existing data
        if session_rowid == 0 or message_rowid == 0 or part_rowid == 0:
            session_rowid, message_rowid, part_rowid = self._initialize_cursors()
            # Save initialized cursors
            self._save_cursors(session_rowid, message_rowid, part_rowid)

        # Start as detached subprocess
        python_exe = sys.executable
        script_path = Path(__file__).resolve()

        # Build command to run the service module as a script
        cmd = [
            python_exe,
            "-m",
            "memory.opencode_service",
            "--project-path", str(self.project_path),
            "--project-name", self.project_name,
            "--trace-db", str(self.trace_db_path),
            "--opencode-db", str(self.opencode_db_path),
            "--action", "run",
        ]

        # Start detached process
        if sys.platform == "win32":
            # Windows: use CREATE_NEW_CONSOLE and CREATE_BREAKAWAY_FROM_JOB for true detachment
            CREATE_NEW_CONSOLE = 0x00000010
            CREATE_BREAKAWAY_FROM_JOB = 0x01000000
            CREATE_NEW_PROCESS_GROUP = 0x00000200
            proc = subprocess.Popen(
                cmd,
                creationflags=CREATE_NEW_CONSOLE | CREATE_BREAKAWAY_FROM_JOB | CREATE_NEW_PROCESS_GROUP,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
            )
        else:
            # Unix: use start_new_session for proper detachment
            proc = subprocess.Popen(
                cmd,
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
            )

        pid = proc.pid
        self._write_pid(pid)
        self._write_status(
            {
                "status": "running",
                "pid": pid,
                "project": self.project_name,
                "project_path": str(self.project_path),
                "started_at": time.time(),
            }
        )
        return {
            "status": "started",
            "pid": pid,
            "message": "OpenCode capture service started",
        }

    def _is_pid_alive(self, pid: int) -> bool:
        """Check whether a PID is still alive."""
        try:
            if sys.platform == "win32":
                result = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {pid}"],
                    capture_output=True, text=True, check=False
                )
                return str(pid) in result.stdout
            else:
                os.kill(pid, 0)
                return True
        except OSError:
            return False

    def _wait_for_exit(self, pid: int, attempts: int = 50, interval: float = 0.1) -> bool:
        """Wait (bounded) for a PID to exit. Returns True when gone."""
        for _ in range(attempts):
            if not self._is_pid_alive(pid):
                return True
            time.sleep(interval)
        return not self._is_pid_alive(pid)

    def _log(self, msg: str) -> None:
        """Write log message to service log file."""
        try:
            log_file = self._service_dir / "service.log"
            log_file.parent.mkdir(parents=True, exist_ok=True)
            with open(log_file, "a") as f:
                import datetime
                timestamp = datetime.datetime.now().isoformat()
                f.write(f"{timestamp} {msg}\n")
        except Exception:
            pass

    def _run_monitor_loop(
        self, session_rowid: int, message_rowid: int, part_rowid: int
    ) -> None:
        # State in child process
        self._running = True
        self._stop_event = False

        # Cursor state
        last_session_rowid = session_rowid
        last_message_rowid = message_rowid
        last_part_rowid = part_rowid

        # ID sets for idempotency
        known_sessions: set[str] = set()
        project_sessions: set[str] = set()
        processed_message_ids: set[str] = set()
        processed_part_ids: set[str] = set()

        # Reconstruct project_sessions from OpenCode DB for restart recovery
        project_sessions = self._reconstruct_project_sessions()
        self._log(f"Reconstructed {len(project_sessions)} project sessions from OpenCode DB")

        # NOTE: processed-ID sets intentionally start empty on (re)start.
        # Restart recovery relies on persisted ROWID cursors: rows with
        # rowid > cursor are (re)processed, older rows are never reselected.
        # Pre-populating the ID sets from current DB state would permanently
        # skip rows created while the service was stopped, defeating
        # catch-up after restart.

        # Setup signal handlers
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

        self._log(f"Service started for project {self.project_name} at {self.project_path}")

        # Update status
        self._write_status(
            {
                "status": "running",
                "pid": os.getpid(),
                "project": self.project_name,
                "project_path": str(self.project_path),
                "started_at": time.time(),
                "cursors": {
                    "session": last_session_rowid,
                    "message": last_message_rowid,
                    "part": last_part_rowid,
                },
            }
        )

        consecutive_failures = 0
        base_backoff = 1.0
        max_backoff = 30.0

        try:
            while self._running:
                try:
                    last_session_rowid = self._check_for_new_sessions(
                        last_session_rowid, known_sessions, project_sessions
                    )
                    last_message_rowid = self._check_for_new_messages(
                        last_message_rowid, processed_message_ids, project_sessions
                    )
                    last_part_rowid = self._check_for_new_parts(
                        last_part_rowid, processed_part_ids, project_sessions
                    )

                    # Persist cursors periodically
                    self._save_cursors(
                        last_session_rowid,
                        last_message_rowid,
                        last_part_rowid,
                    )

                    consecutive_failures = 0

                except sqlite3.OperationalError as e:
                    if "database is locked" in str(e).lower():
                        delay = min(base_backoff * (2**consecutive_failures), max_backoff)
                        time.sleep(delay)
                        consecutive_failures += 1
                    else:
                        self._log(f"Database error: {e}")
                        time.sleep(self._get_backoff_delay(consecutive_failures, base_backoff, max_backoff))
                        consecutive_failures += 1
                except Exception as e:
                    self._log(f"Unhandled error: {e}")
                    import traceback
                    self._log("Traceback: " + traceback.format_exc())
                    time.sleep(self._get_backoff_delay(consecutive_failures, base_backoff, max_backoff))
                    consecutive_failures += 1
                else:
                    time.sleep(1.0)
        finally:
            # Save cursors on exit
            self._save_cursors(
                last_session_rowid,
                last_message_rowid,
                last_part_rowid,
            )
            self._log(f"Service stopped for project {self.project_name}")
            self._remove_pid()
            self._write_status({"status": "stopped", "project": self.project_name})
    def _check_for_new_sessions(self, last_rowid: int, known_sessions: set[str], project_sessions: set[str]) -> int:
        """Detect new sessions in OpenCode database for this project."""
        # Normalize project path for comparison (OpenCode stores with forward slashes)
        project_dir = str(self.project_path.resolve()).replace("\\", "/")
        try:
            with sqlite3.connect(f"file:{self.opencode_db_path}?mode=ro", uri=True) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()

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
                """, (last_rowid,))

                for row in cursor.fetchall():
                    rowid = row["rowid"]
                    session_id = row["id"]
                    # Update cursor
                    last_rowid = max(last_rowid, rowid)

                    # Filter by project directory
                    try:
                        session_dir = row["directory"]
                    except KeyError:
                        session_dir = ""
                    session_dir_normalized = session_dir.replace("\\", "/") if session_dir else ""
                    print(f"[OpenCode service] Session {session_id}: dir='{session_dir}', normalized='{session_dir_normalized}', project_dir='{project_dir}', match={session_dir_normalized == project_dir}")
                    if session_dir:
                        session_dir_normalized = session_dir.replace("\\", "/")
                        if session_dir_normalized != project_dir:
                            continue

                    # Idempotency check
                    if session_id not in known_sessions:
                        known_sessions.add(session_id)
                        project_sessions.add(session_id)
                        print(f"[OpenCode service] Added session {session_id} to project_sessions")
                        self._process_new_session(dict(row))

        except Exception as e:
            print(f"[OpenCode service] Session check error: {e}")
        return last_rowid

    def _check_for_new_messages(self, last_rowid: int, processed_ids: set[str], project_sessions: set[str]) -> int:
        """Detect new messages in OpenCode database for this project."""
        try:
            with sqlite3.connect(f"file:{self.opencode_db_path}?mode=ro", uri=True) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT rowid, id, session_id, time_created, time_updated, data
                    FROM message
                    WHERE rowid > ?
                    ORDER BY rowid ASC
                """, (last_rowid,))

                for row in cursor.fetchall():
                    rowid = row["rowid"]
                    msg_id = row["id"]
                    try:
                        session_id = row["session_id"]
                    except KeyError:
                        session_id = ""
                    last_rowid = max(last_rowid, rowid)

                    # Filter by project sessions
                    if session_id:
                        if session_id not in project_sessions:
                            print(f"[OpenCode service] Skipping message {msg_id}: session {session_id} not in project_sessions ({project_sessions})")
                            continue
                    else:
                        print(f"[OpenCode service] Skipping message {msg_id}: no session_id")

                    if msg_id not in processed_ids:
                        processed_ids.add(msg_id)
                        self._process_message(dict(row))

        except Exception as e:
            print(f"[OpenCode service] Message check error: {e}")
        return last_rowid

    def _check_for_new_parts(self, last_rowid: int, processed_ids: set[str], project_sessions: set[str]) -> int:
        """Detect new message parts in OpenCode database for this project."""
        try:
            with sqlite3.connect(f"file:{self.opencode_db_path}?mode=ro", uri=True) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT rowid, id, message_id, session_id, time_created, time_updated, data
                    FROM part
                    WHERE rowid > ?
                    ORDER BY rowid ASC
                """, (last_rowid,))

                for row in cursor.fetchall():
                    rowid = row["rowid"]
                    part_id = row["id"]
                    try:
                        session_id = row["session_id"]
                    except KeyError:
                        session_id = ""
                    last_rowid = max(last_rowid, rowid)

                    # Filter by project sessions
                    if session_id and session_id not in project_sessions:
                        continue

                    if part_id not in processed_ids:
                        processed_ids.add(part_id)
                        self._process_part(dict(row))

        except Exception as e:
            print(f"[OpenCode service] Part check error: {e}")
        return last_rowid

    def _process_new_session(self, session: dict) -> None:
        """Process a newly detected session."""
        session_id = session["id"]
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

    def _process_message(self, message: dict) -> None:
        """Process a message from OpenCode's message table."""
        message_data = json.loads(message["data"]) if isinstance(message["data"], str) else message["data"]

        role = message_data.get("role", "assistant")
        content = message_data.get("content", "")

        if content:
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

    def _process_part(self, part: dict) -> None:
        """Process a message part from OpenCode."""
        part_data = json.loads(part["data"]) if isinstance(part["data"], str) else part["data"]
        part_type = part_data.get("type")

        session_id = part["session_id"]

        if part_type == "text":
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
            tool_name = part_data.get("tool")
            call_id = part_data.get("callID")
            state = part_data.get("state", {})
            status = state.get("status")
            input_data = state.get("input")
            output_data = state.get("output")

            if tool_name and tool_name in MEMORY_TOOL_NAMES:
                self._persist_memory_tool_event(
                    session_id=session_id,
                    tool_name=tool_name,
                    input_data=input_data,
                    output_data=output_data,
                    status=status,
                )

        elif part_type == "step-finish":
            reason = part_data.get("reason")
            tokens = part_data.get("tokens")
            if reason:
                self._persist_transcript_message(
                    session_id=session_id,
                    role="assistant",
                    content=reason,
                    metadata={"part_type": "step-finish", "tokens": tokens}
                )

    def _persist_transcript_message(self, session_id: str, role: str, content: str, metadata: dict) -> None:
        """Persist a transcript message to TRACE database."""
        try:
            conn = store_mod.connect(self.trace_db_path)
            try:
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
            print(f"[OpenCode service] Failed to persist transcript: {e}")

    def _persist_memory_tool_event(
        self, session_id: str, tool_name: str, input_data: dict, output_data: dict, status: str
    ) -> None:
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
            print(f"[OpenCode service] Failed to persist tool event: {e}")

    def _sanitize_content(self, content: str) -> str:
        """Redact sensitive information from content."""
        if not self.config.redact_secrets:
            return content
        import re
        patterns = [
            (r'(api[_-]?key|secret|password|passwd|credential|auth[_-]?token|access[_-]?token|refresh[_-]?token)\s*[:=]\s*\S+', r'\1=***REDACTED***'),
            (r'sk-[a-zA-Z0-9]{32,}', '***REDACTED***'),
            (r'(sk|pk)_(live|test)_[a-zA-Z0-9]{24,}', '***REDACTED***'),
            (r'gh[psuo]_[a-zA-Z0-9]{36}', '***REDACTED***'),
            (r'xox[baprs]-[\w-]{10,}', '***REDACTED***'),
            (r'AKIA[0-9A-Z]{16}', '***REDACTED***'),
            (r'(aws[_-]?secret[_-]?access[_-]?key)\s*[:=]\s*\S+', r'\1=***REDACTED***'),
            (r'AIza[0-9A-Za-z\-_]{35}', '***REDACTED***'),
            (r'bearer\s+[a-zA-Z0-9\._\-]{20,}', 'bearer ***REDACTED***', re.IGNORECASE),
            (r'-----BEGIN (RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----[\s\S]*?-----END (RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----', '***REDACTED PRIVATE KEY***'),
            (r'(mongodb|postgres|mysql|redis)://[^:]+:[^@]+@', r'\1://***REDACTED:***REDACTED@', re.IGNORECASE),
            (r'eyJ[a-zA-Z0-9_-]{10,}\.eyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}', '***REDACTED JWT***'),
        ]
        result = content
        for pattern in patterns:
            if len(pattern) == 3:
                regex, replacement, flags = pattern
                result = re.sub(regex, replacement, result, flags=flags)
            else:
                regex, replacement = pattern
                result = re.sub(regex, replacement, result, flags=re.IGNORECASE)
        return result

    def _handle_signal(self, signum, frame):
        """Handle shutdown signals."""
        self._running = False

    def _get_backoff_delay(self, failures: int, base: float, max_delay: float) -> float:
        return min(base * (2**failures), max_delay)

    def stop(self) -> dict:
        """Stop the persistent capture service."""
        pid = self._get_pid()
        if not pid:
            self._remove_pid()
            self._write_status({"status": "stopped", "project": self.project_name})
            return {"status": "not_running", "message": "Service not running"}

        try:
            if sys.platform == "win32":
                # Use taskkill on Windows. /T kills the whole child tree:
                # the service is spawned via shim layers, so stopping only
                # the PID-file process could leave a grandchild polling the
                # databases after "stopped" is reported.
                subprocess.run(["taskkill", "/PID", str(pid), "/T"],
                               capture_output=True, check=False)
            else:
                os.kill(pid, signal.SIGTERM)

            # Wait for process to terminate
            if self._wait_for_exit(pid):
                self._remove_pid()
                self._write_status({"status": "stopped", "project": self.project_name})
                return {"status": "stopped", "message": "OpenCode capture service stopped"}

            # Force kill, then wait again so file handles (TRACE DB) are
            # actually released before the caller proceeds to cleanup.
            try:
                if sys.platform == "win32":
                    subprocess.run(["taskkill", "/F", "/PID", str(pid), "/T"],
                                   capture_output=True, check=False)
                else:
                    os.kill(pid, signal.SIGKILL)
            except Exception:
                pass

            if self._wait_for_exit(pid):
                self._remove_pid()
                self._write_status({"status": "stopped", "project": self.project_name})
                return {"status": "stopped", "message": "OpenCode capture service stopped"}

            self._remove_pid()
            return {"status": "error", "message": f"Service process {pid} did not terminate"}
        except OSError as e:
            self._remove_pid()
            return {"status": "error", "message": f"Failed to stop service: {e}"}

    def _get_pid(self) -> int | None:
        """Get PID from file."""
        try:
            pid_str = self._pid_file.read_text().strip()
            return int(pid_str) if pid_str else None
        except Exception:
            return None

    def is_running(self) -> bool:
        """Check if service is running."""
        pid = self._get_pid()
        if not pid:
            return False
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            self._remove_pid()
            return False

    def status(self) -> dict:
        """Get service status."""
        if not self.is_running():
            return {"status": "stopped", "project": self.project_name}

        pid = self._get_pid()
        status = self._read_status() or {}

        return {
            "status": "running",
            "pid": pid,
            "project": self.project_name,
            "project_path": str(self.project_path),
            **status,
        }

    def restart(self) -> dict:
        """Restart the service."""
        self.stop()
        time.sleep(0.5)
        return self.start()


def get_service(
    project_path: Path,
    project_name: str | None = None,
    config: CaptureConfig | None = None,
) -> OpenCodeCaptureService:
    """Factory function to create service for a project."""
    project_path = project_path.resolve()
    project_name = project_name or project_path.name
    config = config or CaptureConfig()

    opencode_db_path = Path.home() / ".local" / "share" / "opencode" / "opencode.db"
    trace_db_path = project_path / ".agent-memory" / "memory.db"

    return OpenCodeCaptureService(
        project_path=project_path,
        opencode_db_path=opencode_db_path,
        trace_db_path=trace_db_path,
        project_name=project_name,
        config=config,
    )


def main():
    """Main entry point for the service subprocess."""
    import argparse

    parser = argparse.ArgumentParser(description="OpenCode Capture Service")
    parser.add_argument("--project-path", required=True, type=Path)
    parser.add_argument("--project-name", required=True)
    parser.add_argument("--trace-db", required=True, type=Path)
    parser.add_argument("--opencode-db", required=True, type=Path)
    parser.add_argument("--action", choices=["start", "stop", "status", "restart", "run"], default="start")
    args = parser.parse_args()

    config = CaptureConfig()
    service = OpenCodeCaptureService(
        project_path=args.project_path,
        opencode_db_path=args.opencode_db,
        trace_db_path=args.trace_db,
        project_name=args.project_name,
        config=config,
    )

    if args.action == "start":
        result = service.start()
        print(json.dumps(result, default=str))
    elif args.action == "run":
        # Run the monitor loop directly (called by the spawned subprocess)
        session_rowid, message_rowid, part_rowid = service._load_cursors()
        service._run_monitor_loop(session_rowid, message_rowid, part_rowid)
    elif args.action == "stop":
        result = service.stop()
        print(json.dumps(result, default=str))
    elif args.action == "status":
        result = service.status()
        print(json.dumps(result, default=str))
    elif args.action == "restart":
        result = service.restart()
        print(json.dumps(result, default=str))
    else:
        result = {"status": "error", "message": "Unknown action"}
        print(json.dumps(result, default=str))


if __name__ == "__main__":
    main()
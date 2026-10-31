"""Persistent OpenCode capture service - runs as a detached subprocess.

Monitors OpenCode's local SQLite database and copies this project's
conversation into the project's TRACE database:

1. One monitor per project, enforced by an OS file lock held for the
   monitor's lifetime (a second start exits immediately).
2. Only *final* rows are captured. OpenCode inserts a text part empty and
   fills it in (``time.end`` marks completion); tool parts move
   pending -> running -> completed/error. A non-final row holds the cursor
   below it so it is re-read later; rows after it still flow, and replays
   are no-ops because every captured row carries a unique ``source_id``.
3. Persistence failures are never swallowed: the cursor stops at the failed
   row, the error is logged to ``.trace-service/service.log`` (without
   message content) and counted in the status file, and the row is retried.
4. Cursors live in the TRACE database, so a restart (or reboot followed by
   ``ensure_running``) resumes exactly where capture stopped.
5. Graceful stop via a stop-request file; forced kill only as a fallback.
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

from memory import episodic as episodic_mod
from memory import redact as redact_mod
from memory import store as store_mod
from memory import synthetic as synthetic_mod
from memory import transcript as transcript_mod


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

# OpenCode exposes MCP tools as "<server>_<tool>". TRACE's server is
# registered as "trace-memory" by setup and "trace_memory" by the benchmark.
MEMORY_TOOL_PREFIXES = ("trace-memory_", "trace_memory_")

# The MCP server persists these itself; capturing the call would duplicate it.
_SELF_PERSISTING_TOOLS = frozenset({"record_event", "record_transcript_message"})


class OpenCodeSchemaError(Exception):
    """Raised when OpenCode database schema is not compatible."""
    pass


SERVICE_DIR_NAME = ".trace-service"
PID_FILE = "opencode_monitor.pid"
STATUS_FILE = "opencode_monitor.status"
LOCK_FILE = "opencode_monitor.lock"
STOP_FILE = "stop.request"
DISABLED_FILE = "disabled"
LOG_FILE = "service.log"

# Win32 exit code reported for a process that is still running.
_WINDOWS_STILL_ACTIVE = 259
_WINDOWS_QUERY_LIMITED_INFORMATION = 0x1000

# A part that never completes (crashed or cancelled turn) stops holding the
# cursor once its message is completed or after this long.
PART_ABANDON_MS = 10 * 60_000

POLL_SECONDS = 1.0
LOCK_STARTUP_WAIT_SECONDS = 2.0
START_TIMEOUT_SECONDS = 20.0
STOP_TIMEOUT_SECONDS = 6.0


def default_opencode_db_path() -> Path:
    """OpenCode's database, honoring XDG_DATA_HOME the way OpenCode does."""
    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / "opencode" / "opencode.db"


def canonical_memory_tool(name: object) -> str | None:
    """Map an OpenCode tool name (bare or server-prefixed) to a TRACE tool."""
    if not isinstance(name, str):
        return None
    if name in MEMORY_TOOL_NAMES:
        return name
    for prefix in MEMORY_TOOL_PREFIXES:
        if name.startswith(prefix) and name[len(prefix):] in MEMORY_TOOL_NAMES:
            return name[len(prefix):]
    return None


def process_is_alive(pid: int) -> bool:
    """Report whether ``pid`` names a live process, never signalling it.

    On Windows ``os.kill(pid, 0)`` is not an existence probe: signal 0 equals
    ``CTRL_C_EVENT``, so CPython calls ``GenerateConsoleCtrlEvent`` and may
    deliver Ctrl+C to a process group. Windows therefore uses the Win32
    exit-code probe; POSIX keeps signal 0, which is a pure probe there.

    Never raises: any unexpected failure is reported as "not alive".
    """
    if not pid or pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = kernel32.OpenProcess(
                _WINDOWS_QUERY_LIMITED_INFORMATION, False, int(pid)
            )
            if not handle:
                return False
            try:
                code = wintypes.DWORD()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return False
                return code.value == _WINDOWS_STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _as_dict(raw: object) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw) if isinstance(raw, (str, bytes)) else None
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def part_is_final(data: dict) -> bool:
    """Whether OpenCode has finished writing this part (verified on 1.18.34).

    Text: user/synthetic parts are written once and carry no ``time``;
    assistant text is inserted empty with ``time.start`` and gets
    ``time.end`` when complete. Tool: ``state.status`` completed or error.
    Every other part type is written once.
    """
    kind = data.get("type")
    if kind == "text":
        when = data.get("time")
        return not isinstance(when, dict) or bool(when.get("end"))
    if kind == "tool":
        state = data.get("state") if isinstance(data.get("state"), dict) else {}
        return state.get("status") in ("completed", "error")
    return True


class LockHandle:
    """An exclusive, non-blocking lock on a file, released when closed or on exit."""

    def __init__(self, path: Path):
        self.path = path
        self._fh = None

    def acquire(self, wait: float = 0.0) -> bool:
        """Try to take the lock, retrying for up to ``wait`` seconds."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.time() + wait
        while True:
            fh = open(self.path, "a+")
            try:
                if sys.platform == "win32":
                    import msvcrt
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                fh.close()
                if time.time() >= deadline:
                    return False
                time.sleep(0.05)
                continue
            self._fh = fh
            return True

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        fh.close()


@dataclass
class _CaptureState:
    """Per-process capture state; everything durable lives in the TRACE DB."""
    cursors: dict = field(default_factory=dict)
    project_sessions: set = field(default_factory=set)
    processed: set = field(default_factory=set)
    roles: dict = field(default_factory=dict)
    captured: int = 0
    errors: int = 0
    last_error: str | None = None


class CaptureError(Exception):
    """A row could not be persisted; the cursor stays before it."""


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
        self._last_proc: subprocess.Popen | None = None
        self._role_cache: dict = {}
        self._batch_conn: sqlite3.Connection | None = None

        self._service_dir = self.project_path / SERVICE_DIR_NAME
        self._pid_file = self._service_dir / PID_FILE
        self._status_file = self._service_dir / STATUS_FILE
        self._lock_file = self._service_dir / LOCK_FILE
        self._stop_file = self._service_dir / STOP_FILE
        self._disabled_file = self._service_dir / DISABLED_FILE

    # -- small file helpers ------------------------------------------------

    def _get_service_dir(self) -> Path:
        return self._service_dir

    def _write_pid(self, pid: int) -> None:
        self._service_dir.mkdir(parents=True, exist_ok=True)
        self._pid_file.write_text(str(pid))

    def _remove_pid(self) -> None:
        try:
            self._pid_file.unlink(missing_ok=True)
        except OSError:
            pass

    def _get_pid(self) -> int | None:
        try:
            pid_str = self._pid_file.read_text().strip()
            return int(pid_str) if pid_str else None
        except (OSError, ValueError):
            return None

    def _write_status(self, status: dict) -> None:
        """Best-effort status write; never raises (it must not stop capture).

        On Windows ``os.replace`` fails while another process (a status
        reader, an indexer, antivirus) briefly holds the target, so retry,
        then fall back to a direct write.
        """
        text = json.dumps(status, indent=2)
        try:
            self._service_dir.mkdir(parents=True, exist_ok=True)
            tmp = self._status_file.with_suffix(".tmp")
            tmp.write_text(text)
            for _ in range(10):
                try:
                    os.replace(tmp, self._status_file)
                    return
                except PermissionError:
                    time.sleep(0.05)
            self._status_file.write_text(text)
            tmp.unlink(missing_ok=True)
        except OSError as exc:
            self._log(f"status write failed: {type(exc).__name__}: {exc}")

    def _read_status(self) -> dict | None:
        try:
            return json.loads(self._status_file.read_text())
        except (OSError, ValueError):
            return None

    def _log(self, msg: str) -> None:
        """Append to service.log. Callers never pass message content."""
        try:
            self._service_dir.mkdir(parents=True, exist_ok=True)
            import datetime
            stamp = datetime.datetime.now().isoformat(timespec="seconds")
            with open(self._service_dir / LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"{stamp} {redact_mod.redact_text(msg)}\n")
        except OSError:
            pass

    def _opencode_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{self.opencode_db_path}?mode=ro", uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _project_dir_key(self) -> str:
        key = str(self.project_path).replace("\\", "/")
        return key.casefold() if sys.platform == "win32" else key

    def _is_project_dir(self, directory: object) -> bool:
        if not isinstance(directory, str) or not directory:
            return False
        key = directory.replace("\\", "/")
        if sys.platform == "win32":
            key = key.casefold()
        return key == self._project_dir_key()

    # -- schema / cursors ----------------------------------------------------

    def _validate_schema(self) -> None:
        """Check the OpenCode tables and columns capture depends on."""
        try:
            with closing(self._opencode_conn()) as conn:
                required = {
                    "session": {"id", "directory", "time_created"},
                    "message": {"id", "session_id", "data"},
                    "part": {"id", "message_id", "session_id", "time_created", "data"},
                }
                for table, cols in required.items():
                    found = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
                    if not found:
                        raise OpenCodeSchemaError(f"Missing required table: {table}")
                    missing = cols - found
                    if missing:
                        raise OpenCodeSchemaError(f"{table} table missing columns: {sorted(missing)}")
        except OpenCodeSchemaError:
            raise
        except Exception as e:
            raise OpenCodeSchemaError(f"Schema validation failed: {e}")

    def _ensure_trace_schema(self) -> None:
        conn = store_mod.connect(self.trace_db_path)
        try:
            store_mod.init_schema(conn)
        finally:
            conn.close()

    def _load_cursors(self) -> dict | None:
        """Persisted cursors, or None when capture has never run for this project."""
        conn = store_mod.connect(self.trace_db_path)
        try:
            cursors = store_mod.get_all_monitor_cursors(conn, self.project_name)
        finally:
            conn.close()
        if not cursors:
            return None
        return {k: int(cursors.get(k, 0)) for k in ("session", "message", "part")}

    def _initial_cursors(self) -> dict:
        """First run starts at the current end of OpenCode's history."""
        with closing(self._opencode_conn()) as conn:
            return {
                table: conn.execute(f"SELECT COALESCE(MAX(rowid), 0) FROM {table}").fetchone()[0]
                for table in ("session", "message", "part")
            }

    def _save_cursors(self, cursors: dict) -> None:
        conn = store_mod.connect(self.trace_db_path)
        try:
            store_mod.set_monitor_cursors(conn, self.project_name, cursors)
        finally:
            conn.close()

    def _prepare_state(self) -> _CaptureState:
        self._ensure_trace_schema()
        cursors = self._load_cursors()
        if cursors is None:
            cursors = self._initial_cursors()
            self._save_cursors(cursors)
        state = _CaptureState(cursors=cursors)
        state.project_sessions = self._reconstruct_project_sessions()
        self._role_cache = state.roles
        return state

    def _reconstruct_project_sessions(self) -> set[str]:
        """Session ids OpenCode has recorded for this project directory."""
        sessions: set[str] = set()
        with closing(self._opencode_conn()) as conn:
            for row in conn.execute("SELECT id, directory FROM session"):
                if self._is_project_dir(row["directory"]):
                    sessions.add(row["id"])
        return sessions

    # -- one capture pass ----------------------------------------------------

    def capture_pass(self, state: _CaptureState) -> bool:
        """Capture everything final since the cursors. Returns True if anything changed.

        On a persistence failure the cursors keep the progress made so far,
        the failure is logged and counted, and CaptureError is raised.
        """
        # One TRACE transaction per pass: captured rows and the cursors that
        # cover them commit together (one fsync instead of one per row). If
        # the commit fails, nothing from this pass is durable, so the
        # in-memory progress is rolled back too and the rows are re-read.
        durable = (dict(state.cursors), set(state.processed), state.captured)
        trace = store_mod.connect(self.trace_db_path)
        self._batch_conn = trace
        failure: CaptureError | None = None
        try:
            with closing(self._opencode_conn()) as conn:
                self._capture_sessions(conn, state)
                self._capture_messages(conn, state)
                self._capture_parts(conn, state)
        except CaptureError as exc:
            failure = exc  # progress before the failed row is still committed below
        finally:
            self._batch_conn = None
            try:
                store_mod.set_monitor_cursors(trace, self.project_name, state.cursors)
            except Exception as exc:
                trace.rollback()
                state.cursors, state.processed, state.captured = durable
                raise self._fail(state, "commit of capture pass", exc) from exc
            finally:
                trace.close()
        if failure is not None:
            raise failure
        return durable[0] != state.cursors or durable[2] != state.captured

    def _fail(self, state: _CaptureState, what: str, exc: Exception) -> CaptureError:
        detail = f"{type(exc).__name__}: {str(exc)[:200]}"
        state.errors += 1
        state.last_error = f"{what}: {detail}"
        self._log(f"capture error on {what}; will retry: {detail}")
        return CaptureError(state.last_error)

    def _capture_sessions(self, conn: sqlite3.Connection, state: _CaptureState) -> None:
        rows = conn.execute(
            "SELECT rowid AS rid, * FROM session WHERE rowid > ? ORDER BY rowid",
            (state.cursors["session"],),
        ).fetchall()
        for row in rows:
            session = dict(row)
            if self._is_project_dir(session.get("directory")):
                state.project_sessions.add(session["id"])
                try:
                    self._process_new_session(session)
                except Exception as exc:
                    raise self._fail(state, f"session row {row['rid']}", exc) from exc
                state.captured += 1
            state.cursors["session"] = row["rid"]

    def _capture_messages(self, conn: sqlite3.Connection, state: _CaptureState) -> None:
        rows = conn.execute(
            "SELECT rowid AS rid, id, session_id, data FROM message WHERE rowid > ? ORDER BY rowid",
            (state.cursors["message"],),
        ).fetchall()
        for row in rows:
            if row["session_id"] in state.project_sessions:
                data = _as_dict(row["data"])
                if data.get("role") in ("user", "assistant", "system"):
                    state.roles[row["id"]] = data["role"]
                try:
                    self._process_message(dict(row))
                except Exception as exc:
                    raise self._fail(state, f"message row {row['rid']}", exc) from exc
            state.cursors["message"] = row["rid"]

    def _message_completed(self, conn: sqlite3.Connection, message_id: str) -> bool:
        row = conn.execute("SELECT data FROM message WHERE id = ?", (message_id,)).fetchone()
        when = _as_dict(row["data"]).get("time") if row else None
        return isinstance(when, dict) and bool(when.get("completed"))

    def _capture_parts(self, conn: sqlite3.Connection, state: _CaptureState) -> None:
        rows = conn.execute(
            "SELECT rowid AS rid, id, message_id, session_id, time_created, data"
            " FROM part WHERE rowid > ? ORDER BY rowid",
            (state.cursors["part"],),
        ).fetchall()
        now_ms = int(time.time() * 1000)
        holding = False
        for row in rows:
            if row["session_id"] not in state.project_sessions:
                if not holding:
                    state.cursors["part"] = row["rid"]
                continue
            data = _as_dict(row["data"])
            final = part_is_final(data)
            abandoned = False
            if not final:
                age = now_ms - int(row["time_created"] or 0)
                if age > PART_ABANDON_MS or self._message_completed(conn, row["message_id"]):
                    abandoned = True
                else:
                    holding = True  # re-read this row next pass; later rows still flow
                    continue
            if row["id"] not in state.processed:
                try:
                    wrote = self._process_part(dict(row), data=data, abandoned=abandoned)
                except Exception as exc:
                    raise self._fail(state, f"part row {row['rid']}", exc) from exc
                state.processed.add(row["id"])
                state.captured += int(bool(wrote))
            if not holding:
                state.cursors["part"] = row["rid"]

    # -- row processing (raises on persistence failure) ----------------------

    def _process_new_session(self, session: dict) -> None:
        session_id = session["id"]
        self._persist_transcript_message(
            session_id=session_id,
            role="system",
            content=f"Session started: {session.get('title') or 'Untitled'}",
            metadata={
                "agent": "opencode",
                "session_slug": session.get("slug"),
                "directory": session.get("directory"),
                "model": session.get("model"),
                "agent_type": session.get("agent"),
                "parent_id": session.get("parent_id"),
            },
            source_id=f"session:{session_id}",
        )

    def _process_message(self, message: dict) -> None:
        """Message rows carry roles; content lives in parts (older formats inline it)."""
        message_data = _as_dict(message.get("data"))
        content = message_data.get("content", "")
        if not isinstance(content, str) or not content.strip():
            return
        if synthetic_mod.is_synthetic_memory_text(content):
            return
        session_id = message.get("session_id", "")
        if session_id:
            self._persist_transcript_message(
                session_id=session_id,
                role=message_data.get("role", "assistant"),
                content=content,
                metadata={"message_id": message["id"]},
                source_id=f"message:{message['id']}",
            )

    def _parent_message_role(self, message_id: str) -> str | None:
        try:
            with closing(self._opencode_conn()) as conn:
                row = conn.execute("SELECT data FROM message WHERE id = ?", (message_id,)).fetchone()
        except sqlite3.Error:
            return None
        role = _as_dict(row["data"]).get("role") if row else None
        return role if role in ("user", "assistant", "system") else None

    def _process_part(self, part: dict, data: dict | None = None, abandoned: bool = False) -> bool:
        """Persist one final part. Returns True when something was written."""
        part_data = data if data is not None else _as_dict(part.get("data"))
        part_type = part_data.get("type")
        session_id = part["session_id"]

        if part_type == "text":
            text_content = part_data.get("text", "")
            if not isinstance(text_content, str) or not text_content.strip():
                return False
            if synthetic_mod.is_synthetic_memory_text(text_content):
                return False  # TRACE-injected memory never becomes transcript
            role = self._role_for(part["message_id"])
            metadata = {"part_type": "text", "part_id": part["id"]}
            if abandoned:
                metadata["incomplete"] = True
            self._persist_transcript_message(
                session_id=session_id,
                role=role,
                content=text_content,
                metadata=metadata,
                source_id=f"part:{part['id']}",
            )
            return True

        if part_type == "tool" and not abandoned and self.config.capture_tool_calls:
            tool = canonical_memory_tool(part_data.get("tool"))
            state = part_data.get("state") if isinstance(part_data.get("state"), dict) else {}
            if tool and tool not in _SELF_PERSISTING_TOOLS and state.get("status") in ("completed", "error"):
                self._persist_memory_tool_event(
                    session_id=session_id,
                    tool_name=tool,
                    input_data=state.get("input"),
                    output_data=state.get("output"),
                    status=state.get("status"),
                    source_id=f"part:{part['id']}",
                )
                return True
        # step-start / step-finish / reasoning / file / other tools: status, not memory.
        return False

    def _role_for(self, message_id: str) -> str:
        roles = self._role_cache
        if message_id not in roles:
            roles[message_id] = self._parent_message_role(message_id) or "assistant"
        return roles[message_id]

    def _persist_transcript_message(
        self,
        session_id: str,
        role: str,
        content: str,
        metadata: dict,
        source_id: str | None = None,
    ) -> None:
        """Persist one transcript row idempotently. Raises on failure."""
        if self.config.redact_secrets:
            content = redact_mod.redact_text(content)
        content = content[: self.config.max_message_length]
        self._write(lambda conn, autocommit: transcript_mod.record_message(
            conn,
            autocommit=autocommit,
            session_id=session_id,
            role=role,
            content=content,
            metadata=metadata,
            source_id=source_id,
        ))

    def _persist_memory_tool_event(
        self,
        session_id: str,
        tool_name: str,
        input_data: object,
        output_data: object,
        status: str | None,
        source_id: str | None = None,
    ) -> None:
        """Record a completed memory-tool call as searchable capture activity."""
        inputs = input_data if isinstance(input_data, dict) else {}
        output_text = output_data if isinstance(output_data, str) else json.dumps(output_data, default=str)
        payload = {
            "tool": tool_name,
            "input": inputs,
            "tool_status": status,
            "output": redact_mod.redact_text(output_text or "")[:500],
            "session_id": session_id,
        }
        self._write(lambda conn, autocommit: episodic_mod.record_event(
            conn,
            autocommit=autocommit,
            type="observation",
            source="capture",
            repo=self.project_name,
            symbol=inputs.get("symbol") or inputs.get("name") or None,
            file=inputs.get("file") or None,
            payload=payload,
            source_id=source_id,
        ))

    def _write(self, fn):
        """Run ``fn(conn, autocommit)`` in the current pass's transaction, or standalone."""
        if self._batch_conn is not None:
            return fn(self._batch_conn, False)
        conn = store_mod.connect(self.trace_db_path)
        try:
            return fn(conn, True)
        finally:
            conn.close()

    def _sanitize_content(self, content: str) -> str:
        """Redact sensitive information from content (shared rules)."""
        return redact_mod.redact_text(content) if self.config.redact_secrets else content

    # -- monitor loop ----------------------------------------------------------

    def _status_payload(self, state: _CaptureState, status: str = "running") -> dict:
        return {
            "status": status,
            "pid": os.getpid(),
            "project": self.project_name,
            "project_path": str(self.project_path),
            "cursors": dict(state.cursors),
            "captured": state.captured,
            "errors": state.errors,
            "last_error": state.last_error,
            "updated_at": time.time(),
        }

    def _handle_signal(self, signum, frame):
        self._running = False

    def _run_monitor_loop(self, state: _CaptureState) -> None:
        """Poll until a stop request or signal. Caller holds the monitor lock."""
        self._running = True
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)
        self._log(
            f"Service started for project {self.project_name} (pid {os.getpid()}); "
            f"{len(state.project_sessions)} known sessions"
        )
        self._write_status(self._status_payload(state))
        failures = 0
        try:
            while self._running and not self._stop_file.exists():
                try:
                    changed = self.capture_pass(state)
                    failures = 0
                except CaptureError:
                    changed = True
                    failures += 1
                except sqlite3.Error as exc:
                    # OpenCode DB busy/unavailable: nothing captured, nothing lost.
                    changed = False
                    failures += 1
                    if failures == 1 or failures % 30 == 0:
                        self._log(f"OpenCode database unavailable: {type(exc).__name__}: {exc}")
                except Exception as exc:  # noqa: BLE001 - an unexpected error must not end capture
                    changed = False
                    failures += 1
                    self._fail(state, "capture pass", exc)
                if changed:
                    try:
                        self._save_cursors(state.cursors)
                    except Exception as exc:  # noqa: BLE001 - retried next pass
                        self._fail(state, "cursor save", exc)
                    self._write_status(self._status_payload(state))
                time.sleep(min(POLL_SECONDS * (2 ** min(failures, 5)), 30.0) if failures else POLL_SECONDS)
        finally:
            try:
                self._save_cursors(state.cursors)
            except Exception as exc:  # noqa: BLE001
                self._log(f"final cursor save failed: {type(exc).__name__}: {exc}")
            self._log(f"Service stopped for project {self.project_name}")
            self._remove_pid()
            self._write_status(self._status_payload(state, status="stopped"))

    def run_foreground(self) -> int:
        """Body of the detached child: take the lock, then monitor until stopped."""
        lock = LockHandle(self._lock_file)
        # Brief retry: a liveness probe may hold the lock for a moment; a real
        # second monitor holds it for its lifetime.
        if not lock.acquire(wait=LOCK_STARTUP_WAIT_SECONDS):
            self._log(f"another monitor already holds the lock; pid {os.getpid()} exiting")
            return 3
        try:
            self._stop_file.unlink(missing_ok=True)
            self._write_pid(os.getpid())
            try:
                state = self._prepare_state()
            except Exception as exc:  # noqa: BLE001 - reported, then exit
                self._log(f"startup failed: {type(exc).__name__}: {exc}")
                self._remove_pid()
                return 2
            self._run_monitor_loop(state)
            return 0
        finally:
            lock.release()

    def capture_once(self) -> dict:
        """One synchronous catch-up pass (no monitor running). Never raises."""
        lock = LockHandle(self._lock_file)
        if not lock.acquire(wait=LOCK_STARTUP_WAIT_SECONDS):
            return {"status": "busy", "message": "a monitor is already capturing"}
        try:
            state = self._prepare_state()
            try:
                self.capture_pass(state)
                result = {"status": "ok", "captured": state.captured}
            except CaptureError as exc:
                result = {"status": "error", "message": str(exc), "captured": state.captured}
            self._save_cursors(state.cursors)
            return result
        except Exception as exc:  # noqa: BLE001 - fail open for the caller
            self._log(f"catch-up failed: {type(exc).__name__}: {exc}")
            return {"status": "error", "message": f"{type(exc).__name__}: {exc}"}
        finally:
            lock.release()

    # -- lifecycle -------------------------------------------------------------

    def _spawn(self) -> subprocess.Popen:
        cmd = [
            sys.executable,
            "-m",
            "memory.opencode_service",
            "--project-path", str(self.project_path),
            "--project-name", self.project_name,
            "--trace-db", str(self.trace_db_path),
            "--opencode-db", str(self.opencode_db_path),
            "--action", "run",
        ]
        kwargs = dict(
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        if sys.platform != "win32":
            return subprocess.Popen(cmd, start_new_session=True, **kwargs)
        no_window = 0x08000000
        new_group = 0x00000200
        breakaway = 0x01000000
        try:
            return subprocess.Popen(cmd, creationflags=no_window | new_group | breakaway, **kwargs)
        except OSError:
            # Job objects may forbid breakaway; detach without it.
            return subprocess.Popen(cmd, creationflags=no_window | new_group, **kwargs)

    def start(self) -> dict:
        """Start the monitor and wait until it reports ready (single instance)."""
        if self.is_running():
            return {"status": "already_running", "pid": self._get_pid(), "message": "Service already running"}
        try:
            self._validate_schema()
        except OpenCodeSchemaError as e:
            return {"status": "error", "message": f"Schema validation failed: {e}"}
        try:
            self._prepare_state()  # schema + first-run cursors before the child starts
        except Exception as e:  # noqa: BLE001
            return {"status": "error", "message": f"TRACE database not ready: {e}"}
        self._disabled_file.unlink(missing_ok=True)
        self._stop_file.unlink(missing_ok=True)
        stale_pid = self._get_pid()  # is_running() said no monitor: any PID here is stale
        proc = self._spawn()
        self._last_proc = proc
        deadline = time.time() + START_TIMEOUT_SECONDS
        while time.time() < deadline:
            # The child writes its PID only after taking the lock, so a live
            # recorded PID that is not the stale one means the monitor is up.
            pid = self._get_pid()
            if pid and pid != stale_pid and process_is_alive(pid):
                return {"status": "started", "pid": pid, "message": "OpenCode capture service started"}
            if proc.poll() is not None:
                pid = self._monitor_pid()
                if pid:
                    return {"status": "already_running", "pid": pid, "message": "Service already running"}
                return {
                    "status": "error",
                    "message": f"capture service exited (code {proc.returncode}); see {self._service_dir / LOG_FILE}",
                }
            time.sleep(0.05)
        return {"status": "error", "message": f"capture service did not report ready; see {self._service_dir / LOG_FILE}"}

    def ensure_running(self) -> dict:
        """Catch up and (re)start capture unless the user stopped it. Never raises.

        Called at OpenCode session start, so capture recovers after a crash
        or reboot and the new session sees everything captured before it.
        """
        try:
            if self._disabled_file.exists():
                return {"status": "disabled"}
            if self.is_running():
                return {"status": "running", "pid": self._get_pid()}
            if not Path(self.opencode_db_path).exists():
                return {"status": "error", "message": "OpenCode database not found"}
            caught_up = self.capture_once()
            started = self.start()
            started["catch_up"] = caught_up
            return started
        except Exception as exc:  # noqa: BLE001 - fail open for the plugin
            self._log(f"ensure_running failed: {type(exc).__name__}: {exc}")
            return {"status": "error", "message": f"{type(exc).__name__}: {exc}"}

    def _is_pid_alive(self, pid: int) -> bool:
        """Check whether a PID is still alive. Never signals the process."""
        return process_is_alive(pid)

    def _wait_for_exit(self, pid: int, attempts: int = 50, interval: float = 0.1) -> bool:
        """Wait (bounded) for a PID to exit. Returns True when gone."""
        for _ in range(attempts):
            if not self._is_pid_alive(pid):
                return True
            time.sleep(interval)
        return not self._is_pid_alive(pid)

    def stop(self, disable: bool = True) -> dict:
        """Stop the monitor (graceful first). ``disable`` keeps it off until start()."""
        if disable and self._service_dir.exists():
            self._disabled_file.write_text("stopped by user\n")
        pid = self._monitor_pid()
        if not pid:
            # Never signal a PID that is not provably our monitor.
            self._remove_pid()
            return {"status": "not_running", "message": "Service not running"}
        self._stop_file.write_text("stop\n")
        attempts = int(STOP_TIMEOUT_SECONDS / 0.1)
        if not self._wait_for_exit(pid, attempts=attempts):
            try:
                if sys.platform == "win32":
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                                   capture_output=True, check=False)
                else:
                    os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
            if not self._wait_for_exit(pid):
                return {"status": "error", "message": f"Service process {pid} did not terminate"}
        self._stop_file.unlink(missing_ok=True)
        self._remove_pid()
        status = self._read_status() or {}
        status.update({"status": "stopped", "project": self.project_name})
        self._write_status(status)
        return {"status": "stopped", "message": "OpenCode capture service stopped"}

    def _monitor_pid(self) -> int | None:
        """PID of the live monitor, or None. Never signals any process.

        A PID file alone is not proof: after a crash or reboot the recorded
        PID can belong to an unrelated process. The monitor holds its lock for
        its whole life, so the PID counts only while that lock is held. The
        probe holds the lock for an instant; a starting monitor retries.
        """
        pid = self._get_pid()
        if not pid or not process_is_alive(pid):
            return None
        probe = LockHandle(self._lock_file)
        if probe.acquire():
            probe.release()
            return None  # nobody holds the monitor lock: the PID file is stale
        return pid

    def is_running(self) -> bool:
        """Check if service is running. Read-only: never signals the service."""
        if self._monitor_pid():
            return True
        if self._get_pid():
            self._remove_pid()  # stale: dead or reused PID
        return False

    def status(self) -> dict:
        """Service status; the PID file is authoritative, the status file adds health."""
        status = self._read_status() or {}
        health = {k: status.get(k) for k in ("captured", "errors", "last_error", "cursors", "updated_at") if k in status}
        if not self.is_running():
            return {"status": "stopped", "project": self.project_name, **health}
        status.pop("pid", None)
        return {
            **status,
            "status": "running",
            "pid": self._get_pid(),
            "project": self.project_name,
            "project_path": str(self.project_path),
        }

    def restart(self) -> dict:
        self.stop(disable=False)
        return self.start()


def get_service(
    project_path: Path,
    project_name: str | None = None,
    config: CaptureConfig | None = None,
    opencode_db_path: Path | None = None,
) -> OpenCodeCaptureService:
    """Factory: the capture service for a project's TRACE database."""
    project_path = project_path.resolve()
    return OpenCodeCaptureService(
        project_path=project_path,
        opencode_db_path=opencode_db_path or default_opencode_db_path(),
        trace_db_path=project_path / ".agent-memory" / "memory.db",
        project_name=project_name or project_path.name,
        config=config or CaptureConfig(),
    )


def main():
    """Entry point for the detached child and manual control."""
    import argparse

    parser = argparse.ArgumentParser(description="OpenCode Capture Service")
    parser.add_argument("--project-path", required=True, type=Path)
    parser.add_argument("--project-name", required=True)
    parser.add_argument("--trace-db", required=True, type=Path)
    parser.add_argument("--opencode-db", required=True, type=Path)
    parser.add_argument("--action", choices=["start", "stop", "status", "restart", "run"], default="start")
    args = parser.parse_args()

    service = OpenCodeCaptureService(
        project_path=args.project_path,
        opencode_db_path=args.opencode_db,
        trace_db_path=args.trace_db,
        project_name=args.project_name,
        config=CaptureConfig(),
    )
    if args.action == "run":
        raise SystemExit(service.run_foreground())
    actions = {
        "start": service.start,
        "stop": service.stop,
        "status": service.status,
        "restart": service.restart,
    }
    print(json.dumps(actions[args.action](), default=str))


if __name__ == "__main__":
    main()

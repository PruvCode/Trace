"""Regression tests: liveness probing must never signal the process.

The defect these guard: ``OpenCodeCaptureService.is_running()`` used
``os.kill(pid, 0)`` as an existence probe. That idiom is safe on POSIX, but
CPython implements ``os.kill`` on Windows via ``TerminateProcess`` for every
signal except CTRL_C_EVENT/CTRL_BREAK_EVENT, so signal 0 *terminated* the
target. Any read-only diagnostic -- ``trace service status``,
``trace monitor status``, ``trace service start`` -- therefore silently killed
the running capture service, which then lost its uncommitted cursors and
replayed rows on restart.

These tests are behavioural: they assert a live process survives being probed,
rather than asserting anything about the implementation's source text.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from memory import opencode_service as service_mod
from memory.opencode_service import (
    PID_FILE,
    STATUS_FILE,
    CaptureConfig,
    OpenCodeCaptureService,
    process_is_alive,
)

# Long enough that a probe cannot finish the process by accident.
_SLEEP_SCRIPT = "import time; time.sleep(120)"


@pytest.fixture()
def live_process():
    """A real, live child process that must survive liveness probing."""
    proc = subprocess.Popen([sys.executable, "-c", _SLEEP_SCRIPT])
    try:
        yield proc
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=30)


def _service(project: Path) -> OpenCodeCaptureService:
    """Build a service handle pointing at a disposable project directory."""
    project.mkdir(parents=True, exist_ok=True)
    return OpenCodeCaptureService(
        project_path=project,
        opencode_db_path=project / "opencode.db",
        trace_db_path=project / ".agent-memory" / "memory.db",
        project_name=project.name,
        config=CaptureConfig(),
    )


def _record_running_service(svc: OpenCodeCaptureService, pid: int) -> None:
    """Write the PID/status files and hold the monitor lock, as a started service does.

    A PID file alone no longer counts as a running service (a stale PID can
    belong to an unrelated process), so the simulated monitor also holds the
    lock for the rest of the test.
    """
    svc._service_dir.mkdir(parents=True, exist_ok=True)
    (svc._service_dir / PID_FILE).write_text(str(pid), encoding="utf-8")
    (svc._service_dir / STATUS_FILE).write_text(
        '{"status": "running", "pid": %d, "project": "x"}' % pid, encoding="utf-8"
    )
    lock = service_mod.LockHandle(svc._service_dir / service_mod.LOCK_FILE)
    assert lock.acquire()
    svc._test_monitor_lock = lock  # held until the service object is discarded


# --------------------------------------------------------------------------
# process_is_alive
# --------------------------------------------------------------------------


def test_process_is_alive_reports_live_process(live_process):
    assert process_is_alive(live_process.pid) is True


def test_process_is_alive_does_not_kill_the_process(live_process):
    """The core regression: probing must not terminate the target."""
    for _ in range(5):
        assert process_is_alive(live_process.pid) is True
    assert live_process.poll() is None, "liveness probe terminated the process"
    assert process_is_alive(live_process.pid) is True


def test_process_is_alive_reports_dead_process():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=30)
    assert process_is_alive(proc.pid) is False


@pytest.mark.parametrize("bad_pid", [0, -1, -12345])
def test_process_is_alive_rejects_invalid_pids(bad_pid):
    assert process_is_alive(bad_pid) is False


def test_process_is_alive_none_is_false():
    assert process_is_alive(None) is False  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# is_running / status must be read-only
# --------------------------------------------------------------------------


def test_is_running_does_not_kill_service(tmp_path, live_process):
    """Regression: `trace service status` used to kill the live service."""
    svc = _service(tmp_path / "proj")
    _record_running_service(svc, live_process.pid)

    assert svc.is_running() is True
    assert live_process.poll() is None, "is_running() terminated the service process"
    assert process_is_alive(live_process.pid) is True


def test_status_does_not_kill_service(tmp_path, live_process):
    svc = _service(tmp_path / "proj")
    _record_running_service(svc, live_process.pid)

    status = svc.status()
    assert status["status"] == "running"
    assert status["pid"] == live_process.pid
    assert live_process.poll() is None, "status() terminated the service process"
    assert process_is_alive(live_process.pid) is True


def test_repeated_status_queries_are_non_destructive(tmp_path, live_process):
    """A user polling status in a loop must not race the service to death."""
    svc = _service(tmp_path / "proj")
    _record_running_service(svc, live_process.pid)

    for _ in range(20):
        assert svc.status()["status"] == "running"
    assert live_process.poll() is None
    assert process_is_alive(live_process.pid) is True


def test_is_running_clears_stale_pid_file(tmp_path):
    """A dead PID must still be reported as stopped and its file cleaned up."""
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait(timeout=30)

    svc = _service(tmp_path / "proj")
    _record_running_service(svc, dead.pid)

    assert svc.is_running() is False
    assert not (svc._service_dir / PID_FILE).exists()
    assert svc.status()["status"] == "stopped"


def test_is_running_without_pid_file(tmp_path):
    svc = _service(tmp_path / "proj")
    assert svc.is_running() is False
    assert svc.status() == {"status": "stopped", "project": "proj"}


def test_is_running_with_unparseable_pid_file(tmp_path):
    svc = _service(tmp_path / "proj")
    svc._service_dir.mkdir(parents=True, exist_ok=True)
    (svc._service_dir / PID_FILE).write_text("not-a-pid", encoding="utf-8")
    assert svc.is_running() is False


# --------------------------------------------------------------------------
# _is_pid_alive shares the safe probe
# --------------------------------------------------------------------------


def test_wait_for_exit_returns_true_when_already_gone(tmp_path):
    svc = _service(tmp_path / "proj")
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait(timeout=30)
    assert svc._wait_for_exit(dead.pid) is True


def test_wait_for_exit_does_not_kill_live_process(tmp_path, live_process):
    svc = _service(tmp_path / "proj")
    started = time.perf_counter()
    # Bounded: attempts=2 so this returns fast instead of the default 5s.
    result = svc._is_pid_alive(live_process.pid)
    elapsed = time.perf_counter() - started
    assert result is True
    assert live_process.poll() is None
    # A subprocess-based probe (tasklist) costs ~0.4s per call.
    assert elapsed < 2.0, f"liveness probe too slow: {elapsed:.3f}s"
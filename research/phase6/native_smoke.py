"""Run real OpenCode against a loopback reply fixture and an installed TRACE wheel."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import threading
import time
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MARKER = "BRONZE_CACTUS_641"
REPLY = "TRACE_PHASE6_FIXTURE_REPLY"
READ_MARKER = "TRACE_PHASE6_READ_FIXTURE"
FOLLOWUP = "What was the release check keyword from the earlier session?"
AFTER_TEARDOWN = "Check whether TRACE is still available after teardown."


class Provider(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def send(self, body, content_type):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path != "/v1/models":
            self.send_error(404)
            return
        self.send(json.dumps({"object": "list", "data": [{"id": "replay", "object": "model"}]}).encode(),
                  "application/json")

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append(body)
        if self.server.wait_for_connection:
            for _ in range(24):
                if mcp_connected(self.server.log_file):
                    break
                time.sleep(0.25)
        header = {"id": "trace-fixture", "model": "replay", "created": 1}
        usage = {"prompt_tokens": 100, "completion_tokens": 6, "total_tokens": 106}
        delta, finish = {"role": "assistant", "content": REPLY}, "stop"
        if body.get("stream") and any(
            (FOLLOWUP in json.dumps(message) or AFTER_TEARDOWN in json.dumps(message))
            and message.get("role") == "user"
            for message in body.get("messages", [])
        ):
            tools = [tool.get("function", {}) for tool in body.get("tools", [])]
            evidence = next((tool["name"] for tool in tools
                             if "trace" in tool.get("name", "") and "get_evidence" in tool["name"]), None)
            call = None
            if evidence and self.server.evidence_probe and not self.server.sent_evidence:
                self.server.sent_evidence = True
                call = (evidence, self.server.evidence_probe, "trace_fixture_evidence")
            elif self.server.read_probe and not self.server.sent_read:
                for tool in tools:
                    fields = tool.get("parameters", {}).get("properties", {})
                    field = next((key for key in ("filePath", "path") if key in fields), None)
                    if tool.get("name") == "read" and field:
                        self.server.sent_read = True
                        call = ("read", {field: self.server.read_probe}, "trace_fixture_read")
                        break
            if call:
                tool_name, args, call_id = call
                delta = {"role": "assistant", "tool_calls": [{
                    "index": 0, "id": call_id, "type": "function",
                    "function": {"name": tool_name, "arguments": json.dumps(args)},
                }]}
                finish = "tool_calls"
        if body.get("stream"):
            header["object"] = "chat.completion.chunk"
            records = [
                {**header, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                {**header, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}], "usage": usage},
            ]
            wire = "".join(f"data: {json.dumps(row)}\n\n" for row in records) + "data: [DONE]\n\n"
            self.send(wire.encode(), "text/event-stream")
        else:
            self.send(json.dumps({**header, "object": "chat.completion", "usage": usage, "choices": [
                {"index": 0, "message": {"role": "assistant", "content": REPLY}, "finish_reason": "stop"},
            ]}).encode(), "application/json")


def mcp_connected(log_file):
    try:
        lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False
    return any('message="mcp connected"' in line and "server=trace-memory tools=5" in line for line in lines)


def profile(root, python):
    home = root / "home"
    home.mkdir()
    env = {k: os.environ[k] for k in ("SystemRoot", "WINDIR", "COMSPEC", "SYSTEMDRIVE", "PATHEXT")
           if k in os.environ}
    paths = [str(python.parent)]
    for name in ("git", "node", "rg"):
        binary = shutil.which(name)
        if binary:
            paths.append(str(Path(binary).parent))
    if "SystemRoot" in env:
        paths.extend((str(Path(env["SystemRoot"]) / "System32"), env["SystemRoot"]))
    env["PATH"] = os.pathsep.join(paths)
    for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_DATA_HOME",
                "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME", "TEMP", "TMP", "TMPDIR"):
        target = home / key.lower()
        target.mkdir()
        env[key] = str(target)
    env.update({"OPENCODE_DISABLE_AUTOUPDATE": "1", "OPENCODE_DISABLE_MODELS_FETCH": "1",
                "OPENCODE_DISABLE_LSP_DOWNLOAD": "1", "OPENCODE_DISABLE_SHARE": "1",
                "TRACE_FIXTURE_API_KEY": "local-test-placeholder", "PYTHONUTF8": "1",
                "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
    return env


def run(root, binary, python, major, managed=False, universal=False):
    project = root / "project"
    project.mkdir()
    (project / ".gitignore").write_text(".agent-memory/\n.trace-service/\n", encoding="utf-8")
    probe = project / "fixture.txt"
    probe.write_text(READ_MARKER, encoding="utf-8")
    env = profile(root, python)
    env["PATH"] = os.pathsep.join((str(binary.parent), env["PATH"]))
    config_path = project / "opencode.json"
    service_state = Path(env["XDG_STATE_HOME"]) / "opencode/service.json"

    def command(*args, timeout=150, check=True):
        result = subprocess.run(args, cwd=project, env=env, stdin=subprocess.DEVNULL,
                                capture_output=True, encoding="utf-8", errors="replace", timeout=timeout)
        if check and result.returncode:
            raise RuntimeError(f"{Path(args[0]).name} exited {result.returncode}\n"
                               f"{result.stdout[-2000:]}\n{result.stderr[-2000:]}")
        return result

    def cli(*args, **kwargs):
        return command(str(python), "-I", "-m", "trace_memory.cli", *args, **kwargs)

    version = command(str(binary), "--version").stdout.strip()
    assert version.lstrip("opencode v").startswith(f"{major}.")
    command(shutil.which("git"), "init", "--quiet")
    port = None
    managed_pid = None
    if managed:
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        command(str(binary), "service", "set", "port", str(port))
    with ThreadingHTTPServer(("127.0.0.1", 0), Provider) as server:
        server.requests = []
        server.read_probe = str(probe) if major == 2 else None
        server.sent_read = False
        server.sent_evidence = False
        server.evidence_probe = None
        server.wait_for_connection = False
        server.log_file = Path(env["XDG_DATA_HOME"]) / "opencode/log/opencode.log"
        address = f"http://127.0.0.1:{server.server_port}/v1"
        model = {"name": "TRACE loopback fixture", "limit": {"context": 8192, "output": 128}}
        if major == 1:
            providers = {"provider": {"tracefixture": {
                "npm": "@ai-sdk/openai-compatible", "name": "Local fixture",
                "options": {"baseURL": address, "apiKey": "local-test-placeholder"},
                "models": {"replay": model},
            }}, "enabled_providers": ["tracefixture"]}
        else:
            providers = {"providers": {"tracefixture": {
                "package": "@opencode/ai/providers/openai-compatible", "env": ["TRACE_FIXTURE_API_KEY"],
                "settings": {"baseURL": address},
                "models": {"replay": model},
            }}}
        config = {"model": "tracefixture/replay", **providers}
        config_path.write_text(json.dumps(config), encoding="utf-8")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def session(prompt):
            before = len(server.requests)
            flags = ["--standalone"] if major == 2 and not managed else []
            result = command(str(binary), "run", *flags, "--format", "json", "-m", "tracefixture/replay", prompt)
            requests = server.requests[before:]
            if not requests or REPLY not in result.stdout:
                raise RuntimeError(f"Native request or fixture reply missing: {result.stdout[-1000:]}")
            return requests

        try:
            session("Initialize this disposable OpenCode profile.")
            if managed:
                state = json.loads(service_state.read_text(encoding="utf-8"))
                assert state["url"].rstrip("/") == f"http://127.0.0.1:{port}"
                managed_pid = state["pid"]
            cli("init")
            if universal:
                setup = json.loads(cli("install", "--agent", "opencode").stdout)["opencode"]
            else:
                setup = json.loads(cli("setup", "opencode", "--transparent").stdout)
            assert setup["monitoring"]["status"] in ("started", "already_running"), setup["monitoring"]
            privacy = cli("privacy").stdout
            assert "OpenCode capture: running" in privacy and "Automatic recall: plugin installed" in privacy
            assert "Selected test recording: off" in privacy
            server.wait_for_connection = major == 2
            session(f"Remember this release check keyword for later: {MARKER}.")
            database = project / ".agent-memory" / "memory.db"
            captured = []
            for _ in range(28):
                with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
                    captured = db.execute("SELECT id FROM transcript_messages WHERE content LIKE ?",
                                          (f"%{MARKER}%",)).fetchall()
                if captured:
                    break
                time.sleep(0.25)
            assert len(captured) == 1, "historical transcript not captured exactly once"
            assert MARKER in cli("preview").stdout
            task = json.loads(cli("task", "create", "--title", "Release fixture").stdout)["id"]
            cli("record", "--type", "observation", "--task", task,
                "--message", "fixture evidence", "--transcript-evidence", str(captured[0][0]))
            with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
                reference = db.execute("SELECT reference FROM event_evidence").fetchone()[0]
            server.evidence_probe = {"reference": reference, "task_id": task}
            evidence = session(FOLLOWUP)
            messages = [message for request in evidence for message in request["messages"]]
            memory = [message for message in messages
                      if MARKER in json.dumps(message) and "[TRACE PROJECT MEMORY" in json.dumps(message)]
            assert memory and all(message["role"] == "user" for message in memory), "TRACE memory missing"
            tools = [str(tool.get("function", {}).get("name") or tool.get("name", ""))
                     for request in evidence for tool in request.get("tools", [])]
            visible = any("trace" in name and "get_evidence" in name for name in tools)
            connected = visible or mcp_connected(server.log_file)
            tool_result = any(message.get("role") == "tool" and MARKER in json.dumps(message) for message in messages)
            server.wait_for_connection = False
            empty_project = root / "empty-project"
            empty_project.mkdir()
            assert cli("preview", "--project", str(empty_project)).stdout.strip() == \
                "No automatic memory block would be included."
            paused = json.loads(cli("service", "stop").stdout)
            assert paused["status"] == "stopped", paused
            assert "OpenCode capture: paused" in cli("privacy").stdout
            assert MARKER in cli("preview").stdout
            stopped = json.loads(cli("teardown", "opencode").stdout)
            assert stopped["service"]["status"] in ("stopped", "not_running")
            after = json.loads(config_path.read_text(encoding="utf-8"))
            assert {key: value for key, value in after.items() if key != "$schema"} == config
            assert database.is_file()
            assert "Automatic recall: disabled" in cli("privacy").stdout
            assert cli("preview").stdout.strip() == "No automatic memory block would be included."
            backup = root / "private-snapshot.db"
            cli("backup", str(backup))
            assert cli("backup", str(backup), check=False).returncode == 2
            restored = root / "restore-drill" / ".agent-memory" / "memory.db"
            restored.parent.mkdir(parents=True)
            shutil.copyfile(backup, restored)
            with closing(sqlite3.connect(restored.as_uri() + "?mode=ro", uri=True)) as db:
                assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            assert MARKER in cli("preview", "--project", str(restored.parent.parent)).stdout
            server.sent_evidence = False
            server.read_probe = None
            after_requests = session(AFTER_TEARDOWN)
            assert all("[TRACE PROJECT MEMORY" not in json.dumps(r) and MARKER not in json.dumps(r)
                       for r in after_requests)
            if server.sent_evidence:
                assert any(
                    message.get("role") == "tool" and "integration is disabled" in json.dumps(message)
                    for request in after_requests for message in request["messages"]
                )
            if managed:
                assert json.loads(service_state.read_text(encoding="utf-8"))["pid"] == managed_pid
            return {"version": version, "mode": "managed" if managed else ("standalone" if major == 2 else "native"),
                    "native_capture": True, "user_channel_injection": True,
                    "mcp_connected": connected, "mcp_evidence_tool_visible": visible,
                    "native_evidence_round_trip": tool_result, "teardown_verified": True,
                    "privacy_preview_verified": True, "backup_restore_verified": True,
                    "same_managed_process_after_teardown": True if managed else None,
                    "post_teardown_evidence_attempted": server.sent_evidence,
                    "second_request_read_fixture": any(READ_MARKER in json.dumps(req) for req in evidence),
                    "synthetic_loopback_requests": len(server.requests)}
        finally:
            try:
                cli("teardown", "opencode", check=False)
            finally:
                if managed and service_state.is_file():
                    state = json.loads(service_state.read_text(encoding="utf-8"))
                    if state["url"].rstrip("/") == f"http://127.0.0.1:{port}":
                        command(str(binary), "service", "stop")
                server.shutdown()
                thread.join(timeout=3)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scratch", type=Path, required=True, help="new disposable directory")
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True, help="Python in an isolated TRACE wheel environment")
    parser.add_argument("--major", type=int, choices=(1, 2), required=True)
    parser.add_argument("--managed", action="store_true", help="use an isolated OpenCode 2.x managed service")
    parser.add_argument("--universal", action="store_true", help="configure OpenCode through trace install")
    args = parser.parse_args()
    if args.managed and args.major != 2:
        parser.error("--managed requires --major 2")
    root = args.scratch.resolve()
    root.mkdir()
    print(json.dumps(run(root, args.binary.resolve(strict=True), args.python.resolve(strict=True),
                         args.major, args.managed, args.universal)))


if __name__ == "__main__":
    main()

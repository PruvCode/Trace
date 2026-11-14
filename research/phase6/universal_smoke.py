"""Verify real Codex and Claude Code hooks with a local, synthetic model provider."""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
import threading
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from research.phase6.native_smoke import profile

MARKER = "TRACE_LIGHTHOUSE_COBALT_702"
CLAUDE_MARKER = "TRACE_LIGHTHOUSE_IRIDIUM_409"
REPLY = "TRACE smoke assistant reply"


class Provider(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append((self.path, body))
        if self.path.startswith("/v1/responses"):
            response = {"id": "resp_fixture", "object": "response", "created_at": 1, "status": "completed",
                        "model": "gpt-5.4", "usage": {"input_tokens": 10, "output_tokens": 6, "total_tokens": 16}}
            item = {"id": "msg_fixture", "type": "message", "status": "completed", "role": "assistant",
                    "content": [{"type": "output_text", "text": REPLY, "annotations": []}]}
            stream = [
                {"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
                {"type": "response.output_item.added", "output_index": 0, "item": item},
                {"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "delta": REPLY},
                {"type": "response.output_item.done", "output_index": 0, "item": item},
                {"type": "response.completed", "response": {**response, "output": [item]}},
            ]
        elif self.path.startswith("/v1/messages"):
            message = {"id": "msg_fixture", "type": "message", "role": "assistant",
                       "model": body["model"], "content": [], "stop_reason": None, "stop_sequence": None,
                       "usage": {"input_tokens": 10, "output_tokens": 0}}
            stream = [
                {"type": "message_start", "message": message},
                {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": REPLY}},
                {"type": "content_block_stop", "index": 0},
                {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                 "usage": {"output_tokens": 6}},
                {"type": "message_stop"},
            ]
        else:
            self.send_error(404)
            return
        wire = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in stream).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(wire)))
        self.end_headers()
        self.wfile.write(wire)


def run(root: Path, python: Path, codex: Path, claude: Path) -> dict:
    project = root / "shared workspace"
    project.mkdir()
    env = profile(root, python)

    def command(*args):
        result = subprocess.run(args, cwd=project, env=env, stdin=subprocess.DEVNULL, capture_output=True,
                                encoding="utf-8", errors="replace", timeout=70)
        if result.returncode:
            raise RuntimeError(f"{Path(args[0]).name} returned {result.returncode}\n"
                               f"{result.stdout[-3000:]}\n{result.stderr[-3000:]}")
        return result.stdout

    def trace(*args):
        return command(str(python), "-I", "-m", "trace_memory.cli", *args)

    command(shutil.which("git"), "init", "--quiet")
    trace("install", "--agent", "codex", "--agent", "claude")
    with ThreadingHTTPServer(("127.0.0.1", 0), Provider) as server:
        server.requests = []
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            codex_home = root / "codex"
            codex_home.mkdir()
            env.update(CODEX_HOME=str(codex_home), TRACE_LOOPBACK_KEY="fixture-only")
            (codex_home / "config.toml").write_text(
                'model = "gpt-5.4"\nmodel_provider = "loopback"\n'
                '[model_providers.loopback]\nname = "TRACE loopback fixture"\n'
                f'base_url = "{base}/v1"\nenv_key = "TRACE_LOOPBACK_KEY"\n'
                'request_max_retries = 0\nstream_max_retries = 0\n'
                f'[projects.{json.dumps(str(project), ensure_ascii=False)}]\ntrust_level = "trusted"\n',
                encoding="utf-8",
            )
            # This invocation has only two freshly generated TRACE hooks and a disposable profile.
            codex_args = (str(codex), "exec", "--json", "--ephemeral", "--sandbox", "read-only",
                          "--dangerously-bypass-hook-trust")
            response = command(*codex_args, f"Remember lighthouse deployment code {MARKER}.")
            assert REPLY in response, response[-3000:]
            env.update(ANTHROPIC_BASE_URL=base, ANTHROPIC_API_KEY="fixture-only",
                       CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1", CLAUDE_CONFIG_DIR=str(root / "claude"))
            response = command(str(claude), "-p", "--tools", "", "--setting-sources", "local",
                               "--model", "claude-sonnet-4-6",
                               f"Recall the lighthouse deployment code. Remember the fallback {CLAUDE_MARKER}.")
            assert REPLY in response, response[-3000:]
            claude_requests = [body for path, body in server.requests if path.startswith("/v1/messages")]
            assert any(MARKER in json.dumps(body) for body in claude_requests), "Claude did not receive Codex memory"
            previous_requests = len(server.requests)
            response = command(*codex_args, "Recall the lighthouse deployment fallback code.")
            assert REPLY in response, response[-3000:]
            assert any(CLAUDE_MARKER in json.dumps(body) for path, body in server.requests[previous_requests:]
                       if path.startswith("/v1/responses")), "Codex did not receive Claude memory"
        finally:
            server.shutdown()
            thread.join()
    db = project / ".agent-memory" / "memory.db"
    with closing(sqlite3.connect(db)) as conn:
        rows = conn.execute("SELECT session_id, role, content FROM transcript_messages ORDER BY id").fetchall()
    for host in ("codex", "claude"):
        assert any(session.startswith(host + ":") and role == "assistant" and REPLY in text
                   for session, role, text in rows), rows
    trace("uninstall", "--all")
    return {"codex": command(str(codex), "--version").strip(), "claude": command(str(claude), "--version").strip(),
            "captured_messages": len(rows), "codex_to_claude_recall": True, "claude_to_codex_recall": True}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--codex", type=Path, required=True)
    parser.add_argument("--claude", type=Path, required=True)
    parser.add_argument("--scratch", type=Path, required=True)
    args = parser.parse_args()
    root = args.scratch.resolve()
    root.mkdir(parents=True)
    print(json.dumps(run(root, args.python.resolve(), args.codex.resolve(), args.claude.resolve())))


if __name__ == "__main__":
    main()

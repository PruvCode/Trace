"""Typed CLI and stdio search agree on relevance and active history."""

import asyncio
import json
import sys
from contextlib import closing

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from memory import episodic, session_context, store, task_library, transcript
from trace_memory import cli, project


def test_context_retrieves_rollout_with_provenance(tmp_path, capsys):
    project.init_project(tmp_path)
    capsys.readouterr()
    with closing(store.connect(project.db_path(tmp_path))) as conn:
        msg = transcript.record_message(conn, session_id="one", role="user", content="rollout discussion")
        event = episodic.record_event(
            conn, type="decision", repo="sample", payload={"message": "Rollout keeps thirty minutes"},
            evidence=[{"type": "transcript", "message_id": msg}],
        )
    assert cli.main(["events", str(tmp_path), "--query", "rollout"]) == 0
    assert json.loads(capsys.readouterr().out)["id"] == event
    assert cli.main(["context", "rollout", "--project", str(tmp_path)]) == 0
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert rows[0]["kind"] == "event" and rows[0]["id"] == event
    assert rows[0]["source"] == "agent" and rows[0]["timestamp"] and rows[0]["evidence_refs"]
    assert rows[1]["kind"] == "transcript" and rows[1]["id"] == msg


def test_natural_rank_strict_controls_and_mcp_parity(tmp_path):
    database = project.db_path(tmp_path)
    project.init_project(tmp_path)
    query = "session timeout bug"
    with closing(store.connect(database)) as conn:
        hidden = task_library.create_task(conn, "Private sibling work")["id"]
        def record(text, **opts):
            return episodic.record_event(conn, type="decision", repo="demo", payload={"message": text}, **opts)

        retired = record("session timeout bug obsolete")
        episodic.record_event(conn, type="decision", repo="demo", payload={"message": "superseding fact",
                                                                          "supersedes": retired})
        record("session timeout bug other task", task_id=hidden)
        record("display rendering bug")
        record("timeout for sessions")
        record("session timeout bug exact")
        for text in ("bug in rendering", "timeout for sessions", "session timeout bug transcript"):
            transcript.record_message(conn, session_id="one", role="user", content=text)
        transcript.record_message(conn, session_id="one", role="user", content="the codename is ZEPHYR")
        assert [item.content for item in transcript.search_messages(conn, query="Find ZEPHYR 🧪", natural=True)] == [
            "the codename is ZEPHYR",
        ]
        assert [item.content for item in transcript.search_messages(conn, query=query, natural=True)] == [
            "session timeout bug transcript", "timeout for sessions",
        ]
        assert len(episodic.search_events(conn, query="bug AND rendering", natural=True)) == 1
        assert (episodic.search_events(conn, query="session_timeout", natural=True)
                == episodic.search_events(conn, query="session_timeout"))
        context = session_context.search_context(conn, query)
        assert len(context) == 3
        assert {row["content"] for row in context[:2]} == {"session timeout bug exact", "session timeout bug transcript"}
        assert context[-1]["content"] == "timeout for sessions"
        for index in range(20):
            record(f"timeout for sessions detail {index} " + "z" * 500)
        bounded = session_context.search_context(conn, query, limit=50)
        assert sum(len(json.dumps(row)) + 1 for row in bounded) <= session_context.MAX_CHARS

    async def call():
        params = StdioServerParameters(command=sys.executable, args=[
            "-m", "trace_memory.native", "mcp", "mcp", str(tmp_path),
        ])
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                result = await session.call_tool("search_context", {"query": query, "limit": 50})
                assert not result.is_error
                assert result.structured_content["result"] == bounded
                events = await session.call_tool("search_events", {"query": query})
                assert events.structured_content["result"] == project.search_project_events(tmp_path, query=query)
                messages = await session.call_tool("search_transcripts", {"query": query})
                assert messages.structured_content["result"] == project.search_transcript_messages(tmp_path, query=query)

    asyncio.run(asyncio.wait_for(call(), timeout=40))

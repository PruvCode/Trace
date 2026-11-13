import json
import os
import shutil
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from memory import episodic, evidence, opencode_plugin, session_context, store, synthetic, task_library, transcript
from trace_memory.cli import main


@pytest.fixture
def memory(tmp_path):
    project = tmp_path / "project"
    db = project / ".agent-memory" / "memory.db"
    with closing(store.connect(db)) as conn:
        store.init_schema(conn)
        yield project, db, conn


def event(conn, task_id, text, **options):
    return episodic.record_event(
        conn, type="observation", repo="fixture", task_id=task_id, payload={"message": text}, **options,
    )


def bind(conn, task_id, session_id):
    transcript.record_message(
        conn, role="system", session_id=session_id, source_id=f"session:{session_id}",
        content="Session started", metadata={"agent": "opencode"},
    )
    task_library.bind_session(conn, task_id, session_id)


def framed_record(text):
    lines = text.splitlines()
    assert lines[0] == synthetic.SYNTHETIC_NOTICE and len(lines) == 4
    return json.loads(lines[2])


def test_bound_context_prefers_current_task_and_only_falls_back_to_unassigned(memory, capsys):
    project, db, conn = memory
    auth = task_library.create_task(conn, "Login timeout")["id"]
    other = task_library.create_task(conn, "Other timeout")["id"]
    empty = task_library.create_task(conn, "Empty timeout")["id"]
    bind(conn, auth, "ses_login")
    bind(conn, other, "ses_other")
    bind(conn, empty, "ses_empty")
    raw = transcript.record_message(conn, role="assistant", session_id="ses_login",
                                    content="timeout full source body")
    transcript.record_message(conn, role="assistant", session_id="ses_other",
                              content="timeout unrelated bound transcript")
    transcript.record_message(conn, role="user", session_id="ses_general",
                              content="timeout general transcript")
    earlier = event(conn, auth, "timeout old theory", evidence=[{"type": "transcript", "message_id": raw}])
    current = episodic.record_event(
        conn, type="decision", repo="fixture", task_id=auth,
        payload={"message": "timeout current decision", "supersedes": earlier},
        evidence=[{"type": "transcript", "message_id": raw}],
    )
    episodic.record_event(
        conn, type="observation", repo="fixture", source="capture", task_id=auth,
        payload={"message": "pytest timeout outcome", "kind": "test_outcome"},
        evidence=[{"type": "transcript", "message_id": raw}],
    )
    for index in range(51):
        event(conn, other, f"timeout unrelated task {index}")
    general = event(conn, None, "timeout shared finding")
    bound = session_context.get_session_start_context(db, exclude_session_id="ses_login", query="timeout")
    text = synthetic.render_user_block(bound["items"])
    assert len(text) <= session_context.MAX_TASK_CHARS
    assert current in {i["event_id"] for i in bound["items"]}
    assert "pytest timeout outcome" in text
    assert all(phrase not in text for phrase in ("unrelated", "old theory", "shared finding", "full source body"))
    fallback = session_context.get_session_start_context(db, exclude_session_id="ses_empty", query="timeout")
    assert fallback["stats"]["task_fallback"]
    assert [i["event_id"] for i in fallback["items"]] == [general]
    assert "unassigned fallback" in fallback["text"]
    unbound = session_context.get_session_start_context(db, exclude_session_id="unknown", query="timeout")
    assert "general transcript" in unbound["text"]
    assert "unrelated bound transcript" not in unbound["text"] and "current decision" not in unbound["text"]
    assert main(["context", "timeout", "--task", auth, "--project", str(project)]) == 0
    assert "current decision" in capsys.readouterr().out



def test_bound_context_keeps_prior_decision_with_findings_and_excludes_other_sessions(memory):
    _, db, conn = memory
    task_id = task_library.create_task(conn, "Retry")["id"]
    other = task_library.create_task(conn, "Another retry")["id"]
    bind(conn, task_id, "prior")
    bind(conn, task_id, "current")
    bind(conn, other, "foreign")
    transcript.record_message(conn, session_id="prior", role="user",
                              content="The retry backoff starts at 0.25 seconds.")
    transcript.record_message(conn, session_id="current", role="user",
                              content="retry backoff current-session-only")
    transcript.record_message(conn, session_id="unassigned", role="user",
                              content="retry backoff unassigned-session-only")
    for i in range(55):
        transcript.record_message(conn, session_id="foreign", role="user",
                                  content=f"retry backoff retry backoff other-task-{i}")
    raw = transcript.record_message(conn, session_id="prior", role="tool", content="pytest output")
    eid = event(conn, task_id, "pytest backoff outcome", evidence=[{"type": "transcript", "message_id": raw}])
    for i in range(6):
        event(conn, task_id, f"retry backoff additional finding {i}")

    result = session_context.get_session_start_context(
        db, exclude_session_id="current", query="retry backoff", max_items=2, max_chars=1200,
    )
    text = synthetic.render_user_block(result["items"])
    assert 0 < len(text) <= 1200 and len(result["items"]) == 2
    assert "0.25 seconds" in text and "pytest backoff outcome" in text
    assert any(item.get("event_id") == eid and item["evidence"] for item in result["items"])
    assert all(marker not in text for marker in ("current-session-only", "unassigned-session-only", "other-task-"))
    assert result["stats"]["prior_sessions"] == ["prior"] and not result["stats"].get("task_fallback")


def test_bound_context_uses_matching_transcript_before_unassigned_fallback(memory):
    _, db, conn = memory
    task_id = task_library.create_task(conn, "Retry")["id"]
    bind(conn, task_id, "prior")
    transcript.record_message(conn, session_id="prior", role="user", content="retry decision starts at 0.25")
    event(conn, None, "retry general finding")
    result = session_context.get_session_start_context(db, task_id=task_id, query="retry decision")
    assert "starts at 0.25" in result["text"]
    assert "general finding" not in result["text"] and not result["stats"].get("task_fallback")


def test_file_context_returns_only_requested_task_and_file(memory, capsys):
    project, db, conn = memory
    task = task_library.create_task(conn, "Events navigation")["id"]
    other = task_library.create_task(conn, "Other task")["id"]
    bind(conn, task, "prior")
    transcript.record_message(conn, session_id="prior", role="user", content="Archive transcript outside file scope")
    event(conn, task, "Archive current rule", file=r"pages\archive.astro")
    event(conn, task, "Archive sibling rule", file="pages/current.astro")
    event(conn, other, "Archive foreign rule", file="pages/archive.astro")
    event(conn, None, "Archive general rule", file="pages/archive.astro")

    result = session_context.get_session_start_context(db, task_id=task, query="Archive", file="pages/archive.astro")
    assert [item["text"] for item in result["items"]] == ["Archive current rule"]
    assert session_context.get_session_start_context(db, task_id=task, query="Archive", file="missing.astro")["text"] == ""
    assert main(["context", "Archive", "--task", task, "--file", r"pages\archive.astro", "--project", str(project)]) == 0
    text = capsys.readouterr().out
    assert "Archive current rule" in text and "Archive sibling rule" not in text


def test_matching_old_decision_survives_a_full_shortlist(memory):
    _, db, conn = memory
    target = task_library.create_task(conn, "Ledger")["id"]
    other = task_library.create_task(conn, "Separate ledger")["id"]
    bind(conn, target, "current")
    bind(conn, target, "history")
    bind(conn, other, "foreign")
    with conn:
        for session, detail in (("history", "approved window is 23 minutes"),
                                ("foreign", "approved window is 99 minutes")):
            transcript.record_message(conn, autocommit=False, session_id=session, role="user",
                                      content=f"Ledger review validation: {detail}.")
        for _ in range(store.MAX_LIMIT + 1):
            transcript.record_message(conn, autocommit=False, session_id="history", role="assistant",
                                      content="Ledger review validation pending.")
    context = session_context.get_session_start_context(db, exclude_session_id="current",
                                                        query="ledger review validation")
    assert "23 minutes" in context["text"]
    assert "99 minutes" not in context["text"]


def test_evidence_reference_pages_are_bounded_redacted_and_project_scoped(memory, tmp_path, capsys):
    project, _, conn = memory
    task = task_library.create_task(conn, "Linked task")["id"]
    other = task_library.create_task(conn, "Different task")["id"]
    raw = transcript.record_message(conn, role="user", session_id="ses_a", content="placeholder")
    secret = "ghp_" + "Ab9Z" * 9
    text = f"Legacy {secret}\n" + ('code "quoted" \\ fragment \U0001f9ea\n' * 140)
    with conn:
        conn.execute("UPDATE transcript_messages SET content = ? WHERE id = ?", (text, raw))
    first = event(conn, task, "prior", evidence=[{"type": "transcript", "message_id": raw}])
    replace = episodic.record_event(conn, type="decision", repo="fixture", task_id=task,
                                    payload={"supersedes": first, "message": "replacement"})
    ref = episodic.search_events(conn, task_id=task)[0].evidence_refs[0]
    page = evidence.expand(conn, project, ref, task_id=task)
    record = framed_record(page)
    assert len(page) <= evidence.MAX_RENDERED_CHARS and secret not in page
    assert record["lifecycle"] == "superseded" and record["superseded_by"] == replace
    assert record["next_offset"] and record["excerpt"].startswith("Legacy")
    second = evidence.expand(conn, project, ref, offset=record["next_offset"])
    assert len(second) <= evidence.MAX_RENDERED_CHARS and framed_record(second)["offset"] == record["next_offset"]
    with pytest.raises(ValueError, match="unavailable"):
        evidence.expand(conn, project, ref, task_id=other)
    with closing(store.connect(tmp_path / "other.db")) as foreign:
        store.init_schema(foreign)
        with pytest.raises(ValueError, match="unavailable"):
            evidence.expand(foreign, tmp_path, ref)
    assert main(["evidence", ref, "--task", task, "--project", str(project)]) == 0
    assert framed_record(capsys.readouterr().out)["reference"] == ref
    with conn:
        conn.execute("UPDATE transcript_messages SET content = ? WHERE id = ?", ("x" * (64 * 1024 + 1), raw))
    assert framed_record(evidence.expand(conn, project, ref))["status"] == "too_large"


def test_git_evidence_uses_original_blob_and_labels_dirty_renamed_and_switched_files(memory):
    project, db, conn = memory
    def git(*args):
        return subprocess.check_output(
            ["git", "-c", "user.name=Trace Fixture", "-c", "user.email=fixture@example.invalid",
             "-c", "commit.gpgsign=false", "-c", "core.hooksPath=missing-hooks", *args],
            cwd=project, text=True,
        ).strip()
    git("init", "-q", "-b", "initial")
    path = project / "settings.py"
    path.write_bytes(b"TIMEOUT = 30\n")
    git("add", path.name)
    git("commit", "-qm", "initial timeout")
    revision = git("rev-parse", "HEAD")
    task = task_library.create_task(conn, "Timeout")["id"]
    event(conn, task, "timeout change", project_root=project,
          evidence=[{"type": "git", "commit": revision, "path": path.name}])
    ref = episodic.search_events(conn, task_id=task)[0].evidence_refs[0]

    def label():
        return session_context.get_session_start_context(db, task_id=task, query="timeout")["items"][0]["freshness"]

    assert label() == "current"
    path.write_bytes(b"TIMEOUT = 30\r\n")
    assert label() == "current"
    path.write_bytes(b"TIMEOUT = 45\n")
    assert label() == "stale"
    opened = framed_record(evidence.expand(conn, project, ref))
    assert opened["freshness"] == "stale" and opened["excerpt"] == "TIMEOUT = 30\n"
    git("switch", "-qc", "alternate")
    git("add", path.name)
    git("commit", "-qm", "alternate timeout")
    assert label() == "stale"
    git("switch", "-q", "initial")
    assert label() == "current"
    path.rename(project / "renamed.py")
    assert label() == "stale"


def test_phase2_evidence_upgrade_assigns_references_once(memory):
    _, _, conn = memory
    task = task_library.create_task(conn, "Existing")["id"]
    msg = transcript.record_message(conn, role="user", session_id="s", content="old evidence")
    old = event(conn, task, "existing finding", evidence=[{"type": "transcript", "message_id": msg}])
    conn.executescript("""
        DROP TRIGGER assign_evidence_reference;
        DROP INDEX idx_evidence_reference;
        ALTER TABLE event_evidence DROP COLUMN reference;
    """)
    store.ensure_evidence_schema(conn)
    (record,) = episodic.search_events(conn, task_id=task)
    assert record.id == old and record.evidence == [{"type": "transcript", "message_id": msg}]
    store.init_schema(conn)
    assert episodic.search_events(conn, task_id=task)[0].evidence_refs == record.evidence_refs
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
@pytest.mark.parametrize("layout", ["v1", "v2"])
def test_live_binding_refreshes_cached_context_in_both_plugin_adapters(memory, tmp_path, layout):
    project, _, conn = memory
    task = task_library.create_task(conn, "Timeout")["id"]
    transcript.record_message(
        conn, role="system", session_id="ses_now", source_id="session:ses_now", content="Session started",
    )
    event(conn, task, "timeout scoped finding")
    event(conn, None, "timeout general finding")
    conn.close()
    opencode_plugin.install_plugin(project, tmp_path / "synthetic-opencode.db")
    if layout == "v1":
        source = opencode_plugin.plugin_path(project)
        plugin = source.with_suffix(".mjs")
    else:
        source = plugin = opencode_plugin.plugin_v2_dir(project) / "index.js"
    plugin.write_text(source.read_text(encoding="utf-8").replace('"--ensure-capture",', ""), encoding="utf-8")
    driver = tmp_path / "driver.mjs"
    driver.write_text("""
import { spawnSync } from "node:child_process";
import { pathToFileURL } from "node:url";
const [layout, pluginPath, project, python, task] = process.argv.slice(2);
globalThis.Bun = { spawnSync: (cmd, opts) => {
  const proc = spawnSync(cmd[0], cmd.slice(1), { cwd: opts.cwd, input: opts.stdin });
  return { stdout: proc.stdout, stderr: proc.stderr, exitCode: proc.status };
} };
const plugin = await import(pathToFileURL(pluginPath).href);
let call;
if (layout === "v1") {
  const hooks = await plugin.TraceMemoryPlugin({directory: project});
  call = async () => {
    const output = {messages:[{info:{role:"user",sessionID:"ses_now"},parts:[{type:"text",text:"timeout"}]}]};
    await hooks["experimental.chat.messages.transform"]({},output);
    return output.messages[0].parts[0].text;
  };
} else {
  let hook;
  await plugin.default.setup({session:{hook:async (_, fn) => {hook=fn;}}});
  call = async () => {
    const event = {sessionID:"ses_now",messages:[{role:"user",content:[{type:"text",text:"timeout"}]}]};
    await hook(event);
    return event.messages[0].content[0].text;
  };
}
const before = await call();
const bind = spawnSync(python, ["-P","-m","trace_memory.cli","task","bind-session",task,"ses_now","--project",project]);
if (bind.status !== 0) throw new Error(bind.stderr.toString());
const after = await call();
function cli(...args) {
  const proc = spawnSync(python, ["-P", "-m", "trace_memory.cli", ...args]);
  if (proc.status !== 0) throw new Error(proc.stderr.toString());
}
cli("reset", project, "--yes");
const disabled = await call();
cli("init", project);
cli("record", project, "--type", "decision", "--message", "timeout replacement finding");
const beforeSetup = await call();
cli("setup", "opencode", "--transparent", "--project", project);
const reenabled = await call();
console.log(JSON.stringify([before, after, disabled, beforeSetup, reenabled]));
""", encoding="utf-8")
    root = Path(__file__).resolve().parents[2]
    run = subprocess.run(
        ["node", str(driver), layout, str(plugin), str(project), sys.executable, task],
        env={**os.environ, "PYTHONPATH": str(root), "TRACE_TASK_AWARE": "1",
             "XDG_DATA_HOME": str(tmp_path / "isolated-opencode")},
        capture_output=True, encoding="utf-8", timeout=65, check=False,
    )
    assert run.returncode == 0, run.stderr
    before, after, disabled, before_setup, reenabled = json.loads(run.stdout)
    assert "general finding" in before and "scoped finding" not in before
    assert "scoped finding" in after and "general finding" not in after
    assert disabled == before_setup == "timeout"
    assert "replacement finding" in reenabled and "scoped finding" not in reenabled

from contextlib import closing
import json
import subprocess

import pytest

from memory import entity_graph, entity_history, episodic, evidence, session_context, store, task_library, transcript
from trace_memory import cli, obsidian_graph, project


def test_graph_history_joins_canonical_records_without_exporting_contents(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "service.py"
    source.write_text("def refresh():\n    return 1\n", encoding="utf-8")
    other = repo / "other.py"
    other.write_text("def refresh():\n    return 2\n", encoding="utf-8")
    vault = tmp_path / "vault"
    vault.mkdir()

    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), "-c", "user.email=test@example.invalid",
                                        "-c", "user.name=Fixture", "-c", "commit.gpgsign=false", *args],
                                       text=True).strip()

    git("init", "-q")
    git("add", "service.py", "other.py")
    git("commit", "-qm", "Fixture")
    sha = git("rev-parse", "HEAD")
    project.init_project(repo)
    with closing(store.connect(project.db_path(repo))) as conn:
        task = task_library.create_task(conn, "Refresh")["id"]
        message = transcript.record_message(conn, session_id="fixture", role="assistant",
                                            content="Private synthetic source body")
        links = [{"type": "transcript", "message_id": message}, {"type": "git", "path": "other.py", "commit": sha}]
        old = episodic.record_event(conn, repo="fixture", type="investigation", file="service.py", symbol="refresh",
                                    payload={"status": "hypothesis", "summary": "Private hypothesis"},
                                    task_id=task, evidence=links, project_root=repo)
        new = episodic.record_event(conn, repo="fixture", type="decision", file="service.py", symbol="refresh",
                                    payload={"status": "verified", "summary": "Private conclusion", "supersedes": old},
                                    task_id=task, evidence=links, project_root=repo, commit=sha)
        matching, unrelated = entity_graph.query(conn, name="refresh")["matches"][::-1]
        assert matching["path"] == "service.py" and unrelated["path"] == "other.py"
        assert entity_history.neighborhood(conn, repo, unrelated["id"]) == []
        history = entity_history.neighborhood(conn, repo, matching["id"])
        assert [item["id"] for item in history] == [new, old]
        assert history[1]["supersededBy"] == new
        assert history[0]["freshness"] == "current"
        ref = next(link["reference"] for link in history[0]["evidence"] if link["type"] == "transcript")
        assert "Private synthetic source body" in evidence.expand(conn, repo, ref, task_id=task)
        source.write_text("def refresh():\n    return 3\n", encoding="utf-8")
        assert entity_history.get(conn, repo, new)["freshness"] == "stale"
        assert entity_history.get(conn, repo, old)["freshness"] == "current"

    entry = obsidian_graph.export(repo, vault)
    default = json.loads(entry.read_text().split("\n", 1)[1])
    assert not default["history"]
    assert not (entry.parent / default["generation"] / "history.json").exists()
    obsidian_graph.export(repo, vault, include_history=True)
    manifest = json.loads(entry.read_text().split("\n", 1)[1])
    data = (entry.parent / manifest["generation"] / "history.json").read_text()
    assert "Private" not in data
    exported = json.loads(data)
    assert set(exported["records"]) == {str(new), str(old)}
    assert exported["attached"][matching["id"]] == [new, old]
    assert sum(new in refs for refs in exported["attached"].values()) >= 3
    assert cli.main(["graph", str(repo), "--event", str(new)]) == 0
    assert json.loads(capsys.readouterr().out)["summary"] == "Private conclusion"

    isolated = tmp_path / "isolated"
    isolated.mkdir()
    project.init_project(isolated)
    assert cli.main(["graph", str(isolated), "--event", str(new)]) == 2
    assert "unavailable in this project" in capsys.readouterr().err


def test_context_prioritizes_linked_investigation_with_noise_and_supersession(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "dispatch.py").write_text("def deliver_batch():\n    return False\n", encoding="utf-8")
    project.init_project(root)
    db = project.db_path(root)
    with closing(store.connect(db)) as conn:
        target = task_library.create_task(conn, "Dispatch")["id"]
        foreign = task_library.create_task(conn, "Other task")["id"]
        raw = transcript.record_message(conn, session_id="fixture", role="tool", content="SYNTHETIC_TEST_OUTPUT")
        transcript.record_message(conn, session_id="prior", role="system", source_id="session:prior", content="Session started")
        task_library.bind_session(conn, target, "prior")
        retired = transcript.record_message(conn, session_id="prior", role="user",
                                            content="Fix deliver_batch in dispatch.py with SUPERSEDED_BAD_RULE")
        common = dict(repo="fixture", task_id=target, file="dispatch.py", symbol="deliver_batch")
        old = episodic.record_event(conn, type="investigation", payload={
            "summary": "SUPERSEDED_BAD_RULE", "status": "hypothesis",
        }, evidence=[{"type": "transcript", "message_id": retired}], **common)
        decision = episodic.record_event(conn, type="decision", payload={
            "summary": "Debug finding: preserve the pending items when validation fails.",
            "status": "verified", "supersedes": old,
        }, evidence=[{"type": "transcript", "message_id": raw}], **common)
        failed = episodic.record_event(conn, type="attempt", payload={
            "summary": "Bulk retry failed the regression test.", "outcome": "failed", "status": "observed",
        }, evidence=[{"type": "transcript", "message_id": raw}], **common)
        for i in range(70):
            episodic.record_event(conn, repo="fixture", type="observation", task_id=foreign,
                                  file="dispatch.py", symbol="deliver_batch",
                                  payload={"summary": f"FOREIGN_NOISE {i}"})
            episodic.record_event(conn, repo="fixture", type="observation", task_id=target,
                                  payload={"summary": "Same routine status"})
        with conn:
            conn.execute("DELETE FROM transcript_messages WHERE id=?", (raw,))
        result = session_context.get_session_start_context(db, task_id=target, query="Fix deliver_batch in dispatch.py")
        assert result["items"][0]["event_id"] == decision
        assert result["items"][0]["code"]["path"] == "dispatch.py"
        assert "debugging" in result["items"][0]["categories"]
        assert failed in {item.get("event_id") for item in result["items"]}
        assert all(marker not in result["text"] for marker in ("FOREIGN_NOISE", "SUPERSEDED_BAD_RULE", "SYNTHETIC_TEST_OUTPUT"))
        assert result["text"].count("Same routine status") <= 1
        assert result["items"][0]["evidence"] is None
        unbound = session_context.get_session_start_context(db, query="Fix deliver_batch in dispatch.py")
        assert "pending items" not in unbound["text"]
def test_unbound_recall_keeps_old_verified_definition_above_same_file_noise(tmp_path):
    (tmp_path / "routing.py").write_text(
        "def deliver_batch():\n    pass\n\ndef other_batch():\n    pass\n", encoding="utf-8",
    )
    project.init_project(tmp_path)
    db = project.db_path(tmp_path)
    with closing(store.connect(db)) as conn:
        def record(symbol, text, **payload):
            ref = transcript.record_message(conn, session_id=symbol, role="user", content=text)
            return episodic.record_event(
                conn, repo="fixture", type="decision", file="routing.py", symbol=symbol,
                payload={"decision": text, "status": "verified", **payload},
                evidence=[{"type": "transcript", "message_id": ref}],
            )

        old = record("deliver_batch", "deliver_batch uses RETIRED_RULE")
        decision = record("deliver_batch", "deliver_batch preserves the accepted queue", supersedes=old)
        record("other_batch", "other_batch uses SIBLING_RULE")
        for n in range(60):
            episodic.record_event(
                conn, type="observation", repo="fixture", file="routing.py",
                symbol="deliver_batch", payload={"summary": f"Routine deliver_batch check {n}"},
            )

    exact = session_context.get_session_start_context(db, query="deliver_batch in routing.py", max_items=1)
    assert exact["items"][0]["event_id"] == decision
    assert all(marker not in exact["text"] for marker in ("RETIRED_RULE", "SIBLING_RULE"))
    recent = session_context.get_session_start_context(db)
    assert "RETIRED_RULE" not in recent["text"]


def test_acceptance_checks_are_bounded_evidence_backed_and_optional(tmp_path, capsys):
    from memory import synthetic

    root = tmp_path / "acceptance"
    root.mkdir()
    (root / "site.py").write_text("def recover():\n    return None\n", encoding="utf-8")
    project.init_project(root)
    db = project.db_path(root)
    with closing(store.connect(db)) as conn:
        task = task_library.create_task(conn, "Recovery")["id"]
        source = transcript.record_message(conn, session_id="review", role="user", content="Both actions must stay available")
        def record(status, checks, *, linked=True, **payload):
            return episodic.record_event(
                conn, repo="fixture", type="decision", file="site.py", symbol="recover", task_id=task,
                payload={"decision": f"Recovery plan {status}", "status": status, "acceptance": checks, **payload},
                evidence=[{"type": "transcript", "message_id": source}] if linked else None,
            )

        with pytest.raises(ValueError, match="one line"):
            record("verified", ["first line\nsecond line"])
        with pytest.raises(ValueError, match="1 to 6"):
            record("verified", ["check"] * 7)
        prior = record("verified", ["Retired fixture expectation"])
        record("verified", ["Both recovery actions remain", "Both recovery actions remain"], supersedes=prior)
        record("hypothesis", ["Hypothesis-only check"])
        record("verified", ["Unlinked fixture check"], linked=False)

    result = session_context.get_session_start_context(db, task_id=task, query="recover site.py")
    text = synthetic.render_user_block(result["items"])
    assert text.count("Both recovery actions remain") == 1
    assert "Verify each applicable check" in text
    assert all(marker not in text for marker in ("Retired fixture expectation", "Hypothesis-only check", "Unlinked fixture check"))
    without_checks = [{key: value for key, value in result["items"][0].items() if key != "acceptance"}]
    limit = len(synthetic.render_user_block(without_checks))
    compact = session_context.get_session_start_context(db, task_id=task, query="recover site.py", max_chars=limit)
    assert len(synthetic.render_user_block(compact["items"])) <= limit
    assert compact["items"][0]["event_id"] == result["items"][0]["event_id"]
    assert not compact["items"][0].get("acceptance")
    assert cli.main(["record", str(root), "--type", "decision", "--status", "verified", "--task", task,
                     "--message", "another choice", "--acceptance", "A check"]) == 0
    assert json.loads(capsys.readouterr().out)["id"] > prior


def test_verified_decision_resolves_both_definitions_and_omits_changed_source(tmp_path):
    root = tmp_path / "definitions"
    root.mkdir()
    (root / "rules.py").write_text("def normalize(name):\n    return name\n", encoding="utf-8")
    (root / "caller.py").write_text("def publish(name):\n    return name\n", encoding="utf-8")
    (root / "other.py").write_text("def unrelated():\n    return None\n", encoding="utf-8")
    project.init_project(root)
    db = project.db_path(root)
    with closing(store.connect(db)) as conn:
        task = task_library.create_task(conn, "Publishing")['id']
        text = "normalize trims whitespace. Preserve letter case. publish returns the cleaned name."
        source = transcript.record_message(conn, session_id="review", role="user", content=text)
        event = episodic.record_event(conn, repo="fixture", type="decision", task_id=task, file="caller.py", symbol="publish",
                                      payload={"decision": text, "status": "verified"},
                                      evidence=[{"type": "transcript", "message_id": source}])

    def targets():
        items = session_context.get_session_start_context(db, task_id=task, query="Continue publishing")['items']
        return next(item for item in items if item['event_id'] == event)['code_targets']

    assert [(item["path"], item["line"], item["decision_excerpt"]) for item in targets()] == [
        ("rules.py", 1, "normalize trims whitespace. Preserve letter case."),
        ("caller.py", 1, "publish returns the cleaned name."),
    ]
    (root / "rules.py").write_text("\ndef normalize(name):\n    return name.strip()\n", encoding="utf-8")
    assert [item["path"] for item in targets()] == ["caller.py"]
    project.index_project(root)
    assert targets()[0]["line"] == 2
    (root / "other.py").write_text("def normalize(name):\n    return name\n", encoding="utf-8")
    project.index_project(root)
    assert [item["path"] for item in targets()] == ["caller.py"]

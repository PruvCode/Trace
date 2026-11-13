from contextlib import closing
import json
import subprocess

import pytest

from memory import entity_graph, episodic, store, structural, task_library, transcript
from memory.graph_model import Fragment
from trace_memory import cli, project


def write(root, path, text):
    file = root / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(text, encoding="utf-8")
    return file


def only(conn, name, path=None, kind=None):
    matches = entity_graph.query(conn, name=name, limit=50)["matches"]
    node, = [row for row in matches if (path is None or row["path"] == path) and (kind is None or row["kind"] == kind)]
    return node


def outgoing(conn, node):
    return entity_graph.query(conn, node_id=node["id"], limit=50)["outgoing"]


def test_parser_cache_round_trips_repeated_long_caller_names():
    caller = "f" * 300
    source = (f"def {caller}():\n" + "    x()\n" * 30_000).encode()
    parsed = structural._parse("large.py", source, ".py")
    cached = Fragment.decode(parsed.encode())
    assert cached.relationships == parsed.relationships
    assert cached.observations == parsed.observations


def test_graph_exposes_containment_exact_edges_and_uncertainty(tmp_path, capsys):
    root = tmp_path / "repo"
    write(root, "pkg/__init__.py", "")
    write(root, "pkg/service.py", """
class Base:
    def run(self):
        def nested():
            return helper()
        return nested()

def helper():
    return 1
""".lstrip())
    write(root, "pkg/consumer.py", """
from .service import Base, helper
class Consumer(Base):
    def run(self):
        helper()
        self.dynamic()
""".lstrip())
    write(root, "packages/contracts/package.json", '{"name":"@fixture/contracts","exports":"./index.ts"}')
    write(root, "packages/contracts/index.ts", """
export interface Contract {}
export class Base {}
export function helper() { return 1; }
""".lstrip())
    write(root, "apps/web/package.json", '{"name":"@fixture/site","dependencies":{"@fixture/contracts":"workspace:*"}}')
    write(root, "apps/web/run.ts", """
import { Base, helper } from '@fixture/contracts';
import type { Contract } from '@fixture/contracts';
class Site extends Base implements Contract {
  run() { helper(); }
}
namespace Nested { export function deep() { helper(); } }
""".lstrip())
    write(root, "apps/web/Component.astro",
          "<section><script>import { helper } from '@fixture/contracts'; function render(){ helper(); }</script></section>")
    write(root, "docs/deeper/arbitrary/reference.md", "# inventory only\n")
    write(root, ".env", "ignored")

    stats = project.init_project(root)
    database = project.db_path(root)
    assert stats["parsed_files"] == 6
    with closing(store.connect(database)) as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        run = only(conn, "run", "pkg/service.py")
        consumer = only(conn, "run", "pkg/consumer.py")
        ts_method = only(conn, "run", "apps/web/run.ts", "method")
        assert {run["kind"], consumer["kind"], ts_method["kind"]} == {"method"}
        assert len({run["id"], consumer["id"], ts_method["id"]}) == 3
        nested = only(conn, "nested", "pkg/service.py")
        assert nested["parent_id"] == run["id"]
        assert nested["qualified_name"] == "pkg/service.py.Base.run.nested"
        assert (nested["line"], nested["byte_column"], nested["end_line"]) == (3, 13, 4)
        assert entity_graph.query(conn, node_id=run["id"])["children"][0]["relationship"] == "DEFINES"
        py_helper = only(conn, "helper", "pkg/service.py")
        ts_helper = only(conn, "helper", "packages/contracts/index.ts")
        assert any(e["kind"] == "CALLS" and e["target_id"] == py_helper["id"] for e in outgoing(conn, nested))
        assert any(e["kind"] == "CALLS" and e["target_id"] == py_helper["id"] for e in outgoing(conn, consumer))
        assert any(e["kind"] == "CALLS" and e["target_id"] == ts_helper["id"] for e in outgoing(conn, ts_method))
        unknown, = [e for e in outgoing(conn, consumer) if e["label"] == "dynamic"]
        assert unknown["confidence"] == "unresolved" and unknown["target_id"] is None
        assert not conn.execute("SELECT 1 FROM graph_edges WHERE label='dynamic'").fetchone()
        py_class = only(conn, "Consumer")
        py_base = only(conn, "Base", "pkg/service.py")
        assert any(e["kind"] == "EXTENDS" and e["target_id"] == py_base["id"] for e in outgoing(conn, py_class))
        site = only(conn, "Site")
        assert {e["kind"] for e in outgoing(conn, site)} == {"EXTENDS", "IMPLEMENTS"}
        module = only(conn, "apps/web/run.ts", kind="module")
        assert {e["kind"] for e in outgoing(conn, module)} == {"IMPORTS", "REFERENCES"}
        package = only(conn, "@fixture/site")
        assert outgoing(conn, package)[0]["kind"] == "DEPENDS_ON"
        namespace = only(conn, "Nested")
        assert only(conn, "deep")["parent_id"] == namespace["id"]
        render = only(conn, "render", "apps/web/Component.astro")
        assert render["byte_column"] == (root / render["path"]).read_bytes().index(b"render") + 1
        assert outgoing(conn, render)[0]["target_id"] == ts_helper["id"]
        assert only(conn, "reference.md")["source_status"] == "inventory"
        assert not entity_graph.query(conn, name=".env")["matches"]
        assert store.find_definition(conn, "run")[0]["kind"] in {"function", "method"}

    assert cli.main(["graph", str(root), "--find", "run", "--limit", "1"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["has_more"] and first["revision_kind"] == "working-tree"
    assert cli.main(["graph", str(root), "--find", "run", "--limit", "1", "--offset", "1"]) == 0
    assert json.loads(capsys.readouterr().out)["matches"][0]["id"] != first["matches"][0]["id"]
    assert cli.main(["graph", str(root), "--node", "does-not-exist"]) == 2


def test_incremental_graph_updates_unchanged_callers_and_matches_clean_build(tmp_path):
    root = tmp_path / "repo"
    target = write(root, "target.ts", "export function run() {}\n")
    consumer = write(root, "caller.ts", "import { run } from './target';\nexport function caller() { run(); }\n")
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True, capture_output=True)
    project.init_project(root)
    database = project.db_path(root)
    with closing(store.connect(database)) as conn:
        original = only(conn, "run")
        caller = only(conn, "caller", "caller.ts", "function")
        legacy_caller = conn.execute("SELECT id FROM symbols WHERE name='caller'").fetchone()[0]

    target.write_text("\n\nexport function run() {}\n", encoding="utf-8")
    assert not structural.is_structural_fresh(root)
    shifted = structural.ensure_structural_memory(root)
    assert (shifted["parsed_files"], shifted["cached_files"]) == (1, 1)
    with closing(store.connect(database)) as conn:
        same = only(conn, "run")
        assert same["id"] == original["id"] and same["line"] == 3
        assert same["source_hash"] != original["source_hash"]
        assert conn.execute("SELECT id FROM symbols WHERE name='caller'").fetchone()[0] == legacy_caller
        assert outgoing(conn, caller)[0]["target_id"] == original["id"]

    moved = target.rename(root / "renamed.ts")
    stats = structural.index_workspace(root, database)
    assert (stats["parsed_files"], stats["cached_files"]) == (1, 1)
    with closing(store.connect(database)) as conn:
        assert only(conn, "run")["id"] != original["id"]
        assert not entity_graph.query(conn, name="target.ts")["matches"]
        assert outgoing(conn, caller)[0]["confidence"] == "unresolved"
        assert conn.execute("SELECT COUNT(*) FROM graph_edges WHERE target_id=?", (original["id"],)).fetchone()[0] == 0

    consumer.write_text("import { run } from './renamed';\nexport function caller() { run(); }\n", encoding="utf-8")
    write(root, "docs/new.md", "# inventory\n")
    assert structural.ensure_structural_memory(root)["parsed_files"] == 1
    with closing(store.connect(database)) as conn:
        assert outgoing(conn, caller)[0]["target_id"] == only(conn, "run")["id"]
        assert only(conn, "new.md")["kind"] == "file"

    moved.unlink()
    assert structural.ensure_structural_memory(root)["parsed_files"] == 0
    oracle = tmp_path / "oracle.db"
    structural.index_workspace(root, oracle)
    with closing(store.connect(database)) as conn, closing(store.connect(oracle)) as clean:
        assert not entity_graph.query(conn, name="run")["matches"]
        assert outgoing(conn, caller)[0]["confidence"] == "unresolved"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        for table in ("graph_nodes", "graph_edges", "code_links"):
            assert {tuple(row) for row in conn.execute(f"SELECT * FROM {table}")} == {
                tuple(row) for row in clean.execute(f"SELECT * FROM {table}")
            }


def test_package_configuration_resolves_cached_calls_and_keeps_inline_scopes_distinct(tmp_path):
    root = tmp_path / "repo"
    manifest = write(root, "lib/package.json", '{"name":"fixture-lib","exports":"./first.ts"}')
    write(root, "lib/first.ts", "export function go() {}\n")
    write(root, "lib/second.ts", "export function go() {}\n")
    write(root, "client.ts", "import { go } from 'fixture-lib'; class First { run(){ go(); } } class Second { run(){ go(); } }")
    project.init_project(root)
    database = project.db_path(root)
    with closing(store.connect(database)) as conn:
        first = only(conn, "client.ts.First.run")
        second = only(conn, "client.ts.Second.run")
        assert first["id"] != second["id"] and first["line"] == second["line"] == 1
        assert first["byte_column"] < second["byte_column"]
        assert all(outgoing(conn, node)[0]["peer_path"] == "lib/first.ts" for node in (first, second))

    manifest.write_text('{"name":"fixture-lib","exports":"./second.ts"}', encoding="utf-8")
    stats = structural.ensure_structural_memory(root)
    assert (stats["parsed_files"], stats["cached_files"]) == (0, 3)
    with closing(store.connect(database)) as conn:
        for node in (first, second):
            call, = outgoing(conn, node)
            assert call["peer_path"] == "lib/second.ts" and call["kind"] == "CALLS"
        assert only(conn, r"lib\second.ts", kind="file")["path"] == "lib/second.ts"


def test_upgrade_and_failed_publication_preserve_memory_and_old_graph(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    source = write(root, "service.py", "def first():\n    return 1\n")
    database = project.db_path(root)
    with closing(store.connect(database)) as conn:
        store.init_schema(conn)
        task = task_library.create_task(conn, "Existing investigation")["id"]
        message = transcript.record_message(conn, session_id="old", role="assistant", content="Existing evidence.")
        event = episodic.record_event(
            conn, type="observation", repo="fixture", task_id=task, payload={"message": "Existing event."},
            evidence=[{"type": "transcript", "message_id": message}], project_root=root,
        )
        for table in ("graph_edges", "graph_nodes", "structural_fragments"):
            conn.execute(f"DROP TABLE {table}")
        conn.execute("INSERT INTO symbols(name,kind,file,line) VALUES('old','function','deleted.py',1)")
        conn.commit()
        old_records = {table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table}")]
                       for table in ("events", "transcript_messages", "tasks", "event_evidence")}
    assert structural.ensure_structural_memory(root)["parsed_files"] == 1
    with closing(store.connect(database)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        assert not store.find_definition(conn, "old")
        before = only(conn, "first")
        marker = store.get_structural_fingerprint(conn)
        assert conn.execute("SELECT id FROM events").fetchone()[0] == event
        for table, records in old_records.items():
            assert [tuple(row) for row in conn.execute(f"SELECT * FROM {table}")] == records

    source.write_text("def second():\n    return 2\n", encoding="utf-8")
    original_publish = entity_graph.publish

    def fail(conn, nodes, edges):
        original_publish(conn, nodes, edges)
        raise RuntimeError("after graph writes")

    monkeypatch.setattr(entity_graph, "publish", fail)
    with pytest.raises(RuntimeError, match="after graph writes"):
        structural.index_workspace(root, database)
    with closing(store.connect(database)) as conn:
        assert only(conn, "first") == before
        assert store.find_definition(conn, "first") and not store.find_definition(conn, "second")
        assert store.search_symbols(conn, "first") and not store.search_symbols(conn, "second")
        assert store.get_structural_fingerprint(conn) == marker

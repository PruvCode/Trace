"""Exercise the TRACE-only map, its provenance, and the working-tree refresh."""

import json
import re
import subprocess
from contextlib import closing

import pytest

from memory import episodic, store, structural, task_library, transcript
from trace_memory import cli, project as project_mod


def _put(project, path, content):
    file = project / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(content, encoding="utf-8")
    return file


def _git(project, *args):
    return subprocess.check_output(
        ["git", "-c", "user.name=TRACE fixture", "-c", "user.email=trace@example.invalid",
         "-c", "commit.gpgsign=false", *args],
        cwd=project, text=True, stderr=subprocess.PIPE,
    ).strip()


def _bundle(vault, home):
    paths = re.findall(r"\[\[([^]|]+\.canvas)\|", home.read_text(encoding="utf-8"))
    assert len(paths) == 2
    return (vault / paths[0]).parent


def _check_links(vault, bundle):
    for file in bundle.rglob("*"):
        if file.suffix == ".canvas":
            canvas = json.loads(file.read_text(encoding="utf-8"))
            ids = {node["id"] for node in canvas["nodes"]}
            assert len(ids) == len(canvas["nodes"])
            assert all(edge["fromNode"] in ids and edge["toNode"] in ids for edge in canvas["edges"])
            text = "\n".join(node["text"] for node in canvas["nodes"])
        elif file.suffix == ".md":
            text = file.read_text(encoding="utf-8")
        else:
            continue
        assert "__TRACE_GENERATION__" not in text
        for target in re.findall(r"\[\[([^]|#]+)\|", text):
            assert (vault / (target if target.endswith(".canvas") else target + ".md")).is_file()


def test_native_map_resolves_rubaru_shape_and_refreshes_dirty_checkout(tmp_path, capsys):
    project = tmp_path / "fixture"
    project.mkdir()
    vault = tmp_path / "vault"
    vault.mkdir()
    media = _put(project, "apps/web/lib/media-gallery.ts", "export function mountMedia() {}\n")
    _put(project, "apps/web/lib/decoy.ts", "export function mountMedia() {}\n")
    featured = _put(project, "apps/web/lib/featured-media.ts",
                    "import { mountMedia as mount } from './media-gallery.js';\nmount();\n")
    _put(project, "apps/web/pages/[slug]/Media.astro",
         "---\nimport type { Item } from '@fixture/contracts';\n---\n"
         "<script\n  type=\"application/json\"\n  set:html={items.map((item) => ({ item }))}\n/>\n"
         "<div><script>\nimport { mountMedia } from '../../lib/media-gallery';\nmountMedia();\n</script></div>\n"
         "<!-- <script>import './fake';</script> -->\n"
         "<script type=\"application/json\">import '../../lib/decoy';</script>\n")
    _put(project, "apps/web/lib/shadow.ts",
         "import { mountMedia } from './media-gallery';\n"
         "function other(mountMedia: () => void) { mountMedia(); }\n")
    _put(project, "apps/web/lib/unbound.ts", "mountMedia();\n")
    _put(project, "packages/contracts/package.json",
         '{"name":"@fixture/contracts","exports":{".":"./src/index.ts"}}')
    _put(project, "packages/contracts/src/index.ts", "export interface Item {}\n")
    _put(project, "apps/worker/worker.js", "export function handle() {}\n")
    _put(project, "apps/worker/caller.jsx", "import * as worker from './worker';\nworker.handle();\n")
    _put(project, "tools/build.py", "def build(): pass\n")
    _put(project, "tools/start.py", "from tools.build import build\nbuild()\n")
    _put(project, ".env.example", "FIXTURE_PRIVATE_ENV")
    _put(project, "docs/readme.md", "# project documentation\n")
    _put(project, ".github/workflows/check.yml", "name: fixture\n")
    _git(project, "init")
    _git(project, "add", ".")
    _git(project, "commit", "-m", "Synthetic fixture")
    commit = _git(project, "rev-parse", "HEAD")
    project_mod.init_project(project)
    database = project_mod.db_path(project)
    with closing(store.connect(database)) as conn:
        task = task_library.create_task(conn, "Gallery history", "Public site")
        transcript_id = transcript.record_message(
            conn, session_id="fixture", role="assistant", content="PRIVATE_MAP_TRANSCRIPT_CANARY",
        )
        episodic.record_event(
            conn, type="decision", repo="fixture", task_id=task["id"],
            payload={"decision": "Keep the gallery back button predictable", "status": "verified"},
            evidence=[{"type": "transcript", "message_id": transcript_id},
                      {"type": "git", "commit": commit, "path": "apps/web/lib/media-gallery.ts"}],
            project_root=project,
        )
        calls = conn.execute(
            "SELECT source_file,source_line,target_file,target_symbol FROM code_links WHERE kind='calls' ORDER BY source_file"
        )
        assert [tuple(row) for row in calls] == [
            ("apps/web/lib/featured-media.ts", 2, "apps/web/lib/media-gallery.ts", "mountMedia"),
            ("apps/web/pages/[slug]/Media.astro", 10, "apps/web/lib/media-gallery.ts", "mountMedia"),
            ("apps/worker/caller.jsx", 2, "apps/worker/worker.js", "handle"),
            ("tools/start.py", 2, "tools/build.py", "build"),
        ]
        assert conn.execute("SELECT COUNT(*) FROM code_links WHERE target_file='packages/contracts/src/index.ts'").fetchone()[0] == 1
        assert len(store.find_callers(conn, "mountMedia")) >= 3
    command = ["obsidian", "--map", "--project", str(project), "--vault", str(vault),
               "--focus", "apps\\web\\lib\\media-gallery.ts"]
    assert cli.main(command) == 0
    home, = vault.glob("TRACE * Code Map.md")
    first = _bundle(vault, home)
    _check_links(vault, first)
    initial_home = home.read_bytes()
    gallery, = (first / "Files").glob("media-gallery-ts-*.md")
    snapshot = gallery.read_text(encoding="utf-8")
    assert "Gallery history" in snapshot and "Keep the gallery back button predictable" in snapshot
    assert "· current" in snapshot and "trace evidence ev_" in snapshot
    assert "PRIVATE_MAP_TRANSCRIPT_CANARY" not in snapshot
    all_notes = "\n".join(file.read_text(encoding="utf-8") for file in first.rglob("*.md"))
    assert ".env.example" not in all_notes and "FIXTURE_PRIVATE_ENV" not in all_notes
    assert ".github" in all_notes and "[slug]" not in all_notes and "&#91;slug&#93;" in all_notes
    tree = first / "Folder Tree.canvas"
    canvas = json.loads(tree.read_text(encoding="utf-8"))
    canvas["nodes"].reverse()
    reserialized = json.dumps(canvas, separators=(",", ":")).encode()
    tree.write_bytes(reserialized)
    assert cli.main(command) == 0 and home.read_bytes() == initial_home
    assert tree.read_bytes() == reserialized
    canvas["nodes"][0]["text"] = "Manually edited card"
    edited_canvas = json.dumps(canvas).encode()
    tree.write_bytes(edited_canvas)
    assert cli.main(command) == 2 and tree.read_bytes() == edited_canvas

    featured.write_text("import { mountMedia as mount } from './decoy';\nmount();\n", encoding="utf-8")
    media.write_text("export function mountMedia() { return 2; }\n", encoding="utf-8")
    assert not structural.is_structural_fresh(project)
    assert cli.main(command) == 0
    updated = _bundle(vault, home)
    assert updated != first and first.is_dir()
    gallery, = (updated / "Files").glob("media-gallery-ts-*.md")
    updated_text = gallery.read_text(encoding="utf-8")
    assert "· stale" in updated_text and "featured-media" not in updated_text
    _check_links(vault, updated)
    with closing(store.connect(database)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    home.write_text("# My edited map entry\n", encoding="utf-8")
    assert cli.main(command) == 2
    assert "was kept" in capsys.readouterr().err
    assert home.read_text(encoding="utf-8") == "# My edited map entry\n"


def test_map_skips_redirects_and_ignores_untracked_dependencies(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    outside = _put(tmp_path, "outside.ts", "export const private_outside_fixture = 1;")
    try:
        (project / "linked.ts").symlink_to(outside)
    except OSError:
        pytest.skip("symbolic links are unavailable")
    _put(project, ".agent-memory/private.ts", "export const private_memory_fixture = 1;")
    _put(project, "node_modules/private.ts", "export const private_dependencies_fixture = 1;")
    _put(project, "public.ts", "export const visible_fixture = 1;")
    project_mod.init_project(project)
    with closing(store.connect(project_mod.db_path(project))) as conn:
        assert [row[0] for row in conn.execute("SELECT file FROM indexed_code_files")] == ["public.ts"]


def test_failed_index_preserves_previous_links_and_fingerprint(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    _put(project, "gallery.ts", "export function gallery() {}\n")
    _put(project, "use.ts", "import { gallery } from './gallery';\ngallery();\n")
    project_mod.init_project(project)
    database = project_mod.db_path(project)
    with closing(store.connect(database)) as conn:
        before = [tuple(row) for row in conn.execute("SELECT * FROM code_links")]
        fingerprint = store.get_structural_fingerprint(conn)
    _put(project, "use.ts", "export function different() {}\n")

    def fail(_connection):
        raise RuntimeError("injected index failure")

    monkeypatch.setattr(store, "rebuild_fts", fail)
    with pytest.raises(RuntimeError, match="injected index failure"):
        structural.index_workspace(project, database)
    with closing(store.connect(database)) as conn:
        assert [tuple(row) for row in conn.execute("SELECT * FROM code_links")] == before
        assert store.get_structural_fingerprint(conn) == fingerprint
        assert store.find_definition(conn, "different") == []

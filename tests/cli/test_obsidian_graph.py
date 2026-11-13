import json

from trace_memory import cli, obsidian_graph, project


def manifest(path):
    return json.loads(path.read_text(encoding="utf-8").split("\n", 1)[1])


def test_export_and_refresh_graph_preserve_notes_settings_and_old_generation(tmp_path, capsys):
    repo = tmp_path / "project"
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / ".obsidian").mkdir()
    preferences = vault / ".obsidian" / "app.json"
    preferences.write_text('{"theme":"existing"}', encoding="utf-8")
    note = vault / "Journal.md"
    note.write_text("# Existing note", encoding="utf-8")
    (repo / "src").mkdir(parents=True)
    (repo / "src/api.py").write_text("def render():\n    return 42\n", encoding="utf-8")
    caller = repo / "use.py"
    caller.write_text("from src.api import render\nrender()\n", encoding="utf-8")
    (repo / ".env").write_text("PRIVATE=fixture", encoding="utf-8")
    (repo / "nested" / "docs").mkdir(parents=True)
    (repo / "nested" / "docs" / "guide.md").write_text("# Guide", encoding="utf-8")
    project.init_project(repo)

    args = ["obsidian", "--project", str(repo), "--vault", str(vault), "--graph", "--install-view"]
    assert cli.main(args) == 0
    assert "Enable TRACE Code Graph" in capsys.readouterr().out
    entry, = vault.glob(".trace-graphs/*/current.json")
    current = manifest(entry)
    generation = entry.parent / current["generation"]
    assert current["nodeCount"] >= 10
    assert current["edgeCount"] == 3
    root = json.loads((generation / "nodes-r.json").read_text())["repository"]
    assert {"src", "nested"} <= {child["name"] for child in root["children"]}
    catalog = json.loads((generation / "catalog.json").read_text())
    assert ".env" not in {entity["name"] for _, entity in catalog}
    assert str(repo) not in (generation / "catalog.json").read_text()
    definition, = [entity for _, entity in catalog if entity["name"] == "render"]
    links = json.loads((generation / f"links-{definition['id'][0]}.json").read_text())[definition["id"]]
    assert [link[1] for link in links["incoming"]] == ["CALLS", "REFERENCES"]
    assert links["incoming"][0][3:7] == ["use.py", 2, 1, True]
    assert any(entity["path"] == "nested/docs/guide.md" and entity["openable"] for _, entity in catalog)

    plugin = vault / ".obsidian" / "plugins" / obsidian_graph.PLUGIN_ID
    assert all((plugin / asset).is_file() for asset in obsidian_graph.ASSETS)
    data = plugin / "data.json"
    data.write_text('{"owned":"by user"}', encoding="utf-8")
    assert cli.main(args) == 0
    assert manifest(entry) == current
    caller.write_text("from src.api import render\n", encoding="utf-8")
    assert cli.main(args) == 0
    assert manifest(entry)["generation"] != current["generation"]
    assert (generation / "catalog.json").is_file()
    assert data.read_text() == '{"owned":"by user"}'
    assert preferences.read_text() == '{"theme":"existing"}'
    assert note.read_text() == "# Existing note"
    assert not (vault / ".obsidian" / "community-plugins.json").exists()

    entry.write_text("human edit", encoding="utf-8")
    assert cli.main(args) == 2
    assert entry.read_text() == "human edit"
    assert "Existing vault files were kept" in capsys.readouterr().err


def test_graph_install_requires_explicit_export_and_protects_foreign_files(tmp_path, capsys):
    repo = tmp_path / "project"
    repo.mkdir()
    project.init_project(repo)
    vault = tmp_path / "vault"
    vault.mkdir()
    args = ["obsidian", "--project", str(repo), "--vault", str(vault)]
    assert cli.main([*args, "--install-view"]) == 2
    assert cli.main([*args, "--graph", "--focus", "missing.py"]) == 2
    assert list(vault.iterdir()) == []

    plugin = vault / ".obsidian" / "plugins" / obsidian_graph.PLUGIN_ID
    plugin.mkdir(parents=True)
    main = plugin / "main.js"
    main.write_text("user file", encoding="utf-8")
    assert cli.main([*args, "--graph", "--install-view"]) == 2
    assert main.read_text() == "user file"
    assert [path.name for path in plugin.iterdir()] == ["main.js"]
    assert "Existing vault files were kept" in capsys.readouterr().err

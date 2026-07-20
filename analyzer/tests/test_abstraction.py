from pathlib import Path
import shutil
import subprocess

from typer.testing import CliRunner

from atlas_analyzer.analysis.analyzer import analyze_repository, write_map
from atlas_analyzer.cli import app
from atlas_analyzer.query import cycles, dependencies, hotspots


FIXTURE = Path(__file__).parent / "fixtures" / "golden_repo"
RUNNER = CliRunner()


def test_layered_map_is_deterministic_and_complete(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    shutil.copytree(FIXTURE, repo)
    first_path = tmp_path / "first.json"
    second_path = tmp_path / "second.json"

    first = analyze_repository(repo)
    second = analyze_repository(repo)
    write_map(first, first_path)
    write_map(second, second_path)

    assert first_path.read_bytes() == second_path.read_bytes()

    files = {node.id: node for node in first.nodes if node.kind.value == "file"}
    modules = {node.id: node for node in first.nodes if node.kind.value == "module"}
    components = {
        node.id: node for node in first.nodes if node.kind.value == "component"
    }
    assert len(files) == 7
    assert modules
    assert components
    assert set(first.levels.module) == set(modules)
    assert set(first.levels.component) == set(components)
    assert {item.root for item in first.levels.system} == set(components)

    module_children = {
        child.root for children in first.levels.module.values() for child in children
    }
    component_children = {
        child.root for children in first.levels.component.values() for child in children
    }
    assert module_children == set(files)
    assert component_children == set(modules)
    assert all(edge.evidence for edge in first.edges)
    assert all(node.prose_source.value == "heuristic" for node in first.nodes)


def test_directory_constraint_and_heuristic_prose_are_sensible() -> None:
    artifact = analyze_repository(FIXTURE)
    nodes = {node.id: node for node in artifact.nodes}

    for children in artifact.levels.module.values():
        anchors = {
            nodes[child.root].files[0].root.split("/", 1)[0] for child in children
        }
        assert len(anchors) == 1

    for kind in ("module", "component"):
        labels = [node.label for node in artifact.nodes if node.kind.value == kind]
        assert len(labels) == len(set(labels))
        assert all(label and label.lower() != "src" for label in labels)
    assert all(
        node.summary
        for node in artifact.nodes
        if node.kind.value in {"module", "component"}
    )


def test_rolled_edges_keep_file_evidence_and_real_symbols() -> None:
    artifact = analyze_repository(FIXTURE)
    higher_edges = [
        edge for edge in artifact.edges if not edge.source.startswith("file:")
    ]

    assert higher_edges
    assert all(edge.evidence for edge in higher_edges)
    assert all(edge.label for edge in higher_edges)
    assert any("Session" in (edge.label or "") for edge in higher_edges)


def test_dependency_and_cycle_queries(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("import b\n")
    (repo / "b.py").write_text("import a\n")
    artifact = analyze_repository(repo)

    assert dependencies(artifact, "file:a.py") == ["file:b.py"]
    assert dependencies(artifact, "file:a.py", reverse=True) == ["file:b.py"]
    assert ("file:a.py", "file:b.py") in cycles(artifact)


def test_hotspots_use_local_git_churn(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", repo], check=True)
    subprocess.run(
        ["git", "-C", repo, "config", "user.email", "atlas@example.test"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", repo, "config", "user.name", "ATLAS Tests"],
        check=True,
    )
    (repo / "a.py").write_text("VALUE = 1\n")
    (repo / "b.py").write_text("import a\n")
    subprocess.run(["git", "-C", repo, "add", "."], check=True)
    subprocess.run(["git", "-C", repo, "commit", "-qm", "initial"], check=True)
    (repo / "a.py").write_text("VALUE = 2\n")
    subprocess.run(["git", "-C", repo, "add", "a.py"], check=True)
    subprocess.run(["git", "-C", repo, "commit", "-qm", "change a"], check=True)

    artifact = analyze_repository(repo)
    ranked = hotspots(artifact, repo, limit=len(artifact.nodes))
    a_row = next(row for row in ranked if row[3] == "file:a.py")

    assert a_row[:3] == (2, 1, 2)


def test_query_cli_commands(tmp_path: Path) -> None:
    map_path = tmp_path / "map.json"
    artifact = analyze_repository(FIXTURE)
    write_map(artifact, map_path)
    file_edge = next(edge for edge in artifact.edges if edge.source.startswith("file:"))

    deps_result = RUNNER.invoke(
        app, ["query", "deps", file_edge.source, "--map", str(map_path)]
    )
    rdeps_result = RUNNER.invoke(
        app, ["query", "rdeps", file_edge.target, "--map", str(map_path)]
    )
    cycles_result = RUNNER.invoke(app, ["query", "cycles", "--map", str(map_path)])
    hotspots_result = RUNNER.invoke(
        app,
        [
            "query",
            "hotspots",
            "--map",
            str(map_path),
            "--repo",
            str(FIXTURE),
            "--limit",
            "3",
        ],
    )

    assert deps_result.exit_code == 0
    assert file_edge.target in deps_result.stdout
    assert rdeps_result.exit_code == 0
    assert file_edge.source in rdeps_result.stdout
    assert cycles_result.exit_code == 0
    assert hotspots_result.exit_code == 0
    assert hotspots_result.stdout.startswith("score\tfan_in\tchurn\tnode")

import json
from pathlib import Path
import shutil
import socket

from typer.testing import CliRunner

from atlas_analyzer.analysis.analyzer import (
    analyze_file_graph,
    analyze_repository,
    write_map,
)
from atlas_analyzer.analysis.languages import parse_file
from atlas_analyzer.analysis.repository import source_files
from atlas_analyzer.cli import app

FIXTURE = Path(__file__).parent / "fixtures" / "golden_repo"
RUNNER = CliRunner()


def test_walk_classifies_sources_and_respects_gitignore(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    shutil.copytree(FIXTURE, repo)
    ignored = repo / "ignored"
    ignored.mkdir()
    (ignored / "not_part_of_the_map.py").write_text("raise RuntimeError\n")

    paths = [path.relative_to(repo).as_posix() for path in source_files(repo)]

    assert paths == [
        "src/app.py",
        "src/auth/__init__.py",
        "src/auth/session.py",
        "src/auth/tokens.py",
        "web/lib/client.ts",
        "web/lib/helper.js",
        "web/main.ts",
    ]


def test_nested_gitignore_layers_match_git(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    shutil.copytree(FIXTURE, repo)
    generated = repo / "src" / "generated"
    generated.mkdir()
    (generated / ".gitignore").write_text("*.py\n!keep.py\n")
    (generated / "machine.py").write_text("VALUE = 1\n")
    (generated / "keep.py").write_text("VALUE = 2\n")

    paths = [path.relative_to(repo).as_posix() for path in source_files(repo)]

    assert "src/generated/machine.py" not in paths
    assert "src/generated/keep.py" in paths


def test_unparsable_file_degrades_without_aborting(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    shutil.copytree(FIXTURE, repo)
    (repo / "src" / "legacy.py").write_text("print 'python 2 syntax'\n")

    artifact = analyze_file_graph(repo)

    node = next(node for node in artifact.nodes if node.id == "file:src/legacy.py")
    assert node.metrics.loc == 1
    assert all(
        "file:src/legacy.py" not in (edge.source, edge.target)
        for edge in artifact.edges
    )


def test_tree_sitter_builds_symbol_tables() -> None:
    python_table = parse_file(FIXTURE / "src" / "app.py")
    typescript_table = parse_file(FIXTURE / "web" / "main.ts")

    assert python_table.definitions == ("create_session",)
    assert [(item.module, item.line) for item in python_table.imports] == [
        ("auth.session.Session", 1)
    ]
    assert python_table.imports[0].fallbacks == ("auth.session",)
    assert typescript_table.definitions == ("loadSession",)
    assert [(item.module, item.line) for item in typescript_table.imports] == [
        ("@lib/client", 1)
    ]


def test_python_package_and_relative_module_resolution(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    package = repo / "src" / "package"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("from . import child\n")
    (package / "child.py").write_text("VALUE = 1\n")
    (repo / "src" / "main.py").write_text("from package import child\n")

    artifact = analyze_file_graph(repo)

    assert {(edge.source, edge.target) for edge in artifact.edges} == {
        ("file:src/main.py", "file:src/package/child.py"),
        ("file:src/package/__init__.py", "file:src/package/child.py"),
    }


def test_bare_stdlib_import_does_not_resolve_to_local_shadow(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    (repo / "logging.py").write_text("def setup() -> None:\n    pass\n")
    (repo / "app" / "main.py").write_text(
        "import logging\n\nlogging.getLogger(__name__)\n"
    )

    artifact = analyze_file_graph(repo)

    assert all(edge.target != "file:logging.py" for edge in artifact.edges)


def test_from_import_drops_edge_to_same_named_file_missing_the_symbol(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    (repo / "pkg_a").mkdir(parents=True)
    (repo / "pkg_b").mkdir(parents=True)
    (repo / "pkg_a" / "utils.py").write_text("def shared_helper():\n    return 1\n")
    (repo / "pkg_b" / "utils.py").write_text("def only_in_b():\n    return 2\n")
    (repo / "pkg_b" / "service.py").write_text("from utils import shared_helper\n")

    artifact = analyze_file_graph(repo)

    # pkg_b/utils.py does not define shared_helper, so the edge is dropped rather
    # than pointed at the wrong same-named file.
    assert (
        "file:pkg_b/service.py",
        "file:pkg_b/utils.py",
    ) not in {(edge.source, edge.target) for edge in artifact.edges}


def test_barrel_reexport_points_at_the_defining_module(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    lib = repo / "lib"
    lib.mkdir(parents=True)
    (lib / "__init__.py").write_text("from lib.core import build\n")
    (lib / "core.py").write_text("def build():\n    return 1\n")
    (repo / "consumer.py").write_text("from lib import build\n")

    artifact = analyze_file_graph(repo)
    edges = {(edge.source, edge.target) for edge in artifact.edges}

    assert ("file:consumer.py", "file:lib/core.py") in edges
    assert ("file:consumer.py", "file:lib/__init__.py") not in edges


def test_src_layout_package_resolves_from_outside(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    package = repo / "analyzer" / "src" / "mypkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "core.py").write_text("def entry():\n    return 1\n")
    (repo / "scripts").mkdir()
    (repo / "scripts" / "run.py").write_text("from mypkg.core import entry\n")

    artifact = analyze_file_graph(repo)

    assert (
        "file:scripts/run.py",
        "file:analyzer/src/mypkg/core.py",
    ) in {(edge.source, edge.target) for edge in artifact.edges}


def test_javascript_dynamic_import_captured_and_type_only_excluded(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.ts").write_text("export const A = 1;\n")
    (repo / "t.ts").write_text("export type T = number;\n")
    (repo / "b.ts").write_text("export const B = 2;\n")
    (repo / "main.ts").write_text(
        'import { A } from "./a";\n'
        'import type { T } from "./t";\n'
        "async function go() {\n"
        '  return await import("./b");\n'
        "}\n"
    )

    artifact = analyze_file_graph(repo)
    edges = {(edge.source, edge.target) for edge in artifact.edges}

    assert ("file:main.ts", "file:a.ts") in edges  # static import kept
    assert ("file:main.ts", "file:b.ts") in edges  # dynamic import() captured
    assert ("file:main.ts", "file:t.ts") not in edges  # type-only erased


def test_analysis_is_deterministic_and_edges_have_source_evidence(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    shutil.copytree(FIXTURE, repo)
    first_path = tmp_path / "first.json"
    second_path = tmp_path / "second.json"

    first = analyze_file_graph(repo)
    second = analyze_file_graph(repo)
    write_map(first, first_path)
    write_map(second, second_path)

    assert first_path.read_bytes() == second_path.read_bytes()
    assert len(first.nodes) == len(source_files(repo)) == 7
    assert {
        (edge.source, edge.target, edge.evidence[0].file, edge.evidence[0].line)
        for edge in first.edges
    } == {
        ("file:src/app.py", "file:src/auth/session.py", "src/app.py", 1),
        (
            "file:src/auth/__init__.py",
            "file:src/auth/session.py",
            "src/auth/__init__.py",
            1,
        ),
        (
            "file:src/auth/session.py",
            "file:src/auth/tokens.py",
            "src/auth/session.py",
            1,
        ),
        ("file:web/lib/client.ts", "file:web/lib/helper.js", "web/lib/client.ts", 1),
        ("file:web/main.ts", "file:web/lib/client.ts", "web/main.ts", 1),
    }
    assert all(edge.evidence for edge in first.edges)
    assert all(node.prose_source.value == "heuristic" for node in first.nodes)

    payload = json.loads(first_path.read_text())
    metrics = {node["id"]: node["metrics"] for node in payload["nodes"]}
    assert metrics["file:src/auth/session.py"]["fan_in"] == 2
    assert metrics["file:src/auth/session.py"]["fan_out"] == 1


def test_analysis_never_opens_a_network_connection(
    monkeypatch,
    tmp_path: Path,
) -> None:
    def blocked_connect(*_args, **_kwargs):
        raise AssertionError("atlas analyze attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", blocked_connect)
    artifact = analyze_repository(FIXTURE)
    output = tmp_path / "map.json"
    write_map(artifact, output)

    assert output.exists()


def test_analyze_cli_uses_the_documented_subcommand(tmp_path: Path) -> None:
    output = tmp_path / "map.json"

    result = RUNNER.invoke(app, ["analyze", str(FIXTURE), "--output", str(output)])

    assert result.exit_code == 0
    assert "Analyzed 7 files and 5 imports; built" in result.stdout
    assert output.exists()


def test_analyze_cli_role_override_tags_generated_paths(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "core.py").write_text("VALUE = 1\n")
    (repo / "pkg" / "bindings.py").write_text("VALUE = 2\n")
    output = tmp_path / "map.json"

    result = RUNNER.invoke(
        app,
        [
            "analyze",
            str(repo),
            "--output",
            str(output),
            "--generated",
            "pkg/bindings.py",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(output.read_text())
    roles = {
        node["id"]: node.get("role")
        for node in payload["nodes"]
        if node["kind"] == "file"
    }
    assert roles["file:pkg/bindings.py"] == "generated"
    assert roles["file:pkg/core.py"] == "source"


def test_analyze_cli_rejects_a_malformed_role_pattern(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "core.py").write_text("VALUE = 1\n")

    result = RUNNER.invoke(app, ["analyze", str(repo), "--generated", "!"])

    # A clean usage error (BadParameter -> SystemExit), not an unhandled
    # GitIgnorePatternError traceback leaking out of analysis.
    assert result.exit_code != 0
    assert not isinstance(result.exception, ValueError)

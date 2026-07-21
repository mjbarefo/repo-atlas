from pathlib import Path
import re
import shutil
import subprocess

from typer.testing import CliRunner

from atlas_analyzer.analysis.analyzer import analyze_repository, write_map
from atlas_analyzer.cli import app
from atlas_analyzer.query import cycles, dependencies, hotspots

FIXTURE = Path(__file__).parent / "fixtures" / "golden_repo"
RUNNER = CliRunner()

# A bare disambiguation counter such as "Widgets 2": a base label followed by a
# space and a run of digits at the end.
COUNTER_LABEL = re.compile(r".*\s\d+$")


def _make_repo(base: Path, files: dict[str, str]) -> Path:
    for relative, body in files.items():
        path = base / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    return base


def _module_labels(artifact) -> list[str]:
    return [node.label for node in artifact.nodes if node.kind.value == "module"]


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
        assert not any(COUNTER_LABEL.match(label) for label in labels)
    assert all(
        node.summary
        for node in artifact.nodes
        if node.kind.value in {"module", "component"}
    )


def test_single_leaf_package_is_not_split_into_counter_modules(tmp_path: Path) -> None:
    # Two internally dense clusters in one leaf directory (two triangles joined
    # by a single bridge) make Louvain split the package. Phase C re-merges
    # communities that share a common leaf directory, so the package is a single
    # module instead of "Widgets" / "Widgets 2".
    repo = _make_repo(
        tmp_path / "repo",
        {
            "widgets/a1.py": (
                "from .a2 import A2\nfrom .a3 import A3\nfrom .b1 import B1\n\n\n"
                "class A1:\n    pass\n"
            ),
            "widgets/a2.py": "from .a1 import A1\nfrom .a3 import A3\n\n\nclass A2:\n    pass\n",
            "widgets/a3.py": "from .a1 import A1\nfrom .a2 import A2\n\n\nclass A3:\n    pass\n",
            "widgets/b1.py": "from .b2 import B2\nfrom .b3 import B3\n\n\nclass B1:\n    pass\n",
            "widgets/b2.py": "from .b1 import B1\nfrom .b3 import B3\n\n\nclass B2:\n    pass\n",
            "widgets/b3.py": "from .b1 import B1\nfrom .b2 import B2\n\n\nclass B3:\n    pass\n",
        },
    )

    artifact = analyze_repository(repo)
    labels = _module_labels(artifact)
    modules = [node for node in artifact.nodes if node.kind.value == "module"]

    assert labels == ["Widgets"]
    assert not any(COUNTER_LABEL.match(label) for label in labels)
    assert len(modules[0].children) == 6


def test_generic_common_directory_walks_up_to_meaningful_label(
    tmp_path: Path,
) -> None:
    # A group whose common directory ends in a generic segment ("viewer/src")
    # must be named after the nearest meaningful ancestor ("Viewer"), not a file
    # stem.
    repo = _make_repo(
        tmp_path / "repo",
        {
            "viewer/src/alpha.py": "from .beta import Beta\n\n\nclass Alpha:\n    pass\n",
            "viewer/src/beta.py": "from .gamma import Gamma\n\n\nclass Beta:\n    pass\n",
            "viewer/src/gamma.py": "class Gamma:\n    pass\n",
        },
    )

    assert _module_labels(analyze_repository(repo)) == ["Viewer"]


def test_dense_cross_directory_coupling_stays_an_edge_not_one_module(
    tmp_path: Path,
) -> None:
    # Every service file imports every store file: dense cross-directory coupling
    # that Louvain fuses into a single multi-anchor module. Phase C keeps a
    # module within one top-level anchor, so the coupling survives as a module
    # edge instead of hidden shared membership.
    repo = _make_repo(
        tmp_path / "repo",
        {
            "service/api.py": "from store.db import read, write\nfrom store.cache import get, put\n",
            "service/web.py": "from store.db import read, write\nfrom store.cache import get, put\n",
            "store/db.py": "def read():\n    return 1\n\n\ndef write():\n    return 2\n",
            "store/cache.py": "def get():\n    return 3\n\n\ndef put():\n    return 4\n",
        },
    )

    artifact = analyze_repository(repo)
    nodes = {node.id: node for node in artifact.nodes}
    modules = [node for node in artifact.nodes if node.kind.value == "module"]

    for children in artifact.levels.module.values():
        anchors = {
            nodes[child.root].files[0].root.split("/", 1)[0] for child in children
        }
        assert len(anchors) == 1
    assert len(modules) == 2

    module_ids = {module.id for module in modules}
    module_edges = [
        edge
        for edge in artifact.edges
        if edge.source in module_ids and edge.target in module_ids
    ]
    assert module_edges


def test_colliding_labels_disambiguated_by_path_segment_not_counter(
    tmp_path: Path,
) -> None:
    # Two distinct directories share the leaf name "api". They must not re-merge
    # (different common directories) and must be disambiguated by the
    # distinguishing path segment rather than a bare "Api 2" counter.
    repo = _make_repo(
        tmp_path / "repo",
        {
            "alpha/api/one.py": "from .two import Two\n\n\nclass One:\n    pass\n",
            "alpha/api/two.py": "class Two:\n    pass\n",
            "beta/api/one.py": "from .two import Two\n\n\nclass One:\n    pass\n",
            "beta/api/two.py": "class Two:\n    pass\n",
        },
    )

    labels = sorted(_module_labels(analyze_repository(repo)))

    assert len(labels) == 2
    assert len(set(labels)) == 2
    assert not any(COUNTER_LABEL.match(label) for label in labels)
    lowered = " ".join(labels).lower()
    assert "alpha" in lowered
    assert "beta" in lowered


def test_edges_carry_import_weight(tmp_path: Path) -> None:
    # Every edge (file and rolled) reports a weight equal to its distinct import
    # sites, so import volume is visible instead of a bare neighbor count.
    repo = _make_repo(
        tmp_path / "repo",
        {
            "alpha/client.py": "from beta.api import run\nfrom beta.api import stop\n",
            "beta/api.py": "def run():\n    return 1\n\n\ndef stop():\n    return 2\n",
        },
    )

    artifact = analyze_repository(repo)

    assert artifact.edges
    for edge in artifact.edges:
        assert edge.weight == len(edge.evidence)
        assert edge.weight >= 1


def test_map_declares_supported_edge_kinds(tmp_path: Path) -> None:
    # The contract honestly advertises which edge kinds are actually produced.
    repo = _make_repo(
        tmp_path / "repo",
        {"pkg/a.py": "from pkg import b\n", "pkg/b.py": "VALUE = 1\n"},
    )

    artifact = analyze_repository(repo)

    assert [kind.value for kind in artifact.capabilities.supported_edge_kinds] == [
        "imports"
    ]
    assert {edge.kind.value for edge in artifact.edges} <= {"imports"}


def test_rolled_label_shows_overflow_count(tmp_path: Path) -> None:
    # A rolled edge that unions more than five symbols shows the first five plus
    # an honest "(+N more)" instead of silently dropping the rest.
    repo = _make_repo(
        tmp_path / "repo",
        {
            "alpha/client.py": "from beta.api import a, b, c, d, e, f, g\n",
            "beta/api.py": "".join(
                f"def {name}():\n    return 0\n\n\n" for name in "abcdefg"
            ),
        },
    )

    artifact = analyze_repository(repo)
    rolled = [edge for edge in artifact.edges if not edge.source.startswith("file:")]
    overflow = [edge for edge in rolled if edge.label and "(+" in edge.label]

    assert overflow, "expected a rolled edge with an overflow label"
    # Every rolled level (module and component) must carry a single, well-formed
    # overflow marker — never a doubled "(+1 more) (+5 more)" from re-parsing an
    # already-truncated child label.
    for edge in overflow:
        label = edge.label
        assert label.count("(+") == 1
        assert label.endswith(" more)")
        head, _, tail = label.partition(" (+")
        head_symbols = [s.strip() for s in head.split(",") if s.strip()]
        assert len(head_symbols) == 5
        assert all("(" not in s and ")" not in s for s in head_symbols)
        assert tail == "2 more)"  # 7 symbols -> 5 shown + 2 more


def test_package_init_docstring_is_the_summary(tmp_path: Path) -> None:
    # A package entry point (__init__) legitimately describes the whole group, so
    # its docstring is the summary.
    repo = _make_repo(
        tmp_path / "repo",
        {
            "widget/__init__.py": '"""The widget package."""\n',
            "widget/core.py": '"""Core internals."""\n\n\ndef run():\n    return 1\n',
            "widget/user.py": "from .core import run\n",
        },
    )

    artifact = analyze_repository(repo)
    module = next(node for node in artifact.nodes if node.kind.value == "module")

    assert module.summary == "The widget package."


def test_summary_without_package_init_uses_generated_form(tmp_path: Path) -> None:
    # With no package __init__, no single file's docstring may speak for the whole
    # group; the summary is the generated aggregate form, not the alphabetically
    # first file's docstring.
    repo = _make_repo(
        tmp_path / "repo",
        {
            "widget/aaa.py": '"""Alphabetically first, unimportant."""\n',
            "widget/core.py": '"""The widget core engine."""\n\n\ndef run():\n    return 1\n',
            "widget/user.py": "from .core import run\n",
        },
    )

    artifact = analyze_repository(repo)
    module = next(node for node in artifact.nodes if node.kind.value == "module")

    assert module.summary.startswith("3 files,")
    assert "unimportant" not in module.summary
    assert "engine" not in module.summary


def test_module_id_is_stable_across_membership_change(tmp_path: Path) -> None:
    # A module's id is keyed on its directory, so adding a file to the same
    # package leaves the id unchanged (ids survive membership churn).
    repo = _make_repo(
        tmp_path / "repo",
        {
            "pkg/a.py": "from .b import B\n\n\nclass A:\n    pass\n",
            "pkg/b.py": "class B:\n    pass\n",
        },
    )
    before = analyze_repository(repo)
    id_before = next(n.id for n in before.nodes if n.kind.value == "module")

    (repo / "pkg" / "c.py").write_text("from .a import A\n\n\nclass C:\n    pass\n")
    after = analyze_repository(repo)
    modules_after = [n for n in after.nodes if n.kind.value == "module"]

    assert len(modules_after) == 1
    assert modules_after[0].id == id_before
    assert len(modules_after[0].children) == 3


def test_non_source_is_tagged_kept_and_excluded_from_layering(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "core.py").write_text("from pkg import util\n")
    (repo / "pkg" / "util.py").write_text("VALUE = 1\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_core.py").write_text("from pkg import core\n")
    (repo / "pkg" / "generated").mkdir()
    (repo / "pkg" / "generated" / "models.py").write_text("from pkg import util\n")
    (repo / "pkg" / "fixtures").mkdir()
    (repo / "pkg" / "fixtures" / "sample.py").write_text("from pkg import core\n")

    artifact = analyze_repository(repo)

    roles = {
        node.id: node.role.value for node in artifact.nodes if node.kind.value == "file"
    }
    assert roles == {
        "file:pkg/core.py": "source",
        "file:pkg/util.py": "source",
        "file:pkg/fixtures/sample.py": "fixture",
        "file:pkg/generated/models.py": "generated",
        "file:tests/test_core.py": "test",
    }

    # Only source files cluster into modules/components; non-source nodes are
    # never a module child, so they cannot form or name architecture.
    module_children = {
        child.root for children in artifact.levels.module.values() for child in children
    }
    assert module_children == {"file:pkg/core.py", "file:pkg/util.py"}

    # Non-source file nodes and their import edges still live in the artifact.
    file_edges = {
        (edge.source, edge.target)
        for edge in artifact.edges
        if edge.source.startswith("file:") and edge.target.startswith("file:")
    }
    assert ("file:tests/test_core.py", "file:pkg/core.py") in file_edges
    assert ("file:pkg/generated/models.py", "file:pkg/util.py") in file_edges

    # No module/component may be named after a non-source directory.
    labels = {
        node.label.lower() for node in artifact.nodes if node.kind.value != "file"
    }
    assert "generated" not in labels
    assert "fixtures" not in labels
    assert "tests" not in labels


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

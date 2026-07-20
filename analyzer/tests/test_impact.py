from __future__ import annotations

import json
from pathlib import Path
import subprocess

from typer.testing import CliRunner

from atlas_analyzer.analysis.analyzer import analyze_repository, write_map
from atlas_analyzer.cli import app
from atlas_analyzer.impact import build_impact, changed_files, write_impact


RUNNER = CliRunner()


def _repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("import b\n")
    (repo / "b.py").write_text("VALUE = 1\n")
    (repo / "deleted.py").write_text("OLD = True\n")
    (repo / "README.md").write_text("# Before\n")
    subprocess.run(["git", "init", "-q", repo], check=True)
    subprocess.run(
        ["git", "-C", repo, "config", "user.email", "atlas@example.test"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", repo, "config", "user.name", "ATLAS Tests"],
        check=True,
    )
    subprocess.run(["git", "-C", repo, "add", "."], check=True)
    subprocess.run(["git", "-C", repo, "commit", "-qm", "base"], check=True)
    base = subprocess.run(
        ["git", "-C", repo, "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return repo, base


def _change_worktree(repo: Path) -> None:
    (repo / "b.py").write_text("VALUE = 2\n")
    (repo / "deleted.py").unlink()
    (repo / "README.md").write_text("# After\n")
    (repo / "new.py").write_text("import b\n")
    (repo / "notes.txt").write_text("untracked\n")


def test_worktree_impact_is_exact_deterministic_and_dependency_ordered(
    tmp_path: Path,
) -> None:
    repo, base = _repo(tmp_path)
    _change_worktree(repo)
    (repo / ".atlas").mkdir()
    (repo / ".atlas" / "map.json").write_text("{}\n")
    artifact = analyze_repository(repo)

    resolved_base, resolved_head, changes = changed_files(repo, base)
    assert resolved_base == base
    assert resolved_head == base
    assert [(item.path, item.status) for item in changes] == [
        ("README.md", "modified"),
        ("b.py", "modified"),
        ("deleted.py", "deleted"),
        ("new.py", "added"),
        ("notes.txt", "added"),
    ]

    impact = build_impact(repo, artifact, base=base)
    assert impact.map_ref.commit == artifact.repo.commit
    assert impact.summary.changed_files == 5
    assert impact.summary.mapped_files == 2
    assert impact.summary.direct_dependents == 1
    assert [
        (item.changed_node_id, item.dependent_node_id)
        for item in impact.direct_dependents
    ] == [("file:b.py", "file:a.py")]
    assert [item.root for item in impact.review_order] == [
        "b.py",
        "new.py",
        "README.md",
        "deleted.py",
        "notes.txt",
    ]

    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    write_impact(impact, first)
    write_impact(build_impact(repo, artifact, base=base), second)
    assert first.read_bytes() == second.read_bytes()


def test_committed_head_and_cli_require_a_matching_map(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path)
    _change_worktree(repo)
    subprocess.run(["git", "-C", repo, "add", "."], check=True)
    subprocess.run(["git", "-C", repo, "commit", "-qm", "head"], check=True)
    head = subprocess.run(
        ["git", "-C", repo, "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    map_path = repo / ".atlas" / "map.json"
    write_map(analyze_repository(repo), map_path)
    output = repo / ".atlas" / "review.json"

    result = RUNNER.invoke(
        app,
        [
            "impact",
            str(repo),
            "--base",
            base,
            "--head",
            head,
            "--map",
            str(map_path),
            "--output",
            str(output),
        ],
    )

    assert result.exit_code == 0
    assert "Impact: 5 changed; 2 mapped; 1 direct dependents" in result.stdout
    assert json.loads(output.read_text())["comparison"] == {
        "base": base,
        "head": head,
    }

    mismatch = RUNNER.invoke(
        app,
        [
            "impact",
            str(repo),
            "--base",
            base,
            "--head",
            base,
            "--map",
            str(map_path),
        ],
    )
    assert mismatch.exit_code == 1
    assert "map is based on" in mismatch.stderr

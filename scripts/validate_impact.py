"""Validate Phase 6 against a disposable 12-file Git comparison."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

from atlas_analyzer.analysis.analyzer import analyze_repository
from atlas_analyzer.impact import build_impact, write_impact


STATUS = {
    "A": "added",
    "M": "modified",
    "D": "deleted",
    "R": "renamed",
    "C": "copied",
    "T": "type_changed",
}


def _git(repo: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _expected(repo: Path, base: str) -> list[tuple[str, str]]:
    expected: list[tuple[str, str]] = []
    for line in _git(
        repo,
        "diff",
        "--name-status",
        "--find-renames",
        base,
        "--",
    ).splitlines():
        fields = line.split("\t")
        code = fields[0][0]
        path = fields[2] if code in {"R", "C"} else fields[1]
        expected.append((path, STATUS[code]))
    expected.extend(
        (path, "added")
        for path in _git(
            repo,
            "ls-files",
            "--others",
            "--exclude-standard",
        ).splitlines()
        if path
    )
    return sorted(expected)


def validate(source: Path) -> dict[str, object]:
    source = source.resolve()
    if not (source / ".git").exists():
        raise ValueError(f"not a Git checkout: {source}")
    if _git(source, "status", "--porcelain", "--untracked-files=no").strip():
        raise ValueError("source checkout has tracked changes")
    with tempfile.TemporaryDirectory(prefix="atlas-impact-") as temporary:
        repo = Path(temporary) / "repo"
        shutil.copytree(source, repo)
        shutil.rmtree(repo / ".atlas", ignore_errors=True)
        base = _git(repo, "rev-parse", "HEAD").strip()
        baseline = analyze_repository(repo)
        file_nodes = [
            node
            for node in baseline.nodes
            if node.kind.value == "file"
            and len(node.files) == 1
            and node.files[0].root.endswith((".py", ".js", ".ts"))
        ]
        tracked = set(_git(repo, "ls-files").splitlines())
        candidates = [
            node
            for node in file_nodes
            if node.files[0].root in tracked
            and Path(node.files[0].root).name != "__init__.py"
        ]
        modified = sorted(
            candidates,
            key=lambda node: (-node.metrics.fan_in, node.files[0].root),
        )[:9]
        reserved = {node.files[0].root for node in modified}
        remaining = [
            node
            for node in sorted(candidates, key=lambda item: item.files[0].root)
            if node.files[0].root not in reserved
        ]
        if len(modified) < 9 or len(remaining) < 2:
            raise ValueError("repository needs at least 11 tracked source files")

        for node in modified:
            path = repo / node.files[0].root
            marker = (
                "# ATLAS Phase 6 validation"
                if path.suffix == ".py"
                else "// ATLAS Phase 6 validation"
            )
            path.write_text(path.read_text() + f"\n{marker}\n")
        renamed_source = remaining[0].files[0].root
        renamed_path = str(
            Path(renamed_source).with_name(
                f"{Path(renamed_source).stem}_atlas_phase6{Path(renamed_source).suffix}"
            )
        )
        subprocess.run(
            ["git", "-C", str(repo), "mv", renamed_source, renamed_path],
            check=True,
        )
        deleted_path = remaining[1].files[0].root
        (repo / deleted_path).unlink()
        added_path = "atlas_phase6_added.py"
        (repo / added_path).write_text("# ATLAS Phase 6 added file\n")

        current = analyze_repository(repo)
        impact = build_impact(repo, current, base=base)
        actual = sorted((item.path, item.status.value) for item in impact.files)
        expected = _expected(repo, base)
        if actual != expected:
            raise AssertionError(
                f"Git status mismatch:\nactual={actual}\nexpected={expected}"
            )
        if len(actual) != 12:
            raise AssertionError(f"expected 12 changed files, received {len(actual)}")
        if impact.summary.direct_dependents < 1:
            raise AssertionError(
                "expected at least one evidence-backed direct dependent"
            )
        if sorted(item.root for item in impact.review_order) != sorted(
            path for path, _ in expected
        ):
            raise AssertionError("review order does not contain every changed file")

        first = Path(temporary) / "impact-one.json"
        second = Path(temporary) / "impact-two.json"
        write_impact(impact, first)
        write_impact(build_impact(repo, current, base=base), second)
        if first.read_bytes() != second.read_bytes():
            raise AssertionError("impact artifacts are not byte-identical")
        return {
            "base": base,
            "head": impact.comparison.head,
            "changed_files": impact.summary.changed_files,
            "mapped_files": impact.summary.mapped_files,
            "direct_dependents": impact.summary.direct_dependents,
            "direct_dependency_pairs": len(impact.direct_dependents),
            "review_order": [item.root for item in impact.review_order],
            "byte_identical": True,
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("repository", type=Path)
    arguments = parser.parse_args()
    print(json.dumps(validate(arguments.repository), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

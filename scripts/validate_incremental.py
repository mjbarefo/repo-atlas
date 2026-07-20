"""Measure the Phase 5 incremental gate against a disposable Git worktree."""

import argparse
import hashlib
from pathlib import Path
import shutil
import tempfile
from time import perf_counter

from atlas_analyzer.analysis.analyzer import (
    analyze_repository,
    analyze_repository_incremental,
    write_map,
)
from atlas_analyzer.analysis.repository import source_files


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("repo", type=Path)
    arguments = parser.parse_args()
    source = arguments.repo.resolve()
    if not (source / ".git").exists():
        raise SystemExit("gate repository must be a Git checkout")

    with tempfile.TemporaryDirectory(prefix="atlas-incremental-gate-") as temporary:
        repo = Path(temporary) / "repo"
        shutil.copytree(
            source,
            repo,
            ignore=shutil.ignore_patterns(
                ".atlas", "__pycache__", ".pytest_cache", ".mypy_cache"
            ),
        )
        repo = repo.resolve()
        baseline = analyze_repository(repo)
        candidates = source_files(repo)
        if len(candidates) < 5:
            raise SystemExit("gate repository must contain at least five source files")
        changed = candidates[:5]
        for path in changed:
            comment = (
                "# ATLAS incremental gate"
                if path.suffix == ".py"
                else "// ATLAS incremental gate"
            )
            path.write_text(f"{path.read_text()}\n{comment}\n")

        started = perf_counter()
        incremental, report = analyze_repository_incremental(repo, baseline)
        incremental_seconds = perf_counter() - started
        started = perf_counter()
        full = analyze_repository(repo)
        full_seconds = perf_counter() - started

        incremental_path = Path(temporary) / "incremental.json"
        full_path = Path(temporary) / "full.json"
        write_map(incremental, incremental_path)
        write_map(full, full_path)
        identical = incremental_path.read_bytes() == full_path.read_bytes()
        ratio = incremental_seconds / full_seconds
        print("changed:")
        for path in changed:
            print(f"  {path.relative_to(repo).as_posix()}")
        print(
            f"parsed={report.parsed_files} reused={report.reused_files} "
            f"clustering={report.clustering}"
        )
        print(
            f"incremental={incremental_seconds:.6f}s "
            f"full={full_seconds:.6f}s ratio={ratio:.4%}"
        )
        print(f"identical={str(identical).lower()} sha256={_sha256(incremental_path)}")
        if report.parsed_files != 5:
            raise SystemExit("incremental analysis did not parse exactly five files")
        if ratio >= 0.10:
            raise SystemExit("incremental runtime was not below 10% of full runtime")
        if not identical:
            raise SystemExit("incremental artifact differs from clean full analysis")


if __name__ == "__main__":
    main()

"""Build deterministic, file-level ATLAS map artifacts."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys

from atlas_analyzer.artifact_io import atomic_write_text
from atlas_analyzer.config import AnalysisConfig
from atlas_analyzer.models import MapArtifact

from .facts import SymbolTable
from .languages import classify, parse_file, unparsable_table
from .repository import ImportResolver, source_files
from .roles import build_role_classifier


def parse_source_file(path: Path) -> SymbolTable:
    """Parse one source file, degrading to an import-free table (with a
    stderr warning) when the file cannot be parsed; one legacy or non-UTF-8
    file must not abort analysis of the whole repository. Full and
    incremental analysis share this policy so their artifacts stay
    byte-identical."""
    try:
        return parse_file(path)
    except (SyntaxError, UnicodeDecodeError, ValueError) as error:
        print(f"atlas: imports skipped for unparsable {path}: {error}", file=sys.stderr)
        return unparsable_table(path, classify(path) or "unknown")


@dataclass(frozen=True)
class IncrementalReport:
    changed_files: tuple[str, ...]
    parsed_files: int
    reused_files: int
    clustering: str


def _worktree_digest(root: Path, files: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _git_worktree_digest(root: Path, commit: str, dirty: list[str]) -> str:
    digest = hashlib.sha256()
    digest.update(commit.encode())
    digest.update(b"\0")
    for relative in dirty:
        digest.update(relative.encode())
        digest.update(b"\0")
        path = root / relative
        if path.is_file():
            digest.update(path.read_bytes())
        else:
            digest.update(b"<deleted>")
        digest.update(b"\0")
    return digest.hexdigest()


def _dirty_source_paths(root: Path) -> list[str]:
    result = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    paths: list[str] = []
    entries = result.stdout.split("\0")
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if not entry:
            continue
        status = entry[:2]
        value = entry[3:]
        if classify(Path(value)) is not None:
            paths.append(value)
        if ("R" in status or "C" in status) and index < len(entries):
            original = entries[index]
            index += 1
            if classify(Path(original)) is not None:
                paths.append(original)
    return sorted(paths)


def current_worktree_version(root: Path, commit: str) -> str:
    """Deterministic source version for COMMIT plus any dirty source paths;
    equals COMMIT exactly when the worktree is clean."""
    dirty = _dirty_source_paths(root)
    if not dirty:
        return commit
    return f"worktree:{commit}:{_git_worktree_digest(root, commit, dirty)}"


def _repository_identity(root: Path, files: list[Path]) -> tuple[str, str]:
    try:
        commit = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        timestamp = subprocess.run(
            ["git", "-C", str(root), "show", "-s", "--format=%cI", commit],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        generated_at = (
            datetime.fromisoformat(timestamp)
            .astimezone(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
        return current_worktree_version(root, commit), generated_at
    except (subprocess.CalledProcessError, ValueError):
        return f"worktree:{_worktree_digest(root, files)}", "1970-01-01T00:00:00Z"


def analyze_file_graph(
    root: Path, config: AnalysisConfig = AnalysisConfig()
) -> MapArtifact:
    root = root.resolve()
    files = source_files(root)
    tables = [parse_source_file(path) for path in files]
    resolver = ImportResolver(root, files, tables=tables)
    classifier = build_role_classifier(config)
    relative = {path.resolve(): path.relative_to(root).as_posix() for path in files}

    edge_evidence: dict[tuple[str, str], set[tuple[str, int]]] = defaultdict(set)
    edge_symbols: dict[tuple[str, str], set[str]] = defaultdict(set)
    for table in tables:
        source = relative[table.path.resolve()]
        for fact in table.imports:
            target_path = resolver.resolve(table.path, fact)
            if target_path is not None and target_path != table.path.resolve():
                key = (source, relative[target_path])
                edge_evidence[key].add((source, fact.line))
                edge_symbols[key].update(fact.symbols)

    fan_in: dict[str, int] = defaultdict(int)
    fan_out: dict[str, int] = defaultdict(int)
    for source, target in edge_evidence:
        fan_out[source] += 1
        fan_in[target] += 1

    nodes = []
    for table in tables:
        path = relative[table.path.resolve()]
        nodes.append(
            {
                "id": f"file:{path}",
                "kind": "file",
                "role": classifier.role_for(path),
                "label": table.path.name,
                "summary": "",
                "prose_source": "heuristic",
                "children": [],
                "files": [path],
                "metrics": {
                    "loc": table.loc,
                    "fan_in": fan_in[path],
                    "fan_out": fan_out[path],
                },
            }
        )

    edges = [
        {
            "source": f"file:{source}",
            "target": f"file:{target}",
            "kind": "imports",
            "evidence": [
                {"file": file, "line": line}
                for file, line in sorted(edge_evidence[(source, target)])
            ],
            "weight": len(edge_evidence[(source, target)]),
            "label": ", ".join(sorted(edge_symbols[(source, target)])) or None,
        }
        for source, target in sorted(edge_evidence)
    ]
    commit, generated_at = _repository_identity(root, files)
    return MapArtifact.model_validate(
        {
            "schema_version": "1.0",
            "repo": {"root": ".", "commit": commit, "generated_at": generated_at},
            "nodes": nodes,
            "edges": edges,
            "levels": {"system": [], "component": {}, "module": {}},
            "capabilities": {"supported_edge_kinds": ["imports"]},
        }
    )


def analyze_repository(
    root: Path, config: AnalysisConfig = AnalysisConfig()
) -> MapArtifact:
    from atlas_analyzer.abstraction import build_layered_map

    return build_layered_map(analyze_file_graph(root, config), root.resolve())


def _base_commit(version: str) -> str:
    if version.startswith("worktree:"):
        parts = version.split(":")
        if len(parts) == 3:
            return parts[1]
        raise ValueError("incremental analysis requires a map based on a Git commit")
    return version


def changed_source_paths(root: Path, version: str) -> tuple[str, ...]:
    """Return source paths changed between VERSION and the current worktree."""
    root = root.resolve()
    commit = _base_commit(version)
    try:
        tracked = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "diff",
                "--name-only",
                "-z",
                "--no-renames",
                commit,
                "--",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.split("\0")
        untracked = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "ls-files",
                "--others",
                "--exclude-standard",
                "-z",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.split("\0")
    except subprocess.CalledProcessError as error:
        raise ValueError(
            f"could not diff repository from map commit {commit}"
        ) from error
    return tuple(
        sorted(
            {
                path
                for path in (*tracked, *untracked)
                if classify(Path(path)) is not None
            }
        )
    )


def _incremental_file_graph(
    root: Path,
    previous: MapArtifact,
    changed: tuple[str, ...],
    config: AnalysisConfig,
) -> MapArtifact | None:
    previous_nodes = {
        node.id: node.model_dump(mode="json", exclude_none=True)
        for node in previous.nodes
        if node.kind.value == "file"
    }
    previous_paths = {node_id.removeprefix("file:") for node_id in previous_nodes}
    if any(
        path not in previous_paths or not (root / path).is_file() for path in changed
    ):
        return None
    relative = {path: root / path for path in sorted(previous_paths)}
    files = list(relative.values())

    classifier = build_role_classifier(config)
    changed_existing = [path for path in changed if path in relative]
    tables = [parse_source_file(relative[path]) for path in changed_existing]
    resolver = ImportResolver(root, files, tables=tables)
    path_by_resolved = {
        path.resolve(): relative_path for relative_path, path in relative.items()
    }
    changed_ids = {f"file:{path}" for path in changed_existing}
    edges = [
        edge.model_dump(mode="json", exclude_none=True)
        for edge in previous.edges
        if edge.source.startswith("file:")
        and edge.target.startswith("file:")
        and edge.source not in changed_ids
    ]
    for table in tables:
        source = table.path.relative_to(root).as_posix()
        evidence: dict[str, set[tuple[str, int]]] = defaultdict(set)
        symbols: dict[str, set[str]] = defaultdict(set)
        for fact in table.imports:
            target_path = resolver.resolve(table.path, fact)
            if target_path is None or target_path == table.path.resolve():
                continue
            target = path_by_resolved[target_path]
            evidence[target].add((source, fact.line))
            symbols[target].update(fact.symbols)
        edges.extend(
            {
                "source": f"file:{source}",
                "target": f"file:{target}",
                "kind": "imports",
                "evidence": [
                    {"file": file, "line": line}
                    for file, line in sorted(evidence[target])
                ],
                "label": ", ".join(sorted(symbols[target])) or None,
            }
            for target in sorted(evidence)
        )

    fan_in: dict[str, int] = defaultdict(int)
    fan_out: dict[str, int] = defaultdict(int)
    for edge in edges:
        fan_out[edge["source"]] += 1
        fan_in[edge["target"]] += 1
    table_by_id = {
        f"file:{table.path.relative_to(root).as_posix()}": table for table in tables
    }
    nodes = []
    for node_id in sorted(previous_nodes):
        node = previous_nodes[node_id]
        node["metrics"] = dict(node["metrics"])
        # Recompute role from the path with the same classifier the full run
        # uses, so incremental output stays byte-identical and never inherits a
        # stale role from a map produced before this field existed.
        node["role"] = classifier.role_for(node_id.removeprefix("file:"))
        if node_id in table_by_id:
            node["metrics"]["loc"] = table_by_id[node_id].loc
            node["summary"] = ""
            node["prose_source"] = "heuristic"
        node["metrics"]["fan_in"] = fan_in[node_id]
        node["metrics"]["fan_out"] = fan_out[node_id]
        nodes.append(node)

    # Weight is a pure function of evidence; recompute it (rather than trust a
    # reused edge) so incremental output matches a full run even against a
    # baseline produced before this field existed.
    for edge in edges:
        edge["weight"] = len(edge["evidence"])
    commit, generated_at = _repository_identity(root, files)
    return MapArtifact.model_validate(
        {
            "schema_version": "1.0",
            "repo": {"root": ".", "commit": commit, "generated_at": generated_at},
            "nodes": nodes,
            "edges": sorted(
                edges, key=lambda edge: (edge["source"], edge["target"], edge["kind"])
            ),
            "levels": {"system": [], "component": {}, "module": {}},
            "capabilities": {"supported_edge_kinds": ["imports"]},
        }
    )


def analyze_repository_incremental(
    root: Path,
    previous: MapArtifact,
    config: AnalysisConfig = AnalysisConfig(),
) -> tuple[MapArtifact, IncrementalReport]:
    """Update a map by parsing only source files changed from its source commit."""
    from atlas_analyzer.abstraction import build_layered_map
    from atlas_analyzer.abstraction.layering import refresh_layered_map

    root = root.resolve()
    changed = changed_source_paths(root, previous.repo.commit)
    previous_file_count = sum(node.kind.value == "file" for node in previous.nodes)
    if not changed:
        return previous, IncrementalReport((), 0, previous_file_count, "reused")

    file_map = _incremental_file_graph(root, previous, changed, config)
    if file_map is None:
        artifact = analyze_repository(root, config)
        file_count = sum(node.kind.value == "file" for node in artifact.nodes)
        return artifact, IncrementalReport(
            changed,
            file_count,
            0,
            "full (file set changed)",
        )

    changed_ids = {f"file:{path}" for path in changed}
    artifact = refresh_layered_map(file_map, previous, root, changed_ids)
    if artifact is None:
        artifact = build_layered_map(file_map, root)
        clustering = "full (dependency weights changed)"
    else:
        clustering = "affected communities"
    return artifact, IncrementalReport(
        changed,
        len(changed),
        previous_file_count - len(changed),
        clustering,
    )


def write_map(artifact: MapArtifact, output: Path) -> None:
    payload = artifact.model_dump(mode="json", exclude_none=True)
    atomic_write_text(output, json.dumps(payload, indent=2, sort_keys=True) + "\n")

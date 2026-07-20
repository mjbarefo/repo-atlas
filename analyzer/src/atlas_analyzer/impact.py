"""Project a local Git comparison onto an existing ATLAS map."""

from __future__ import annotations

from dataclasses import dataclass
import heapq
import json
from pathlib import Path
import subprocess

import networkx as nx

from .analysis.analyzer import current_worktree_version
from .artifact_io import atomic_write_text
from .models import ImpactArtifact, MapArtifact


@dataclass(frozen=True)
class GitChange:
    path: str
    status: str
    old_path: str | None = None


_STATUSES = {
    "A": "added",
    "M": "modified",
    "D": "deleted",
    "R": "renamed",
    "C": "copied",
    "T": "type_changed",
}


def _git(repo: Path, *arguments: str) -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *arguments],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except subprocess.CalledProcessError as error:
        detail = error.stderr.strip() or error.stdout.strip() or "Git command failed"
        raise ValueError(detail) from error


def _commit(repo: Path, ref: str) -> str:
    return _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").strip()


def _map_base_commit(version: str) -> str:
    if version.startswith("worktree:"):
        parts = version.split(":")
        if len(parts) == 3:
            return parts[1]
        raise ValueError(f"invalid worktree map version: {version}")
    return version


def _parse_name_status(payload: str) -> list[GitChange]:
    fields = payload.split("\0")
    changes: list[GitChange] = []
    index = 0
    while index < len(fields):
        code = fields[index]
        index += 1
        if not code:
            continue
        kind = code[0]
        if kind not in _STATUSES:
            raise ValueError(f"unsupported Git change status: {code}")
        if kind in {"R", "C"}:
            if index + 1 >= len(fields):
                raise ValueError("truncated Git rename/copy record")
            old_path = fields[index]
            path = fields[index + 1]
            index += 2
            changes.append(GitChange(path, _STATUSES[kind], old_path))
        else:
            if index >= len(fields):
                raise ValueError("truncated Git change record")
            path = fields[index]
            index += 1
            changes.append(GitChange(path, _STATUSES[kind]))
    return changes


def changed_files(
    repo: Path,
    base: str,
    head: str | None = None,
) -> tuple[str, str, list[GitChange]]:
    """Return resolved comparison refs and exact local Git path changes."""
    repo = repo.resolve()
    base_commit = _commit(repo, base)
    arguments = [
        "diff",
        "--name-status",
        "-z",
        "--find-renames",
        base_commit,
    ]
    if head is not None:
        head_commit = _commit(repo, head)
        arguments.append(head_commit)
    else:
        head_commit = _commit(repo, "HEAD")
    arguments.append("--")
    changes = _parse_name_status(_git(repo, *arguments))

    if head is None:
        tracked = {change.path for change in changes}
        untracked = _git(
            repo,
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
        ).split("\0")
        changes.extend(
            GitChange(path, "added")
            for path in untracked
            if path and path not in tracked and not path.startswith(".atlas/")
        )
    return (
        base_commit,
        head_commit,
        sorted(
            changes,
            key=lambda change: (change.path, change.status, change.old_path or ""),
        ),
    )


def _file_nodes(artifact: MapArtifact) -> dict[str, str]:
    return {
        node.files[0].root: node.id
        for node in artifact.nodes
        if node.kind.value == "file" and len(node.files) == 1
    }


def _review_order(
    artifact: MapArtifact,
    changes: list[GitChange],
    node_by_path: dict[str, str],
) -> list[str]:
    path_by_node = {
        node_by_path[change.path]: change.path
        for change in changes
        if change.path in node_by_path
    }
    changed_nodes = set(path_by_node)
    graph = nx.DiGraph()
    graph.add_nodes_from(sorted(changed_nodes))
    graph.add_edges_from(
        sorted(
            (edge.source, edge.target)
            for edge in artifact.edges
            if edge.source in changed_nodes and edge.target in changed_nodes
        )
    )
    groups = sorted(
        (
            tuple(sorted(component))
            for component in nx.strongly_connected_components(graph)
        ),
        key=lambda group: group,
    )
    group_by_node = {
        node: group_index for group_index, group in enumerate(groups) for node in group
    }
    dependents: dict[int, set[int]] = {index: set() for index in range(len(groups))}
    indegree = [0] * len(groups)
    for source, target in graph.edges:
        source_group = group_by_node[source]
        target_group = group_by_node[target]
        if source_group == target_group or source_group in dependents[target_group]:
            continue
        dependents[target_group].add(source_group)
        indegree[source_group] += 1
    ready = [
        (groups[index], index) for index, degree in enumerate(indegree) if degree == 0
    ]
    heapq.heapify(ready)
    ordered_nodes: list[str] = []
    while ready:
        _, group_index = heapq.heappop(ready)
        ordered_nodes.extend(groups[group_index])
        for dependent in sorted(dependents[group_index], key=groups.__getitem__):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                heapq.heappush(ready, (groups[dependent], dependent))
    ordered_paths = [path_by_node[node] for node in ordered_nodes]
    ordered_paths.extend(
        sorted(change.path for change in changes if change.path not in node_by_path)
    )
    return ordered_paths


def build_impact(
    repo: Path,
    artifact: MapArtifact,
    *,
    base: str,
    head: str | None = None,
) -> ImpactArtifact:
    """Build a deterministic change-impact artifact for one local comparison."""
    base_commit, head_commit, changes = changed_files(repo, base, head)
    map_version = artifact.repo.commit
    map_commit = _map_base_commit(map_version)
    if map_commit != head_commit:
        selected = f"head {head!r}" if head is not None else "the current HEAD"
        raise ValueError(
            f"map is based on {map_commit}, but {selected} resolves to {head_commit}; "
            "analyze the selected head before building impact"
        )
    if map_version.startswith("worktree:"):
        if head is not None:
            raise ValueError(
                "map was built from a dirty worktree and cannot describe "
                f"committed head {head!r}; commit or re-analyze first"
            )
        # The digest half of a worktree version captures the dirty file
        # contents at analysis time. If it no longer matches, the map's edges
        # describe a worktree state that no longer exists and would project
        # phantom dependencies onto the review.
        if current_worktree_version(repo.resolve(), map_commit) != map_version:
            raise ValueError(
                "map no longer matches the current worktree state; re-run "
                "atlas analyze (or analyze --incremental) before building impact"
            )

    node_by_path = _file_nodes(artifact)
    changed_nodes = {
        node_by_path[change.path] for change in changes if change.path in node_by_path
    }
    file_node_ids = set(node_by_path.values())
    dependent_pairs = sorted(
        {
            (edge.target, edge.source)
            for edge in artifact.edges
            if edge.target in changed_nodes
            and edge.source in file_node_ids
            and edge.source not in changed_nodes
        }
    )
    payload = {
        "schema_version": "1.0",
        "map_ref": {"commit": artifact.repo.commit},
        "comparison": {
            "base": base_commit,
            "head": artifact.repo.commit if head is None else head_commit,
        },
        "files": [
            {
                "path": change.path,
                "old_path": change.old_path,
                "status": change.status,
                "node_id": node_by_path.get(change.path),
            }
            for change in changes
        ],
        "direct_dependents": [
            {
                "changed_node_id": changed,
                "dependent_node_id": dependent,
            }
            for changed, dependent in dependent_pairs
        ],
        "review_order": _review_order(artifact, changes, node_by_path),
        "summary": {
            "changed_files": len(changes),
            "mapped_files": sum(change.path in node_by_path for change in changes),
            "direct_dependents": len({dependent for _, dependent in dependent_pairs}),
        },
    }
    return ImpactArtifact.model_validate(payload)


def write_impact(artifact: ImpactArtifact, output: Path) -> None:
    payload = artifact.model_dump(mode="json", exclude_none=True)
    atomic_write_text(output, json.dumps(payload, indent=2, sort_keys=True) + "\n")

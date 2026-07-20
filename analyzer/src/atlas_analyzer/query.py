"""Read-only graph queries over a completed ATLAS map."""

from collections import Counter
import json
from pathlib import Path
import subprocess

import networkx as nx

from atlas_analyzer.models import MapArtifact


def load_map(path: Path) -> MapArtifact:
    return MapArtifact.model_validate(json.loads(path.read_text()))


def dependencies(
    artifact: MapArtifact,
    node_id: str,
    *,
    reverse: bool = False,
) -> list[str]:
    known = {node.id for node in artifact.nodes}
    if node_id not in known:
        raise KeyError(node_id)
    if reverse:
        return sorted(
            {edge.source for edge in artifact.edges if edge.target == node_id}
        )
    return sorted({edge.target for edge in artifact.edges if edge.source == node_id})


def cycles(artifact: MapArtifact) -> list[tuple[str, ...]]:
    graph = nx.DiGraph()
    graph.add_nodes_from(sorted(node.id for node in artifact.nodes))
    graph.add_edges_from(sorted((edge.source, edge.target) for edge in artifact.edges))
    result = []
    cyclic_regions = [
        component
        for component in nx.strongly_connected_components(graph)
        if len(component) > 1 or any(graph.has_edge(node, node) for node in component)
    ]
    for component in sorted(cyclic_regions, key=lambda items: tuple(sorted(items))):
        subgraph = graph.subgraph(component)
        edges = nx.find_cycle(subgraph, source=min(component))
        cycle = [source for source, _ in edges]
        smallest = min(range(len(cycle)), key=cycle.__getitem__)
        normalized = tuple(cycle[smallest:] + cycle[:smallest])
        result.append(normalized)
    return result


def _git_churn(repo: Path) -> Counter[str]:
    try:
        output = subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "log",
                "--format=",
                "--name-only",
                "--no-renames",
                "--",
                ".",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except subprocess.CalledProcessError:
        return Counter()
    return Counter(line.strip() for line in output.splitlines() if line.strip())


def _descendant_files(artifact: MapArtifact) -> dict[str, set[str]]:
    nodes = {node.id: node for node in artifact.nodes}
    result: dict[str, set[str]] = {}

    def resolve(node_id: str) -> set[str]:
        if node_id in result:
            return result[node_id]
        node = nodes[node_id]
        if node.kind.value == "file":
            files = {item.root for item in node.files}
        else:
            files = {path for child in node.children for path in resolve(child.root)}
        result[node_id] = files
        return files

    for node_id in sorted(nodes):
        resolve(node_id)
    return result


def hotspots(
    artifact: MapArtifact,
    repo: Path,
    *,
    limit: int = 20,
) -> list[tuple[int, int, int, str]]:
    churn = _git_churn(repo)
    files_by_node = _descendant_files(artifact)
    ranked = []
    for node in artifact.nodes:
        node_churn = sum(churn[path] for path in files_by_node[node.id])
        fan_in = node.metrics.fan_in
        ranked.append((fan_in * node_churn, fan_in, node_churn, node.id))
    return sorted(
        ranked,
        key=lambda item: (-item[0], -item[1], -item[2], item[3]),
    )[:limit]

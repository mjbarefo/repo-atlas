"""Build deterministic module and component layers over the file graph."""

import ast
from collections import defaultdict
import hashlib
import posixpath
from pathlib import Path, PurePosixPath
import re
from typing import Any

import networkx as nx

from atlas_analyzer.models import MapArtifact

GENERIC_DIRECTORIES = {
    ".",
    "app",
    "apps",
    "lib",
    "libs",
    "pkg",
    "packages",
    "source",
    "src",
}


def _path_for_file_node(node: dict[str, Any]) -> str:
    return node["files"][0]


def _is_source(node: dict[str, Any]) -> bool:
    """Only production source forms and names architecture. Non-source file
    nodes (test, fixture, generated, vendored) are kept in the map but never
    cluster into modules or components. The default preserves older maps that
    predate the ``role`` field."""
    return node.get("role", "source") == "source"


def _module_anchor(path: str) -> str:
    parts = PurePosixPath(path).parts
    parents = parts[:-1]
    if not parents:
        return "."
    return "/".join(parents[:2])


def _component_anchor(path: str) -> str:
    parts = PurePosixPath(path).parts
    return parts[0] if len(parts) > 1 else "."


def _graph(
    node_ids: list[str],
    edges: list[dict[str, Any]],
) -> nx.Graph:
    graph = nx.Graph()
    graph.add_nodes_from(sorted(node_ids))
    for edge in sorted(edges, key=lambda item: (item["source"], item["target"])):
        source = edge["source"]
        target = edge["target"]
        if source not in graph or target not in graph or source == target:
            continue
        weight = len(edge["evidence"])
        if graph.has_edge(source, target):
            graph[source][target]["weight"] += weight
        else:
            graph.add_edge(source, target, weight=weight)
    return graph


def _directory_constrained_communities(
    graph: nx.Graph,
    anchors: dict[str, str],
) -> list[tuple[str, ...]]:
    isolated = {node for node, degree in graph.degree if degree == 0}
    active = graph.subgraph(sorted(set(graph.nodes) - isolated))
    raw = (
        nx.community.louvain_communities(active, seed=0, weight="weight")
        if active.number_of_edges()
        else []
    )
    constrained: list[list[str]] = []
    for community in raw:
        grouped: dict[str, list[str]] = defaultdict(list)
        for node in sorted(community):
            grouped[anchors[node]].append(node)
        # A module or component never spans more than one anchor. Each anchor a
        # Louvain community touches becomes its own group, so genuine
        # cross-directory coupling survives as edges rather than shared
        # membership (a single-anchor community is unchanged).
        for _, nodes in sorted(grouped.items()):
            constrained.append(sorted(nodes))

    orphans: dict[str, list[str]] = defaultdict(list)
    for node in sorted(isolated):
        anchor = anchors[node]
        candidates = [
            (index, community)
            for index, community in enumerate(constrained)
            if any(anchors[member] == anchor for member in community)
        ]
        if candidates:
            index, community = min(
                candidates,
                key=lambda item: (
                    -sum(anchors[member] == anchor for member in item[1]),
                    tuple(item[1]),
                ),
            )
            constrained[index] = sorted([*community, node])
        else:
            orphans[anchor].append(node)
    constrained.extend(nodes for _, nodes in sorted(orphans.items()))
    return sorted(tuple(community) for community in constrained)


def _merge_by_common_directory(
    communities: list[tuple[str, ...]],
    path_by_node: dict[str, str],
) -> list[tuple[str, ...]]:
    """Re-merge communities that resolve to the same common (leaf) directory.

    Louvain can split one leaf package into several communities; grouping them
    back by their shared directory keeps a single package a single module
    (instead of ``Foo`` / ``Foo 2``), while distinct directories stay separate so
    genuine cross-directory coupling survives as edges rather than membership.
    Two communities with an identical common directory necessarily share the
    same anchor, so this never fuses across anchors.
    """
    grouped: dict[str, list[str]] = defaultdict(list)
    for community in communities:
        key = _common_directory([path_by_node[node] for node in community])
        grouped[key].extend(community)
    return sorted(tuple(sorted(members)) for members in grouped.values())


def _title(value: str) -> str:
    words = re.sub(r"[^A-Za-z0-9]+", " ", value).strip().split()
    return " ".join(word.upper() if len(word) <= 2 else word.title() for word in words)


def _common_directory(paths: list[str]) -> str:
    parents = [PurePosixPath(path).parent.as_posix() for path in paths]
    return posixpath.commonpath(parents) if parents else "."


def _base_label(
    paths: list[str],
    file_nodes: dict[str, dict[str, Any]],
) -> str:
    if len(paths) == 1:
        return _title(PurePosixPath(paths[0]).stem)

    common = _common_directory(paths)
    for segment in reversed(PurePosixPath(common).parts):
        if segment and segment.lower() not in GENERIC_DIRECTORIES:
            return _title(segment)

    ranked = sorted(
        paths,
        key=lambda path: (
            PurePosixPath(path).stem.lower() in {"__init__", "index"},
            -file_nodes[f"file:{path}"]["metrics"]["fan_in"],
            path,
        ),
    )
    return _title(PurePosixPath(ranked[0]).stem)


def _distinguishing_segment(common: str, siblings: list[str]) -> str:
    """The nearest common-directory segment that sets ``common`` apart from the
    colliding ``siblings``.

    Walk up from the shared leaf so ``alpha/api`` and ``beta/api`` disambiguate
    on ``alpha`` / ``beta`` rather than the shared ``api``.
    """
    parts = PurePosixPath(common).parts
    sibling_parts = [PurePosixPath(sibling).parts for sibling in siblings]
    shared = 0
    while all(
        len(parts) > shared
        and len(other) > shared
        and other[-1 - shared] == parts[-1 - shared]
        for other in sibling_parts
    ):
        shared += 1
    remainder = parts[: len(parts) - shared] if shared < len(parts) else parts
    if remainder:
        return remainder[-1]
    return parts[-1] if parts else ""


def _deduplicate_labels(labels: list[str], common_dirs: list[str]) -> list[str]:
    """Give every layer a unique label.

    Genuine base-label collisions (distinct directories that share a leaf name)
    are disambiguated by the distinguishing path segment, so ``Api`` / ``Api``
    become ``Api / Alpha`` / ``Api / Beta``. A bare numeric counter is only a
    last-resort safety net, keeping the result unique and deterministic even if
    a segment cannot separate two labels.
    """
    groups: dict[str, list[int]] = defaultdict(list)
    for index, label in enumerate(labels):
        groups[label].append(index)

    resolved = list(labels)
    for label, indices in groups.items():
        if len(indices) == 1:
            continue
        for index in indices:
            siblings = [common_dirs[other] for other in indices if other != index]
            segment = _distinguishing_segment(common_dirs[index], siblings)
            if segment:
                resolved[index] = f"{label} / {_title(segment)}"

    used: set[str] = set()
    next_suffix: dict[str, int] = defaultdict(lambda: 2)
    result: list[str] = []
    for label in resolved:
        candidate = label
        while candidate in used:
            candidate = f"{label} {next_suffix[label]}"
            next_suffix[label] += 1
        used.add(candidate)
        result.append(candidate)
    return result


def _node_id(prefix: str, label: str, children: tuple[str, ...]) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:32] or prefix
    digest = hashlib.sha256("\0".join(children).encode()).hexdigest()[:10]
    return f"{prefix}:{slug}-{digest}"


def _first_docstring(root: Path, paths: list[str]) -> str | None:
    python_paths = sorted(
        (path for path in paths if path.endswith(".py")),
        key=lambda path: (PurePosixPath(path).name != "__init__.py", path),
    )
    for relative in python_paths:
        try:
            module = ast.parse((root / relative).read_text())
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        docstring = ast.get_docstring(module, clean=True)
        if docstring:
            return docstring.splitlines()[0].strip()
    return None


def _readme_heading(root: Path, paths: list[str]) -> str | None:
    common = _common_directory(paths)
    if not common or common == ".":
        return None
    directory = root / common
    for name in ("README.md", "README.rst", "README.txt"):
        readme = directory / name
        if not readme.is_file():
            continue
        try:
            for line in readme.read_text().splitlines():
                stripped = line.lstrip()
                if stripped.startswith("#"):
                    heading = stripped.lstrip("#").strip()
                else:
                    heading = ""
                if heading:
                    return heading
        except (OSError, UnicodeDecodeError):
            continue
    return None


def _export_names(root: Path, paths: list[str]) -> list[str]:
    names: set[str] = set()
    for relative in paths:
        path = root / relative
        try:
            source = path.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        if path.suffix == ".py":
            try:
                module = ast.parse(source)
            except SyntaxError:
                continue
            for statement in module.body:
                if isinstance(
                    statement,
                    (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
                ):
                    names.add(statement.name)
        else:
            names.update(
                re.findall(
                    r"\bexport\s+(?:default\s+)?(?:async\s+)?"
                    r"(?:function|class|const|let|var)\s+([A-Za-z_$][\w$]*)",
                    source,
                )
            )
    return sorted(names)[:5]


def _summary(root: Path, paths: list[str], loc: int) -> str:
    extracted = _first_docstring(root, paths) or _readme_heading(root, paths)
    if extracted:
        return extracted[:240]
    exports = _export_names(root, paths)
    exported = f"; exports {', '.join(exports)}" if exports else ""
    noun = "file" if len(paths) == 1 else "files"
    return f"{len(paths)} {noun}, {loc} LOC{exported}."


def _rolled_edges(
    edges: list[dict[str, Any]],
    parent_by_child: dict[str, str],
) -> list[dict[str, Any]]:
    evidence: dict[tuple[str, str], set[tuple[str, int]]] = defaultdict(set)
    symbols: dict[tuple[str, str], set[str]] = defaultdict(set)
    for edge in edges:
        source = parent_by_child.get(edge["source"])
        target = parent_by_child.get(edge["target"])
        if source is None or target is None or source == target:
            continue
        key = (source, target)
        evidence[key].update((item["file"], item["line"]) for item in edge["evidence"])
        if edge.get("label"):
            symbols[key].update(
                symbol.strip()
                for symbol in edge["label"].split(",")
                if symbol.strip() and symbol.strip() != "*"
            )

    return [
        {
            "source": source,
            "target": target,
            "kind": "imports",
            "evidence": [
                {"file": file, "line": line}
                for file, line in sorted(evidence[(source, target)])
            ],
            "label": ", ".join(sorted(symbols[(source, target)])[:5]) or None,
        }
        for source, target in sorted(evidence)
    ]


def _layer_metrics(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> None:
    fan_in: dict[str, int] = defaultdict(int)
    fan_out: dict[str, int] = defaultdict(int)
    for edge in edges:
        fan_out[edge["source"]] += 1
        fan_in[edge["target"]] += 1
    for node in nodes:
        node["metrics"]["fan_in"] = fan_in[node["id"]]
        node["metrics"]["fan_out"] = fan_out[node["id"]]


def build_layered_map(file_map: MapArtifact, root: Path) -> MapArtifact:
    payload = file_map.model_dump(mode="json", exclude_none=True)
    file_nodes = {
        node["id"]: node for node in payload["nodes"] if node["kind"] == "file"
    }
    file_edges = [
        edge
        for edge in payload["edges"]
        if edge["source"] in file_nodes and edge["target"] in file_nodes
    ]
    # Community detection, labels, and fan-in ranking run over source file
    # nodes only. Non-source nodes stay in `file_nodes` (emitted below with
    # their edges) but never join a module, so they can neither form nor name
    # architecture. `_graph` drops the edges whose endpoints fall outside this
    # set, keeping non-source coupling out of the clustering weights.
    source_file_nodes = {
        node_id: node for node_id, node in file_nodes.items() if _is_source(node)
    }
    source_paths = {
        node_id: _path_for_file_node(node)
        for node_id, node in source_file_nodes.items()
    }

    file_graph = _graph(list(source_file_nodes), file_edges)
    module_communities = _directory_constrained_communities(
        file_graph,
        {node_id: _module_anchor(path) for node_id, path in source_paths.items()},
    )
    module_communities = _merge_by_common_directory(module_communities, source_paths)
    module_paths = [
        sorted(source_paths[node_id] for node_id in community)
        for community in module_communities
    ]
    module_labels = _deduplicate_labels(
        [_base_label(paths, source_file_nodes) for paths in module_paths],
        [_common_directory(paths) for paths in module_paths],
    )

    modules: list[dict[str, Any]] = []
    module_by_file: dict[str, str] = {}
    paths_by_module: dict[str, list[str]] = {}
    for community, paths, label in zip(
        module_communities, module_paths, module_labels, strict=True
    ):
        module_id = _node_id("mod", label, community)
        loc = sum(source_file_nodes[node_id]["metrics"]["loc"] for node_id in community)
        modules.append(
            {
                "id": module_id,
                "kind": "module",
                "role": "source",
                "label": label,
                "summary": _summary(root, paths, loc),
                "prose_source": "heuristic",
                "children": list(community),
                "files": [],
                "metrics": {"loc": loc, "fan_in": 0, "fan_out": 0},
            }
        )
        paths_by_module[module_id] = paths
        module_by_file.update((node_id, module_id) for node_id in community)

    module_edges = _rolled_edges(file_edges, module_by_file)
    _layer_metrics(modules, module_edges)
    module_nodes = {node["id"]: node for node in modules}
    module_graph = _graph(list(module_nodes), module_edges)
    component_communities = _directory_constrained_communities(
        module_graph,
        {
            module_id: _component_anchor(paths_by_module[module_id][0])
            for module_id in module_nodes
        },
    )
    component_paths = [
        sorted({path for module_id in community for path in paths_by_module[module_id]})
        for community in component_communities
    ]
    component_base_labels = []
    for community, paths in zip(component_communities, component_paths, strict=True):
        if len(community) == 1:
            component_base_labels.append(module_nodes[community[0]]["label"])
        else:
            component_base_labels.append(_base_label(paths, source_file_nodes))
    component_labels = _deduplicate_labels(
        component_base_labels,
        [_common_directory(paths) for paths in component_paths],
    )

    components: list[dict[str, Any]] = []
    component_by_module: dict[str, str] = {}
    for community, paths, label in zip(
        component_communities,
        component_paths,
        component_labels,
        strict=True,
    ):
        component_id = _node_id("comp", label, community)
        loc = sum(module_nodes[node_id]["metrics"]["loc"] for node_id in community)
        components.append(
            {
                "id": component_id,
                "kind": "component",
                "role": "source",
                "label": label,
                "summary": _summary(root, paths, loc),
                "prose_source": "heuristic",
                "children": list(community),
                "files": [],
                "metrics": {"loc": loc, "fan_in": 0, "fan_out": 0},
            }
        )
        component_by_module.update((node_id, component_id) for node_id in community)

    component_edges = _rolled_edges(module_edges, component_by_module)
    _layer_metrics(components, component_edges)

    payload["nodes"] = [
        *sorted(components, key=lambda item: item["id"]),
        *sorted(modules, key=lambda item: item["id"]),
        *sorted(file_nodes.values(), key=lambda item: item["id"]),
    ]
    payload["edges"] = sorted(
        [*file_edges, *module_edges, *component_edges],
        key=lambda item: (item["source"], item["target"], item["kind"]),
    )
    payload["levels"] = {
        "system": sorted(component_by_module.values()),
        "component": {
            component["id"]: sorted(component["children"])
            for component in sorted(components, key=lambda item: item["id"])
        },
        "module": {
            module["id"]: sorted(module["children"])
            for module in sorted(modules, key=lambda item: item["id"])
        },
    }
    payload["levels"]["system"] = sorted(set(payload["levels"]["system"]))
    return MapArtifact.model_validate(payload)


def refresh_layered_map(
    file_map: MapArtifact,
    previous: MapArtifact,
    root: Path,
    changed_file_ids: set[str],
) -> MapArtifact | None:
    """Refresh unchanged communities without running global Louvain again.

    Community membership is reusable only when the weighted file graph and the
    per-file ``role`` are unchanged. A topology, weight, or role change returns
    ``None`` so the caller can conservatively rebuild every layer.
    """
    payload = file_map.model_dump(mode="json", exclude_none=True)
    previous_payload = previous.model_dump(mode="json", exclude_none=True)
    file_nodes = {
        node["id"]: node for node in payload["nodes"] if node["kind"] == "file"
    }
    file_edges = [
        edge
        for edge in payload["edges"]
        if edge["source"] in file_nodes and edge["target"] in file_nodes
    ]
    previous_file_nodes = {
        node["id"]: node for node in previous_payload["nodes"] if node["kind"] == "file"
    }
    if set(file_nodes) != set(previous_file_nodes):
        return None
    # A role reclassification (for example a changed [analysis] override) can
    # move a file into or out of architecture, invalidating the reused
    # community membership; rebuild fully so incremental output still matches a
    # clean run.
    if any(
        file_nodes[node_id].get("role", "source")
        != previous_file_nodes[node_id].get("role", "source")
        for node_id in file_nodes
    ):
        return None

    def weights(edges: list[dict[str, Any]]) -> dict[tuple[str, str], int]:
        return {
            (edge["source"], edge["target"]): len(edge["evidence"])
            for edge in edges
            if edge["source"].startswith("file:") and edge["target"].startswith("file:")
        }

    if weights(file_edges) != weights(previous_payload["edges"]):
        return None

    previous_modules = [
        node for node in previous_payload["nodes"] if node["kind"] == "module"
    ]
    modules: list[dict[str, Any]] = []
    module_by_file: dict[str, str] = {}
    paths_by_module: dict[str, list[str]] = {}
    affected_modules: set[str] = set()
    for old_module in previous_modules:
        module = dict(old_module)
        children = tuple(module["children"])
        paths = sorted(_path_for_file_node(file_nodes[node_id]) for node_id in children)
        if changed_file_ids.intersection(children):
            affected_modules.add(module["id"])
            loc = sum(file_nodes[node_id]["metrics"]["loc"] for node_id in children)
            module["summary"] = _summary(root, paths, loc)
            module["prose_source"] = "heuristic"
            module["metrics"] = {"loc": loc, "fan_in": 0, "fan_out": 0}
        else:
            module["metrics"] = dict(module["metrics"])
        modules.append(module)
        paths_by_module[module["id"]] = paths
        module_by_file.update((node_id, module["id"]) for node_id in children)

    module_edges = _rolled_edges(file_edges, module_by_file)
    _layer_metrics(modules, module_edges)
    module_nodes = {node["id"]: node for node in modules}

    components: list[dict[str, Any]] = []
    component_by_module: dict[str, str] = {}
    for old_component in (
        node for node in previous_payload["nodes"] if node["kind"] == "component"
    ):
        component = dict(old_component)
        children = tuple(component["children"])
        paths = sorted(
            {path for module_id in children for path in paths_by_module[module_id]}
        )
        if affected_modules.intersection(children):
            loc = sum(module_nodes[node_id]["metrics"]["loc"] for node_id in children)
            component["summary"] = _summary(root, paths, loc)
            component["prose_source"] = "heuristic"
            component["metrics"] = {"loc": loc, "fan_in": 0, "fan_out": 0}
        else:
            component["metrics"] = dict(component["metrics"])
        components.append(component)
        component_by_module.update((node_id, component["id"]) for node_id in children)

    component_edges = _rolled_edges(module_edges, component_by_module)
    _layer_metrics(components, component_edges)
    payload["nodes"] = [
        *sorted(components, key=lambda item: item["id"]),
        *sorted(modules, key=lambda item: item["id"]),
        *sorted(file_nodes.values(), key=lambda item: item["id"]),
    ]
    payload["edges"] = sorted(
        [*file_edges, *module_edges, *component_edges],
        key=lambda item: (item["source"], item["target"], item["kind"]),
    )
    payload["levels"] = previous_payload["levels"]
    return MapArtifact.model_validate(payload)

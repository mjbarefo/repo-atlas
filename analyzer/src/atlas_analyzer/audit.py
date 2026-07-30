"""Deterministic map-health findings and node explanations."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

import networkx as nx

from atlas_analyzer.models import MapArtifact
from atlas_analyzer.models.map import Audit, Node

LARGE_FILE_LOC = 800
LARGE_MODULE_FILES = 15
LARGE_MODULE_LOC = 2500
THIN_MODULE_LOC = 25


def _node_index(artifact: MapArtifact) -> dict[str, Node]:
    return {node.id: node for node in artifact.nodes}


def _finding(
    *,
    code: str,
    severity: str,
    node_ids: list[str],
    message: str,
    evidence: list[str],
) -> dict[str, Any]:
    return {
        "id": f"{code}:{node_ids[0]}",
        "code": code,
        "severity": severity,
        "message": message,
        "node_ids": sorted(node_ids),
        "evidence": evidence,
    }


def _module_cycle_findings(
    artifact: MapArtifact, nodes: dict[str, Node]
) -> list[dict[str, Any]]:
    module_ids = {
        node_id for node_id, node in nodes.items() if node.kind.value == "module"
    }
    graph = nx.DiGraph()
    graph.add_nodes_from(sorted(module_ids))
    graph.add_edges_from(
        sorted(
            (edge.source, edge.target)
            for edge in artifact.edges
            if edge.source in module_ids and edge.target in module_ids
        )
    )
    findings = []
    regions = [
        sorted(region)
        for region in nx.strongly_connected_components(graph)
        if len(region) > 1
        or any(graph.has_edge(node_id, node_id) for node_id in region)
    ]
    for region in sorted(regions):
        region_set = set(region)
        labels = [nodes[node_id].label for node_id in region]
        joined_labels = (
            labels[0]
            if len(labels) == 1
            else f"{', '.join(labels[:-1])} and {labels[-1]}"
        )
        evidence = []
        for edge in sorted(
            (
                edge
                for edge in artifact.edges
                if edge.source in region_set and edge.target in region_set
            ),
            key=lambda item: (item.source, item.target, item.kind.value),
        ):
            evidence.append(
                f"{nodes[edge.source].label} -> {nodes[edge.target].label}: "
                f"{edge.weight or len(edge.evidence)} {edge.kind.value} site(s)"
            )
        findings.append(
            _finding(
                code="module-cycle",
                severity="warning",
                node_ids=region,
                message=(f"{joined_labels} participate in a module dependency cycle."),
                evidence=evidence,
            )
        )
    return findings


def audit_map(artifact: MapArtifact) -> Audit:
    """Evaluate one completed map with stable, evidence-backed health rules."""
    nodes = _node_index(artifact)
    findings: list[dict[str, Any]] = []

    for node in sorted(artifact.nodes, key=lambda item: item.id):
        if (
            node.kind.value == "file"
            and node.role is not None
            and node.role.value == "source"
            and node.metrics.loc >= LARGE_FILE_LOC
        ):
            findings.append(
                _finding(
                    code="large-file",
                    severity="warning",
                    node_ids=[node.id],
                    message=(
                        f"{node.label} has {node.metrics.loc:,} LOC; inspect it for "
                        "multiple responsibilities."
                    ),
                    evidence=[
                        f"loc={node.metrics.loc}",
                        f"warning threshold={LARGE_FILE_LOC}",
                    ],
                )
            )
        if node.kind.value != "module":
            continue

        child_nodes = [
            nodes[child.root] for child in node.children if child.root in nodes
        ]
        source_files = [
            child
            for child in child_nodes
            if child.kind.value == "file"
            and child.role is not None
            and child.role.value == "source"
        ]
        if (
            len(source_files) >= LARGE_MODULE_FILES
            or node.metrics.loc >= LARGE_MODULE_LOC
        ):
            findings.append(
                _finding(
                    code="large-module",
                    severity="warning",
                    node_ids=[node.id],
                    message=(
                        f"{node.label} contains {len(source_files)} source files and "
                        f"{node.metrics.loc:,} LOC; inspect this boundary for hidden "
                        "submodules."
                    ),
                    evidence=[
                        f"source files={len(source_files)}",
                        f"loc={node.metrics.loc}",
                        (
                            f"warning thresholds={LARGE_MODULE_FILES} files or "
                            f"{LARGE_MODULE_LOC} LOC"
                        ),
                    ],
                )
            )
        if len(source_files) == 1 and node.metrics.loc <= THIN_MODULE_LOC:
            findings.append(
                _finding(
                    code="thin-module",
                    severity="notice",
                    node_ids=[node.id],
                    message=(
                        f"{node.label} is a {node.metrics.loc}-LOC singleton module; "
                        "it may be architectural noise."
                    ),
                    evidence=[
                        f"source files={len(source_files)}",
                        f"loc={node.metrics.loc}",
                        f"notice threshold={THIN_MODULE_LOC} LOC",
                    ],
                )
            )

    findings.extend(_module_cycle_findings(artifact, nodes))
    severity_order = {"warning": 0, "notice": 1}
    findings.sort(
        key=lambda item: (
            severity_order[item["severity"]],
            item["code"],
            item["id"],
        )
    )
    kinds = defaultdict(int)
    for node in artifact.nodes:
        kinds[node.kind.value] += 1
    return Audit.model_validate(
        {
            "ruleset": "1",
            "summary": {
                "files": kinds["file"],
                "modules": kinds["module"],
                "components": kinds["component"],
                "edges": len(artifact.edges),
                "warnings": sum(
                    finding["severity"] == "warning" for finding in findings
                ),
                "notices": sum(finding["severity"] == "notice" for finding in findings),
            },
            "findings": findings,
        }
    )


def attach_audit(artifact: MapArtifact) -> MapArtifact:
    """Return ARTIFACT with a freshly calculated ruleset-1 audit."""
    return artifact.model_copy(update={"audit": audit_map(artifact)})


def explain_node(artifact: MapArtifact, node_id: str) -> dict[str, Any]:
    """Return deterministic evidence describing one node and its relationships."""
    nodes = _node_index(artifact)
    if node_id not in nodes:
        raise KeyError(node_id)
    node = nodes[node_id]
    parent_by_child = {
        child.root: parent
        for parent in artifact.nodes
        for child in parent.children
        if child.root in nodes
    }
    hierarchy = []
    current = parent_by_child.get(node_id)
    while current is not None:
        hierarchy.append(
            {"id": current.id, "kind": current.kind.value, "label": current.label}
        )
        current = parent_by_child.get(current.id)
    hierarchy.reverse()

    relationships = []
    for edge in sorted(
        (
            edge
            for edge in artifact.edges
            if edge.source == node_id or edge.target == node_id
        ),
        key=lambda item: (item.source, item.target, item.kind.value),
    ):
        outgoing = edge.source == node_id
        other_id = edge.target if outgoing else edge.source
        other = nodes.get(other_id)
        relationships.append(
            {
                "direction": "depends_on" if outgoing else "used_by",
                "node": {
                    "id": other_id,
                    "kind": other.kind.value if other is not None else "unknown",
                    "label": other.label if other is not None else "Unknown",
                },
                "kind": edge.kind.value,
                "weight": edge.weight or len(edge.evidence),
                "label": edge.label,
                "evidence": [
                    {"file": item.file, "line": item.line} for item in edge.evidence
                ],
            }
        )

    current_audit = audit_map(artifact)
    findings = [
        finding.model_dump(mode="json")
        for finding in current_audit.findings
        if node_id in {item.root for item in finding.node_ids}
    ]
    return {
        "node": node.model_dump(mode="json", exclude_none=True),
        "hierarchy": hierarchy,
        "relationships": relationships,
        "audit_findings": findings,
    }

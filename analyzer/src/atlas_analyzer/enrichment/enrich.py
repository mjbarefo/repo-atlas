"""Apply bounded LLM prose enrichment without changing graph structure."""

from copy import deepcopy
from dataclasses import dataclass
import json
from typing import Any

from atlas_analyzer.models import MapArtifact

from .contracts import ClusterEnrichment, EdgeProse, NodeProse, SystemEnrichment
from .provider import BudgetedEnrichmentClient, EnrichmentResponseError


SYSTEM_PROMPT = """You improve labels and summaries for a parser-derived code map.
Return only the requested structured response. Preserve every supplied ID.
Do not invent modules, components, dependencies, or implementation facts."""


@dataclass(frozen=True)
class EnrichmentReport:
    modules_enriched: int
    components_enriched: int
    edges_enriched: int


def _bounded_prompt(
    instruction: str,
    required: dict[str, Any],
    items: list[dict[str, Any]],
    limit: int,
) -> tuple[str, list[dict[str, Any]]]:
    included: list[dict[str, Any]] = []
    for item in items:
        candidate = {
            **required,
            "context": [*included, item],
            "omitted_context": len(items) - len(included) - 1,
        }
        content = (
            instruction
            + "\n"
            + json.dumps(candidate, sort_keys=True, separators=(",", ":"))
        )
        if len(content) > limit:
            break
        included.append(item)
    payload = {
        **required,
        "context": included,
        "omitted_context": len(items) - len(included),
    }
    content = (
        instruction + "\n" + json.dumps(payload, sort_keys=True, separators=(",", ":"))
    )
    if len(content) > limit:
        raise ValueError("required enrichment context exceeds the prompt limit")
    return content, included


def _edge_key(edge: dict[str, Any]) -> tuple[str, str]:
    return edge["source"], edge["target"]


def _validate_nodes(
    proposed: list[NodeProse],
    expected_ids: set[str],
    purpose: str,
) -> None:
    ids = [item.id for item in proposed]
    if len(ids) != len(set(ids)) or set(ids) != expected_ids:
        raise EnrichmentResponseError(
            f"{purpose} must return every supplied node ID exactly once"
        )


def _validated_edge_labels(
    proposed: list[EdgeProse],
    visible_pairs: set[tuple[str, str]],
    purpose: str,
) -> dict[tuple[str, str], str]:
    result: dict[tuple[str, str], str] = {}
    for item in proposed:
        pair = (item.source, item.target)
        if pair not in visible_pairs:
            raise EnrichmentResponseError(
                f"{purpose} returned an unknown or omitted edge: "
                f"{item.source} -> {item.target}"
            )
        if pair in result:
            raise EnrichmentResponseError(
                f"{purpose} returned a duplicate edge label: "
                f"{item.source} -> {item.target}"
            )
        result[pair] = item.label
    return result


def _structural_projection(artifact: MapArtifact) -> dict[str, Any]:
    payload = artifact.model_dump(mode="json", exclude_none=True)
    for node in payload["nodes"]:
        node.pop("label", None)
        node.pop("summary", None)
        node.pop("prose_source", None)
    for edge in payload["edges"]:
        edge.pop("label", None)
    return payload


def validate_structural_identity(
    original: MapArtifact,
    enriched: MapArtifact,
) -> None:
    if _structural_projection(original) != _structural_projection(enriched):
        raise ValueError("enrichment changed fields outside the prose allowlist")


def enrich_map(
    artifact: MapArtifact,
    client: BudgetedEnrichmentClient,
) -> tuple[MapArtifact, EnrichmentReport]:
    payload = artifact.model_dump(mode="json", exclude_none=True)
    nodes = {node["id"]: node for node in payload["nodes"]}
    component_ids = [item.root for item in artifact.levels.system]
    if not component_ids:
        raise ValueError("map has no component layer to enrich")

    module_updates: dict[str, NodeProse] = {}
    edge_updates: dict[tuple[str, str], str] = {}
    prompt_limit = client.max_prompt_chars - len(SYSTEM_PROMPT)

    for component_id in sorted(component_ids):
        module_ids = sorted(
            item.root for item in artifact.levels.component[component_id]
        )
        required = {
            "component": {
                "id": component_id,
                "label": nodes[component_id]["label"],
            },
            "modules": [
                {"id": module_id, "label": nodes[module_id]["label"]}
                for module_id in module_ids
            ],
        }
        module_set = set(module_ids)
        internal_edges = [
            edge
            for edge in payload["edges"]
            if edge["source"] in module_set and edge["target"] in module_set
        ]
        context = [
            {
                "kind": "module",
                "id": module_id,
                "summary": nodes[module_id]["summary"],
                "metrics": nodes[module_id]["metrics"],
                "files": [
                    nodes[child]["files"][0]
                    for child in nodes[module_id]["children"][:12]
                ],
            }
            for module_id in module_ids
        ]
        context.extend(
            {
                "kind": "edge",
                "source": edge["source"],
                "target": edge["target"],
                "identifiers": edge.get("label", ""),
            }
            for edge in internal_edges
        )
        prompt, included = _bounded_prompt(
            "Rewrite every supplied module label and summary. Optionally rewrite "
            "labels only for dependency edges present in the context.",
            required,
            context,
            prompt_limit,
        )
        response = client.complete(
            f"component:{component_id}",
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            ClusterEnrichment,
            max_tokens=min(
                client.max_output_tokens,
                max(600, len(module_ids) * 120),
            ),
        )
        _validate_nodes(response.modules, module_set, component_id)
        visible_pairs = {
            (item["source"], item["target"])
            for item in included
            if item["kind"] == "edge"
        }
        edge_updates.update(
            _validated_edge_labels(
                response.edge_labels,
                visible_pairs,
                component_id,
            )
        )
        module_updates.update((item.id, item) for item in response.modules)

    required = {
        "components": [
            {"id": component_id, "label": nodes[component_id]["label"]}
            for component_id in sorted(component_ids)
        ]
    }
    component_set = set(component_ids)
    component_edges = [
        edge
        for edge in payload["edges"]
        if edge["source"] in component_set and edge["target"] in component_set
    ]
    context = [
        {
            "kind": "component",
            "id": component_id,
            "summary": nodes[component_id]["summary"],
            "metrics": nodes[component_id]["metrics"],
            "modules": [item.root for item in artifact.levels.component[component_id]],
        }
        for component_id in sorted(component_ids)
    ]
    context.extend(
        {
            "kind": "edge",
            "source": edge["source"],
            "target": edge["target"],
            "identifiers": edge.get("label", ""),
        }
        for edge in component_edges
    )
    prompt, included = _bounded_prompt(
        "Rewrite every supplied component label and summary. Optionally rewrite "
        "labels only for dependency edges present in the context.",
        required,
        context,
        prompt_limit,
    )
    system = client.complete(
        "system",
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        SystemEnrichment,
        max_tokens=min(
            client.max_output_tokens,
            max(600, len(component_ids) * 120),
        ),
    )
    _validate_nodes(system.components, component_set, "system")
    visible_pairs = {
        (item["source"], item["target"]) for item in included if item["kind"] == "edge"
    }
    edge_updates.update(
        _validated_edge_labels(system.edge_labels, visible_pairs, "system")
    )

    enriched_payload = deepcopy(payload)
    enriched_nodes = {node["id"]: node for node in enriched_payload["nodes"]}
    for item in [*module_updates.values(), *system.components]:
        node = enriched_nodes[item.id]
        node["label"] = item.label
        node["summary"] = item.summary
        node["prose_source"] = "llm"
    for edge in enriched_payload["edges"]:
        pair = _edge_key(edge)
        if pair in edge_updates:
            edge["label"] = edge_updates[pair]

    enriched = MapArtifact.model_validate(enriched_payload)
    validate_structural_identity(artifact, enriched)
    return enriched, EnrichmentReport(
        modules_enriched=len(module_updates),
        components_enriched=len(system.components),
        edges_enriched=len(edge_updates),
    )

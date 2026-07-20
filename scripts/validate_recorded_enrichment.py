"""Run deterministic, offline enrichment for golden-repository validation only."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from atlas_analyzer.analysis.analyzer import write_map
from atlas_analyzer.enrichment import (
    BudgetedEnrichmentClient,
    Pricing,
    ProviderResult,
    enrich_map,
)
from atlas_analyzer.enrichment.contracts import (
    ClusterEnrichment,
    SystemEnrichment,
)
from atlas_analyzer.models import MapArtifact


class RecordedProvider:
    def __init__(self) -> None:
        self.prompt_sizes: list[int] = []

    def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        response_model: type,
        max_tokens: int,
        temperature: float,
    ) -> ProviderResult:
        del temperature
        prompt_chars = sum(len(message["content"]) for message in messages)
        self.prompt_sizes.append(prompt_chars)
        payload = json.loads(messages[-1]["content"].split("\n", 1)[1])
        if response_model is ClusterEnrichment:
            records = payload["modules"]
            key = "modules"
        elif response_model is SystemEnrichment:
            records = payload["components"]
            key = "components"
        else:
            raise TypeError(f"unsupported response model: {response_model}")
        content: dict[str, Any] = {
            key: [
                {
                    "id": record["id"],
                    "label": f"Enriched {record['label']}"[:80],
                    "summary": f"Recorded enrichment for {record['label']}.",
                }
                for record in records
            ],
            "edge_labels": [
                {
                    "source": item["source"],
                    "target": item["target"],
                    "label": "recorded dependency",
                }
                for item in payload["context"]
                if item["kind"] == "edge"
            ],
        }
        encoded = json.dumps(content, sort_keys=True)
        output_tokens = max(1, len(encoded) // 4)
        if output_tokens > max_tokens:
            raise ValueError(
                f"recorded response needs {output_tokens} tokens; cap is {max_tokens}"
            )
        return ProviderResult(
            encoded,
            model,
            max(1, prompt_chars // 4),
            output_tokens,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="map.json artifact to enrich")
    parser.add_argument(
        "output", type=Path, help="destination for the enriched artifact"
    )
    args = parser.parse_args()

    artifact = MapArtifact.model_validate_json(args.input.read_text())
    provider = RecordedProvider()
    client = BudgetedEnrichmentClient(
        provider,
        model="recorded/validation",
        pricing=Pricing(0.25, 2.0),
        budget_usd=0.50,
        max_prompt_chars=30_000,
        max_output_tokens=8_000,
    )
    enriched, report = enrich_map(artifact, client)
    write_map(enriched, args.output)
    print(
        json.dumps(
            {
                "calls": len(client.records),
                "components": report.components_enriched,
                "modules": report.modules_enriched,
                "edges": report.edges_enriched,
                "max_prompt_chars": max(provider.prompt_sizes),
                "cost_usd": round(client.total_cost_usd, 6),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

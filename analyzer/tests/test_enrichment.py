import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from atlas_analyzer.analysis.analyzer import analyze_repository, write_map
from atlas_analyzer.cli import app
from atlas_analyzer.enrichment import (
    BudgetExceededError,
    BudgetedEnrichmentClient,
    EnrichmentResponseError,
    LiteLLMProvider,
    Pricing,
    ProviderResult,
    enrich_map,
    validate_structural_identity,
)
from atlas_analyzer.enrichment.contracts import (
    ClusterEnrichment,
    SystemEnrichment,
)
from atlas_analyzer.models import MapArtifact


FIXTURE = Path(__file__).parent / "fixtures" / "golden_repo"
RUNNER = CliRunner()


class RecordedProvider:
    def __init__(
        self,
        *,
        malformed: bool = False,
        invented_edge: bool = False,
        missing_node: bool = False,
        output_tokens: int = 25,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self.malformed = malformed
        self.invented_edge = invented_edge
        self.missing_node = missing_node
        self.output_tokens = output_tokens

    def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        response_model: type,
        max_tokens: int,
        temperature: float,
    ) -> ProviderResult:
        self.calls.append(
            {
                "model": model,
                "messages": messages,
                "response_model": response_model,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
        )
        if self.malformed:
            return ProviderResult("not-json", model, 100, self.output_tokens)

        payload = json.loads(messages[-1]["content"].split("\n", 1)[1])
        if response_model is ClusterEnrichment:
            records = payload["modules"]
            key = "modules"
        elif response_model is SystemEnrichment:
            records = payload["components"]
            key = "components"
        else:
            raise AssertionError(f"unexpected response model: {response_model}")
        if self.missing_node:
            records = records[:-1]
        content = {
            key: [
                {
                    "id": record["id"],
                    "label": f"LLM {record['label']}"[:80],
                    "summary": f"Enriched summary for {record['id']}.",
                }
                for record in records
            ],
            "edge_labels": [
                {
                    "source": item["source"],
                    "target": item["target"],
                    "label": "uses enriched dependency",
                }
                for item in payload["context"]
                if item["kind"] == "edge"
            ],
        }
        if self.invented_edge:
            content["edge_labels"].append(
                {
                    "source": "mod:invented",
                    "target": "mod:also-invented",
                    "label": "hallucinated dependency",
                }
            )
        return ProviderResult(
            json.dumps(content),
            f"{model}-recorded",
            100,
            self.output_tokens,
        )


class FailingProvider:
    def complete(self, **_: Any) -> ProviderResult:
        raise AssertionError("provider should not have been called")


def client(
    provider: Any,
    *,
    budget: float = 1.0,
    pricing: Pricing = Pricing(1.0, 2.0),
    cache_directory: Path | None = None,
    max_prompt_chars: int = 5_000,
) -> BudgetedEnrichmentClient:
    return BudgetedEnrichmentClient(
        provider,
        model="recorded/test-model",
        pricing=pricing,
        budget_usd=budget,
        cache_directory=cache_directory,
        max_prompt_chars=max_prompt_chars,
        max_output_tokens=600,
    )


def test_enrichment_is_deterministic_bounded_and_structurally_immutable() -> None:
    artifact = analyze_repository(FIXTURE)
    first_provider = RecordedProvider()
    second_provider = RecordedProvider()
    first_client = client(first_provider)
    second_client = client(second_provider)

    first, report = enrich_map(artifact, first_client)
    second, second_report = enrich_map(artifact, second_client)

    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert report == second_report
    validate_structural_identity(artifact, first)
    assert report.modules_enriched == len(artifact.levels.module)
    assert report.components_enriched == len(artifact.levels.system)
    assert len(first_provider.calls) == len(artifact.levels.system) + 1
    assert all(
        sum(len(message["content"]) for message in call["messages"])
        <= first_client.max_prompt_chars
        for call in first_provider.calls
    )
    assert all(call["temperature"] == 0 for call in first_provider.calls)

    original = {node.id: node for node in artifact.nodes}
    enriched = {node.id: node for node in first.nodes}
    higher_ids = [
        *artifact.levels.module,
        *(item.root for item in artifact.levels.system),
    ]
    assert all(enriched[node_id].prose_source.value == "llm" for node_id in higher_ids)
    assert all(
        enriched[node_id].model_dump(mode="json")
        == original[node_id].model_dump(mode="json")
        for node_id, node in original.items()
        if node.kind.value == "file"
    )


def test_structural_validator_rejects_topology_changes() -> None:
    artifact = analyze_repository(FIXTURE)
    payload = artifact.model_dump(mode="json", exclude_none=True)
    component = next(node for node in payload["nodes"] if node["kind"] == "component")
    component["children"] = component["children"][1:]
    mutated = MapArtifact.model_validate(payload)

    with pytest.raises(ValueError, match="outside the prose allowlist"):
        validate_structural_identity(artifact, mutated)


def test_budget_rejects_before_provider_call() -> None:
    llm = client(
        FailingProvider(),
        budget=0.000001,
        pricing=Pricing(100.0, 100.0),
    )

    with pytest.raises(BudgetExceededError, match="could cost up to"):
        enrich_map(analyze_repository(FIXTURE), llm)

    assert llm.records == []


def test_provider_cannot_report_usage_above_the_requested_token_cap() -> None:
    with pytest.raises(EnrichmentResponseError, match="requested limits"):
        enrich_map(
            analyze_repository(FIXTURE),
            client(RecordedProvider(output_tokens=601)),
        )


@pytest.mark.parametrize(
    ("provider", "message"),
    [
        (RecordedProvider(malformed=True), "invalid component:"),
        (
            RecordedProvider(missing_node=True),
            "must return every supplied node ID exactly once",
        ),
        (
            RecordedProvider(invented_edge=True),
            "returned an unknown or omitted edge",
        ),
    ],
)
def test_invalid_provider_responses_are_rejected(
    provider: RecordedProvider,
    message: str,
) -> None:
    with pytest.raises((EnrichmentResponseError, ValueError), match=message):
        enrich_map(analyze_repository(FIXTURE), client(provider))


def test_cache_reuses_validated_responses_without_new_cost(tmp_path: Path) -> None:
    artifact = analyze_repository(FIXTURE)
    provider = RecordedProvider()
    first = client(provider, cache_directory=tmp_path)
    expected, _ = enrich_map(artifact, first)
    cached = client(FailingProvider(), cache_directory=tmp_path)

    actual, _ = enrich_map(artifact, cached)

    assert actual == expected
    assert first.total_cost_usd > 0
    assert cached.total_cost_usd == 0
    assert all(record.cached for record in cached.records)


def test_litellm_adapter_requests_structured_temperature_zero_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def completion(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return SimpleNamespace(
            model="provider-version",
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps(
                            {
                                "modules": [
                                    {
                                        "id": "mod:auth",
                                        "label": "Authentication",
                                        "summary": "Manages user sessions.",
                                    }
                                ],
                                "edge_labels": [],
                            }
                        )
                    )
                )
            ],
            usage=SimpleNamespace(prompt_tokens=12, completion_tokens=8),
        )

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(completion=completion))
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "False")

    result = LiteLLMProvider(retries=2).complete(
        model="openai/test",
        messages=[{"role": "user", "content": "Describe this module."}],
        response_model=ClusterEnrichment,
        max_tokens=400,
        temperature=0,
    )

    assert result.model == "provider-version"
    assert result.input_tokens == 12
    assert result.output_tokens == 8
    assert captured["response_format"] is ClusterEnrichment
    assert captured["temperature"] == 0
    assert captured["num_retries"] == 2
    assert os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] == "True"


def test_enrich_cli_writes_only_after_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    map_path = tmp_path / "map.json"
    output = tmp_path / "enriched.json"
    artifact = analyze_repository(FIXTURE)
    write_map(artifact, map_path)
    provider = RecordedProvider()
    monkeypatch.setattr("atlas_analyzer.cli.LiteLLMProvider", lambda: provider)

    result = RUNNER.invoke(
        app,
        [
            "enrich",
            str(map_path),
            "--model",
            "recorded/test-model",
            "--input-cost-per-million",
            "1",
            "--output-cost-per-million",
            "2",
            "--budget",
            "0.50",
            "--no-cache",
            "--output",
            str(output),
        ],
    )

    assert result.exit_code == 0
    assert "LLM total cost:" in result.stdout
    assert output.exists()
    validate_structural_identity(
        artifact,
        MapArtifact.model_validate_json(output.read_text()),
    )

    failed_output = tmp_path / "failed.json"
    monkeypatch.setattr(
        "atlas_analyzer.cli.LiteLLMProvider",
        lambda: RecordedProvider(malformed=True),
    )
    failed = RUNNER.invoke(
        app,
        [
            "enrich",
            str(map_path),
            "--model",
            "recorded/test-model",
            "--input-cost-per-million",
            "1",
            "--output-cost-per-million",
            "2",
            "--no-cache",
            "--output",
            str(failed_output),
        ],
    )

    assert failed.exit_code == 1
    assert not failed_output.exists()

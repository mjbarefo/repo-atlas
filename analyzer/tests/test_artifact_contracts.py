from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

from atlas_analyzer.models import ImpactArtifact, MapArtifact, TraceArtifact

ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "shared" / "schemas"
FIXTURES = ROOT / "shared" / "fixtures"


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


@pytest.mark.parametrize(
    ("fixture_name", "schema_name", "model_type"),
    [
        ("sample.map.json", "map.schema.json", MapArtifact),
        ("sample.trace.json", "trace.schema.json", TraceArtifact),
        ("sample.impact.json", "impact.schema.json", ImpactArtifact),
    ],
)
def test_artifact_round_trip(
    fixture_name: str,
    schema_name: str,
    model_type: type[ImpactArtifact] | type[MapArtifact] | type[TraceArtifact],
) -> None:
    artifact = load_json(FIXTURES / fixture_name)
    schema = load_json(SCHEMAS / schema_name)
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(artifact)

    parsed = model_type.model_validate(artifact)
    serialized = parsed.model_dump(
        mode="json",
        by_alias=True,
        exclude_none=model_type is ImpactArtifact,
    )

    assert canonical_bytes(serialized) == canonical_bytes(artifact)


def test_map_rejects_unknown_properties() -> None:
    artifact = load_json(FIXTURES / "sample.map.json")
    artifact["unexpected"] = True

    with pytest.raises(ValidationError):
        Draft202012Validator(load_json(SCHEMAS / "map.schema.json")).validate(artifact)
    with pytest.raises(ValueError):
        MapArtifact.model_validate(artifact)


def test_map_rejects_missing_edge_evidence() -> None:
    artifact = load_json(FIXTURES / "sample.map.json")
    artifact["edges"][0]["evidence"] = []

    with pytest.raises(ValidationError):
        Draft202012Validator(load_json(SCHEMAS / "map.schema.json")).validate(artifact)


def test_map_rejects_invalid_prose_source() -> None:
    artifact = load_json(FIXTURES / "sample.map.json")
    artifact["nodes"][0]["prose_source"] = "unknown"

    with pytest.raises(ValidationError):
        Draft202012Validator(load_json(SCHEMAS / "map.schema.json")).validate(artifact)
    with pytest.raises(ValueError):
        MapArtifact.model_validate(artifact)


def test_map_rejects_malformed_timestamp() -> None:
    artifact = load_json(FIXTURES / "sample.map.json")
    artifact["repo"]["generated_at"] = "not-a-timestamp"
    validator = Draft202012Validator(
        load_json(SCHEMAS / "map.schema.json"),
        format_checker=FormatChecker(),
    )

    with pytest.raises(ValidationError):
        validator.validate(artifact)
    with pytest.raises(ValueError):
        MapArtifact.model_validate(artifact)


def test_trace_rejects_invalid_tool() -> None:
    artifact = copy.deepcopy(load_json(FIXTURES / "sample.trace.json"))
    artifact["events"][0]["tool"] = "Delete"

    with pytest.raises(ValidationError):
        Draft202012Validator(load_json(SCHEMAS / "trace.schema.json")).validate(
            artifact
        )
    with pytest.raises(ValueError):
        TraceArtifact.model_validate(artifact)


def test_impact_rejects_unknown_status() -> None:
    artifact = load_json(FIXTURES / "sample.impact.json")
    artifact["files"][0]["status"] = "unknown"

    with pytest.raises(ValidationError):
        Draft202012Validator(load_json(SCHEMAS / "impact.schema.json")).validate(
            artifact
        )
    with pytest.raises(ValueError):
        ImpactArtifact.model_validate(artifact)

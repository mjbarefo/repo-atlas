"""User configuration for the explicit, network-capable enrichment command."""

from contextlib import contextmanager
from dataclasses import dataclass, field
import math
import os
from pathlib import Path
import re
import sys
import tomllib
from typing import Iterator

ENVIRONMENT_KEY = re.compile(r"^[A-Z_][A-Z0-9_]*$")


@dataclass(frozen=True)
class EnrichmentConfig:
    provider: str = "litellm"
    model: str | None = None
    budget: float = 0.50
    input_cost_per_million: float | None = None
    output_cost_per_million: float | None = None
    provider_keys: dict[str, str] = field(default_factory=dict)


def default_config_path() -> Path:
    return Path.home() / ".atlas" / "config.toml"


def load_config(path: Path | None = None) -> EnrichmentConfig:
    source = path or default_config_path()
    if not source.exists():
        if path is not None:
            raise ValueError(f"config does not exist: {source}")
        return EnrichmentConfig()
    try:
        payload = tomllib.loads(source.read_text())
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ValueError(f"could not read config {source}: {error}") from error

    enrichment = payload.get("enrichment", {})
    keys = payload.get("provider_keys", {})
    if not isinstance(enrichment, dict) or not isinstance(keys, dict):
        raise ValueError(
            "config sections [enrichment] and [provider_keys] must be tables"
        )
    provider_keys: dict[str, str] = {}
    for name, value in keys.items():
        if not ENVIRONMENT_KEY.fullmatch(name) or not isinstance(value, str):
            raise ValueError(
                "provider keys must be uppercase environment names with string values"
            )
        provider_keys[name] = value

    def optional_number(name: str) -> float | None:
        value = enrichment.get(name)
        if value is None:
            return None
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            raise ValueError(f"enrichment.{name} must be a non-negative number")
        return float(value)

    provider = enrichment.get("provider", "litellm")
    model = enrichment.get("model")
    if not isinstance(provider, str) or (
        model is not None and not isinstance(model, str)
    ):
        raise ValueError("enrichment.provider and enrichment.model must be strings")
    budget = optional_number("budget")
    return EnrichmentConfig(
        provider=provider,
        model=model,
        budget=0.50 if budget is None else budget,
        input_cost_per_million=optional_number("input_cost_per_million"),
        output_cost_per_million=optional_number("output_cost_per_million"),
        provider_keys=provider_keys,
    )


@contextmanager
def provider_environment(keys: dict[str, str]) -> Iterator[None]:
    """Temporarily supply config credentials without overriding the shell."""
    missing = object()
    previous: dict[str, str | object] = {}
    for name, value in keys.items():
        existing = os.environ.get(name)
        previous[name] = existing if existing is not None else missing
        if existing is not None and existing != value:
            print(
                f"atlas: environment variable {name} already set; "
                "it takes precedence over the config.toml value",
                file=sys.stderr,
            )
        os.environ.setdefault(name, value)
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is missing:
                os.environ.pop(name, None)
            else:
                os.environ[name] = str(value)

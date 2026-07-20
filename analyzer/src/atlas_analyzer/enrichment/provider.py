"""Provider-neutral, cached, budget-enforcing enrichment boundary."""

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

ResponseModel = TypeVar("ResponseModel", bound=BaseModel)


class BudgetExceededError(RuntimeError):
    """Raised before a request can exceed the configured dollar budget."""


class EnrichmentResponseError(RuntimeError):
    """Raised when a provider returns an unusable structured response."""


@dataclass(frozen=True)
class Pricing:
    input_per_million: float
    output_per_million: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.input_per_million) or not math.isfinite(
            self.output_per_million
        ):
            raise ValueError("token prices must be finite")
        if self.input_per_million < 0 or self.output_per_million < 0:
            raise ValueError("token prices cannot be negative")


@dataclass(frozen=True)
class ProviderResult:
    content: str
    model: str
    input_tokens: int
    output_tokens: int


class CompletionProvider(Protocol):
    def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        response_model: type[BaseModel],
        max_tokens: int,
        temperature: float,
    ) -> ProviderResult: ...


class LiteLLMProvider:
    """LiteLLM adapter imported lazily so analysis remains provider-free."""

    def __init__(self, retries: int = 2) -> None:
        self.retries = retries

    def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        response_model: type[BaseModel],
        max_tokens: int,
        temperature: float,
    ) -> ProviderResult:
        os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
        import litellm

        response = litellm.completion(
            model=model,
            messages=messages,
            response_format=response_model,
            max_tokens=max_tokens,
            temperature=temperature,
            num_retries=self.retries,
        )
        content = response.choices[0].message.content
        if not isinstance(content, str):
            raise EnrichmentResponseError(
                "provider returned no structured response content"
            )
        usage = response.usage
        return ProviderResult(
            content=content,
            model=str(response.model),
            input_tokens=int(usage.prompt_tokens or 0),
            output_tokens=int(usage.completion_tokens or 0),
        )


@dataclass(frozen=True)
class CallRecord:
    purpose: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    cached: bool


class BudgetedEnrichmentClient:
    def __init__(
        self,
        provider: CompletionProvider,
        *,
        model: str,
        pricing: Pricing,
        budget_usd: float,
        cache_directory: Path | None = None,
        max_prompt_chars: int = 30_000,
        max_output_tokens: int = 8_000,
    ) -> None:
        if not math.isfinite(budget_usd) or budget_usd < 0:
            raise ValueError("budget_usd must be finite and non-negative")
        if max_prompt_chars < 1:
            raise ValueError("max_prompt_chars must be positive")
        if max_output_tokens < 1:
            raise ValueError("max_output_tokens must be positive")
        self.provider = provider
        self.model = model
        self.pricing = pricing
        self.budget_usd = budget_usd
        self.cache_directory = cache_directory
        self.max_prompt_chars = max_prompt_chars
        self.max_output_tokens = max_output_tokens
        self.records: list[CallRecord] = []

    @property
    def total_cost_usd(self) -> float:
        return sum(record.cost_usd for record in self.records)

    def _cost(self, input_tokens: int, output_tokens: int) -> float:
        return (
            input_tokens * self.pricing.input_per_million
            + output_tokens * self.pricing.output_per_million
        ) / 1_000_000

    def _cache_key(
        self,
        messages: list[dict[str, str]],
        response_model: type[BaseModel],
        max_tokens: int,
    ) -> str:
        payload = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": max_tokens,
            "messages": messages,
            "schema": response_model.model_json_schema(),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    def _read_cache(self, key: str) -> dict[str, Any] | None:
        if self.cache_directory is None:
            return None
        path = self.cache_directory / f"{key}.json"
        return json.loads(path.read_text()) if path.exists() else None

    def _write_cache(self, key: str, payload: dict[str, Any]) -> None:
        if self.cache_directory is None:
            return
        self.cache_directory.mkdir(parents=True, exist_ok=True)
        destination = self.cache_directory / f"{key}.json"
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_file.write(json.dumps(payload, sort_keys=True) + "\n")
            temporary = Path(temporary_file.name)
        try:
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)

    def complete(
        self,
        purpose: str,
        messages: list[dict[str, str]],
        response_model: type[ResponseModel],
        *,
        max_tokens: int | None = None,
    ) -> ResponseModel:
        output_limit = max_tokens or self.max_output_tokens
        if output_limit < 1 or output_limit > self.max_output_tokens:
            raise ValueError(
                f"{purpose} output limit must be between 1 and "
                f"{self.max_output_tokens}"
            )
        prompt_chars = sum(len(message["content"]) for message in messages)
        if prompt_chars > self.max_prompt_chars:
            raise ValueError(
                f"{purpose} prompt has {prompt_chars} characters; "
                f"limit is {self.max_prompt_chars}"
            )
        key = self._cache_key(messages, response_model, output_limit)
        cached = self._read_cache(key)
        if cached is not None:
            parsed = response_model.model_validate(cached["response"])
            self.records.append(
                CallRecord(purpose, cached["model"], 0, 0, 0.0, cached=True)
            )
            return parsed

        schema_bytes = len(
            json.dumps(
                response_model.model_json_schema(), separators=(",", ":")
            ).encode()
        )
        estimated_input = (
            sum(len(message["content"].encode()) for message in messages)
            + schema_bytes
            + 100
        )
        maximum_cost = self._cost(estimated_input, output_limit)
        if self.total_cost_usd + maximum_cost > self.budget_usd:
            raise BudgetExceededError(
                f"{purpose} could cost up to ${maximum_cost:.6f}; "
                f"${self.budget_usd - self.total_cost_usd:.6f} remains"
            )

        result = self.provider.complete(
            model=self.model,
            messages=messages,
            response_model=response_model,
            max_tokens=output_limit,
            temperature=0,
        )
        if (
            result.input_tokens < 0
            or result.output_tokens < 0
            or result.output_tokens > output_limit
        ):
            raise EnrichmentResponseError(
                "provider returned token usage outside the requested limits"
            )
        try:
            parsed = response_model.model_validate_json(result.content)
        except ValueError as error:
            raise EnrichmentResponseError(
                f"invalid {purpose} response: {error}"
            ) from error
        cost = self._cost(result.input_tokens, result.output_tokens)
        if self.total_cost_usd + cost > self.budget_usd:
            # Unlike the pre-call stop above, this call already ran and was
            # billed: the provider reported more usage than the local
            # byte-length estimate. Make the overspend explicit.
            raise BudgetExceededError(
                f"provider call already spent ${cost:.6f}, exceeding the "
                f"${self.budget_usd:.6f} budget after the fact; reported "
                "usage was larger than the pre-call estimate"
            )
        self.records.append(
            CallRecord(
                purpose,
                result.model,
                result.input_tokens,
                result.output_tokens,
                cost,
                cached=False,
            )
        )
        self._write_cache(
            key,
            {
                "model": result.model,
                "response": parsed.model_dump(mode="json"),
            },
        )
        return parsed

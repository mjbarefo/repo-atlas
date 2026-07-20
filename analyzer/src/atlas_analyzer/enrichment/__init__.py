"""Optional, topology-preserving LLM enrichment."""

from .enrich import EnrichmentReport, enrich_map, validate_structural_identity
from .provider import (
    BudgetExceededError,
    BudgetedEnrichmentClient,
    CallRecord,
    CompletionProvider,
    EnrichmentResponseError,
    LiteLLMProvider,
    Pricing,
    ProviderResult,
)

__all__ = [
    "BudgetExceededError",
    "BudgetedEnrichmentClient",
    "CallRecord",
    "CompletionProvider",
    "EnrichmentReport",
    "EnrichmentResponseError",
    "LiteLLMProvider",
    "Pricing",
    "ProviderResult",
    "enrich_map",
    "validate_structural_identity",
]

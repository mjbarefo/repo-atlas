"""Strict structured responses accepted from enrichment providers."""

from pydantic import BaseModel, ConfigDict, Field


class NodeProse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    label: str = Field(min_length=1, max_length=80)
    summary: str = Field(min_length=1, max_length=400)


class EdgeProse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1)
    target: str = Field(min_length=1)
    label: str = Field(min_length=1, max_length=120)


class ClusterEnrichment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    modules: list[NodeProse] = Field(min_length=1)
    edge_labels: list[EdgeProse] = Field(default_factory=list)


class SystemEnrichment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    components: list[NodeProse] = Field(min_length=1)
    edge_labels: list[EdgeProse] = Field(default_factory=list)

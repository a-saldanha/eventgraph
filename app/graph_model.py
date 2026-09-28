"""The event knowledge graph: entities, edges, provenance.

Deterministic-heuristic build for the local mockup; each stage sits behind a plain
function so an LLM implementation can replace it later without touching the graph
model or the API.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class EntityType(str, Enum):
    PERSON = "person"
    ORG = "org"
    LOCATION = "location"
    MONEY = "money"
    DOCUMENT = "document"
    SUBEVENT = "subevent"


class Mention(BaseModel):
    """One surface-form occurrence of an entity, grounded to a source item."""

    item_id: str
    text: str  # the surface form as it appeared (e.g. "J@ne", "jane.doe042@…")
    span: Optional[tuple[int, int]] = None  # char range in the item body, when known


class Entity(BaseModel):
    id: str
    type: EntityType
    label: str  # canonical display name
    aliases: list[str] = Field(default_factory=list)  # distinct surface forms merged in
    mentions: list[Mention] = Field(default_factory=list)
    attrs: dict = Field(default_factory=dict)  # e.g. {"emails": [...], "amount": 1236.18}

    @property
    def item_ids(self) -> set[str]:
        return {m.item_id for m in self.mentions}


class Edge(BaseModel):
    id: str
    source: str  # entity id
    target: str  # entity id
    kind: str  # corresponded_with | affiliated_with | paid_to | held_at | document_for | on_date
    weight: float = 1.0
    evidence_item_ids: list[str] = Field(default_factory=list)
    attrs: dict = Field(default_factory=dict)


class MergeRecord(BaseModel):
    """Audit of one entity-resolution merge — shown in the UI 'why merged' panel."""

    canonical_id: str
    merged_forms: list[str]
    rationale: str


class RelevanceVerdict(BaseModel):
    item_id: str
    relevant: bool
    score: float
    rationale: str
    topics: list[str] = Field(default_factory=list)


class EventGraph(BaseModel):
    entities: list[Entity] = Field(default_factory=list)
    edges: list[Edge] = Field(default_factory=list)
    merges: list[MergeRecord] = Field(default_factory=list)
    relevance: list[RelevanceVerdict] = Field(default_factory=list)

    def entity(self, eid: str) -> Optional[Entity]:
        return next((e for e in self.entities if e.id == eid), None)

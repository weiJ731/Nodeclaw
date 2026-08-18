from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class MemoryType(StrEnum):
    PERSONAL_FACT = "personal_fact"
    PREFERENCE = "preference"
    LONG_TERM_GOAL = "long_term_goal"
    PROJECT_CONTEXT = "project_context"
    RELATIONSHIP = "relationship"
    DECISION = "decision"
    CONSTRAINT = "constraint"


class MemoryStatus(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    EXPIRED = "expired"
    DELETED = "deleted"


class LifecycleAction(StrEnum):
    NEW = "NEW"
    UPDATE = "UPDATE"
    MERGE = "MERGE"
    DISCARD = "DISCARD"


class IndexStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"


class CandidateMemory(BaseModel):
    type: MemoryType
    summary: str = Field(min_length=2, max_length=500)
    facts: list[str] = Field(default_factory=list, max_length=20)
    keywords: list[str] = Field(default_factory=list, max_length=20)
    importance: float = Field(default=0.5, ge=0.0, le=1.0)
    confidence: float = Field(default=0.7, ge=0.0, le=1.0)
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    evidence_summary: str = Field(default="", max_length=500)

    @field_validator("facts", "keywords")
    @classmethod
    def _deduplicate(cls, values: list[str]) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for value in values:
            clean = " ".join(str(value).split()).strip()
            if clean and clean not in seen:
                seen.add(clean)
                result.append(clean)
        return result

    @property
    def retrieval_text(self) -> str:
        parts = [self.summary, "；".join(self.facts), " ".join(self.keywords)]
        return "\n".join(part for part in parts if part).strip()


class LifecycleDecision(BaseModel):
    action: LifecycleAction
    target_memory_ids: list[str] = Field(default_factory=list)
    reason: str = Field(default="", max_length=500)
    confidence: float = Field(default=0.7, ge=0.0, le=1.0)
    resolved_memory: CandidateMemory | None = None


class MemorySearchHit(BaseModel):
    memory_id: str
    rank: int
    summary: str
    type: str
    facts: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    importance: float = 0.0
    confidence: float = 0.0
    version: int = 1
    scores: dict[str, float] = Field(default_factory=dict)


class MemorySearchResponse(BaseModel):
    query: str
    mode: str
    hits: list[MemorySearchHit]
    latency_ms: float
    debug: dict[str, Any] | None = None

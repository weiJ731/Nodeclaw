from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip() or default


def _int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    return _env(name, str(default)).lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class MemoryV3Config:
    mongodb_uri: str = field(default_factory=lambda: _env(
        "MONGODB_URI",
        "mongodb://nodeclaw:nodeclaw_password@localhost:27018/nodeclaw?authSource=admin&replicaSet=rs0&directConnection=true",
    ))
    mongodb_database: str = field(default_factory=lambda: _env("MONGODB_DATABASE", "nodeclaw"))
    qdrant_url: str = field(default_factory=lambda: _env("QDRANT_URL", "http://localhost:6335"))
    qdrant_collection: str = field(default_factory=lambda: _env("QDRANT_COLLECTION", "nodeclaw_memories_v3"))
    redis_url: str = field(default_factory=lambda: _env("REDIS_URL", "redis://localhost:6380/0"))
    celery_broker_url: str = field(default_factory=lambda: _env("CELERY_BROKER_URL", "redis://localhost:6380/1"))
    celery_result_backend: str = field(default_factory=lambda: _env("CELERY_RESULT_BACKEND", "redis://localhost:6380/2"))

    raw_exchange_ttl_days: int = field(default_factory=lambda: _int("RAW_EXCHANGE_TTL_DAYS", 30))
    audit_ttl_days: int = field(default_factory=lambda: _int("AUDIT_TTL_DAYS", 90))
    recent_exchange_limit: int = field(default_factory=lambda: _int("RECENT_EXCHANGE_LIMIT", 6))
    recent_exchange_token_limit: int = field(default_factory=lambda: _int("RECENT_EXCHANGE_TOKEN_LIMIT", 6000))
    summary_max_tokens: int = field(default_factory=lambda: _int("SUMMARY_MAX_TOKENS", 800))
    summary_compact_count: int = field(default_factory=lambda: _int("SUMMARY_COMPACT_COUNT", 3))

    retrieval_top_k: int = field(default_factory=lambda: _int("MEMORY_RETRIEVAL_TOP_K", 5))
    context_token_limit: int = field(default_factory=lambda: _int("MEMORY_CONTEXT_TOKEN_LIMIT", 1200))
    dense_candidate_count: int = field(default_factory=lambda: _int("DENSE_CANDIDATE_COUNT", 20))
    sparse_candidate_count: int = field(default_factory=lambda: _int("SPARSE_CANDIDATE_COUNT", 20))
    rrf_k: int = field(default_factory=lambda: _int("RRF_K", 60))
    extraction_enabled: bool = field(default_factory=lambda: _bool("MEMORY_EXTRACTION_ENABLED", True))
    merge_enabled: bool = field(default_factory=lambda: _bool("MEMORY_MERGE_ENABLED", True))
    summary_enabled: bool = field(default_factory=lambda: _bool("SESSION_SUMMARY_ENABLED", True))

    memory_provider: str = field(default_factory=lambda: _env("MEMORY_PROVIDER", _env("DEFAULT_PROVIDER", "aliyun")))
    memory_model: str = field(default_factory=lambda: _env("MEMORY_MODEL", _env("DEFAULT_MODEL", "glm-5")))
    embedding_model: str = field(default_factory=lambda: _env("EMBEDDING_MODEL", "text-embedding-v2"))


@lru_cache(maxsize=1)
def get_memory_config() -> MemoryV3Config:
    return MemoryV3Config()


def clear_memory_config_cache() -> None:
    get_memory_config.cache_clear()

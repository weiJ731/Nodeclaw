from __future__ import annotations

import hashlib
import math
import time
from collections import Counter
from functools import lru_cache
from typing import Any, Literal

import jieba
from qdrant_client import QdrantClient, models

from nodeclaw.core.provider import get_embedding_model

from .config import get_memory_config
from .models import MemorySearchHit, MemorySearchResponse
from .storage import get_active_memories

SearchMode = Literal["dense", "sparse", "hybrid"]


class ChineseBM25Encoder:
    """Deterministic Chinese tokenizer producing Qdrant sparse vectors."""

    def __init__(self, *, k1: float = 1.2, b: float = 0.75, average_length: float = 80.0):
        self.k1 = k1
        self.b = b
        self.average_length = max(average_length, 1.0)

    @staticmethod
    def tokenize(text: str) -> list[str]:
        normalized = " ".join(text.lower().split())
        return [token.strip() for token in jieba.lcut(normalized) if token.strip() and not token.isspace()]

    @staticmethod
    def token_id(token: str) -> int:
        return int.from_bytes(hashlib.blake2s(token.encode("utf-8"), digest_size=4).digest(), "big")

    def encode_document(self, text: str) -> models.SparseVector:
        tokens = self.tokenize(text)
        counts = Counter(tokens)
        length = max(len(tokens), 1)
        pairs: list[tuple[int, float]] = []
        for token, frequency in counts.items():
            denominator = frequency + self.k1 * (1 - self.b + self.b * length / self.average_length)
            weight = frequency * (self.k1 + 1) / denominator
            pairs.append((self.token_id(token), float(weight)))
        pairs.sort(key=lambda item: item[0])
        return models.SparseVector(indices=[item[0] for item in pairs], values=[item[1] for item in pairs])

    def encode_query(self, text: str) -> models.SparseVector:
        ids = sorted({self.token_id(token) for token in self.tokenize(text)})
        return models.SparseVector(indices=ids, values=[1.0] * len(ids))


@lru_cache(maxsize=1)
def get_qdrant_client() -> QdrantClient:
    return QdrantClient(url=get_memory_config().qdrant_url, timeout=10)


@lru_cache(maxsize=1)
def get_sparse_encoder() -> ChineseBM25Encoder:
    return ChineseBM25Encoder()


def ensure_collection(vector_size: int) -> None:
    client = get_qdrant_client()
    name = get_memory_config().qdrant_collection
    if client.collection_exists(name):
        collection = client.get_collection(name)
        configured = collection.config.params.vectors
        dense = configured.get("dense") if isinstance(configured, dict) else None
        if dense and dense.size != vector_size:
            raise RuntimeError(
                f"Qdrant dense vector size mismatch: collection={dense.size}, model={vector_size}. "
                "Run the reindex command after changing the embedding model."
            )
        return
    client.create_collection(
        collection_name=name,
        vectors_config={"dense": models.VectorParams(size=vector_size, distance=models.Distance.COSINE)},
        sparse_vectors_config={
            "sparse": models.SparseVectorParams(index=models.SparseIndexParams(on_disk=False), modifier=models.Modifier.IDF)
        },
    )
    client.create_payload_index(name, "user_id", models.PayloadSchemaType.KEYWORD)
    client.create_payload_index(name, "status", models.PayloadSchemaType.KEYWORD)
    client.create_payload_index(name, "type", models.PayloadSchemaType.KEYWORD)


def _embedding_model():
    return get_embedding_model(model_name=get_memory_config().embedding_model)


def index_memory(memory: dict[str, Any]) -> None:
    dense = _embedding_model().embed_query(memory["retrieval_text"])
    ensure_collection(len(dense))
    sparse = get_sparse_encoder().encode_document(memory["retrieval_text"])
    payload = {
        "memory_id": memory["memory_id"],
        "user_id": memory["user_id"],
        "type": str(memory["type"]),
        "status": memory["status"],
        "version": memory["version"],
        "importance": memory.get("importance", 0.5),
        "confidence": memory.get("confidence", 0.7),
    }
    get_qdrant_client().upsert(
        collection_name=get_memory_config().qdrant_collection,
        points=[models.PointStruct(
            id=memory["memory_id"],
            vector={"dense": dense, "sparse": sparse},
            payload=payload,
        )],
        wait=True,
    )


def delete_memory_index(memory_id: str) -> None:
    client = get_qdrant_client()
    name = get_memory_config().qdrant_collection
    if client.collection_exists(name):
        client.delete(name, points_selector=models.PointIdsList(points=[memory_id]), wait=True)


def _query_filter(user_id: str) -> models.Filter:
    return models.Filter(must=[
        models.FieldCondition(key="user_id", match=models.MatchValue(value=user_id)),
        models.FieldCondition(key="status", match=models.MatchValue(value="active")),
    ])


def _query_dense(user_id: str, query: str, limit: int) -> list[dict[str, Any]]:
    vector = _embedding_model().embed_query(query)
    ensure_collection(len(vector))
    points = get_qdrant_client().query_points(
        collection_name=get_memory_config().qdrant_collection,
        query=vector,
        using="dense",
        query_filter=_query_filter(user_id),
        limit=limit,
        with_payload=True,
    ).points
    return [{"memory_id": str(point.id), "score": float(point.score)} for point in points]


def _query_sparse(user_id: str, query: str, limit: int) -> list[dict[str, Any]]:
    client = get_qdrant_client()
    name = get_memory_config().qdrant_collection
    if not client.collection_exists(name):
        return []
    vector = get_sparse_encoder().encode_query(query)
    if not vector.indices:
        return []
    points = client.query_points(
        collection_name=name,
        query=vector,
        using="sparse",
        query_filter=_query_filter(user_id),
        limit=limit,
        with_payload=True,
    ).points
    return [{"memory_id": str(point.id), "score": float(point.score)} for point in points]


def rrf_fusion(
    dense: list[dict[str, Any]], sparse: list[dict[str, Any]], *, k: int = 60, limit: int = 5
) -> list[dict[str, Any]]:
    scores: dict[str, float] = {}
    details: dict[str, dict[str, float]] = {}
    for source, rows in (("dense", dense), ("sparse", sparse)):
        for rank, row in enumerate(rows, start=1):
            memory_id = row["memory_id"]
            scores[memory_id] = scores.get(memory_id, 0.0) + 1.0 / (k + rank)
            details.setdefault(memory_id, {})[source] = row["score"]
    ranked = sorted(scores, key=lambda memory_id: scores[memory_id], reverse=True)[:limit]
    return [
        {"memory_id": memory_id, "score": scores[memory_id], **details.get(memory_id, {})}
        for memory_id in ranked
    ]


def search_memories(
    *, user_id: str, query: str, mode: SearchMode = "hybrid", top_k: int | None = None, debug: bool = False
) -> MemorySearchResponse:
    started = time.perf_counter()
    config = get_memory_config()
    top_k = top_k or config.retrieval_top_k
    dense = _query_dense(user_id, query, config.dense_candidate_count) if mode in {"dense", "hybrid"} else []
    sparse = _query_sparse(user_id, query, config.sparse_candidate_count) if mode in {"sparse", "hybrid"} else []
    if mode == "dense":
        ranked = [{**row, "dense": row["score"]} for row in dense[:top_k]]
    elif mode == "sparse":
        ranked = [{**row, "sparse": row["score"]} for row in sparse[:top_k]]
    else:
        ranked = rrf_fusion(dense, sparse, k=config.rrf_k, limit=top_k)
    documents = get_active_memories(user_id, [row["memory_id"] for row in ranked])
    score_map = {row["memory_id"]: row for row in ranked}
    hits = [MemorySearchHit(
        memory_id=document["memory_id"],
        rank=rank,
        summary=document["summary"],
        type=str(document["type"]),
        facts=document.get("facts", []),
        keywords=document.get("keywords", []),
        importance=float(document.get("importance", 0.0)),
        confidence=float(document.get("confidence", 0.0)),
        version=int(document.get("version", 1)),
        scores={key: float(value) for key, value in score_map[document["memory_id"]].items() if key != "memory_id"},
    ) for rank, document in enumerate(documents, start=1)]
    latency_ms = (time.perf_counter() - started) * 1000
    return MemorySearchResponse(
        query=query,
        mode=mode,
        hits=hits,
        latency_ms=latency_ms,
        debug={"dense": dense, "sparse": sparse} if debug else None,
    )

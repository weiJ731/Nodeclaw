from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any, Iterable

from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.database import Database

from .config import get_memory_config
from .models import CandidateMemory, IndexStatus, MemoryStatus, utc_now


@lru_cache(maxsize=1)
def get_mongo_client() -> MongoClient:
    config = get_memory_config()
    return MongoClient(config.mongodb_uri, tz_aware=True, connectTimeoutMS=5000)


def get_database() -> Database:
    config = get_memory_config()
    return get_mongo_client()[config.mongodb_database]


def init_database() -> None:
    db = get_database()
    config = get_memory_config()

    db.users.create_index("username_normalized", unique=True)
    db.users.create_index("email_normalized", unique=True)
    db.refresh_tokens.create_index("token_hash", unique=True)
    db.refresh_tokens.create_index("expires_at", expireAfterSeconds=0)

    db.sessions.create_index([("user_id", ASCENDING), ("updated_at", DESCENDING)])
    db.sessions.create_index([("user_id", ASCENDING), ("session_id", ASCENDING)], unique=True)
    db.raw_exchanges.create_index([("user_id", ASCENDING), ("exchange_id", ASCENDING)], unique=True)
    db.raw_exchanges.create_index("expires_at", expireAfterSeconds=0)

    db.memories.create_index([("user_id", ASCENDING), ("memory_id", ASCENDING)], unique=True)
    db.memories.create_index([("user_id", ASCENDING), ("status", ASCENDING), ("type", ASCENDING)])
    db.memory_versions.create_index([("memory_id", ASCENDING), ("version", DESCENDING)], unique=True)
    db.outbox_events.create_index("event_id", unique=True)
    db.outbox_events.create_index([("status", ASCENDING), ("created_at", ASCENDING)])
    db.dead_letters.create_index([("queue", ASCENDING), ("created_at", DESCENDING)])

    db.scheduled_tasks.create_index([("user_id", ASCENDING), ("next_run_at", ASCENDING)])
    db.scheduled_tasks.create_index([("status", ASCENDING), ("next_run_at", ASCENDING)])
    db.notifications.create_index([("user_id", ASCENDING), ("created_at", DESCENDING)])
    db.notifications.create_index([("user_id", ASCENDING), ("read_at", ASCENDING)])
    db.notifications.create_index("event_key", unique=True, sparse=True)
    db.audit_events.create_index([("user_id", ASCENDING), ("created_at", DESCENDING)])
    db.audit_events.create_index("expires_at", expireAfterSeconds=0)

    # The value is intentionally materialized per document so retention remains
    # stable even when the configured default changes later.
    db.system_config.update_one(
        {"_id": "retention"},
        {"$set": {
            "raw_exchange_ttl_days": config.raw_exchange_ttl_days,
            "audit_ttl_days": config.audit_ttl_days,
            "updated_at": utc_now(),
        }},
        upsert=True,
    )


def healthcheck() -> dict[str, Any]:
    client = get_mongo_client()
    response = client.admin.command("ping")
    return {"ok": response.get("ok") == 1.0, "database": get_memory_config().mongodb_database}


def make_exchange_id(user_id: str, session_id: str, sequence: int) -> str:
    raw = f"{user_id}:{session_id}:{sequence}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


def save_raw_exchange(
    *,
    user_id: str,
    session_id: str,
    exchange_id: str,
    sequence: int,
    user_text: str,
    assistant_text: str,
    tool_events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    config = get_memory_config()
    now = utc_now()
    document = {
        "exchange_id": exchange_id,
        "user_id": user_id,
        "session_id": session_id,
        "sequence": sequence,
        "user_text": user_text,
        "assistant_text": assistant_text,
        "tool_events": tool_events or [],
        "created_at": now,
        "expires_at": now + timedelta(days=config.raw_exchange_ttl_days),
    }
    get_database().raw_exchanges.update_one(
        {"user_id": user_id, "exchange_id": exchange_id},
        {"$setOnInsert": document},
        upsert=True,
    )
    return document


def append_exchange_to_session(user_id: str, session_id: str, exchange: dict[str, Any]) -> dict[str, Any]:
    now = utc_now()
    return get_database().sessions.find_one_and_update(
        {"user_id": user_id, "session_id": session_id, "deleted_at": None},
        {
            "$push": {"recent_exchanges": exchange},
            "$set": {"updated_at": now, "memory_sync_status": "pending"},
            "$inc": {"exchange_count": 1},
        },
        return_document=ReturnDocument.AFTER,
    ) or {}


def get_session(user_id: str, session_id: str) -> dict[str, Any] | None:
    return get_database().sessions.find_one(
        {"user_id": user_id, "session_id": session_id, "deleted_at": None}
    )


def get_active_memories(user_id: str, memory_ids: Iterable[str]) -> list[dict[str, Any]]:
    ids = list(dict.fromkeys(memory_ids))
    if not ids:
        return []
    now = utc_now()
    query = {
        "user_id": user_id,
        "memory_id": {"$in": ids},
        "status": MemoryStatus.ACTIVE.value,
        "$or": [
            {"valid_until": {"$exists": False}},
            {"valid_until": None},
            {"valid_until": {"$gt": now}},
        ],
    }
    by_id = {doc["memory_id"]: doc for doc in get_database().memories.find(query)}
    return [by_id[memory_id] for memory_id in ids if memory_id in by_id]


def count_active_memories(user_id: str | None = None) -> int:
    query: dict[str, Any] = {"status": MemoryStatus.ACTIVE.value}
    if user_id:
        query["user_id"] = user_id
    return get_database().memories.count_documents(query)


def _version_snapshot(memory: dict[str, Any], action: str, reason: str) -> dict[str, Any]:
    return {
        "memory_id": memory["memory_id"],
        "user_id": memory["user_id"],
        "version": memory["version"],
        "action": action,
        "reason": reason,
        "snapshot": {key: value for key, value in memory.items() if key != "_id"},
        "created_at": utc_now(),
    }


def _outbox_document(memory: dict[str, Any], operation: str = "upsert") -> dict[str, Any]:
    return {
        "event_id": str(uuid.uuid4()),
        "kind": "memory_index",
        "operation": operation,
        "user_id": memory["user_id"],
        "memory_id": memory["memory_id"],
        "version": memory["version"],
        "status": "pending",
        "attempts": 0,
        "created_at": utc_now(),
        "updated_at": utc_now(),
    }


def create_memory(
    *,
    user_id: str,
    candidate: CandidateMemory,
    source_session_id: str,
    source_exchange_id: str,
    evidence_hash: str,
    reason: str,
) -> dict[str, Any]:
    now = utc_now()
    memory_id = str(uuid.uuid4())
    memory = {
        "memory_id": memory_id,
        "user_id": user_id,
        **candidate.model_dump(mode="python"),
        "retrieval_text": candidate.retrieval_text,
        "status": MemoryStatus.ACTIVE.value,
        "version": 1,
        "source_session_ids": [source_session_id],
        "source_exchange_ids": [source_exchange_id],
        "evidence_hashes": [evidence_hash],
        "source_refs": [{
            "session_id": source_session_id,
            "exchange_id": source_exchange_id,
            "evidence_hash": evidence_hash,
        }],
        "created_at": now,
        "updated_at": now,
        "indexed_version": 0,
        "index_status": IndexStatus.PENDING.value,
        "last_index_error": None,
    }
    db = get_database()
    with get_mongo_client().start_session() as mongo_session:
        with mongo_session.start_transaction():
            db.memories.insert_one(memory, session=mongo_session)
            db.memory_versions.insert_one(_version_snapshot(memory, "NEW", reason), session=mongo_session)
            db.outbox_events.insert_one(_outbox_document(memory), session=mongo_session)
    return memory


def update_memory(
    *,
    user_id: str,
    target: dict[str, Any],
    candidate: CandidateMemory,
    source_session_id: str,
    source_exchange_id: str,
    evidence_hash: str,
    action: str,
    reason: str,
) -> dict[str, Any]:
    now = utc_now()
    version = int(target.get("version", 1)) + 1
    updated = {
        **target,
        **candidate.model_dump(mode="python"),
        "retrieval_text": candidate.retrieval_text,
        "version": version,
        "updated_at": now,
        "indexed_version": int(target.get("indexed_version", 0)),
        "index_status": IndexStatus.PENDING.value,
        "last_index_error": None,
    }
    updated.pop("_id", None)
    updated["source_session_ids"] = list(dict.fromkeys([*target.get("source_session_ids", []), source_session_id]))
    updated["source_exchange_ids"] = list(dict.fromkeys([*target.get("source_exchange_ids", []), source_exchange_id]))
    updated["evidence_hashes"] = list(dict.fromkeys([*target.get("evidence_hashes", []), evidence_hash]))
    updated["source_refs"] = [
        *target.get("source_refs", []),
        {"session_id": source_session_id, "exchange_id": source_exchange_id, "evidence_hash": evidence_hash},
    ]
    updated["source_refs"] = list({
        (item["session_id"], item["exchange_id"]): item for item in updated["source_refs"]
    }.values())

    db = get_database()
    with get_mongo_client().start_session() as mongo_session:
        with mongo_session.start_transaction():
            result = db.memories.replace_one(
                {"user_id": user_id, "memory_id": target["memory_id"], "version": target["version"]},
                updated,
                session=mongo_session,
            )
            if result.modified_count != 1:
                raise RuntimeError("memory version conflict")
            db.memory_versions.insert_one(_version_snapshot(updated, action, reason), session=mongo_session)
            db.outbox_events.insert_one(_outbox_document(updated), session=mongo_session)
    return updated


def merge_memories(
    *,
    user_id: str,
    targets: list[dict[str, Any]],
    candidate: CandidateMemory,
    source_session_id: str,
    source_exchange_id: str,
    evidence_hash: str,
    reason: str,
) -> dict[str, Any]:
    if not targets:
        return create_memory(
            user_id=user_id,
            candidate=candidate,
            source_session_id=source_session_id,
            source_exchange_id=source_exchange_id,
            evidence_hash=evidence_hash,
            reason=reason,
        )
    now = utc_now()
    merged = {
        "memory_id": str(uuid.uuid4()),
        "user_id": user_id,
        **candidate.model_dump(mode="python"),
        "retrieval_text": candidate.retrieval_text,
        "status": MemoryStatus.ACTIVE.value,
        "version": 1,
        "source_session_ids": list(dict.fromkeys([
            source_session_id,
            *(item for target in targets for item in target.get("source_session_ids", [])),
        ])),
        "source_exchange_ids": list(dict.fromkeys([
            source_exchange_id,
            *(item for target in targets for item in target.get("source_exchange_ids", [])),
        ])),
        "evidence_hashes": list(dict.fromkeys([
            evidence_hash,
            *(item for target in targets for item in target.get("evidence_hashes", [])),
        ])),
        "source_refs": list({
            (item["session_id"], item["exchange_id"]): item
            for item in [
                {"session_id": source_session_id, "exchange_id": source_exchange_id, "evidence_hash": evidence_hash},
                *(item for target in targets for item in target.get("source_refs", [])),
            ]
        }.values()),
        "created_at": now,
        "updated_at": now,
        "indexed_version": 0,
        "index_status": IndexStatus.PENDING.value,
        "last_index_error": None,
    }
    db = get_database()
    target_ids = [target["memory_id"] for target in targets]
    with get_mongo_client().start_session() as mongo_session:
        with mongo_session.start_transaction():
            current_count = db.memories.count_documents(
                {"user_id": user_id, "memory_id": {"$in": target_ids}, "status": MemoryStatus.ACTIVE.value},
                session=mongo_session,
            )
            if current_count != len(target_ids):
                raise RuntimeError("memory merge version conflict")
            db.memories.insert_one(merged, session=mongo_session)
            db.memory_versions.insert_one(_version_snapshot(merged, "MERGE", reason), session=mongo_session)
            db.outbox_events.insert_one(_outbox_document(merged), session=mongo_session)
            db.memories.update_many(
                {"user_id": user_id, "memory_id": {"$in": target_ids}, "status": MemoryStatus.ACTIVE.value},
                {"$set": {
                    "status": MemoryStatus.SUPERSEDED.value,
                    "superseded_by": merged["memory_id"],
                    "updated_at": now,
                }},
                session=mongo_session,
            )
            for target in targets:
                superseded = {**target, "status": MemoryStatus.SUPERSEDED.value, "superseded_by": merged["memory_id"]}
                db.memory_versions.insert_one(
                    _version_snapshot(superseded, "SUPERSEDE", reason), session=mongo_session
                )
                db.outbox_events.insert_one(
                    _outbox_document(target, operation="delete"), session=mongo_session
                )
    return merged


def forget_memory_source(
    *, user_id: str, memory: dict[str, Any], source_session_id: str
) -> dict[str, Any]:
    refs = memory.get("source_refs", [])
    remaining_refs = [item for item in refs if item.get("session_id") != source_session_id]
    remaining_sessions = [item for item in memory.get("source_session_ids", []) if item != source_session_id]
    if refs:
        remaining_sessions = list(dict.fromkeys(item["session_id"] for item in remaining_refs))
    updated = {key: value for key, value in memory.items() if key != "_id"}
    updated.update({
        "source_refs": remaining_refs,
        "source_session_ids": remaining_sessions,
        "source_exchange_ids": (
            list(dict.fromkeys(item["exchange_id"] for item in remaining_refs))
            if refs else list(memory.get("source_exchange_ids", []))
        ),
        "evidence_hashes": (
            list(dict.fromkeys(item["evidence_hash"] for item in remaining_refs))
            if refs else list(memory.get("evidence_hashes", []))
        ),
        "version": int(memory["version"]) + 1,
        "updated_at": utc_now(),
        "status": MemoryStatus.ACTIVE.value if remaining_sessions else MemoryStatus.DELETED.value,
        "index_status": IndexStatus.PENDING.value,
    })
    operation = "upsert" if remaining_sessions else "delete"
    db = get_database()
    with get_mongo_client().start_session() as mongo_session:
        with mongo_session.start_transaction():
            result = db.memories.replace_one(
                {"user_id": user_id, "memory_id": memory["memory_id"], "version": memory["version"]},
                updated,
                session=mongo_session,
            )
            if result.modified_count != 1:
                raise RuntimeError("memory version conflict while forgetting source")
            db.memory_versions.insert_one(
                _version_snapshot(updated, "FORGET_SOURCE", f"forget session {source_session_id}"),
                session=mongo_session,
            )
            db.outbox_events.insert_one(_outbox_document(updated, operation=operation), session=mongo_session)
    return updated


def mark_index_ready(memory_id: str, version: int) -> None:
    get_database().memories.update_one(
        {"memory_id": memory_id, "version": version},
        {"$set": {"indexed_version": version, "index_status": IndexStatus.READY.value, "last_index_error": None}},
    )


def mark_index_failed(memory_id: str, version: int, error: str) -> None:
    get_database().memories.update_one(
        {"memory_id": memory_id, "version": version},
        {"$set": {"index_status": IndexStatus.FAILED.value, "last_index_error": error[:1000]}},
    )

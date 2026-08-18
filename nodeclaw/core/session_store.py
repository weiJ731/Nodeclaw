from __future__ import annotations

import uuid
from typing import Any
from pymongo import ReturnDocument

from memory_module_v3.models import utc_now
from memory_module_v3.storage import forget_memory_source, get_database


def create_session(user_id: str, title: str = "新对话") -> dict[str, Any]:
    now = utc_now()
    document = {
        "session_id": str(uuid.uuid4()),
        "user_id": user_id,
        "title": title.strip()[:80] or "新对话",
        "summary": "",
        "summary_version": 0,
        "recent_exchanges": [],
        "exchange_count": 0,
        "next_exchange_sequence": 0,
        "memory_sync_status": "ready",
        "last_memory_action": None,
        "created_at": now,
        "updated_at": now,
        "deleted_at": None,
    }
    get_database().sessions.insert_one(document)
    document.pop("_id", None)
    return document


def list_sessions(user_id: str, limit: int = 100) -> list[dict[str, Any]]:
    cursor = get_database().sessions.find(
        {"user_id": user_id, "deleted_at": None},
        {"_id": 0, "recent_exchanges": 0},
    ).sort("updated_at", -1).limit(limit)
    return list(cursor)


def require_session(user_id: str, session_id: str) -> dict[str, Any]:
    document = get_database().sessions.find_one(
        {"user_id": user_id, "session_id": session_id, "deleted_at": None}
    )
    if not document:
        raise KeyError("session not found")
    return document


def rename_session(user_id: str, session_id: str, title: str) -> dict[str, Any]:
    result = get_database().sessions.find_one_and_update(
        {"user_id": user_id, "session_id": session_id, "deleted_at": None},
        {"$set": {"title": title.strip()[:80] or "新对话", "updated_at": utc_now()}},
        return_document=ReturnDocument.AFTER,
        projection={"_id": 0, "recent_exchanges": 0},
    )
    if not result:
        raise KeyError("session not found")
    return result


def delete_session(user_id: str, session_id: str) -> None:
    result = get_database().sessions.update_one(
        {"user_id": user_id, "session_id": session_id, "deleted_at": None},
        {"$set": {"deleted_at": utc_now(), "updated_at": utc_now()}},
    )
    if result.modified_count != 1:
        raise KeyError("session not found")
    get_database().raw_exchanges.delete_many({"user_id": user_id, "session_id": session_id})
    get_database().checkpoints.delete_many({"thread_id": session_id})
    get_database().checkpoint_writes.delete_many({"thread_id": session_id})


def forget_session_memories(user_id: str, session_id: str) -> dict[str, int]:
    db = get_database()
    affected = 0
    for memory in db.memories.find({
        "user_id": user_id,
        "status": "active",
        "source_session_ids": session_id,
    }):
        forget_memory_source(user_id=user_id, memory=memory, source_session_id=session_id)
        affected += 1
    return {"affected": affected}

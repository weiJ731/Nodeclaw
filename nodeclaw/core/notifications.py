from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from redis import Redis

from memory_module_v3.config import get_memory_config
from memory_module_v3.models import utc_now
from memory_module_v3.storage import get_database

logger = logging.getLogger(__name__)


def create_notification(
    user_id: str,
    kind: str,
    content: str,
    *,
    event_key: str | None = None,
    **metadata: Any,
) -> dict[str, Any]:
    db = get_database()
    if event_key:
        existing = db.notifications.find_one({"event_key": event_key})
        if existing:
            return {key: value for key, value in existing.items() if key != "_id"}
    document = {
        "notification_id": str(uuid.uuid4()),
        "user_id": user_id,
        "kind": kind,
        "content": content,
        "metadata": metadata,
        "created_at": utc_now(),
        "read_at": None,
    }
    if event_key:
        document["event_key"] = event_key
    try:
        db.notifications.insert_one(document)
    except Exception:
        if event_key:
            existing = db.notifications.find_one({"event_key": event_key})
            if existing:
                return {key: value for key, value in existing.items() if key != "_id"}
        raise
    payload = {key: value for key, value in document.items() if key != "_id"}
    try:
        Redis.from_url(get_memory_config().redis_url).publish(
            f"nodeclaw:events:{user_id}", json.dumps(payload, ensure_ascii=False, default=str)
        )
    except Exception as exc:
        # Mongo persists offline notifications; Redis only accelerates live delivery.
        logger.warning("Notification persisted but live publish failed: %s", exc)
    return payload


def list_notifications(user_id: str, unread_only: bool = False, limit: int = 100) -> list[dict[str, Any]]:
    query: dict[str, Any] = {"user_id": user_id}
    if unread_only:
        query["read_at"] = None
    return list(get_database().notifications.find(query, {"_id": 0}).sort("created_at", -1).limit(limit))


def mark_notification_read(user_id: str, notification_id: str) -> bool:
    result = get_database().notifications.update_one(
        {"user_id": user_id, "notification_id": notification_id},
        {"$set": {"read_at": utc_now()}},
    )
    return result.modified_count == 1

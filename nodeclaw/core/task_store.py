from __future__ import annotations

import calendar
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo
from pymongo import ReturnDocument

from memory_module_v3.models import utc_now
from memory_module_v3.storage import get_database

LOCAL_TZ = ZoneInfo("Asia/Shanghai")
REPEAT_VALUES = {None, "hourly", "daily", "weekly", "monthly"}


def parse_local_time(value: str) -> datetime:
    parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=LOCAL_TZ)
    return parsed.astimezone(timezone.utc)


def display_local_time(value: datetime) -> str:
    return value.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")


def create_task(
    *, user_id: str, session_id: str | None, target_time: str, description: str,
    repeat: str | None = None, repeat_count: int | None = None,
) -> dict[str, Any]:
    if repeat not in REPEAT_VALUES:
        raise ValueError("repeat 只能是 hourly、daily、weekly、monthly 或空值")
    next_run = parse_local_time(target_time)
    if next_run <= utc_now():
        raise ValueError("提醒时间必须晚于当前时间")
    document = {
        "task_id": str(uuid.uuid4()),
        "user_id": user_id,
        "session_id": session_id,
        "description": description.strip(),
        "repeat": repeat,
        "repeat_count": repeat_count,
        "next_run_at": next_run,
        "status": "active",
        "created_at": utc_now(),
        "updated_at": utc_now(),
    }
    get_database().scheduled_tasks.insert_one(document)
    return serialize_task(document)


def serialize_task(document: dict[str, Any]) -> dict[str, Any]:
    next_run = document["next_run_at"]
    return {
        "id": document["task_id"],
        "session_id": document.get("session_id"),
        "target_time": display_local_time(next_run),
        "description": document["description"],
        "repeat": document.get("repeat"),
        "repeat_count": document.get("repeat_count"),
        "seconds_left": int((next_run - utc_now()).total_seconds()),
        "status": document.get("status", "active"),
    }


def list_tasks(user_id: str) -> list[dict[str, Any]]:
    rows = get_database().scheduled_tasks.find({"user_id": user_id, "status": "active"}).sort("next_run_at", 1)
    return [serialize_task(row) for row in rows]


def delete_task(user_id: str, task_id: str) -> bool:
    result = get_database().scheduled_tasks.update_one(
        {"user_id": user_id, "task_id": task_id, "status": "active"},
        {"$set": {"status": "cancelled", "updated_at": utc_now()}},
    )
    return result.modified_count == 1


def update_task(user_id: str, task_id: str, **changes: Any) -> dict[str, Any] | None:
    update: dict[str, Any] = {"updated_at": utc_now()}
    if changes.get("target_time"):
        update["next_run_at"] = parse_local_time(changes["target_time"])
    if changes.get("description") is not None:
        update["description"] = str(changes["description"]).strip()
    if "repeat" in changes:
        if changes["repeat"] not in REPEAT_VALUES:
            raise ValueError("invalid repeat")
        update["repeat"] = changes["repeat"]
    if "repeat_count" in changes:
        update["repeat_count"] = changes["repeat_count"]
    row = get_database().scheduled_tasks.find_one_and_update(
        {"user_id": user_id, "task_id": task_id, "status": "active"},
        {"$set": update},
        return_document=ReturnDocument.AFTER,
    )
    return serialize_task(row) if row else None


def _next_occurrence(current: datetime, repeat: str) -> datetime:
    if repeat == "hourly":
        return current + timedelta(hours=1)
    if repeat == "daily":
        return current + timedelta(days=1)
    if repeat == "weekly":
        return current + timedelta(days=7)
    local = current.astimezone(LOCAL_TZ)
    month = local.month + 1
    year = local.year
    if month > 12:
        month = 1
        year += 1
    day = min(local.day, calendar.monthrange(year, month)[1])
    return local.replace(year=year, month=month, day=day).astimezone(timezone.utc)


def claim_due_tasks(limit: int = 100) -> list[dict[str, Any]]:
    db = get_database()
    lease_cutoff = utc_now() - timedelta(minutes=5)
    db.scheduled_tasks.update_many(
        {"status": "processing", "claimed_at": {"$lt": lease_cutoff}},
        {"$set": {"status": "active", "updated_at": utc_now()}, "$unset": {"claim_id": "", "claimed_at": ""}},
    )
    due = list(db.scheduled_tasks.find({"status": "active", "next_run_at": {"$lte": utc_now()}}).limit(limit))
    claimed: list[dict[str, Any]] = []
    for row in due:
        claim_id = str(uuid.uuid4())
        result = db.scheduled_tasks.find_one_and_update(
            {"_id": row["_id"], "status": "active", "next_run_at": row["next_run_at"]},
            {"$set": {
                "status": "processing",
                "claim_id": claim_id,
                "claimed_at": utc_now(),
                "updated_at": utc_now(),
            }},
            return_document=ReturnDocument.AFTER,
        )
        if result:
            claimed.append(result)
    return claimed


def finish_task(document: dict[str, Any]) -> None:
    repeat = document.get("repeat")
    count = document.get("repeat_count")
    if repeat and (count is None or count > 1):
        update = {
            "status": "active",
            "next_run_at": _next_occurrence(document["next_run_at"], repeat),
            "updated_at": utc_now(),
        }
        if count is not None:
            update["repeat_count"] = count - 1
    else:
        update = {"status": "completed", "updated_at": utc_now()}
    get_database().scheduled_tasks.update_one(
        {"_id": document["_id"], "claim_id": document.get("claim_id")},
        {"$set": update, "$unset": {"claim_id": "", "claimed_at": ""}},
    )

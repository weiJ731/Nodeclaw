from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from datetime import timedelta
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from pymongo import ReturnDocument

from nodeclaw.core.provider import get_provider

from .config import get_memory_config
from .lifecycle import decide_lifecycle, extract_candidates
from .models import LifecycleAction, utc_now
from .retrieval import delete_memory_index, index_memory, search_memories
from .storage import (
    append_exchange_to_session,
    create_memory,
    get_active_memories,
    get_database,
    get_session,
    make_exchange_id,
    mark_index_failed,
    mark_index_ready,
    merge_memories,
    save_raw_exchange,
    update_memory,
)

logger = logging.getLogger(__name__)


def _jsonl_audit(document: dict[str, Any]) -> None:
    log_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs")
    os.makedirs(log_dir, exist_ok=True)
    safe = {
        **document,
        "user_id": hashlib.sha256(document["user_id"].encode("utf-8")).hexdigest()[:16],
    }
    with open(os.path.join(log_dir, "memory_v3_audit.jsonl"), "a", encoding="utf-8") as handle:
        handle.write(json.dumps(safe, ensure_ascii=False, default=str) + "\n")

TRIVIAL_MESSAGES = {"好", "好的", "嗯", "哦", "谢谢", "知道了", "继续", "可以", "ok", "okay"}
MEMORY_HINTS = re.compile(r"记得|之前|以前|偏好|喜欢|不喜欢|目标|计划|项目|工作|学校|家人|朋友|以后|长期|决定|必须|不要")


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    chinese = sum("\u4e00" <= char <= "\u9fff" for char in text)
    return int(chinese / 1.5 + (len(text) - chinese) / 4) + 1


def should_extract(user_text: str, assistant_text: str) -> bool:
    clean = "".join(user_text.lower().split())
    if not clean or clean in TRIVIAL_MESSAGES:
        return False
    if len(clean) < 6 and not MEMORY_HINTS.search(clean):
        return False
    return bool(MEMORY_HINTS.search(user_text) or len(user_text) >= 20)


def should_retrieve(query: str) -> bool:
    clean = "".join(query.lower().split())
    if not clean or clean in TRIVIAL_MESSAGES:
        return False
    return bool(MEMORY_HINTS.search(query) or len(query) >= 12)


def audit_event(user_id: str, event: str, **payload: Any) -> None:
    config = get_memory_config()
    now = utc_now()
    safe_payload = {key: value for key, value in payload.items() if key not in {"prompt", "token", "password"}}
    document = {
        "user_id": user_id,
        "event": event,
        "payload": safe_payload,
        "created_at": now,
        "expires_at": now + timedelta(days=config.audit_ttl_days),
    }
    get_database().audit_events.insert_one(document)
    _jsonl_audit(document)


def ingest_exchange(
    *, user_id: str, session_id: str, user_text: str, assistant_text: str, tool_events: list[dict[str, Any]] | None = None
) -> tuple[str, bool]:
    db = get_database()
    session = db.sessions.find_one_and_update(
        {"user_id": user_id, "session_id": session_id, "deleted_at": None},
        {"$inc": {"next_exchange_sequence": 1}},
        return_document=ReturnDocument.AFTER,
    )
    if not session:
        raise ValueError("session not found")
    sequence = int(session.get("next_exchange_sequence", 1))
    exchange_id = make_exchange_id(user_id, session_id, sequence)
    document = save_raw_exchange(
        user_id=user_id,
        session_id=session_id,
        exchange_id=exchange_id,
        sequence=sequence,
        user_text=user_text,
        assistant_text=assistant_text,
        tool_events=tool_events,
    )
    short_exchange = {
        "exchange_id": exchange_id,
        "sequence": sequence,
        "user_text": user_text,
        "assistant_text": assistant_text,
        "tool_events": [{"name": item.get("name", "tool")} for item in (tool_events or [])],
        "created_at": document["created_at"],
    }
    updated = append_exchange_to_session(user_id, session_id, short_exchange)
    recent = updated.get("recent_exchanges", [])
    token_count = sum(estimate_tokens(row.get("user_text", "") + row.get("assistant_text", "")) for row in recent)
    config = get_memory_config()
    needs_summary = config.summary_enabled and (
        len(recent) > config.recent_exchange_limit or token_count > config.recent_exchange_token_limit
    )
    audit_event(user_id, "exchange_recorded", session_id=session_id, exchange_id=exchange_id)
    return exchange_id, needs_summary


def process_exchange(user_id: str, session_id: str, exchange_id: str) -> dict[str, Any]:
    db = get_database()
    exchange = db.raw_exchanges.find_one({"user_id": user_id, "exchange_id": exchange_id})
    session = get_session(user_id, session_id)
    if not exchange or not session:
        return {"status": "skipped", "reason": "exchange or session missing"}
    if not get_memory_config().extraction_enabled or not should_extract(exchange["user_text"], exchange["assistant_text"]):
        audit_event(user_id, "memory_discarded", exchange_id=exchange_id, stage="gate")
        db.sessions.update_one({"user_id": user_id, "session_id": session_id}, {"$set": {"memory_sync_status": "ready"}})
        return {"status": "discarded", "reason": "write gate"}

    recent = session.get("recent_exchanges", [])
    candidates = extract_candidates(
        session_summary=session.get("summary", ""),
        recent_exchanges=[row for row in recent if row.get("exchange_id") != exchange_id],
        current_exchange=exchange,
    )
    actions: list[dict[str, Any]] = []
    evidence_hash = hashlib.sha256(
        f"{exchange['user_text']}\n{exchange['assistant_text']}".encode("utf-8")
    ).hexdigest()
    for candidate in candidates:
        try:
            related_hits = search_memories(
                user_id=user_id, query=candidate.retrieval_text, mode="hybrid", top_k=8
            ).hits
            existing = get_active_memories(user_id, [hit.memory_id for hit in related_hits])
        except Exception as exc:
            logger.warning("Related-memory search unavailable; treating candidate conservatively: %s", exc)
            existing = []
        decision = decide_lifecycle(candidate, existing)
        resolved = decision.resolved_memory or candidate
        if decision.action == LifecycleAction.NEW:
            memory = create_memory(
                user_id=user_id,
                candidate=resolved,
                source_session_id=session_id,
                source_exchange_id=exchange_id,
                evidence_hash=evidence_hash,
                reason=decision.reason,
            )
        elif decision.action == LifecycleAction.UPDATE:
            target = next((item for item in existing if item["memory_id"] in decision.target_memory_ids), None)
            if not target:
                continue
            memory = update_memory(
                user_id=user_id,
                target=target,
                candidate=resolved,
                source_session_id=session_id,
                source_exchange_id=exchange_id,
                evidence_hash=evidence_hash,
                action="UPDATE",
                reason=decision.reason,
            )
        elif decision.action == LifecycleAction.MERGE:
            targets = [item for item in existing if item["memory_id"] in decision.target_memory_ids]
            memory = merge_memories(
                user_id=user_id,
                targets=targets,
                candidate=resolved,
                source_session_id=session_id,
                source_exchange_id=exchange_id,
                evidence_hash=evidence_hash,
                reason=decision.reason,
            )
        else:
            audit_event(user_id, "memory_discarded", exchange_id=exchange_id, reason=decision.reason)
            actions.append({"action": "DISCARD", "reason": decision.reason})
            continue
        audit_event(
            user_id,
            f"memory_{decision.action.value.lower()}",
            memory_id=memory["memory_id"],
            exchange_id=exchange_id,
            version=memory["version"],
        )
        actions.append({"action": decision.action.value, "memory_id": memory["memory_id"]})
    db.sessions.update_one(
        {"user_id": user_id, "session_id": session_id},
        {"$set": {"memory_sync_status": "ready", "last_memory_action": actions[-1] if actions else None}},
    )
    return {"status": "processed", "candidate_count": len(candidates), "actions": actions}


def compact_session(user_id: str, session_id: str) -> dict[str, Any]:
    config = get_memory_config()
    session = get_session(user_id, session_id)
    if not session:
        return {"status": "skipped", "reason": "session missing"}
    recent = session.get("recent_exchanges", [])
    if len(recent) <= config.recent_exchange_limit:
        token_count = sum(estimate_tokens(row.get("user_text", "") + row.get("assistant_text", "")) for row in recent)
        if token_count <= config.recent_exchange_token_limit:
            return {"status": "skipped", "reason": "below thresholds"}
    compact_count = min(config.summary_compact_count, max(1, len(recent) - 3))
    selected = recent[:compact_count]
    prompt = {
        "old_summary": session.get("summary", ""),
        "exchanges": [{"user": row.get("user_text"), "assistant": row.get("assistant_text")} for row in selected],
        "max_tokens": config.summary_max_tokens,
    }
    llm = get_provider(config.memory_provider, config.memory_model, temperature=0.0)
    response = llm.invoke([
        SystemMessage(content=(
            "你负责增量维护当前会话摘要。保留当前任务、已确认结论、未完成事项和关键指代；"
            "不要写长期用户画像，不要添加原文中不存在的事实。只返回摘要正文。"
        )),
        HumanMessage(content=json.dumps(prompt, ensure_ascii=False)),
    ])
    summary = str(response.content).strip()
    if estimate_tokens(summary) > config.summary_max_tokens:
        summary = summary[: config.summary_max_tokens * 2]
    selected_ids = [row["exchange_id"] for row in selected]
    result = get_database().sessions.update_one(
        {"user_id": user_id, "session_id": session_id, "summary_version": session.get("summary_version", 0)},
        {
            "$set": {"summary": summary, "updated_at": utc_now()},
            "$inc": {"summary_version": 1},
            "$pull": {"recent_exchanges": {"exchange_id": {"$in": selected_ids}}},
        },
    )
    if result.modified_count != 1:
        raise RuntimeError("session summary version conflict")
    audit_event(user_id, "session_summary_updated", session_id=session_id, compacted_exchange_ids=selected_ids)
    return {"status": "updated", "compacted": len(selected_ids), "summary_tokens": estimate_tokens(summary)}


def build_memory_context(user_id: str, query: str) -> str:
    if not should_retrieve(query):
        return ""
    config = get_memory_config()
    try:
        response = search_memories(user_id=user_id, query=query, mode="hybrid", top_k=config.retrieval_top_k)
    except Exception as exc:
        logger.warning("Memory retrieval failed without blocking chat: %s", exc)
        return ""
    blocks: list[str] = []
    used_tokens = 0
    for hit in response.hits:
        block = f"[{hit.type}] {hit.summary}"
        if hit.facts:
            block += "\n事实：" + "；".join(hit.facts)
        block_tokens = estimate_tokens(block)
        if used_tokens + block_tokens > config.context_token_limit:
            break
        blocks.append(block)
        used_tokens += block_tokens
    if blocks:
        audit_event(user_id, "memory_injected", memory_ids=[hit.memory_id for hit in response.hits[:len(blocks)]])
    return "\n\n".join(blocks)


def sync_outbox_event(event_id: str) -> dict[str, Any]:
    db = get_database()
    event = db.outbox_events.find_one({"event_id": event_id})
    if not event or event.get("status") == "done":
        return {"status": "skipped"}
    try:
        if event["operation"] == "delete":
            delete_memory_index(event["memory_id"])
        else:
            memory = db.memories.find_one({"memory_id": event["memory_id"], "version": event["version"]})
            if not memory:
                db.outbox_events.update_one(
                    {"event_id": event_id},
                    {"$set": {"status": "done", "result": "stale", "updated_at": utc_now()}},
                )
                return {"status": "stale"}
            index_memory(memory)
            mark_index_ready(memory["memory_id"], memory["version"])
        db.outbox_events.update_one({"event_id": event_id}, {"$set": {"status": "done", "updated_at": utc_now()}})
        return {"status": "done"}
    except Exception as exc:
        db.outbox_events.update_one(
            {"event_id": event_id},
            {"$set": {"status": "retrying", "last_error": str(exc)[:1000], "updated_at": utc_now()}, "$inc": {"attempts": 1}},
        )
        mark_index_failed(event["memory_id"], event["version"], str(exc))
        raise


def reindex_user(user_id: str | None = None) -> dict[str, int]:
    query: dict[str, Any] = {"status": "active"}
    if user_id:
        query["user_id"] = user_id
    indexed = 0
    failed = 0
    for memory in get_database().memories.find(query):
        try:
            index_memory(memory)
            mark_index_ready(memory["memory_id"], memory["version"])
            indexed += 1
        except Exception as exc:
            mark_index_failed(memory["memory_id"], memory["version"], str(exc))
            failed += 1
    return {"indexed": indexed, "failed": failed}

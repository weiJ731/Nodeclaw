from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from nodeclaw.core.provider import get_provider

from .config import get_memory_config
from .models import CandidateMemory, LifecycleAction, LifecycleDecision

logger = logging.getLogger(__name__)

ALLOWED_MEMORY_DESCRIPTION = """
只允许提取以下长期信息：个人事实、长期偏好、长期目标、项目/任务背景、人物关系、重要决定、长期约束。
临时状态、寒暄、一次性命令、助手自行推断、未被用户确认的工具结果必须忽略。
每条候选记忆必须原子化；一次对话可以返回多条候选，也可以返回空列表。
""".strip()


def _memory_llm():
    config = get_memory_config()
    return get_provider(config.memory_provider, config.memory_model, temperature=0.0)


def _json_from_response(response: Any) -> Any:
    content = getattr(response, "content", response)
    if isinstance(content, (dict, list)):
        return content
    text = str(content).strip()
    if text.startswith("```"):
        text = "\n".join(text.splitlines()[1:-1]).strip()
    decoder = json.JSONDecoder()
    for index, character in enumerate(text):
        if character not in "[{":
            continue
        try:
            parsed, _ = decoder.raw_decode(text[index:])
            return parsed
        except json.JSONDecodeError:
            continue
    raise ValueError("MEMORY_LLM did not return valid JSON")


def extract_candidates(
    *, session_summary: str, recent_exchanges: list[dict[str, Any]], current_exchange: dict[str, Any]
) -> list[CandidateMemory]:
    context_rows = []
    for exchange in recent_exchanges[-5:]:
        context_rows.append(
            f"User: {exchange.get('user_text', '')}\nAssistant: {exchange.get('assistant_text', '')}"
        )
    prompt = {
        "session_summary": session_summary,
        "recent_context": context_rows,
        "current_exchange": {
            "user": current_exchange.get("user_text", ""),
            "assistant": current_exchange.get("assistant_text", ""),
            "tool_names": [item.get("name") for item in current_exchange.get("tool_events", [])],
        },
    }
    messages = [
        SystemMessage(content=(
            "你是 Nodeclaw 的长期记忆提取器。\n"
            f"{ALLOWED_MEMORY_DESCRIPTION}\n"
            "输出严格 JSON：{\"candidates\":[{\"type\":\"preference\",\"summary\":\"...\","
            "\"facts\":[\"...\"],\"keywords\":[\"...\"],\"importance\":0.0,"
            "\"confidence\":0.0,\"valid_from\":null,\"valid_until\":null,"
            "\"evidence_summary\":\"不超过100字的最小证据摘要\"}]}。"
            "type 只能是 personal_fact、preference、long_term_goal、project_context、relationship、decision、constraint。"
        )),
        HumanMessage(content=json.dumps(prompt, ensure_ascii=False, default=str)),
    ]
    raw = _json_from_response(_memory_llm().invoke(messages))
    rows = raw.get("candidates", []) if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        raise ValueError("candidates must be a list")
    candidates: list[CandidateMemory] = []
    for row in rows[:10]:
        try:
            candidates.append(CandidateMemory.model_validate(row))
        except Exception as exc:
            logger.warning("Ignoring invalid memory candidate: %s", exc)
    return candidates


def _normalized_facts(memory: dict[str, Any] | CandidateMemory) -> set[str]:
    values = memory.facts if isinstance(memory, CandidateMemory) else memory.get("facts", [])
    return {"".join(str(value).lower().split()) for value in values if value}


def deterministic_decision(
    candidate: CandidateMemory, existing: list[dict[str, Any]]
) -> LifecycleDecision | None:
    if candidate.confidence < 0.45 or candidate.importance < 0.25:
        return LifecycleDecision(
            action=LifecycleAction.DISCARD,
            reason="candidate below confidence or importance threshold",
            confidence=1.0,
        )
    candidate_summary = "".join(candidate.summary.lower().split())
    candidate_facts = _normalized_facts(candidate)
    for memory in existing:
        same_summary = candidate_summary == "".join(str(memory.get("summary", "")).lower().split())
        existing_facts = _normalized_facts(memory)
        if same_summary or (candidate_facts and candidate_facts <= existing_facts):
            return LifecycleDecision(
                action=LifecycleAction.DISCARD,
                target_memory_ids=[memory["memory_id"]],
                reason="semantically identical memory already exists",
                confidence=1.0,
            )
    if not existing:
        return LifecycleDecision(
            action=LifecycleAction.NEW,
            reason="no related active memory",
            confidence=1.0,
            resolved_memory=candidate,
        )
    return None


def decide_lifecycle(candidate: CandidateMemory, existing: list[dict[str, Any]]) -> LifecycleDecision:
    deterministic = deterministic_decision(candidate, existing)
    if deterministic:
        return deterministic

    safe_existing = [{
        "memory_id": memory["memory_id"],
        "type": memory.get("type"),
        "summary": memory.get("summary"),
        "facts": memory.get("facts", []),
        "version": memory.get("version", 1),
    } for memory in existing[:8]]
    messages = [
        SystemMessage(content=(
            "你是长期记忆生命周期控制器。只输出 JSON。动作："
            "NEW=新主题；UPDATE=对一条旧记忆的明确补充或修正；"
            "MERGE=多条同主题旧记忆应合并；DISCARD=重复、含糊或无长期价值。"
            "最新且明确的用户陈述可以修正旧信息；不明确时不得覆盖。"
            "输出 {\"action\":\"UPDATE\",\"target_memory_ids\":[\"...\"],"
            "\"reason\":\"...\",\"confidence\":0.0,\"resolved_memory\":{候选记忆完整结构}}。"
        )),
        HumanMessage(content=json.dumps({
            "candidate": candidate.model_dump(mode="json"),
            "existing": safe_existing,
        }, ensure_ascii=False)),
    ]
    raw = _json_from_response(_memory_llm().invoke(messages))
    decision = LifecycleDecision.model_validate(raw)
    allowed_ids = {memory["memory_id"] for memory in existing}
    decision.target_memory_ids = [item for item in decision.target_memory_ids if item in allowed_ids]
    if decision.action in {LifecycleAction.UPDATE, LifecycleAction.MERGE} and not decision.target_memory_ids:
        decision.action = LifecycleAction.NEW
    if decision.action == LifecycleAction.MERGE and (
        not get_memory_config().merge_enabled or len(decision.target_memory_ids) < 2
    ):
        decision.action = LifecycleAction.UPDATE
        decision.target_memory_ids = decision.target_memory_ids[:1]
    if decision.resolved_memory is None and decision.action != LifecycleAction.DISCARD:
        decision.resolved_memory = candidate
    return decision

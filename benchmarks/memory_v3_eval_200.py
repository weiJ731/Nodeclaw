"""Nodeclaw Memory V3 200-case retrieval and generation evaluation."""

from __future__ import annotations

import argparse
import json
import math
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from langchain_core.messages import HumanMessage, SystemMessage
from qdrant_client import models

from memory_module_v3.config import get_memory_config
from memory_module_v3.retrieval import (
    ChineseBM25Encoder,
    delete_memory_index,
    ensure_collection,
    get_qdrant_client,
    get_sparse_encoder,
    search_memories,
)
from memory_module_v3.storage import get_database
from nodeclaw.core.provider import get_embedding_model, get_provider


DATASET_PATH = Path(__file__).parent / "data" / "memory_v3_eval_200.json"
BUCKETS = {"fact_qa": 80, "multi_hop_qa": 60, "business_rule_qa": 60}
BENCHMARK_TAG = "memory_v3_eval_200_v1"

PROJECTS = [
    "星舟", "云桥", "青岚", "远帆", "晨曦", "灵犀", "天穹", "北辰", "流光", "山海",
    "启明", "知微", "沧澜", "望舒", "玄鸟", "赤霄", "白泽", "扶摇", "长风", "归鸿",
]
FACT_PROJECTS = ["海棠", "松风", "月泉", "竹影", "清和", "南枝", "听雨", "兰台", "疏影", "微澜"]
TECHS = [
    "FastAPI", "Django", "Flask", "Spring Boot", "NestJS", "Go Fiber", "Gin", "Ktor", "Express", "Sanic",
    "FastAPI", "Django", "Flask", "Spring Boot", "NestJS", "Go Fiber", "Gin", "Ktor", "Express", "Sanic",
]
OWNERS = [
    "陈工", "李工", "王工", "周工", "赵工", "孙工", "吴工", "郑工", "冯工", "何工",
    "郭工", "林工", "高工", "罗工", "宋工", "谢工", "唐工", "韩工", "曹工", "许工",
]
CITIES = [
    "苏州", "南京", "上海", "杭州", "无锡", "常州", "合肥", "武汉", "成都", "深圳",
    "北京", "天津", "重庆", "西安", "长沙", "青岛", "厦门", "宁波", "济南", "福州",
]


def _memory(label: str, memory_type: str, summary: str, keywords: list[str]) -> dict[str, Any]:
    return {
        "label": label,
        "type": memory_type,
        "summary": summary,
        "facts": [summary],
        "keywords": keywords,
    }


def _case(
    case_id: str,
    bucket: str,
    question: str,
    expected: list[str],
    reference_answer: str,
) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "bucket": bucket,
        "question": question,
        "expected_memory_ids": expected,
        "reference_answer": reference_answer,
    }


def build_eval_dataset() -> dict[str, Any]:
    memories: list[dict[str, Any]] = []
    cases: list[dict[str, Any]] = []

    fact_specs = [
        ("project_context", "{name}项目的服务端框架是{value}。", "{name}项目采用什么服务端框架？", TECHS[:10]),
        ("decision", "{name}项目的主数据库确定为{value}。", "{name}项目最终选用了哪种主数据库？", ["MongoDB", "PostgreSQL", "MySQL", "MongoDB", "PostgreSQL", "MySQL", "MongoDB", "PostgreSQL", "MySQL", "MongoDB"]),
        ("relationship", "{name}项目的技术负责人是{value}。", "谁负责{name}项目的技术工作？", OWNERS[:10]),
        ("project_context", "{name}项目计划在{value}完成首版交付。", "{name}项目的首版交付时间是什么时候？", [f"2026年{month}月15日" for month in range(1, 11)]),
        ("constraint", "{name}项目要求所有接口必须使用{value}。", "{name}项目的接口必须采用什么安全方式？", ["JWT鉴权", "OAuth 2.0", "双向TLS", "API Key签名", "JWT鉴权", "OAuth 2.0", "双向TLS", "API Key签名", "JWT鉴权", "OAuth 2.0"]),
        ("project_context", "{name}项目的主要部署区域是{value}。", "{name}项目主要部署在哪个城市？", CITIES[:10]),
        ("preference", "{name}项目的周报偏好使用{value}。", "{name}项目周报应该采用什么表达方式？", ["中文分点", "先给结论", "表格汇总", "简洁短句", "中文分点", "先给结论", "表格汇总", "简洁短句", "中文分点", "先给结论"]),
        ("long_term_goal", "{name}项目本季度的核心目标是{value}。", "{name}项目本季度最重要的目标是什么？", ["完成内测", "降低延迟", "提升召回率", "完成安全审计", "完成内测", "降低延迟", "提升召回率", "完成安全审计", "完成内测", "降低延迟"]),
    ]
    fact_index = 0
    for memory_type, summary_template, question_template, values in fact_specs:
        for offset, value in enumerate(values):
            fact_index += 1
            name = FACT_PROJECTS[offset]
            label = f"fact-{fact_index:03d}"
            summary = summary_template.format(name=name, value=value)
            memories.append(_memory(label, memory_type, summary, [name, value]))
            cases.append(_case(
                f"fact-{fact_index:03d}",
                "fact_qa",
                question_template.format(name=name),
                [label],
                summary,
            ))

    for index, name in enumerate(PROJECTS, start=1):
        owner = OWNERS[index - 1]
        tech = TECHS[index - 1]
        deadline = f"2026年{(index % 12) + 1}月{(index % 20) + 8}日"
        city = CITIES[index - 1]
        owner_id = f"multi-{index:02d}-owner"
        tech_id = f"multi-{index:02d}-tech"
        plan_id = f"multi-{index:02d}-plan"
        owner_summary = f"{name}平台由{owner}担任技术负责人。"
        tech_summary = f"{name}平台使用{tech}构建服务端。"
        plan_summary = f"{name}平台将在{deadline}于{city}完成上线。"
        memories.extend([
            _memory(owner_id, "relationship", owner_summary, [name, owner]),
            _memory(tech_id, "project_context", tech_summary, [name, tech]),
            _memory(plan_id, "project_context", plan_summary, [name, deadline, city]),
        ])
        cases.extend([
            _case(f"multi-{index:02d}-a", "multi_hop_qa", f"{name}平台由谁负责，并使用什么服务端技术？", [owner_id, tech_id], f"由{owner}负责，服务端使用{tech}。"),
            _case(f"multi-{index:02d}-b", "multi_hop_qa", f"{name}平台使用什么技术，计划何时上线？", [tech_id, plan_id], f"服务端使用{tech}，计划在{deadline}上线。"),
            _case(f"multi-{index:02d}-c", "multi_hop_qa", f"{name}平台的负责人是谁，最终在哪个城市上线？", [owner_id, plan_id], f"负责人是{owner}，上线城市是{city}。"),
        ])

    for index in range(1, 21):
        policy = f"POL-{index:02d}"
        threshold = 3000 + index * 500
        approver = ["部门经理", "业务总监", "财务负责人", "采购负责人"][index % 4]
        quotes = 2 + index % 3
        days = 5 + index % 6
        amount_id = f"rule-{index:02d}-amount"
        urgent_id = f"rule-{index:02d}-urgent"
        invoice_id = f"rule-{index:02d}-invoice"
        amount_summary = f"{policy}规定：采购金额不超过{threshold}元由组长审批，超过{threshold}元由{approver}审批。"
        urgent_summary = f"{policy}规定：紧急采购仍须至少提供{quotes}家供应商报价。"
        invoice_summary = f"{policy}规定：采购完成后须在{days}个自然日内提交发票，逾期需要补充说明。"
        memories.extend([
            _memory(amount_id, "constraint", amount_summary, [policy, str(threshold), approver]),
            _memory(urgent_id, "constraint", urgent_summary, [policy, str(quotes), "紧急采购"]),
            _memory(invoice_id, "constraint", invoice_summary, [policy, str(days), "发票"]),
        ])
        cases.extend([
            _case(
                f"rule-{index:02d}-a", "business_rule_qa",
                f"按{policy}执行一笔{threshold + 800}元的紧急采购，需要谁审批并准备多少家报价？",
                [amount_id, urgent_id], f"需要{approver}审批，并至少提供{quotes}家供应商报价。",
            ),
            _case(
                f"rule-{index:02d}-b", "business_rule_qa",
                f"按{policy}完成采购后第{days + 2}天才提交发票，是否合规？",
                [invoice_id], f"不合规，已超过{days}个自然日，需要补充逾期说明。",
            ),
            _case(
                f"rule-{index:02d}-c", "business_rule_qa",
                f"按{policy}进行{threshold - 200}元紧急采购，应由谁审批，还要满足什么报价要求？",
                [amount_id, urgent_id], f"应由组长审批，并至少提供{quotes}家供应商报价。",
            ),
        ])

    dataset = {
        "metadata": {
            "name": "nodeclaw-memory-v3-eval-200",
            "version": "1.0.0",
            "language": "zh-CN",
            "bucket_counts": BUCKETS,
            "metric_contract": {
                "retrieval": ["recall_at_5", "mrr", "retrieval_latency_p95_ms"],
                "generation": ["faithfulness", "response_latency_p95_ms"],
            },
        },
        "memories": memories,
        "cases": cases,
    }
    validate_dataset(dataset)
    return dataset


def validate_dataset(dataset: dict[str, Any]) -> None:
    memories = dataset.get("memories", [])
    cases = dataset.get("cases", [])
    labels = [row["label"] for row in memories]
    case_ids = [row["case_id"] for row in cases]
    if len(memories) != 200 or len(labels) != len(set(labels)):
        raise ValueError("dataset must contain 200 uniquely labelled memories")
    if len(cases) != 200 or len(case_ids) != len(set(case_ids)):
        raise ValueError("dataset must contain 200 uniquely labelled cases")
    counts = {bucket: sum(row["bucket"] == bucket for row in cases) for bucket in BUCKETS}
    if counts != BUCKETS:
        raise ValueError(f"invalid bucket counts: {counts}")
    known = set(labels)
    for row in cases:
        expected = row.get("expected_memory_ids", [])
        if not expected or not set(expected).issubset(known):
            raise ValueError(f"invalid expected memories in {row['case_id']}")
        if row["bucket"] == "multi_hop_qa" and len(expected) < 2:
            raise ValueError(f"multi-hop case {row['case_id']} needs at least two memories")


def write_dataset(dataset: dict[str, Any], path: Path = DATASET_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dataset, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_dataset(path: Path = DATASET_PATH) -> dict[str, Any]:
    dataset = json.loads(path.read_text(encoding="utf-8"))
    validate_dataset(dataset)
    return dataset


class OfflineRetriever:
    def __init__(self, memories: list[dict[str, Any]]):
        self.memories = memories
        encoder = ChineseBM25Encoder()
        self.encoder = encoder
        self.tokens = {
            row["label"]: set(encoder.tokenize(row["summary"] + " " + " ".join(row["keywords"])))
            for row in memories
        }
        self.by_label = {row["label"]: row for row in memories}

    def __call__(self, question: str, top_k: int) -> list[dict[str, Any]]:
        query_tokens = set(self.encoder.tokenize(question))
        scores: list[tuple[float, str]] = []
        for label, tokens in self.tokens.items():
            overlap = len(query_tokens & tokens)
            if overlap:
                scores.append((overlap / math.sqrt(max(len(tokens), 1)), label))
        labels = [label for _, label in sorted(scores, reverse=True)[:top_k]]
        return [self.by_label[label] for label in labels]


class LiveRetriever:
    def __init__(self, user_id: str, memories: list[dict[str, Any]]):
        self.user_id = user_id
        self.labels_by_id = {benchmark_memory_id(row["label"]): row["label"] for row in memories}

    def __call__(self, question: str, top_k: int) -> list[dict[str, Any]]:
        response = with_retry(
            lambda: search_memories(user_id=self.user_id, query=question, mode="hybrid", top_k=top_k),
            attempts=3,
        )
        return [{
            "label": self.labels_by_id.get(hit.memory_id, hit.memory_id),
            "summary": hit.summary,
            "facts": hit.facts,
            "keywords": hit.keywords,
            "type": hit.type,
        } for hit in response.hits]


class LLMAnswerGenerator:
    def __init__(self, provider: str, model: str):
        self.llm = get_provider(provider, model, temperature=0.0)

    def __call__(self, question: str, context: str) -> str:
        response = self.llm.invoke([
            SystemMessage(content=(
                "你是严格的检索增强问答助手。只能依据给定记忆回答；不得使用外部知识或猜测。"
                "证据不足时明确回答‘根据现有记忆无法确定’。答案应简洁。"
            )),
            HumanMessage(content=json.dumps({"question": question, "memory_context": context}, ensure_ascii=False)),
        ])
        return str(response.content).strip()


def _parse_json_object(text: str) -> dict[str, Any]:
    clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE)
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", clean, re.DOTALL)
        if not match:
            raise ValueError("faithfulness judge did not return JSON")
        return json.loads(match.group(0))


class LLMFaithfulnessJudge:
    def __init__(self, provider: str, model: str):
        self.llm = get_provider(provider, model, temperature=0.0)

    def __call__(self, question: str, answer: str, context: str) -> dict[str, Any]:
        response = self.llm.invoke([
            SystemMessage(content=(
                "你是Faithfulness评测器。将回答拆成可独立验证的原子陈述，逐条判断是否能由给定上下文直接支持。"
                "只判断忠实度，不判断答案是否完整。不要使用外部知识。"
                "只返回JSON：{\"claims\":[{\"claim\":\"...\",\"supported\":true或false}]}。"
            )),
            HumanMessage(content=json.dumps({
                "question": question,
                "answer": answer,
                "memory_context": context,
            }, ensure_ascii=False)),
        ])
        parsed = _parse_json_object(str(response.content))
        claims = [row for row in parsed.get("claims", []) if isinstance(row, dict) and row.get("claim")]
        if not claims:
            score = 1.0 if "无法确定" in answer or "信息不足" in answer else 0.0
        else:
            score = sum(bool(row.get("supported")) for row in claims) / len(claims)
        return {"score": score, "claims": claims}


def percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(quantile * len(ordered)) - 1)
    return ordered[index]


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _rounded(value: float | None) -> float | None:
    return round(value, 4) if value is not None else None


def _summarize(records: list[dict[str, Any]], generation_enabled: bool) -> dict[str, Any]:
    faithfulness = [row["faithfulness"] for row in records if row["faithfulness"] is not None]
    return {
        "cases": len(records),
        "recall_at_5": _rounded(_mean([row["recall_at_5"] for row in records])),
        "mrr": _rounded(_mean([row["mrr"] for row in records])),
        "faithfulness": _rounded(_mean(faithfulness)),
        "retrieval_latency_p95_ms": _rounded(percentile([row["retrieval_latency_ms"] for row in records], 0.95)),
        "generation_latency_p95_ms": _rounded(percentile([row["generation_latency_ms"] for row in records if row["generation_latency_ms"] is not None], 0.95)),
        "response_latency_p95_ms": _rounded(percentile([row["response_latency_ms"] for row in records if row["response_latency_ms"] is not None], 0.95)) if generation_enabled else None,
        "judge_latency_p95_ms": _rounded(percentile([row["judge_latency_ms"] for row in records if row["judge_latency_ms"] is not None], 0.95)),
    }


def evaluate_dataset(
    dataset: dict[str, Any],
    retriever: Callable[[str, int], list[dict[str, Any]]],
    *,
    generator: Callable[[str, str], str] | None = None,
    judge: Callable[[str, str, str], dict[str, Any]] | None = None,
    top_k: int = 5,
    max_cases: int | None = None,
) -> dict[str, Any]:
    if (generator is None) != (judge is None):
        raise ValueError("generator and faithfulness judge must be enabled together")
    cases = dataset["cases"][:max_cases] if max_cases else dataset["cases"]
    records: list[dict[str, Any]] = []
    for case in cases:
        retrieval_started = time.perf_counter()
        hits = retriever(case["question"], top_k)
        retrieval_latency = (time.perf_counter() - retrieval_started) * 1000
        labels = [row["label"] for row in hits]
        expected = set(case["expected_memory_ids"])
        matches = [rank for rank, label in enumerate(labels, start=1) if label in expected]
        recall = len(expected.intersection(labels[:top_k])) / len(expected)
        reciprocal_rank = 1 / matches[0] if matches else 0.0
        context = "\n".join(f"[{row['label']}] {row['summary']}" for row in hits)
        answer = None
        faithfulness = None
        generation_latency = None
        response_latency = None
        judge_latency = None
        unsupported_claims: list[str] = []
        if generator and judge:
            generation_started = time.perf_counter()
            answer = generator(case["question"], context)
            generation_latency = (time.perf_counter() - generation_started) * 1000
            response_latency = retrieval_latency + generation_latency
            judge_started = time.perf_counter()
            judged = judge(case["question"], answer, context)
            judge_latency = (time.perf_counter() - judge_started) * 1000
            faithfulness = float(judged["score"])
            unsupported_claims = [
                str(row["claim"]) for row in judged.get("claims", []) if not row.get("supported")
            ]
        records.append({
            "case_id": case["case_id"],
            "bucket": case["bucket"],
            "recall_at_5": recall,
            "mrr": reciprocal_rank,
            "retrieved": labels,
            "expected": case["expected_memory_ids"],
            "answer": answer,
            "faithfulness": faithfulness,
            "unsupported_claims": unsupported_claims,
            "retrieval_latency_ms": retrieval_latency,
            "generation_latency_ms": generation_latency,
            "response_latency_ms": response_latency,
            "judge_latency_ms": judge_latency,
        })
    generation_enabled = generator is not None
    return {
        "dataset": {
            "name": dataset["metadata"]["name"],
            "version": dataset["metadata"]["version"],
            "memories": len(dataset["memories"]),
            "cases": len(cases),
            "bucket_counts": {bucket: sum(row["bucket"] == bucket for row in cases) for bucket in BUCKETS},
        },
        "overall": _summarize(records, generation_enabled),
        "buckets": {
            bucket: _summarize([row for row in records if row["bucket"] == bucket], generation_enabled)
            for bucket in BUCKETS
        },
        "worst_cases": sorted(
            records,
            key=lambda row: (row["recall_at_5"], row["mrr"], row["faithfulness"] if row["faithfulness"] is not None else 1.0),
        )[:20],
    }


def benchmark_memory_id(label: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"nodeclaw-eval-200:{label}"))


def with_retry(operation: Callable[[], Any], *, attempts: int = 5, base_delay: float = 1.0) -> Any:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            return operation()
        except Exception as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(base_delay * (2 ** attempt))
    assert last_error is not None
    raise last_error


def seed_live_dataset(
    user_id: str,
    memories: list[dict[str, Any]],
    *,
    batch_size: int = 10,
    retry_attempts: int = 5,
) -> int:
    db = get_database()
    if not db.users.find_one({"user_id": user_id}):
        raise ValueError("benchmark user does not exist")
    now = datetime.now(timezone.utc)
    documents: list[dict[str, Any]] = []
    for row in memories:
        memory_id = benchmark_memory_id(row["label"])
        document = {
            "memory_id": memory_id,
            "user_id": user_id,
            "type": row["type"],
            "summary": row["summary"],
            "facts": row["facts"],
            "keywords": row["keywords"],
            "importance": 0.7,
            "confidence": 1.0,
            "valid_from": None,
            "valid_until": None,
            "evidence_summary": "fixed 200-case benchmark",
            "retrieval_text": row["summary"] + "\n" + " ".join(row["keywords"]),
            "status": "active",
            "version": 1,
            "source_session_ids": ["benchmark-eval-200"],
            "source_exchange_ids": [f"benchmark-{row['label']}"],
            "evidence_hashes": [row["label"]],
            "source_refs": [{
                "session_id": "benchmark-eval-200",
                "exchange_id": f"benchmark-{row['label']}",
                "evidence_hash": row["label"],
            }],
            "created_at": now,
            "updated_at": now,
            "indexed_version": 0,
            "index_status": "pending",
            "benchmark_tag": BENCHMARK_TAG,
        }
        db.memories.replace_one({"user_id": user_id, "memory_id": memory_id}, document, upsert=True)
        documents.append(document)

    config = get_memory_config()
    embedding_model = get_embedding_model(model_name=config.embedding_model)
    encoder = get_sparse_encoder()
    client = get_qdrant_client()
    for start in range(0, len(documents), batch_size):
        batch = documents[start:start + batch_size]
        texts = [row["retrieval_text"] for row in batch]
        dense_vectors = with_retry(
            lambda texts=texts: embedding_model.embed_documents(texts),
            attempts=retry_attempts,
        )
        if len(dense_vectors) != len(batch):
            raise RuntimeError("embedding batch size mismatch")
        ensure_collection(len(dense_vectors[0]))
        points = []
        for document, dense in zip(batch, dense_vectors, strict=True):
            points.append(models.PointStruct(
                id=document["memory_id"],
                vector={"dense": dense, "sparse": encoder.encode_document(document["retrieval_text"])},
                payload={
                    "memory_id": document["memory_id"],
                    "user_id": document["user_id"],
                    "type": document["type"],
                    "status": document["status"],
                    "version": document["version"],
                    "importance": document["importance"],
                    "confidence": document["confidence"],
                },
            ))
        with_retry(
            lambda points=points: client.upsert(
                collection_name=config.qdrant_collection,
                points=points,
                wait=True,
            ),
            attempts=retry_attempts,
        )
        db.memories.update_many(
            {"user_id": user_id, "memory_id": {"$in": [row["memory_id"] for row in batch]}},
            {"$set": {"indexed_version": 1, "index_status": "ready"}},
        )
    return len(memories)


def cleanup_live_dataset(user_id: str) -> int:
    db = get_database()
    rows = list(db.memories.find({"user_id": user_id, "benchmark_tag": BENCHMARK_TAG}, {"memory_id": 1}))
    for row in rows:
        delete_memory_index(row["memory_id"])
    db.memories.delete_many({"user_id": user_id, "benchmark_tag": BENCHMARK_TAG})
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DATASET_PATH)
    parser.add_argument("--refresh-dataset", action="store_true")
    parser.add_argument("--mode", choices=["offline", "live"], default="offline")
    parser.add_argument("--user-id")
    parser.add_argument("--seed", action="store_true")
    parser.add_argument("--cleanup-after", action="store_true")
    parser.add_argument("--generation", choices=["none", "llm"], default="none")
    parser.add_argument("--provider")
    parser.add_argument("--model")
    parser.add_argument("--judge-provider")
    parser.add_argument("--judge-model")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--max-cases", type=int)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    if args.refresh_dataset or not args.dataset.exists():
        write_dataset(build_eval_dataset(), args.dataset)
    dataset = load_dataset(args.dataset)
    config = get_memory_config()
    try:
        if args.mode == "live":
            if not args.user_id:
                parser.error("--user-id is required in live mode")
            if args.seed:
                seed_live_dataset(args.user_id, dataset["memories"])
            retriever: Callable[[str, int], list[dict[str, Any]]] = LiveRetriever(args.user_id, dataset["memories"])
        else:
            retriever = OfflineRetriever(dataset["memories"])

        generator = judge = None
        if args.generation == "llm":
            provider = args.provider or config.memory_provider
            model = args.model or config.memory_model
            judge_provider = args.judge_provider or provider
            judge_model = args.judge_model or model
            generator = LLMAnswerGenerator(provider, model)
            judge = LLMFaithfulnessJudge(judge_provider, judge_model)

        report = {
            "mode": args.mode,
            "generation": args.generation,
            "top_k": args.top_k,
            **evaluate_dataset(
                dataset,
                retriever,
                generator=generator,
                judge=judge,
                top_k=args.top_k,
                max_cases=args.max_cases,
            ),
        }
    finally:
        if args.mode == "live" and args.cleanup_after:
            cleanup_live_dataset(args.user_id)

    encoded = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)


if __name__ == "__main__":
    main()

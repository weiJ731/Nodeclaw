"""Deterministic Memory V3 retrieval benchmark.

The generated fixture is fixed by source code: 100 exchanges, 50 labelled
memories, and 80 queries. Offline mode validates the dataset and lexical
baseline without network access. Live mode evaluates a previously seeded
Memory V3 user through the real hybrid Qdrant retriever.
"""

from __future__ import annotations

import argparse
import json
import math
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from memory_module_v3.retrieval import ChineseBM25Encoder, delete_memory_index, index_memory, search_memories
from memory_module_v3.storage import get_database


GROUPS = [
    ("preference", "用户偏好使用{value}", "用户偏好什么？", ["中文回答", "简洁表达", "分点说明", "先给结论", "提供代码示例"]),
    ("preference", "用户喜欢喝{value}", "用户喜欢喝什么？", ["冰美式", "无糖绿茶", "温水", "拿铁", "柠檬水"]),
    ("project_context", "用户正在开发{value}", "用户正在开发什么项目？", ["Nodeclaw 智能体", "销售分析系统", "图像分割模型", "课程管理平台", "知识库问答系统"]),
    ("decision", "项目决定采用{value}", "项目决定采用什么技术？", ["MongoDB", "Qdrant", "Redis", "FastAPI", "Celery"]),
    ("constraint", "项目必须满足{value}", "项目有什么硬性约束？", ["用户数据隔离", "中文输出", "接口鉴权", "异步处理", "可追溯版本"]),
    ("long_term_goal", "用户的长期目标是{value}", "用户的长期目标是什么？", ["成为 AI 应用工程师", "完成论文投稿", "获得暑期实习", "提升系统设计能力", "完成开源项目"]),
    ("relationship", "用户提到{value}是重要协作者", "谁是用户的重要协作者？", ["导师陈老师", "同学小王", "产品经理李姐", "开发同事小周", "客户负责人张工"]),
    ("personal_fact", "用户通常在{value}", "用户通常在什么时候工作？", ["早上九点上课", "晚上十点复盘", "周一开组会", "周三跑步", "周末整理笔记"]),
    ("personal_fact", "用户目前常驻{value}", "用户目前常驻哪里？", ["苏州", "盐城", "南京", "上海", "杭州"]),
    ("decision", "用户已经决定{value}", "用户已经做了什么决定？", ["保留原始 exchange 三十天", "删除旧用户画像", "会话短期记忆隔离", "长期记忆跨会话共享", "只启动三个基础服务"]),
]


def build_dataset() -> dict[str, list[dict[str, Any]]]:
    memories: list[dict[str, Any]] = []
    exchanges: list[dict[str, Any]] = []
    queries: list[dict[str, Any]] = []
    index = 0
    for memory_type, template, question, values in GROUPS:
        for value in values:
            index += 1
            label = f"memory-{index:02d}"
            summary = template.format(value=value)
            memories.append({"label": label, "type": memory_type, "summary": summary, "keywords": [value]})
            exchanges.extend([
                {"exchange_id": f"exchange-{index:02d}-a", "user": summary, "assistant": "好的，我已了解。", "labels": [label]},
                {"exchange_id": f"exchange-{index:02d}-b", "user": f"请记住：{summary}", "assistant": "明白。", "labels": [label]},
            ])
            queries.append({"query_id": f"query-{index:02d}", "text": f"{question} {value}", "expected": [label]})
    for offset, memory in enumerate(memories[:20], start=51):
        queries.append({
            "query_id": f"query-{offset:02d}",
            "text": f"请回忆与“{memory['keywords'][0]}”相关的长期信息",
            "expected": [memory["label"]],
        })
    negatives = ["今天的天气", "随机笑话", "股票价格", "菜谱推荐", "电影票房", "航班状态", "数学证明", "诗歌赏析", "新闻摘要", "设备温度"]
    for offset, text in enumerate(negatives, start=71):
        queries.append({"query_id": f"query-{offset:02d}", "text": text, "expected": []})
    return {"exchanges": exchanges, "memories": memories, "queries": queries}


def benchmark_memory_id(label: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"nodeclaw-benchmark:{label}"))


def seed_live_dataset(user_id: str, memories: list[dict[str, Any]]) -> int:
    db = get_database()
    if not db.users.find_one({"user_id": user_id}):
        raise ValueError("benchmark user does not exist")
    seeded = 0
    now = datetime.now(timezone.utc)
    for row in memories:
        memory_id = benchmark_memory_id(row["label"])
        document = {
            "memory_id": memory_id,
            "user_id": user_id,
            "type": row["type"],
            "summary": row["summary"],
            "facts": [row["summary"]],
            "keywords": row["keywords"],
            "importance": 0.7,
            "confidence": 1.0,
            "valid_from": None,
            "valid_until": None,
            "evidence_summary": "fixed synthetic benchmark",
            "retrieval_text": row["summary"] + "\n" + " ".join(row["keywords"]),
            "status": "active",
            "version": 1,
            "source_session_ids": ["benchmark-session"],
            "source_exchange_ids": [f"benchmark-{row['label']}"],
            "evidence_hashes": [row["label"]],
            "source_refs": [{
                "session_id": "benchmark-session",
                "exchange_id": f"benchmark-{row['label']}",
                "evidence_hash": row["label"],
            }],
            "created_at": now,
            "updated_at": now,
            "indexed_version": 0,
            "index_status": "pending",
            "benchmark_tag": "memory_v3_fixed_v1",
        }
        db.memories.replace_one({"user_id": user_id, "memory_id": memory_id}, document, upsert=True)
        index_memory(document)
        db.memories.update_one(
            {"user_id": user_id, "memory_id": memory_id},
            {"$set": {"indexed_version": 1, "index_status": "ready"}},
        )
        seeded += 1
    return seeded


def cleanup_live_dataset(user_id: str) -> int:
    db = get_database()
    rows = list(db.memories.find({"user_id": user_id, "benchmark_tag": "memory_v3_fixed_v1"}, {"memory_id": 1}))
    for row in rows:
        delete_memory_index(row["memory_id"])
    db.memories.delete_many({"user_id": user_id, "benchmark_tag": "memory_v3_fixed_v1"})
    return len(rows)


def offline_ranker(memories: list[dict[str, Any]]) -> Callable[[str], list[str]]:
    encoder = ChineseBM25Encoder()
    token_sets = {row["label"]: set(encoder.tokenize(row["summary"] + " " + " ".join(row["keywords"]))) for row in memories}

    def rank(query: str) -> list[str]:
        query_tokens = set(encoder.tokenize(query))
        scores = []
        for label, tokens in token_sets.items():
            overlap = len(query_tokens & tokens)
            if overlap:
                scores.append((overlap / math.sqrt(max(len(tokens), 1)), label))
        return [label for _, label in sorted(scores, reverse=True)[:5]]

    return rank


def evaluate(dataset: dict[str, list[dict[str, Any]]], rank: Callable[[str], list[str]]) -> dict[str, Any]:
    recall = reciprocal = ndcg = false_positives = positives = negatives = 0.0
    latencies = []
    for query in dataset["queries"]:
        started = time.perf_counter()
        ranked = rank(query["text"])
        latencies.append((time.perf_counter() - started) * 1000)
        expected = set(query["expected"])
        if not expected:
            negatives += 1
            false_positives += float(bool(ranked))
            continue
        positives += 1
        matches = [position for position, label in enumerate(ranked[:5], start=1) if label in expected]
        if matches:
            recall += 1
            reciprocal += 1 / matches[0]
            ndcg += 1 / math.log2(matches[0] + 1)
    return {
        "dataset": {key: len(value) for key, value in dataset.items()},
        "metrics": {
            "recall_at_5": round(recall / max(positives, 1), 4),
            "mrr": round(reciprocal / max(positives, 1), 4),
            "ndcg_at_5": round(ndcg / max(positives, 1), 4),
            "negative_false_positive_rate": round(false_positives / max(negatives, 1), 4),
            "average_latency_ms": round(sum(latencies) / max(len(latencies), 1), 3),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["offline", "live"], default="offline")
    parser.add_argument("--user-id", help="User containing benchmark-labelled memory IDs")
    parser.add_argument("--seed", action="store_true", help="Seed the fixed dataset through the real embedding/index path")
    parser.add_argument("--cleanup-after", action="store_true", help="Delete seeded benchmark memories after evaluation")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    dataset = build_dataset()
    assert len(dataset["exchanges"]) >= 100
    assert len(dataset["memories"]) >= 50
    assert len(dataset["queries"]) >= 80
    seeded = False
    try:
        if args.mode == "offline":
            rank = offline_ranker(dataset["memories"])
        else:
            if not args.user_id:
                parser.error("--user-id is required in live mode")
            if args.seed:
                seed_live_dataset(args.user_id, dataset["memories"])
                seeded = True
            labels_by_id = {benchmark_memory_id(row["label"]): row["label"] for row in dataset["memories"]}
            rank = lambda query: [
                labels_by_id.get(hit.memory_id, hit.memory_id)
                for hit in search_memories(user_id=args.user_id, query=query, mode="hybrid", top_k=5).hits
            ]
        report = {"mode": args.mode, **evaluate(dataset, rank)}
    finally:
        if args.mode == "live" and args.cleanup_after and (seeded or not args.seed):
            cleanup_live_dataset(args.user_id)
    encoded = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)


if __name__ == "__main__":
    main()

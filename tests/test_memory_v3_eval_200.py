from benchmarks.memory_v3_eval_200 import (
    BUCKETS,
    OfflineRetriever,
    _parse_json_object,
    build_eval_dataset,
    evaluate_dataset,
    percentile,
)


def test_eval_200_dataset_shape_and_labels():
    dataset = build_eval_dataset()
    assert len(dataset["memories"]) == 200
    assert len(dataset["cases"]) == 200
    assert {
        bucket: sum(case["bucket"] == bucket for case in dataset["cases"])
        for bucket in BUCKETS
    } == BUCKETS
    assert all(len(case["expected_memory_ids"]) >= 2 for case in dataset["cases"] if case["bucket"] == "multi_hop_qa")


def test_eval_200_perfect_pipeline_metrics():
    dataset = build_eval_dataset()
    memories = {row["label"]: row for row in dataset["memories"]}
    cases = {row["question"]: row for row in dataset["cases"]}

    def retrieve(question, top_k):
        return [memories[label] for label in cases[question]["expected_memory_ids"]][:top_k]

    def generate(question, context):
        assert question and context
        return cases[question]["reference_answer"]

    def judge(question, answer, context):
        assert question and answer and context
        return {"score": 1.0, "claims": [{"claim": answer, "supported": True}]}

    report = evaluate_dataset(dataset, retrieve, generator=generate, judge=judge)
    assert report["overall"]["recall_at_5"] == 1.0
    assert report["overall"]["mrr"] == 1.0
    assert report["overall"]["faithfulness"] == 1.0
    assert report["overall"]["response_latency_p95_ms"] is not None


def test_offline_retrieval_baseline_is_deterministic_and_bucketed():
    dataset = build_eval_dataset()
    report = evaluate_dataset(dataset, OfflineRetriever(dataset["memories"]))
    assert report["overall"]["recall_at_5"] >= 0.8
    assert report["overall"]["mrr"] >= 0.75
    assert report["overall"]["faithfulness"] is None
    assert set(report["buckets"]) == set(BUCKETS)


def test_percentile_and_json_fence_parser():
    assert percentile([1, 2, 3, 4, 5], 0.95) == 5
    parsed = _parse_json_object('```json\n{"claims":[{"claim":"x","supported":true}]}\n```')
    assert parsed["claims"][0]["supported"] is True

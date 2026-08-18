from __future__ import annotations

from memory_module_v3.models import CandidateMemory, MemoryType
from memory_module_v3.retrieval import ChineseBM25Encoder, rrf_fusion
from memory_module_v3.service import estimate_tokens, should_extract, should_retrieve


def test_candidate_memory_deduplicates_and_builds_retrieval_text():
    candidate = CandidateMemory(
        type=MemoryType.PREFERENCE,
        summary="用户长期偏好中文回答",
        facts=["偏好中文", "偏好中文", "  回答要简洁  "],
        keywords=["中文", "中文", "简洁"],
    )
    assert candidate.facts == ["偏好中文", "回答要简洁"]
    assert candidate.keywords == ["中文", "简洁"]
    assert "用户长期偏好中文回答" in candidate.retrieval_text


def test_chinese_sparse_encoder_is_deterministic_and_sorted():
    encoder = ChineseBM25Encoder()
    first = encoder.encode_document("我喜欢 Python，也喜欢构建智能体")
    second = encoder.encode_document("我喜欢 Python，也喜欢构建智能体")
    assert first.indices == second.indices
    assert first.values == second.values
    assert first.indices == sorted(first.indices)
    assert len(first.indices) == len(first.values) > 0


def test_rrf_fusion_combines_dense_and_sparse_rankings():
    dense = [{"memory_id": "a", "score": 0.9}, {"memory_id": "b", "score": 0.8}]
    sparse = [{"memory_id": "b", "score": 7.0}, {"memory_id": "c", "score": 6.0}]
    result = rrf_fusion(dense, sparse, k=60, limit=3)
    assert result[0]["memory_id"] == "b"
    assert {row["memory_id"] for row in result} == {"a", "b", "c"}


def test_memory_read_and_write_gates():
    assert not should_extract("好的", "收到")
    assert should_extract("我长期偏好中文回答，而且希望内容简洁", "我记住了")
    assert not should_retrieve("谢谢")
    assert should_retrieve("你还记得我之前决定采用哪个数据库吗？")


def test_token_estimator_handles_chinese_and_english():
    assert estimate_tokens("") == 0
    assert estimate_tokens("这是一个中文句子") > 1
    assert estimate_tokens("this is an english sentence") > 1

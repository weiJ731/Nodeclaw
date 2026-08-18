# Memory V3 评测

## 200 条正式评测集

`memory_v3_eval_200.py` 使用固定中文数据，包含 200 条候选记忆与 200 个评测问题：

- `fact_qa`：80 条事实问答，单条证据。
- `multi_hop_qa`：60 条多跳问答，每题需要两条证据。
- `business_rule_qa`：60 条业务规则问答，覆盖金额阈值、紧急采购例外与发票时限。

检索层输出 `Recall@5`、`MRR` 和检索延迟 `P95`；启用真实模型后，生成层输出基于原子陈述支持率的 `Faithfulness`、生成延迟 `P95` 和用户可感知响应时间 `P95`。评测器自身的 Judge 延迟单独统计，不计入响应时间。

生成数据文件并运行无网络词法基线：

```bash
python -m benchmarks.memory_v3_eval_200 \
  --refresh-dataset \
  --mode offline \
  --output benchmarks/reports/memory_v3_eval_200_offline.json
```

先用少量样本验证真实生成与 Faithfulness Judge：

```bash
python -m benchmarks.memory_v3_eval_200 \
  --mode offline \
  --generation llm \
  --max-cases 10 \
  --output benchmarks/reports/memory_v3_eval_200_generation_smoke.json
```

运行真实 Embedding、Qdrant 混合检索和生成层全量评测：

```bash
python -m benchmarks.memory_v3_eval_200 \
  --mode live \
  --user-id YOUR_USER_ID \
  --seed \
  --generation llm \
  --cleanup-after \
  --output benchmarks/reports/memory_v3_eval_200_live.json
```

`--generation llm` 对每个样本执行一次回答生成和一次独立 Faithfulness 判定，全量运行会产生约 400 次模型请求。建议先通过 `--max-cases` 做费用和限流检查。

## 2026-08-17 在线基线

顺序执行 200 条真实 Embedding、Qdrant 混合检索、答案生成和独立 Judge 后，结果如下。Judge 延迟不计入响应时间。

| 分桶 | Recall@5 | MRR | Faithfulness | 检索 P95 | 响应 P95 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 总体 | 0.9500 | 0.9677 | 0.9875 | 333 ms | 2768 ms |
| 事实问答 | 1.0000 | 1.0000 | 1.0000 | 315 ms | 1123 ms |
| 多跳问答 | 1.0000 | 0.9750 | 1.0000 | 310 ms | 1226 ms |
| 业务规则问答 | 0.8333 | 0.9172 | 0.9583 | 361 ms | 3533 ms |

业务规则桶的主要失败模式是：同一问题需要同时召回金额审批规则和紧急采购规则时，金额规则容易被其他政策的高相似紧急条款挤出 Top 5。完整结果位于 `benchmarks/reports/memory_v3_eval_200_live.json`。

## 旧版快速回归

`memory_v3_benchmark.py` 仍保留为轻量回归集，规模为 100 条 Exchange、50 条记忆和 80 条查询。

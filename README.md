# Nodeclaw

Nodeclaw 是一个基于 FastAPI、LangGraph 和工具调用构建的多用户 AI Agent。当前 V3 版本采用双轨记忆：会话内短期摘要与用户级长期结构化记忆彼此独立，并通过 MongoDB、Qdrant、Redis/Celery 完成持久化、混合检索和异步处理。

## 核心能力

- 注册、登录、JWT Access/Refresh Token 轮换与用户数据隔离
- 多会话聊天，短期上下文按会话隔离，长期记忆按用户共享
- Memory V3 多候选提取、NEW/UPDATE/MERGE/DISCARD 生命周期和版本溯源
- Qdrant Dense + 中文 Sparse/BM25 检索，应用层 RRF 融合
- 原始 exchange 仅保留 30 天且不参与长期检索
- Session Summary 达到 6 轮或 6000 Token 后异步增量压缩
- MongoDB Outbox 保证事实库与向量索引最终一致
- Celery 按用户串行化记忆写入，不同用户可并行处理
- Mongo 定时任务、Redis 实时提醒与离线通知
- 内置工具、动态 Skill、可选 MCP 工具接入
- Web 聊天、会话列表、任务面板、Memory/Health Console

## 架构

```text
Browser
  |  Access Token + Refresh Cookie / SSE
FastAPI + LangGraph Agent
  |-- MongoDB: users, sessions, checkpoints, memories, versions, tasks, audit
  |-- Redis: Celery broker, user locks, live notification pub/sub
  |-- Qdrant: derived dense+sparse memory index
  `-- Celery Worker + Beat: extraction, summary, indexing, reminders
```

MongoDB 是唯一事实源；Qdrant 可从 MongoDB 重建。`raw_exchanges` 只用于 30 天内排错，长期记忆仅保存最小来源 ID 和证据哈希。

## 环境要求

- macOS/Linux
- Python 3.12，推荐使用独立 Conda 环境
- Docker Desktop
- 已配置可用的对话模型与 Embedding API

## 首次安装

```bash
git clone https://github.com/weiJ731/Nodeclaw.git
cd Nodeclaw
conda create -n nodeclaw python=3.12 -y
conda activate nodeclaw
pip install -r requirements.txt
cp .env.example .env
```

`.env` 包含本地密钥且已加入忽略规则，公开仓库只提交 `.env.example`。

至少检查 `.env` 中这些配置：

```dotenv
DEFAULT_PROVIDER=aliyun
DEFAULT_MODEL=glm-5
MEMORY_PROVIDER=aliyun
MEMORY_MODEL=qwen-plus
EMBEDDING_MODEL=text-embedding-v2
OPENAI_API_KEY=your-key
JWT_SECRET=replace-with-a-long-random-secret
```

本地端口：MongoDB `27018`、Qdrant HTTP/gRPC `6335/6336`、Redis `6380`、Web `8000`。

## 启动

打开四个终端，均先执行：

```bash
cd Nodeclaw
conda activate nodeclaw
```

终端 1，启动三个基础服务：

```bash
docker compose up -d
docker compose ps
```

终端 2，启动 Celery Worker：

```bash
celery -A memory_module_v3.tasks.celery_app worker \
  -Q memory,summary,index,scheduler --loglevel=info
```

终端 3，启动 Celery Beat：

```bash
celery -A memory_module_v3.tasks.celery_app beat --loglevel=info
```

终端 4，启动 Web/API：

```bash
uvicorn entry.server:app --host 127.0.0.1 --port 8000 --reload
```

访问：

- Web：[http://127.0.0.1:8000](http://127.0.0.1:8000)
- OpenAPI：[http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)

CLI 可用：

```bash
python -m entry.main
```

## Memory V3 流程

1. Agent 完成回答后，将 exchange 写入 MongoDB；原文设置 30 天 TTL。
2. API 只负责投递 Celery 任务，不同步调用记忆 LLM。
3. Write Gate 过滤寒暄、短句和不稳定信息。
4. Memory LLM 一次可提取多条原子候选，限制为事实、偏好、目标、项目背景、关系、决定和约束。
5. 对候选召回相关旧记忆并判定 NEW、UPDATE、MERGE 或 DISCARD。
6. Mongo 事务同时写当前记忆、版本快照和 outbox。
7. Index Worker 消费 outbox，写入 Qdrant dense+sparse 命名向量。
8. 新问题通过 Read Gate 后执行 Dense、中文 Sparse 与 RRF 融合，最多注入 5 条或约 1200 Token。

同一用户的记忆任务通过 Redis 锁串行执行，避免跨会话并发覆盖；不同用户由 Celery 并行处理。索引失败会重试，耗尽后进入 Mongo `dead_letters`，Mongo 中的长期记忆不丢失。

## 会话与删除语义

- 删除会话：删除短期 checkpoint/raw exchange，不自动删除共享长期记忆。
- 忘记该会话记忆：显式解除或删除来源仅属于该会话的长期记忆。
- 删除账号：删除账号、Token、会话、checkpoint、长期记忆、任务、通知和 Qdrant 索引。

## 测试与评测

```bash
pytest -q
python -m benchmarks.memory_v3_eval_200 --mode offline
```

固定评测集包含 200 条记忆与 200 条查询，按事实问答、多跳问答和业务规则问答分桶。完整在线评测结果如下：

| 指标 | 结果 |
| --- | ---: |
| Recall@5 | 0.9500 |
| MRR | 0.9677 |
| Faithfulness | 0.9875 |
| 检索延迟 P95 | 333 ms |
| 端到端响应 P95 | 2.77 s |

当前测试结果为 `47 passed`，另含 5 个子测试。评测集设计、运行参数和分桶结果详见 [`benchmarks/README.md`](benchmarks/README.md)。

## 常用维护

```bash
# 查看基础服务
docker compose ps

# 查看日志
docker compose logs --tail=100 nodeclaw-mongodb nodeclaw-qdrant nodeclaw-redis

# 停止服务，保留数据
docker compose down

# 运行测试
pytest -q
```

生产部署前必须替换 `.env` 中的数据库密码、`MONGO_REPLICA_KEY` 和 `JWT_SECRET`，开启 HTTPS 并设置 `COOKIE_SECURE=true`。

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Optional

from dotenv import load_dotenv

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from langchain_core.messages import BaseMessage, HumanMessage
from langgraph.checkpoint.mongodb import MongoDBSaver
from pydantic import BaseModel, Field
from pymongo.errors import PyMongoError
from redis import Redis
from redis.asyncio import Redis as AsyncRedis

from memory_module_v3.config import get_memory_config
from memory_module_v3.retrieval import get_qdrant_client, search_memories
from memory_module_v3.service import ingest_exchange
from memory_module_v3.storage import count_active_memories, get_database, healthcheck, init_database
from memory_module_v3.tasks import enqueue_exchange, reindex_user_task
from nodeclaw.core.agent import create_agent_app
from nodeclaw.core.auth import (
    AuthUser,
    authenticate,
    create_user,
    delete_user_data,
    issue_token_pair,
    require_user,
    revoke_refresh_token,
    rotate_refresh_token,
)
from nodeclaw.core.mcp_bridge import get_mcp_status
from nodeclaw.core.notifications import list_notifications, mark_notification_read
from nodeclaw.core.session_store import (
    create_session,
    delete_session,
    forget_session_memories,
    list_sessions,
    rename_session,
    require_session,
)
from nodeclaw.core.skill_loader import load_dynamic_skills
from nodeclaw.core.task_store import create_task, delete_task, list_tasks, update_task
from nodeclaw.core.tools.builtins import BUILTIN_TOOLS

FRONTEND_DIR = os.path.join(PROJECT_ROOT, "frontend")
REFRESH_COOKIE = "nodeclaw_refresh"


def _cookie_settings() -> dict[str, Any]:
    return {
        "httponly": True,
        "secure": os.getenv("COOKIE_SECURE", "false").lower() == "true",
        "samesite": "lax",
        "path": "/api/auth",
        "max_age": int(os.getenv("JWT_REFRESH_DAYS", "30")) * 86400,
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_database()
    config = get_memory_config()
    with MongoDBSaver.from_conn_string(config.mongodb_uri, config.mongodb_database) as checkpointer:
        app.state.checkpointer = checkpointer
        app.state.agent = create_agent_app(
            provider_name=os.getenv("DEFAULT_PROVIDER", "aliyun"),
            model_name=os.getenv("DEFAULT_MODEL", "glm-5"),
            checkpointer=checkpointer,
        )
        yield


app = FastAPI(
    title="Nodeclaw Backend API",
    version="3.0.0",
    description="Authenticated AI agent with lifecycle-managed dual-track memory.",
    lifespan=lifespan,
)

origins = [item.strip() for item in os.getenv(
    "NODECLAW_CORS_ORIGINS", "http://127.0.0.1:8000,http://localhost:8000"
).split(",") if item.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)
app.mount("/assets", StaticFiles(directory=FRONTEND_DIR), name="assets")


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=50)
    email: str = Field(min_length=5, max_length=254)
    password: str = Field(min_length=8, max_length=256)


class LoginRequest(BaseModel):
    login: str
    password: str


class SessionCreateRequest(BaseModel):
    title: str = Field(default="新对话", max_length=80)


class SessionRenameRequest(BaseModel):
    title: str = Field(min_length=1, max_length=80)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=50_000)
    session_id: str


class TaskCreateRequest(BaseModel):
    target_time: str
    description: str = Field(min_length=1, max_length=1000)
    session_id: str | None = None
    repeat: str | None = None
    repeat_count: int | None = Field(default=None, ge=1)


class TaskUpdateRequest(BaseModel):
    target_time: str | None = None
    description: str | None = Field(default=None, max_length=1000)
    repeat: str | None = None
    repeat_count: int | None = Field(default=None, ge=1)


class MemorySearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=5000)
    mode: str = "hybrid"
    top_k: int = Field(default=5, ge=1, le=20)
    debug: bool = False


def user_payload(user: AuthUser) -> dict[str, str]:
    return user.model_dump()


def set_refresh_cookie(response: Response, refresh_token: str) -> None:
    response.set_cookie(REFRESH_COOKIE, refresh_token, **_cookie_settings())


def clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(REFRESH_COOKIE, path="/api/auth")


def sse_event(data: str, event: str | None = None) -> str:
    prefix = f"event: {event}\n" if event else ""
    lines = str(data).splitlines() or [""]
    return prefix + "".join(f"data: {line}\n" for line in lines) + "\n"


def serialize_message(message: BaseMessage) -> dict[str, Any]:
    return {
        "id": getattr(message, "id", None),
        "role": "user" if message.type == "human" else "assistant" if message.type == "ai" else message.type,
        "content": message.content if isinstance(message.content, str) else str(message.content),
    }


def serialize_session(document: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in document.items() if key not in {"_id", "recent_exchanges"}}


def summarize_tool(tool: Any, source: str) -> dict[str, str]:
    description = getattr(tool, "description", "") or getattr(tool, "__doc__", "") or ""
    return {
        "name": getattr(tool, "name", getattr(tool, "__name__", "unknown_tool")),
        "source": source,
        "description": " ".join(description.strip().split())[:260],
    }


@app.get("/", include_in_schema=False)
async def web_chat():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))


@app.get("/api/health")
async def health_check():
    return {"status": "ok", "message": "Nodeclaw is running smoothly.", "version": "3.0.0"}


@app.post("/api/auth/register", status_code=201)
async def register(request: RegisterRequest, response: Response):
    try:
        user = await asyncio.to_thread(create_user, request.username, request.email, request.password)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    access, refresh = await asyncio.to_thread(issue_token_pair, user)
    set_refresh_cookie(response, refresh)
    return {"access_token": access, "token_type": "bearer", "user": user_payload(user)}


@app.post("/api/auth/login")
async def login(request: LoginRequest, response: Response):
    user = await asyncio.to_thread(authenticate, request.login, request.password)
    if not user:
        raise HTTPException(status_code=401, detail="用户名、邮箱或密码错误")
    access, refresh = await asyncio.to_thread(issue_token_pair, user)
    set_refresh_cookie(response, refresh)
    return {"access_token": access, "token_type": "bearer", "user": user_payload(user)}


@app.post("/api/auth/refresh")
async def refresh_auth(request: Request, response: Response):
    token = request.cookies.get(REFRESH_COOKIE)
    if not token:
        raise HTTPException(status_code=401, detail="缺少 Refresh Token")
    user, access, refresh = await asyncio.to_thread(rotate_refresh_token, token)
    set_refresh_cookie(response, refresh)
    return {"access_token": access, "token_type": "bearer", "user": user_payload(user)}


@app.post("/api/auth/logout", status_code=204)
async def logout(request: Request, response: Response):
    await asyncio.to_thread(revoke_refresh_token, request.cookies.get(REFRESH_COOKIE))
    clear_refresh_cookie(response)


@app.get("/api/auth/me")
async def current_user(user: AuthUser = Depends(require_user)):
    return {"user": user_payload(user)}


@app.delete("/api/auth/account", status_code=204)
async def delete_account(request: Request, response: Response, user: AuthUser = Depends(require_user)):
    await asyncio.to_thread(delete_user_data, user.user_id)
    clear_refresh_cookie(response)


@app.post("/api/sessions", status_code=201)
async def create_session_endpoint(request: SessionCreateRequest, user: AuthUser = Depends(require_user)):
    document = await asyncio.to_thread(create_session, user.user_id, request.title)
    return {"session": serialize_session(document)}


@app.get("/api/sessions")
async def list_sessions_endpoint(user: AuthUser = Depends(require_user)):
    rows = await asyncio.to_thread(list_sessions, user.user_id)
    return {"sessions": [serialize_session(row) for row in rows]}


@app.patch("/api/sessions/{session_id}")
async def rename_session_endpoint(
    session_id: str, request: SessionRenameRequest, user: AuthUser = Depends(require_user)
):
    try:
        row = await asyncio.to_thread(rename_session, user.user_id, session_id, request.title)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="会话不存在") from exc
    return {"session": serialize_session(row)}


@app.delete("/api/sessions/{session_id}", status_code=204)
async def delete_session_endpoint(session_id: str, user: AuthUser = Depends(require_user)):
    try:
        await asyncio.to_thread(delete_session, user.user_id, session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="会话不存在") from exc


@app.post("/api/sessions/{session_id}/forget-memories")
async def forget_session_memories_endpoint(session_id: str, user: AuthUser = Depends(require_user)):
    try:
        await asyncio.to_thread(require_session, user.user_id, session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="会话不存在") from exc
    return await asyncio.to_thread(forget_session_memories, user.user_id, session_id)


@app.get("/api/sessions/{session_id}/messages")
async def session_messages(session_id: str, user: AuthUser = Depends(require_user)):
    try:
        session = await asyncio.to_thread(require_session, user.user_id, session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="会话不存在") from exc
    config = {"configurable": {"thread_id": session_id}}
    state = await app.state.checkpointer.aget_tuple(config)
    messages = []
    if state:
        messages = state.checkpoint.get("channel_values", {}).get("messages", [])
    return {
        "session": serialize_session(session),
        "messages": [serialize_message(message) for message in messages if message.type in {"human", "ai"} and message.content],
    }


@app.post("/api/chat")
async def chat_endpoint(request: ChatRequest, user: AuthUser = Depends(require_user)):
    try:
        session = await asyncio.to_thread(require_session, user.user_id, request.session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="会话不存在") from exc
    config = {"configurable": {
        "thread_id": request.session_id,
        "session_id": request.session_id,
        "user_id": user.user_id,
    }}
    inputs = {"messages": [HumanMessage(content=request.message)]}

    async def event_generator():
        full_reply = ""
        tool_events: list[dict[str, Any]] = []
        try:
            async for event in app.state.agent.astream(inputs, config=config, stream_mode="updates"):
                for node_name, node_data in event.items():
                    if node_name != "agent":
                        continue
                    last_msg = node_data["messages"][-1]
                    if getattr(last_msg, "tool_calls", None):
                        for call in last_msg.tool_calls:
                            tool_events.append({"name": call["name"]})
                            yield sse_event(f"[TOOL_CALL] 正在调用技能: {call['name']}...")
                    elif last_msg.content:
                        text = str(last_msg.content)
                        full_reply += text
                        yield sse_event(text)
        except Exception as exc:
            yield sse_event(f"[ERROR] 引擎异常: {exc}")
        if full_reply:
            try:
                exchange_id, needs_summary = await asyncio.to_thread(
                    ingest_exchange,
                    user_id=user.user_id,
                    session_id=request.session_id,
                    user_text=request.message,
                    assistant_text=full_reply,
                    tool_events=tool_events,
                )
                try:
                    await asyncio.to_thread(enqueue_exchange, user.user_id, request.session_id, exchange_id, needs_summary)
                except Exception as exc:
                    get_database().sessions.update_one(
                        {"user_id": user.user_id, "session_id": request.session_id},
                        {"$set": {"memory_sync_status": "queue_error", "memory_sync_error": str(exc)[:500]}},
                    )
                if session.get("exchange_count", 0) == 0 and session.get("title") == "新对话":
                    await asyncio.to_thread(rename_session, user.user_id, request.session_id, request.message[:30])
            except Exception as exc:
                get_database().sessions.update_one(
                    {"user_id": user.user_id, "session_id": request.session_id},
                    {"$set": {"memory_sync_status": "error", "memory_sync_error": str(exc)[:500]}},
                )
        yield sse_event("[DONE]")

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.get("/api/tasks")
async def tasks_endpoint(user: AuthUser = Depends(require_user)):
    return {"tasks": await asyncio.to_thread(list_tasks, user.user_id)}


@app.post("/api/tasks", status_code=201)
async def create_task_endpoint(request: TaskCreateRequest, user: AuthUser = Depends(require_user)):
    if request.session_id:
        try:
            await asyncio.to_thread(require_session, user.user_id, request.session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="会话不存在") from exc
    try:
        task = await asyncio.to_thread(
            create_task,
            user_id=user.user_id,
            session_id=request.session_id,
            target_time=request.target_time,
            description=request.description,
            repeat=request.repeat,
            repeat_count=request.repeat_count,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"task": task}


@app.patch("/api/tasks/{task_id}")
async def update_task_endpoint(task_id: str, request: TaskUpdateRequest, user: AuthUser = Depends(require_user)):
    try:
        task = await asyncio.to_thread(update_task, user.user_id, task_id, **request.model_dump(exclude_unset=True))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"task": task}


@app.delete("/api/tasks/{task_id}", status_code=204)
async def delete_task_endpoint(task_id: str, user: AuthUser = Depends(require_user)):
    if not await asyncio.to_thread(delete_task, user.user_id, task_id):
        raise HTTPException(status_code=404, detail="任务不存在")


@app.get("/api/notifications")
async def notifications_endpoint(unread_only: bool = False, user: AuthUser = Depends(require_user)):
    rows = await asyncio.to_thread(list_notifications, user.user_id, unread_only)
    return {"notifications": rows}


@app.patch("/api/notifications/{notification_id}/read", status_code=204)
async def notification_read_endpoint(notification_id: str, user: AuthUser = Depends(require_user)):
    if not await asyncio.to_thread(mark_notification_read, user.user_id, notification_id):
        raise HTTPException(status_code=404, detail="通知不存在")


@app.get("/api/events")
async def event_stream(user: AuthUser = Depends(require_user)):
    async def generator():
        redis = AsyncRedis.from_url(get_memory_config().redis_url, decode_responses=True)
        pubsub = redis.pubsub()
        await pubsub.subscribe(f"nodeclaw:events:{user.user_id}")
        try:
            yield sse_event(json.dumps({"status": "connected"}), "connected")
            while True:
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=15)
                if message and message.get("data"):
                    yield sse_event(str(message["data"]), "notification")
                else:
                    yield ": keep-alive\n\n"
                await asyncio.sleep(0.1)
        finally:
            await pubsub.unsubscribe()
            await pubsub.close()
            await redis.close()

    return StreamingResponse(generator(), media_type="text/event-stream")


@app.get("/api/memory/status")
async def memory_status(user: AuthUser = Depends(require_user)):
    session_rows = await asyncio.to_thread(list_sessions, user.user_id)
    pending = sum(row.get("memory_sync_status") == "pending" for row in session_rows)
    return {
        "backend": "v3",
        "enabled": True,
        "object_count": await asyncio.to_thread(count_active_memories, user.user_id),
        "pending_sessions": pending,
        "retrieval_mode": "hybrid_rrf",
        "last_action": next((row.get("last_memory_action") for row in session_rows if row.get("last_memory_action")), None),
    }


@app.post("/api/memory/search")
async def memory_search(request: MemorySearchRequest, user: AuthUser = Depends(require_user)):
    if request.mode not in {"dense", "sparse", "hybrid"}:
        raise HTTPException(status_code=422, detail="mode 只能是 dense、sparse 或 hybrid")
    result = await asyncio.to_thread(
        search_memories,
        user_id=user.user_id,
        query=request.query,
        mode=request.mode,
        top_k=request.top_k,
        debug=request.debug,
    )
    return result.model_dump(mode="json")


@app.post("/api/memory/reindex")
async def memory_reindex(user: AuthUser = Depends(require_user)):
    await asyncio.to_thread(reindex_user_task.delay, user.user_id)
    return {"status": "queued"}


@app.get("/api/tools")
async def tools_endpoint(user: AuthUser = Depends(require_user)):
    tools = [summarize_tool(tool, "builtin") for tool in BUILTIN_TOOLS]
    tools.extend(summarize_tool(tool, "dynamic_skill") for tool in load_dynamic_skills())
    return {"tools": tools, "count": len(tools)}


@app.get("/api/mcp/status")
async def mcp_status(user: AuthUser = Depends(require_user)):
    return get_mcp_status()


@app.get("/api/health/deep")
async def deep_health(user: AuthUser = Depends(require_user)):
    checks: list[dict[str, Any]] = []
    try:
        mongo = await asyncio.to_thread(healthcheck)
        checks.append({"name": "mongodb", "status": "ok" if mongo["ok"] else "error", "message": "MongoDB 可访问。"})
    except Exception as exc:
        checks.append({"name": "mongodb", "status": "error", "message": f"MongoDB 不可访问: {exc}"})
    try:
        qdrant = await asyncio.to_thread(get_qdrant_client().get_collections)
        checks.append({"name": "qdrant", "status": "ok", "message": f"Qdrant 可访问，共 {len(qdrant.collections)} 个 Collection。"})
    except Exception as exc:
        checks.append({"name": "qdrant", "status": "error", "message": f"Qdrant 不可访问: {exc}"})
    try:
        pong = await asyncio.to_thread(Redis.from_url(get_memory_config().redis_url).ping)
        checks.append({"name": "redis", "status": "ok" if pong else "error", "message": "Redis 可访问。"})
    except Exception as exc:
        checks.append({"name": "redis", "status": "error", "message": f"Redis 不可访问: {exc}"})
    checks.append({"name": "memory_v3", "status": "ok", "message": "Memory V3 已启用。"})
    mcp = get_mcp_status()
    checks.append({"name": "mcp", "status": "ok", "message": "MCP 已启用。" if mcp.get("enabled") else "MCP 未启用。"})
    overall = "error" if any(row["status"] == "error" for row in checks) else "ok"
    return {"status": overall, "checks": checks}

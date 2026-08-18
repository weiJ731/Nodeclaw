from __future__ import annotations

import asyncio
import os
import uuid

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.mongodb import MongoDBSaver
from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout

from memory_module_v3.config import get_memory_config
from memory_module_v3.models import utc_now
from memory_module_v3.service import ingest_exchange
from memory_module_v3.storage import get_database, init_database
from memory_module_v3.tasks import enqueue_exchange
from nodeclaw.core.agent import create_agent_app
from nodeclaw.core.session_store import create_session

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))


def _get_cli_identity() -> tuple[str, str]:
    db = get_database()
    user = db.users.find_one({"username_normalized": "local_cli"})
    if not user:
        user_id = str(uuid.uuid4())
        db.users.insert_one({
            "user_id": user_id,
            "username": "local_cli",
            "username_normalized": "local_cli",
            "email": "local-cli@nodeclaw.local",
            "email_normalized": "local-cli@nodeclaw.local",
            "password_hash": "cli-only-account",
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "disabled_at": None,
        })
    else:
        user_id = user["user_id"]
    session = db.sessions.find_one({"user_id": user_id, "title": "CLI 会话", "deleted_at": None})
    if not session:
        session = create_session(user_id, "CLI 会话")
    return user_id, session["session_id"]


async def async_main() -> None:
    init_database()
    user_id, session_id = _get_cli_identity()
    memory_config = get_memory_config()
    prompt = PromptSession("Nodeclaw > ")
    print("Nodeclaw CLI 已启动。输入 /exit 退出。")

    with MongoDBSaver.from_conn_string(memory_config.mongodb_uri, memory_config.mongodb_database) as checkpointer:
        agent = create_agent_app(
            provider_name=os.getenv("DEFAULT_PROVIDER", "aliyun"),
            model_name=os.getenv("DEFAULT_MODEL", "glm-5"),
            checkpointer=checkpointer,
        )
        config = {"configurable": {
            "thread_id": session_id,
            "session_id": session_id,
            "user_id": user_id,
        }}
        with patch_stdout():
            while True:
                try:
                    user_text = (await prompt.prompt_async()).strip()
                except (EOFError, KeyboardInterrupt):
                    break
                if user_text.lower() in {"/exit", "/quit"}:
                    break
                if not user_text:
                    continue
                assistant_text = ""
                tool_events: list[dict[str, str]] = []
                try:
                    async for event in agent.astream(
                        {"messages": [HumanMessage(content=user_text)]}, config=config, stream_mode="updates"
                    ):
                        for node_name, node_data in event.items():
                            if node_name != "agent":
                                continue
                            message = node_data["messages"][-1]
                            for call in getattr(message, "tool_calls", []) or []:
                                tool_events.append({"name": call["name"]})
                                print(f"[tool] {call['name']}")
                            if message.content:
                                assistant_text += str(message.content)
                    if assistant_text:
                        print(f"Nodeclaw: {assistant_text}\n")
                        exchange_id, needs_summary = await asyncio.to_thread(
                            ingest_exchange,
                            user_id=user_id,
                            session_id=session_id,
                            user_text=user_text,
                            assistant_text=assistant_text,
                            tool_events=tool_events,
                        )
                        await asyncio.to_thread(enqueue_exchange, user_id, session_id, exchange_id, needs_summary)
                except Exception as exc:
                    print(f"Nodeclaw 运行失败: {exc}")


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()

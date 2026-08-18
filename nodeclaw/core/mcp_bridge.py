from __future__ import annotations

import asyncio
import json
import os
import threading
from pathlib import Path
from typing import Any

from .config import WORKSPACE_DIR

_last_error: str | None = None
_last_loaded_tool_names: list[str] = []


def mcp_enabled() -> bool:
    raw = os.getenv("MCP_ENABLED", "false").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def get_mcp_config_path() -> Path:
    configured = os.getenv("MCP_CONFIG_FILE", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(WORKSPACE_DIR) / "mcp_servers.json"


def load_mcp_server_config() -> dict[str, Any]:
    path = get_mcp_config_path()
    if not path.exists():
        return {}

    data = json.loads(path.read_text(encoding="utf-8"))
    servers = data.get("mcpServers", data)
    if not isinstance(servers, dict):
        raise ValueError("MCP config must be an object or contain an object field named mcpServers.")
    return servers


async def _load_mcp_tools_async() -> list[Any]:
    from langchain_mcp_adapters.client import MultiServerMCPClient  # type: ignore[import-not-found]

    servers = load_mcp_server_config()
    if not servers:
        return []

    client = MultiServerMCPClient(servers)
    return await client.get_tools()


def _run_async_in_thread(coro) -> Any:
    result: dict[str, Any] = {}

    def runner() -> None:
        try:
            result["value"] = asyncio.run(coro)
        except Exception as exc:  # pragma: no cover - surfaced to caller
            result["error"] = exc

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join(timeout=30)

    if thread.is_alive():
        raise TimeoutError("Timed out while loading MCP tools.")
    if "error" in result:
        raise result["error"]
    return result.get("value", [])


def load_mcp_tools() -> list[Any]:
    """Load MCP tools as LangChain tools when MCP is enabled.

    The bridge is deliberately optional. A normal Nodeclaw install runs without
    MCP dependencies; install `nodeclaw[mcp]` and set MCP_ENABLED=true to activate it.
    """
    global _last_error, _last_loaded_tool_names

    _last_error = None
    _last_loaded_tool_names = []

    if not mcp_enabled():
        return []

    try:
        try:
            asyncio.get_running_loop()
            tools = _run_async_in_thread(_load_mcp_tools_async())
        except RuntimeError:
            tools = asyncio.run(_load_mcp_tools_async())

        _last_loaded_tool_names = [getattr(tool, "name", str(tool)) for tool in tools]
        if tools:
            print(f" [OK] 已加载 {len(tools)} 个 MCP 工具: {', '.join(_last_loaded_tool_names)}")
        return tools
    except ModuleNotFoundError:
        _last_error = "Missing optional dependency: install with `pip install -e \".[mcp]\"`."
    except Exception as exc:
        _last_error = str(exc)

    print(f" [警告] MCP 工具加载失败: {_last_error}")
    return []


def get_mcp_status() -> dict[str, Any]:
    path = get_mcp_config_path()
    servers: dict[str, Any] = {}
    config_error = None

    try:
        servers = load_mcp_server_config()
    except Exception as exc:
        config_error = str(exc)

    return {
        "enabled": mcp_enabled(),
        "config_file": str(path),
        "config_exists": path.exists(),
        "server_count": len(servers),
        "servers": sorted(servers.keys()),
        "loaded_tool_count": len(_last_loaded_tool_names),
        "loaded_tools": _last_loaded_tool_names,
        "last_error": _last_error or config_error,
    }

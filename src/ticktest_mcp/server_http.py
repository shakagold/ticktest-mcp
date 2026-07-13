# -*- coding: utf-8 -*-
"""
TickTest MCP Server — HTTP 传输入口（用于 Smithery / 云端部署）

复用 server.py 的全部 Tool 定义和业务逻辑，
仅将传输层从 stdio 切换为 Streamable HTTP。

启动方式：
    python -m ticktest_mcp.server_http
    或: uvicorn ticktest_mcp.server_http:app --host 0.0.0.0 --port 8000

环境变量：
    TICKTEST_API_KEY  — API Key（不配则只能调公开 Tool）
    TICKTEST_API_URL  — API 地址（默认 https://api.ticktest.cn）
    HTTP_PORT         — 监听端口（默认 8000）
    HTTP_HOST         — 监听地址（默认 0.0.0.0）
"""

import os
import sys
import logging
import asyncio
import contextlib
from typing import AsyncGenerator

from mcp.server.streamable_http import (
    StreamableHTTPServerTransport,
    EventStore,
    TransportSecuritySettings,
    EventId,
    SessionMessage,
)

# ── 复用 server.py 的 MCP Server 实例 ──────────────────
from ticktest_mcp.server import server, API_URL, HAS_AUTH, logger as base_logger

logger = logging.getLogger("ticktest-mcp-http")

# ── 配置 ──────────────────────────────────────────────

HTTP_HOST = os.environ.get("HTTP_HOST", "0.0.0.0")
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8000"))


# ── 内存事件存储 ─────────────────────────────────────

class MemoryEventStore(EventStore):
    """简单的内存事件存储，用于单实例部署。"""

    def __init__(self):
        self._events: dict[EventId, SessionMessage] = {}
        self._order: list[EventId] = []

    async def store_event(self, event_id: EventId, message: SessionMessage) -> None:
        self._events[event_id] = message
        self._order.append(event_id)
        if len(self._order) > 1000:
            old = self._order.pop(0)
            self._events.pop(old, None)

    async def replay_events_after(
        self, last_event_id: EventId | None
    ) -> list[SessionMessage]:
        if last_event_id is None:
            return [self._events[eid] for eid in self._order]
        try:
            idx = self._order.index(last_event_id)
            return [self._events[eid] for eid in self._order[idx + 1:]]
        except ValueError:
            return []


# ── 全局 transport（长连接模式，Smithery 推荐）─────────

_event_store = MemoryEventStore()
_transport = StreamableHTTPServerTransport(
    mcp_session_id="ticktest-mcp-http",
    event_store=_event_store,
    security_settings=TransportSecuritySettings(
        is_secure=False,
        enable_dns_rebinding_protection=False,  # 允许任意 Host（Smithery 代理会处理）
    ),
)
_server_task: asyncio.Task | None = None


@contextlib.asynccontextmanager
async def lifespan(app):
    """应用生命周期：启动时连接 transport，关闭时清理。"""
    global _server_task
    async with _transport.connect() as (read_stream, write_stream):
        _server_task = asyncio.create_task(
            server.run(read_stream, write_stream, server.create_initialization_options())
        )
        logger.info("MCP Server transport connected (HTTP mode)")
        yield
        if _server_task and not _server_task.done():
            _server_task.cancel()
            try:
                await _server_task
            except asyncio.CancelledError:
                pass
        logger.info("MCP Server transport disconnected")


def create_app():
    """创建 ASGI 应用 —— 纯手工路由，避免 Starlette 与 transport 冲突。"""
    import json as _json

    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            # 处理 lifespan 事件（启动/关闭）
            async with lifespan(None):
                await send({"type": "lifespan.startup.complete"})
                # 等待关闭信号
                while True:
                    msg = await receive()
                    if msg["type"] == "lifespan.shutdown":
                        break
                await send({"type": "lifespan.shutdown.complete"})
            return

        # 普通 HTTP 请求
        path = scope.get("path", "")
        method = scope.get("method", "")

        # CORS preflight
        if method == "OPTIONS":
            headers = [
                (b"access-control-allow-origin", b"*"),
                (b"access-control-allow-methods", b"GET, POST, OPTIONS"),
                (b"access-control-allow-headers", b"*"),
            ]
            await send({"type": "http.response.start", "status": 204, "headers": headers})
            await send({"type": "http.response.body", "body": b""})
            return

        # 健康检查
        if path == "/health" and method == "GET":
            body = _json.dumps({
                "status": "ok", "transport": "http", "version": "0.3.1",
                "api_url": API_URL, "auth_configured": HAS_AUTH,
            }).encode()
            headers = [
                (b"content-type", b"application/json"),
                (b"access-control-allow-origin", b"*"),
            ]
            await send({"type": "http.response.start", "status": 200, "headers": headers})
            await send({"type": "http.response.body", "body": body})
            return

        # MCP 端点
        if path == "/mcp" and method == "POST":
            await _transport.handle_request(scope, receive, send)
            return

        # 404
        headers = [(b"access-control-allow-origin", b"*")]
        await send({"type": "http.response.start", "status": 404, "headers": headers})
        await send({"type": "http.response.body", "body": b"not found"})

    return app


# ── 模块级 app ────────────────────────────────────────

app = create_app()


# ── 直接运行 ─────────────────────────────────────────

def main():
    import uvicorn

    logger.info("=" * 50)
    logger.info("TickTest MCP Server v0.3.1 (HTTP mode)")
    logger.info(f"API URL: {API_URL}")
    logger.info(f"认证状态: {'已配置' if HAS_AUTH else '未配置（只读模式）'}")
    logger.info(f"监听地址: {HTTP_HOST}:{HTTP_PORT}")
    logger.info(f"端点: GET /health, POST /mcp")
    logger.info("=" * 50)

    uvicorn.run(
        "ticktest_mcp.server_http:app",
        host=HTTP_HOST,
        port=HTTP_PORT,
        log_level="info",
    )


if __name__ == "__main__":
    main()

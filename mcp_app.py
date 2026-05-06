"""ASGI 入口：以 streamable-HTTP 方式把平台 MCP server 挂到 ``/mcp``。

启动方式（在 venv 里）:
    uvicorn mcp_app:app --host 0.0.0.0 --port 8765

或者直接：
    python mcp_app.py

鉴权：通过 ``Authorization: Bearer <key>`` header。允许的 key 列表来自环境变量
``MCP_API_KEYS``（逗号分隔）。如果该变量为空，会复用 ``ADMIN_JWT_SECRET`` 当作单个 key
（仅用于本地开发；生产请单独配 MCP_API_KEYS）。

Claude Code / Cursor 配置示例（写到 ``~/.claude.json`` 的 ``mcpServers`` 段）：
    {
      "ai-ops-platform": {
        "type": "http",
        "url": "http://your-host:8765/mcp",
        "headers": { "Authorization": "Bearer <你设的 key>" }
      }
    }
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Mount

from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

from config import Config
from ops_platform.mcp_server import create_mcp_server
from runtime import create_runtime
from utils.logger import configure_logging


logger = logging.getLogger(__name__)


def _allowed_keys() -> set[str]:
    raw = os.getenv("MCP_API_KEYS", "").strip()
    if raw:
        return {k.strip() for k in raw.split(",") if k.strip()}
    fallback = (Config.ADMIN_JWT_SECRET or "").strip()
    return {fallback} if fallback and fallback != "change-me-in-prod" else set()


class BearerAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, allowed: set[str]) -> None:
        super().__init__(app)
        self.allowed = allowed
        self.allow_anon = os.getenv("MCP_ALLOW_ANON", "false").lower() == "true"

    async def dispatch(self, request, call_next):
        if self.allow_anon or not self.allowed:
            return await call_next(request)
        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        token = auth.split(" ", 1)[1].strip()
        if token not in self.allowed:
            return JSONResponse({"error": "forbidden"}, status_code=403)
        return await call_next(request)


def create_app() -> Starlette:
    configure_logging()
    runtime = create_runtime(Config)

    server = create_mcp_server(runtime)
    session_manager = StreamableHTTPSessionManager(
        app=server,
        json_response=False,
        stateless=False,
    )

    @asynccontextmanager
    async def lifespan(_app: Starlette):
        async with session_manager.run():
            logger.info("MCP server lifecycle started; tools mounted at /mcp")
            yield

    allowed = _allowed_keys()
    if not allowed:
        logger.warning("MCP_API_KEYS 为空且 ADMIN_JWT_SECRET 未自定义；当前未启用鉴权（仅限本地）。")
    else:
        logger.info("MCP server 鉴权已启用，受信 key 数量：%d", len(allowed))

    return Starlette(
        debug=False,
        routes=[Mount("/mcp", app=session_manager.handle_request)],
        middleware=[Middleware(BearerAuthMiddleware, allowed=allowed)],
        lifespan=lifespan,
    )


app = create_app()


if __name__ == "__main__":
    import uvicorn

    host = os.getenv("MCP_HOST", "0.0.0.0")
    port = int(os.getenv("MCP_PORT", "8765"))
    uvicorn.run("mcp_app:app", host=host, port=port, log_level="info")

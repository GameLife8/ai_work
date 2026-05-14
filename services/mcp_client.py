"""External MCP server 客户端——把别人家的 MCP 暴露成本地 SkillSpec。

设计动机
========
现在 ``ops_platform/mcp_server.py`` 是把**本地 skill 暴露成 MCP**（让 Claude Code 当外部
能力用）。反向方向缺失：用户想让本平台的国产模型也能用 GitHub MCP / FileSystem MCP /
其它第三方 MCP，就必须**反向**——把别人家的 MCP server 注册成本地 skill。

本模块解决"协议适配 + 异步/同步桥接"两件事：

  1. 协议适配：MCP 用 streamable_http 或 stdio；只支持 HTTP（运维场景需求最大、最简单）。
  2. 异步/同步桥接：MCP Python SDK 是 ``async``，但 SkillRegistry handler 签名是 sync。
     用一个一次性 asyncio 子线程跑每个调用，串行往里塞 coroutine，避免在 Chainlit 自己的
     事件循环里 nested loop。

不做的事
========
- **不做持久连接**：每次 ``list_tools`` / ``call_tool`` 都新开一次 streamablehttp 会话。
  MCP 调用本身是 HTTP 往返，初始化握手有 ~30-50ms 开销，对运维 skill 频次完全够。
  以后真有性能需求再加 connection pooling。
- **不做 stdio transport**：stdio 需要管理子进程生命周期，跟容器化部署不合。
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from contextlib import asynccontextmanager
from typing import Any

# MCP SDK 是可选依赖——没装时本模块仍可被 import（运行时才会报真错）
try:
    from mcp import ClientSession    # type: ignore[import-not-found]
    from mcp.client.streamable_http import streamablehttp_client  # type: ignore[import-not-found]
    _MCP_AVAILABLE = True
except ImportError:                 # pragma: no cover
    ClientSession = None            # type: ignore[assignment]
    streamablehttp_client = None    # type: ignore[assignment]
    _MCP_AVAILABLE = False


logger = logging.getLogger(__name__)


# ---------------------------- 同步桥 ---------------------------- #


def _run_coro_sync(coro):
    """在一个全新的子线程 + 全新事件循环里跑 coroutine，阻塞等结果。

    为什么不直接 ``asyncio.run``：Chainlit / FastAPI 进入 handler 时通常**已在事件循环**里，
    再嵌套 ``asyncio.run`` 会抛 RuntimeError("This event loop is already running")。
    用子线程隔离 = 永远新开 loop = 一定能跑。

    代价：每次调用多一次线程创建（~毫秒级）。MCP HTTP 往返本就 30+ms 起步，不显著。
    """
    holder: dict[str, Any] = {}

    def _target():
        try:
            holder["v"] = asyncio.run(coro)
        except BaseException as exc:    # 含 KeyboardInterrupt / SystemExit
            holder["e"] = exc

    t = threading.Thread(target=_target, name="mcp-coro", daemon=True)
    t.start()
    t.join()
    if "e" in holder:
        raise holder["e"]
    return holder.get("v")


# ---------------------------- 客户端 ---------------------------- #


class MCPClientError(Exception):
    """MCP 远端调用错误（区别于 transport/network 错误，便于上层包装信号）。"""


class ExternalMCPClient:
    """一个外部 MCP server 的轻量代理。

    Public API:
        ``healthcheck() -> dict``
            for driver.validate()——尝试 initialize + list_tools，看通不通。
        ``list_tools() -> list[dict]``
            返回 [{name, description, inputSchema}, ...]，**保留 inputSchema 原样**，
            让 loader 当 ``params_schema`` 直接喂给 OpenAI function calling。
        ``call_tool(name, args) -> dict``
            返回 {"content": [...], "isError": bool, "raw": ...}——
            content 是 MCP TextContent / ImageContent 的纯净 dict，方便传给模型。
    """

    name = "mcp_client"   # 给 MetricProvider 风格的 ``name`` 字段（日志/错误用）

    def __init__(
        self,
        *,
        endpoint: str,
        auth_header: str = "",
        timeout_seconds: int = 30,
        tool_filter: str = "",
    ) -> None:
        if not _MCP_AVAILABLE:
            raise RuntimeError(
                "MCP SDK 未安装；请在 requirements.txt 加 'mcp[cli]' 后重装依赖"
            )
        if not endpoint:
            raise ValueError("MCPClient: endpoint is required")
        self.endpoint = endpoint.rstrip("/")
        self.auth_header = (auth_header or "").strip()
        self.timeout_seconds = max(5, int(timeout_seconds))
        # 简单包含子串的过滤——支持逗号分隔多个，**或者** "!foo,!bar" 的排除
        # （以 ! 开头的表示排除）。loader 用这个过滤减少噪音 skill。
        self.tool_filter = (tool_filter or "").strip()

    # ---------- public sync API ----------

    def healthcheck(self) -> dict[str, Any]:
        try:
            tools = self.list_tools()
            return {"healthy": True, "tool_count": len(tools), "endpoint": self.endpoint}
        except Exception as exc:    # noqa: BLE001
            return {"healthy": False, "error": str(exc), "endpoint": self.endpoint}

    def list_tools(self) -> list[dict[str, Any]]:
        return _run_coro_sync(self._async_list_tools())

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        return _run_coro_sync(self._async_call_tool(name, arguments or {}))

    # ---------- internal async ----------

    @asynccontextmanager
    async def _session(self):
        """开一次性 streamable_http 会话 + ClientSession，yield 已 initialize 的 session。"""
        headers = {}
        if self.auth_header:
            # 调用方可以传 "Authorization: Bearer xxx" 整行；这里宽松解析
            if ":" in self.auth_header:
                k, v = self.auth_header.split(":", 1)
                headers[k.strip()] = v.strip()
            else:
                # 没冒号就当 Bearer token 用
                headers["Authorization"] = f"Bearer {self.auth_header}"

        async with streamablehttp_client(self.endpoint, headers=headers) as (
            read_stream, write_stream, _get_session_id,
        ):
            async with ClientSession(read_stream, write_stream) as session:
                await asyncio.wait_for(session.initialize(), timeout=self.timeout_seconds)
                yield session

    async def _async_list_tools(self) -> list[dict[str, Any]]:
        async with self._session() as session:
            resp = await asyncio.wait_for(session.list_tools(), timeout=self.timeout_seconds)
        tools_out: list[dict[str, Any]] = []
        # resp.tools 是 list[mcp.types.Tool]
        for t in getattr(resp, "tools", None) or []:
            tools_out.append({
                "name": t.name,
                "description": t.description or "",
                "inputSchema": getattr(t, "inputSchema", None) or {
                    "type": "object", "properties": {},
                },
            })
        return [t for t in tools_out if self._should_include(t["name"])]

    async def _async_call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        async with self._session() as session:
            resp = await asyncio.wait_for(
                session.call_tool(name, args), timeout=self.timeout_seconds,
            )
        # resp.content 是 list[TextContent | ImageContent | EmbeddedResource]
        content_dicts: list[dict[str, Any]] = []
        for item in getattr(resp, "content", None) or []:
            content_dicts.append(_content_item_to_dict(item))
        return {
            "content": content_dicts,
            "isError": bool(getattr(resp, "isError", False)),
            # 给排错方便——保留每段 type 列表
            "content_types": [c.get("type") for c in content_dicts],
        }

    # ---------- tool filter ----------

    def _should_include(self, tool_name: str) -> bool:
        """tool_filter 支持的语法：
          - 空 → 全部接受
          - "a,b,c" → 仅接受名字包含 a/b/c 任一子串
          - "!foo,!bar" → 全部接受，但排除名字包含 foo/bar 的
          - 混用 → 先按 include 列表筛，再按 exclude 列表排
        """
        if not self.tool_filter:
            return True
        tokens = [t.strip() for t in self.tool_filter.split(",") if t.strip()]
        includes = [t for t in tokens if not t.startswith("!")]
        excludes = [t[1:] for t in tokens if t.startswith("!") and len(t) > 1]
        # 任何 exclude 命中即拒绝
        if any(e and e in tool_name for e in excludes):
            return False
        # 有 include 列表时必须命中至少一个
        if includes and not any(i in tool_name for i in includes):
            return False
        return True


# ---------------------------- helpers ---------------------------- #


def _content_item_to_dict(item: Any) -> dict[str, Any]:
    """把 MCP SDK 的内容对象转成普通 dict（避免上游序列化时碰到 BaseModel）。"""
    # 尝试 .model_dump / .__dict__ —— mcp.types 都是 pydantic v2 BaseModel
    if hasattr(item, "model_dump"):
        try:
            return item.model_dump(mode="json")
        except Exception:           # noqa: BLE001
            pass
    if isinstance(item, dict):
        return item
    # 兜底：字符串化
    return {"type": "raw", "value": str(item)}


def serialize_call_result_for_model(result: dict[str, Any], *, max_chars: int = 8000) -> str:
    """把 ``call_tool`` 返回的 dict 序列化成适合喂 LLM 的字符串。

    优先把 text 段拼起来；超长截断；非 text 段（图/资源）标记 [N items omitted]。
    """
    pieces: list[str] = []
    omitted = 0
    for c in result.get("content") or []:
        if c.get("type") == "text" and isinstance(c.get("text"), str):
            pieces.append(c["text"])
        else:
            omitted += 1
    body = "\n".join(pieces)
    if omitted:
        body += f"\n[{omitted} non-text item(s) omitted]"
    if len(body) > max_chars:
        body = body[:max_chars] + f"\n... [truncated to {max_chars} chars]"
    if result.get("isError"):
        return f"[MCP isError=true]\n{body}"
    return body or "(empty)"

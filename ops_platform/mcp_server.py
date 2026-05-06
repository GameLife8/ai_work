"""把平台 SkillRegistry 暴露成 MCP server。

设计要点：
- **不重新实现执行逻辑**：所有 skill 调用走 ``runtime.skill_invoker``，与 Chainlit /
  Flask Admin API 共用同一份 invoke 路径，**审计统一落到 platform_skill_call**。
- **每个 skill 自动变成一个 MCP tool**：name = skill.code，description = skill.description，
  inputSchema = skill.params_schema。新加 skill 不用改这里。
- **额外提供两个 meta tool**：
    - ``platform_list_connections``：让远端 agent 知道当前有哪些 Swarm/Zabbix 集群可选。
    - ``platform_list_skills``：让远端 agent 列出所有 skill 元数据（可选）。
- **身份**：MCP 调用方在审计里以 ``mcp:<api_key_label>`` 出现。可见性默认按 ``role`` 决定。

调用 ASGI 入口见 ``mcp_app.py``。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from mcp.server.lowlevel import Server
import mcp.types as mcp_types

from ops_platform.context import SkillContext


logger = logging.getLogger(__name__)


META_TOOLS = [
    mcp_types.Tool(
        name="platform_list_connections",
        description=(
            "列出平台已配置的接入（Swarm 集群 / Zabbix 实例 等）。"
            "在调用业务 skill 前，可先用本工具查清楚有哪些 connection_id 可用，"
            "再把 connection_id 作为业务 skill 的参数传入。"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "type_code": {
                    "type": "string",
                    "description": "可选；按类型筛选，例如 swarm / zabbix",
                },
            },
        },
    ),
    mcp_types.Tool(
        name="platform_list_skills",
        description="列出平台已注册的所有 skill 及其参数 schema，用于发现能力。",
        inputSchema={"type": "object", "properties": {}},
    ),
    mcp_types.Tool(
        name="platform_confirm_action",
        description=(
            "确认执行一个先前由写操作 skill 返回的 ``pending_token``。"
            "**调用前必须先获得用户口头同意**——例如用户回复'确认'/'yes'/'go'。"
        ),
        inputSchema={
            "type": "object",
            "properties": {"token": {"type": "string"}},
            "required": ["token"],
        },
    ),
    mcp_types.Tool(
        name="platform_reject_action",
        description="拒绝并放弃一个挂起的写操作（pending_token）。",
        inputSchema={
            "type": "object",
            "properties": {
                "token": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["token"],
        },
    ),
]


def _safe_connection(record: dict) -> dict:
    """脱敏：只返回 id/type/name/alias/is_default/enabled/status，不返回 config 内容。"""
    return {
        "id": record["id"],
        "type_code": record["type_code"],
        "name": record["name"],
        "alias": record.get("alias", ""),
        "is_default": record.get("is_default", False),
        "enabled": record.get("enabled", True),
        "status": record.get("status"),
        "config_keys": list((record.get("config") or {}).keys()),
    }


def _to_text_content(payload: Any) -> list[mcp_types.TextContent]:
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    return [mcp_types.TextContent(type="text", text=text)]


def create_mcp_server(
    runtime,
    *,
    server_name: str = "ai-ops-platform",
    role: str = "admin",
    actor_label: str = "mcp",
) -> Server:
    """构造一个 mcp.lowlevel.Server，把所有 skill 注册成 MCP tool。

    role 决定 skill 可见性过滤：admin 看到全部，user 只看到 ``visibility == 'all'``。
    actor_label 会写到审计里（platform_skill_call.user 列）。
    """

    server: Server = Server(server_name)

    @server.list_tools()
    async def _list_tools() -> list[mcp_types.Tool]:
        visibility = "user" if role != "admin" else None
        tools: list[mcp_types.Tool] = list(META_TOOLS)
        for spec in runtime.skill_registry.list(visibility=visibility, only_enabled=True):
            tools.append(
                mcp_types.Tool(
                    name=spec.code,
                    description=spec.description,
                    inputSchema=spec.params_schema or {"type": "object", "properties": {}},
                )
            )
        return tools

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any] | None):
        args = arguments or {}

        ctx = SkillContext(
            runtime=runtime,
            user={"username": actor_label, "role": role},
            session_id=None,
            selected_connections={},
        )

        if name == "platform_list_connections":
            type_code = args.get("type_code")
            items = runtime.connection_manager.list(type_code=type_code)
            return _to_text_content([_safe_connection(c) for c in items])

        if name == "platform_list_skills":
            return _to_text_content([s.public_dict() for s in runtime.skill_registry.list()])

        if name == "platform_confirm_action":
            envelope = runtime.skill_invoker.confirm(args.get("token", ""), ctx)
            return _to_text_content(envelope)

        if name == "platform_reject_action":
            envelope = runtime.skill_invoker.reject(args.get("token", ""), ctx, reason=args.get("reason", ""))
            return _to_text_content(envelope)

        try:
            envelope = runtime.skill_invoker.invoke(name, args, ctx)
        except KeyError:
            return _to_text_content({"error": f"未知 skill: {name}"})

        # 把 invoker 返回的完整 envelope 给客户端，方便它判断 status / latency。
        return _to_text_content(envelope)

    return server

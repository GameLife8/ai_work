"""External MCP server 接入 driver。

每条 ``mcp_client`` connection 代表一个第三方 MCP server（GitHub MCP / FileSystem MCP /
任意 streamable_http 兼容 MCP）。建好 connection 后，``MCPSkillLoader`` 在启动时扫一遍，
把远端工具列表注册成本地 ``source="mcp"`` 的 SkillSpec——国产模型即可像调本地 skill
一样用第三方 MCP 工具，审计仍统一走 ``platform_skill_call``。
"""

from __future__ import annotations

from typing import Any

from ops_platform.drivers.base import ConnectionDriver, DriverField
from services.mcp_client import ExternalMCPClient


class MCPClientDriver(ConnectionDriver):
    type_code = "mcp_client"
    display_name = "External MCP Server（反向接入）"
    category = "integration"

    def __init__(self) -> None:
        self.fields = [
            DriverField(
                "endpoint", "MCP 端点 URL", required=True,
                placeholder="http://mcp-host:8765/mcp",
                help="对端 MCP server 的 streamable_http URL；通常以 /mcp 结尾。",
            ),
            DriverField(
                "auth_header", "认证 Header", type="password",
                placeholder="Bearer your-token",
                help=(
                    "可填整行（如 ``Authorization: Bearer xxx`` / ``X-Api-Key: yyy``），"
                    "或只填 token——平台会按 ``Authorization: Bearer <token>`` 拼装。"
                ),
            ),
            DriverField(
                "timeout_seconds", "调用超时(秒)", type="integer", default=30,
                help="单次 list_tools / call_tool 上限；MCP 握手 + 工具执行都共用这个时长。",
            ),
            DriverField(
                "tool_filter", "工具过滤", type="textarea", default="",
                placeholder="github_,!debug_",
                help=(
                    "可选。逗号分隔；含子串则保留；前缀 ``!`` 表示排除。"
                    "例：``github_,!debug_`` = 保留所有 github_* 工具但排除其中 debug_* 的。"
                    "留空 = 全量注册。"
                ),
            ),
            DriverField(
                "skill_prefix", "Skill 名前缀", default="",
                placeholder="ghmcp_",
                help=(
                    "可选。注册到本地 SkillRegistry 时给每个 MCP 工具加这个前缀，"
                    "避免跟原生 skill / 别的 MCP 连接重名（推荐用 connection alias 简写）。"
                ),
            ),
            DriverField(
                "category", "Skill 分类", default="mcp",
                help="该 connection 下所有注册的 skill 会挂到这个 category，便于 UI 过滤。",
            ),
            DriverField(
                "visibility", "可见性", default="admin",
                help="``admin`` (仅管理员) / ``all`` (普通用户也能看到)。外部 MCP 默认 admin。",
            ),
        ]

    def build_client(self, config: dict[str, Any]) -> ExternalMCPClient:
        return ExternalMCPClient(
            endpoint=config["endpoint"],
            auth_header=config.get("auth_header") or "",
            timeout_seconds=int(config.get("timeout_seconds") or 30),
            tool_filter=config.get("tool_filter") or "",
        )

    def validate(self, config: dict[str, Any]) -> dict[str, Any]:
        try:
            client = self.build_client(config)
        except Exception as exc:    # noqa: BLE001
            return {"ok": False, "message": str(exc)}
        health = client.healthcheck()
        return {
            "ok": bool(health.get("healthy")),
            "message": health.get("error", ""),
            "details": health,
        }

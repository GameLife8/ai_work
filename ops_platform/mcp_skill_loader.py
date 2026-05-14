"""MCPSkillLoader —— 把所有 ``mcp_client`` connection 暴露的远端工具
注册成本地 SkillSpec（``source="mcp"``）。

加载时机
========
1. 启动末尾 ``runtime.py`` 调一次 ``reload()`` —— 跟 ``http_skill_loader`` 同位。
2. admin 在后台改完 mcp_client connection 时手动触发（暂未在 UI 上挂；先后端 API）。

设计要点
========
- **每个 connection 独立 reload**：失败一个连接不影响其它（远端挂了不应该全平台
  没 MCP skill）。一个连接失败会在 ``reload()`` 返回值里报错。
- **skill code 命名**：``<prefix><tool_name>``——``prefix`` 默认拿 connection.alias，
  也允许 connection.config.skill_prefix 显式覆盖。
- **Handler 包装**：每个 MCP 工具变成的 SkillSpec.handler 是一个闭包，捕获
  ``connection_id`` + ``original_tool_name``——执行时通过 ``ctx.connection_for``
  拿 ExternalMCPClient 调 ``call_tool``。
- **重名兜底**：跟原生 skill / http_skill 撞名时记 warning 并加 ``mcp__`` 强制前缀重试。
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Any

from ops_platform.registry import SkillRegistry, SkillSpec


logger = logging.getLogger(__name__)


# Python identifier-ish 化的字符——把 GitHub MCP 的 "github::list_repos" 变成
# "github_list_repos" 让 OpenAI tool_choice 能正确路由（OpenAI 要求 [a-zA-Z0-9_-]）
_SAFE_NAME = re.compile(r"[^A-Za-z0-9_-]+")


def _safe_skill_code(prefix: str, tool_name: str) -> str:
    raw = f"{prefix}{tool_name}" if prefix else tool_name
    safe = _SAFE_NAME.sub("_", raw).strip("_-")
    return safe or f"mcp_tool_{abs(hash(tool_name)) % 10_000}"


class MCPSkillLoader:
    def __init__(self, runtime) -> None:
        self.runtime = runtime
        self._lock = threading.RLock()
        # 维护：connection_id -> list[skill_code]，便于 reload 时清掉旧的
        self._registered: dict[str, list[str]] = {}

    def reload(self) -> dict[str, list[str]]:
        """重新扫所有 enabled 的 mcp_client connection，注册 / 替换它们的 skill。

        Returns:
            ``{connection_id: [error_str, ...]}``——空 list 表示该 connection 没问题。
        """
        with self._lock:
            errors: dict[str, list[str]] = {}
            store = self.runtime.store
            try:
                connections = store.list_connections(type_code="mcp_client")
            except Exception as exc:    # noqa: BLE001
                logger.warning("list mcp_client connections 失败：%s", exc)
                return {"_global": [str(exc)]}

            # 先把之前注册过的 mcp skill 全部撤掉——避免 connection 改了 prefix 后留垃圾
            self._unregister_all()

            for conn in connections:
                if not conn.get("enabled", True):
                    continue
                cid = conn["id"]
                conn_errs = self._reload_one_connection(conn)
                if conn_errs:
                    errors[cid] = conn_errs

            total = sum(len(codes) for codes in self._registered.values())
            logger.info(
                "MCP skill loader：已注册 %d 个 skill，来自 %d 个 mcp_client connection（%d 个出错）",
                total, len(self._registered), len(errors),
            )
            return errors

    def reload_one(self, connection_id: str) -> list[str]:
        """只重新加载单个 connection（用于 admin 后台改完单个 connection 时调）。"""
        with self._lock:
            self._unregister_connection(connection_id)
            try:
                conn = self.runtime.store.get_connection(connection_id)
            except Exception as exc:    # noqa: BLE001
                return [str(exc)]
            if not conn or conn.get("type_code") != "mcp_client":
                return ["connection 不存在或类型不对"]
            if not conn.get("enabled", True):
                return []
            return self._reload_one_connection(conn)

    # ---------- 内部 ----------

    def _reload_one_connection(self, conn: dict[str, Any]) -> list[str]:
        """注册一个 connection 的所有 tool。返回错误列表（空 = 全部成功）。"""
        cid = conn["id"]
        cfg = conn.get("config") or {}
        prefix = (cfg.get("skill_prefix") or "").strip()
        # 没显式 prefix 时拿 connection alias 的简化版做默认前缀。
        # 同时把连字符也转下划线——alias 像 ``"FileSystem-MCP"`` 比 ``"filesystem-mcp_"``
        # 用 ``"filesystem_mcp_"`` 更视觉一致（OpenAI 两种都合法）。
        if not prefix:
            alias = conn.get("alias") or conn.get("name") or "mcp"
            alias_normalized = _SAFE_NAME.sub("_", alias).replace("-", "_")
            prefix = alias_normalized.strip("_").lower() + "_"
        category = cfg.get("category") or "mcp"
        visibility = cfg.get("visibility") or "admin"

        try:
            client = self.runtime.connection_manager.get_client(cid)
        except Exception as exc:    # noqa: BLE001
            return [f"建客户端失败：{exc}"]

        try:
            tools = client.list_tools()
        except Exception as exc:    # noqa: BLE001
            return [f"远端 list_tools 失败：{exc}"]

        registered_codes: list[str] = []
        errors: list[str] = []
        for tool in tools:
            tool_name = tool.get("name") or ""
            if not tool_name:
                errors.append("跳过：tool 没有 name 字段")
                continue
            code = _safe_skill_code(prefix, tool_name)
            spec = self._build_spec(
                code=code,
                tool=tool,
                connection_id=cid,
                category=category,
                visibility=visibility,
            )
            try:
                self.runtime.skill_registry.register(spec)
                registered_codes.append(code)
            except ValueError:
                # 重名——加强制前缀重试一次
                alt = f"mcp__{cid[:8]}__{code}"
                spec.code = alt
                try:
                    self.runtime.skill_registry.register(spec)
                    registered_codes.append(alt)
                    logger.warning(
                        "MCP skill code 重名，已用强制前缀重注册：%s → %s（connection=%s）",
                        code, alt, cid,
                    )
                except ValueError as exc:
                    errors.append(f"注册 {code} 失败（重名兜底也撞）：{exc}")

        self._registered[cid] = registered_codes
        return errors

    def _build_spec(
        self,
        *,
        code: str,
        tool: dict[str, Any],
        connection_id: str,
        category: str,
        visibility: str,
    ) -> SkillSpec:
        """造一个调远端 MCP tool 的 SkillSpec。

        Handler 是闭包：捕获 connection_id 和 original tool name，运行时
        从 ctx 拿 client（同样的 connection_id 缓存命中，零额外开销）。
        """
        tool_name = tool["name"]
        description = tool.get("description") or f"MCP 工具 {tool_name}（来自外部 MCP server）"
        # 给 description 加个明确的来源标记，让模型知道这是远端工具，可能有延迟
        description = (
            f"[远端 MCP 工具] {description}"
            "\n（调用走第三方 MCP server，延迟比本地 skill 高，失败不影响本地诊断。）"
        )
        input_schema = tool.get("inputSchema") or {"type": "object", "properties": {}}

        def _handler(ctx, **params) -> dict[str, Any]:
            # 用鸭子类型而不是 ``isinstance``——测试里换 FakeMCPClient 时仍能通过。
            # call_tool / list_tools 是 ExternalMCPClient 的契约方法。
            from services.mcp_client import serialize_call_result_for_model

            client = ctx.runtime.connection_manager.get_client(connection_id)
            if not hasattr(client, "call_tool"):
                raise RuntimeError(
                    f"connection {connection_id} 的 client 没有 call_tool 方法"
                    f"（实际类型 {type(client).__name__}）"
                )
            raw_result = client.call_tool(tool_name, params)
            # 返回结构：保留原始 + 一段适合模型读的纯文本汇总
            return {
                "mcp_tool": tool_name,
                "mcp_connection_id": connection_id,
                "isError": bool(raw_result.get("isError")),
                "content": raw_result.get("content") or [],
                "text": serialize_call_result_for_model(raw_result),
            }

        return SkillSpec(
            code=code,
            name=f"MCP: {tool_name}",
            description=description,
            category=category,
            required_connection_type=None,    # handler 自己解析；不强制 connection_manager 注入
            read_only=True,    # 默认按只读对待——外部 MCP 工具的副作用平台无从判断，
                               # 保守起见不走双步流；admin 自己评估接入哪些 MCP server
            visibility=visibility if visibility in {"admin", "all"} else "admin",
            params_schema=input_schema,
            handler=_handler,
            version="1.0.0",
            enabled=True,
            requires_admin_approval=False,
            confirmation_ttl_seconds=300,
            source="mcp",
            source_id=connection_id,
        )

    def _unregister_connection(self, connection_id: str) -> None:
        for code in self._registered.pop(connection_id, []):
            self.runtime.skill_registry.unregister(code)

    def _unregister_all(self) -> None:
        for codes in list(self._registered.values()):
            for code in codes:
                self.runtime.skill_registry.unregister(code)
        self._registered.clear()

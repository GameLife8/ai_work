from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any


@dataclass
class SkillContext:
    """传给 skill handler 的上下文。

    Skill handler 签名：``def run(ctx: SkillContext, **params) -> dict``。
    Skill 不直接 import client；通过 ``ctx.connection_for(type_code, override_id)``
    获取一个解析好的 client（按"参数 → 会话默认 → 平台默认"三层 fallback）。
    """

    runtime: Any
    user: dict[str, Any] | None = None
    session_id: str | None = None
    selected_connections: dict[str, str] = field(default_factory=dict)
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("ops_platform.skill"))
    extra: dict[str, Any] = field(default_factory=dict)

    def connection_for(
        self,
        type_code: str,
        override_id: str | None = None,
    ) -> Any:
        manager = self.runtime.connection_manager
        if override_id:
            return manager.get_client(override_id)
        selected = self.selected_connections.get(type_code)
        if selected:
            return manager.get_client(selected)
        default = manager.get_default(type_code)
        if default is None:
            raise RuntimeError(
                f"未找到 {type_code} 类型的接入；请在管理员后台先创建 Connection 或在会话里选择。"
            )
        return manager.get_client(default["id"])

    def resolve_connection_id(
        self,
        type_code: str,
        override_id: str | None = None,
    ) -> str | None:
        """返回当前会话/参数最终解析到的 connection_id。

        给"需要把 connection_id 入库审计"的 skill 用——比如 async task 落库时
        要知道是哪条接入提交的，``connection_for`` 只返回 client，不暴露 ID。
        """
        if override_id:
            return override_id
        selected = self.selected_connections.get(type_code)
        if selected:
            return selected
        default = self.runtime.connection_manager.get_default(type_code)
        return (default or {}).get("id")

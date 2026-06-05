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
        *,
        node: str | None = None,
    ) -> Any:
        manager = self.runtime.connection_manager
        if override_id:
            return manager.get_client(override_id)
        # host_agent:节点名是路由真相——先按 node 反查它所属集群,命中即用(优先于
        # 会话默认/平台默认)。这能根治"消息里没集群关键词('这个集群')→ 掉默认集群,
        # 拿别的集群的 agent 去找本集群节点 → node not found"。
        by_node = self._resolve_by_node(type_code, node)
        if by_node:
            return manager.get_client(by_node)
        selected = self.selected_connections.get(type_code)
        if selected:
            return manager.get_client(selected)
        default = manager.get_default(type_code)
        if default is None:
            raise RuntimeError(
                f"未找到 {type_code} 类型的接入；请在管理员后台先创建 Connection 或在会话里选择。"
            )
        return manager.get_client(default["id"])

    def _resolve_by_node(self, type_code: str, node: str | None) -> str | None:
        """host_agent 专用:按 node 反查它所属连接;非 host_agent / 无 node / 查不到都返回 None。

        路由兜底**绝不能**把正常调用搞挂——找不到 finder(老 manager / 测试 mock)或
        查询出错时,静默回退到 selected/default。
        """
        if not node or type_code != "host_agent":
            return None
        finder = getattr(self.runtime.connection_manager, "find_host_agent_for_node", None)
        if finder is None:
            return None
        try:
            cid = finder(node)
            if cid is None:
                cid = finder(node, refresh=True)   # 未命中强刷一次,抓刚加入的节点
            return cid
        except Exception:   # noqa: BLE001
            self.logger.debug("按 node 路由 host_agent 失败,回退默认", exc_info=True)
            return None

    def resolve_connection_id(
        self,
        type_code: str,
        override_id: str | None = None,
        *,
        node: str | None = None,
    ) -> str | None:
        """返回当前会话/参数最终解析到的 connection_id（解析顺序与 ``connection_for`` 一致）。

        给"需要把 connection_id 入库审计"的 skill 用——比如 async task 落库时
        要知道是哪条接入提交的，``connection_for`` 只返回 client，不暴露 ID。
        传 ``node`` 时同样先按节点名路由,保证审计记录的就是真正执行的那条连接。
        """
        if override_id:
            return override_id
        by_node = self._resolve_by_node(type_code, node)
        if by_node:
            return by_node
        selected = self.selected_connections.get(type_code)
        if selected:
            return selected
        default = self.runtime.connection_manager.get_default(type_code)
        return (default or {}).get("id")

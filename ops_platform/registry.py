from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from ops_platform.context import SkillContext


@dataclass
class SkillSpec:
    code: str
    name: str
    description: str
    category: str
    required_connection_type: str | None
    read_only: bool
    visibility: str                # 'admin' / 'all'
    params_schema: dict[str, Any]
    handler: Callable[..., dict[str, Any]]
    version: str = "1.0.0"
    enabled: bool = True
    requires_admin_approval: bool = False
    confirmation_ttl_seconds: int = 300
    # 写 skill 在「只传这些参数」时其实是只读的 → 该次调用免确认直接执行。
    # 例:host_run_command 只传 ``task_id`` = 查异步任务结果(读),不该弹确认。
    read_only_params: tuple[str, ...] = ()
    source: str = "python"         # 'python' | 'http' | 'mcp'（future）
    source_id: str | None = None   # http skill 的 DB row id（便于热加载逐条管理）

    def to_openai_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.code,
                "description": self.description,
                "parameters": self.params_schema or {"type": "object", "properties": {}},
            },
        }

    def public_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "required_connection_type": self.required_connection_type,
            "read_only": self.read_only,
            "visibility": self.visibility,
            "version": self.version,
            "enabled": self.enabled,
            "requires_admin_approval": self.requires_admin_approval,
            "params_schema": self.params_schema,
            "source": self.source,
        }


class SkillRegistry:
    """内存中的 skill 表，由 loader.py 在启动时填充。"""

    def __init__(self) -> None:
        self._skills: dict[str, SkillSpec] = {}

    def register(self, spec: SkillSpec) -> None:
        if spec.code in self._skills:
            raise ValueError(f"skill code 重复：{spec.code}")
        self._skills[spec.code] = spec

    def unregister(self, code: str) -> bool:
        """从注册表移除。HTTP skill 热加载用。返回是否真的移除了。"""
        return self._skills.pop(code, None) is not None

    def unregister_by_source(self, source: str) -> int:
        """批量移除某来源（如 'http'）的全部 skill。"""
        codes = [c for c, s in self._skills.items() if s.source == source]
        for c in codes:
            del self._skills[c]
        return len(codes)

    def get(self, code: str) -> SkillSpec:
        if code not in self._skills:
            raise KeyError(f"未注册的 skill：{code}")
        return self._skills[code]

    def list(
        self,
        *,
        visibility: str | None = None,
        only_enabled: bool = True,
    ) -> list[SkillSpec]:
        items = list(self._skills.values())
        if only_enabled:
            items = [s for s in items if s.enabled]
        if visibility == "user":
            items = [s for s in items if s.visibility == "all"]
        return items

    def openai_tools(self, *, visibility: str | None = None) -> list[dict[str, Any]]:
        return [s.to_openai_tool() for s in self.list(visibility=visibility)]

    def execute(self, code: str, params: dict[str, Any], ctx: SkillContext) -> dict[str, Any]:
        spec = self.get(code)
        if not spec.enabled:
            raise RuntimeError(f"skill {code} 已被禁用")
        return spec.handler(ctx, **params)

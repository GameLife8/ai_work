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
        }


class SkillRegistry:
    """内存中的 skill 表，由 loader.py 在启动时填充。"""

    def __init__(self) -> None:
        self._skills: dict[str, SkillSpec] = {}

    def register(self, spec: SkillSpec) -> None:
        if spec.code in self._skills:
            raise ValueError(f"skill code 重复：{spec.code}")
        self._skills[spec.code] = spec

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

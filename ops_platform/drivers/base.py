from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class DriverField:
    """Admin 表单的一个字段定义。"""

    key: str
    label: str
    type: str = "string"          # string / password / integer / boolean / textarea
    required: bool = False
    default: Any = None
    placeholder: str = ""
    help: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "type": self.type,
            "required": self.required,
            "default": self.default,
            "placeholder": self.placeholder,
            "help": self.help,
        }


class ConnectionDriver:
    """接入驱动基类。"""

    type_code: str = ""
    display_name: str = ""
    category: str = "generic"
    fields: list[DriverField] = field(default_factory=list)

    def schema(self) -> dict[str, Any]:
        return {
            "type": self.type_code,
            "display_name": self.display_name,
            "category": self.category,
            "fields": [f.to_dict() for f in self.fields],
        }

    def validate(self, config: dict[str, Any]) -> dict[str, Any]:
        """admin 点"验证 Config"时调，返回 {ok, message, details}。子类应覆盖。"""
        try:
            client = self.build_client(config)
            health = getattr(client, "healthcheck", lambda: {"healthy": True})()
            return {"ok": bool(health.get("healthy", True)), "details": health}
        except Exception as exc:
            return {"ok": False, "message": str(exc)}

    def build_client(self, config: dict[str, Any]) -> Any:
        raise NotImplementedError

from __future__ import annotations

import importlib
import logging
import pkgutil
from typing import Any

from ops_platform.registry import SkillRegistry, SkillSpec


logger = logging.getLogger(__name__)

REQUIRED_MANIFEST_KEYS = {"code", "name", "description", "category"}


def load_skills_from_package(package_name: str, registry: SkillRegistry) -> int:
    """扫描指定包下的子模块/子包，每一个暴露 ``MANIFEST`` + ``run`` 的就是一个 skill。

    约定：
        skills/<skill_code>/__init__.py
            MANIFEST = {
                "code": "...",
                "name": "...",
                "description": "...",                  # 给模型看的触发说明
                "category": "swarm" | "zabbix" | ...,
                "required_connection_type": "swarm" | None,
                "read_only": True,
                "visibility": "all" | "admin",
                "params_schema": {...JSON Schema...},
            }

            def run(ctx, **params) -> dict: ...
    """

    package = importlib.import_module(package_name)
    count = 0
    for module_info in pkgutil.iter_modules(package.__path__, prefix=f"{package_name}."):
        try:
            module = importlib.import_module(module_info.name)
        except Exception as exc:  # pragma: no cover
            logger.exception("加载 skill 模块 %s 失败：%s", module_info.name, exc)
            continue

        manifest: dict[str, Any] | None = getattr(module, "MANIFEST", None)
        handler = getattr(module, "run", None)
        if manifest is None or handler is None:
            continue

        missing = REQUIRED_MANIFEST_KEYS - set(manifest)
        if missing:
            logger.warning("skill %s manifest 缺字段 %s，已跳过", module_info.name, missing)
            continue

        spec = SkillSpec(
            code=manifest["code"],
            name=manifest["name"],
            description=manifest["description"],
            category=manifest["category"],
            required_connection_type=manifest.get("required_connection_type"),
            read_only=bool(manifest.get("read_only", True)),
            visibility=manifest.get("visibility", "all"),
            params_schema=manifest.get("params_schema") or {"type": "object", "properties": {}},
            handler=handler,
            version=manifest.get("version", "1.0.0"),
            enabled=bool(manifest.get("enabled", True)),
            requires_admin_approval=bool(manifest.get("requires_admin_approval", False)),
            confirmation_ttl_seconds=int(manifest.get("confirmation_ttl_seconds", 300)),
        )
        registry.register(spec)
        count += 1

    logger.info("已从 %s 加载 %d 个 skill", package_name, count)
    return count

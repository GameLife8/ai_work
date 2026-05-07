"""把 ``platform_http_skill`` 表里的 YAML 定义注册成 SkillRegistry 中的 SkillSpec。

热加载流程
----------
1. admin 后台 PUT 一个 HTTP skill YAML
2. validate（结构 + Jinja2 编译 + connection 存在性）
3. upsert 到 ``platform_http_skill``
4. 调 ``HttpSkillLoader.reload()``：
   - ``registry.unregister_by_source('http')`` 清掉所有 HTTP 来源的 skill
   - 重新读 DB 注册一遍
5. 下次 agent.ask() / MCP / Admin invoke 都看到新版本

设计要点
--------
- HTTP skill 走和 Python skill **一模一样的 SkillSpec**——读写、可见性、二次确认、
  审计、信号注入、runbook 引用、MCP 暴露**全部自动**。
- ``source='http'`` 字段让我们知道哪些是动态加的，热加载只动这一层。
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from ops_platform.http_skill import (
    HttpSkillLoadError,
    HttpSkillRunner,
    HttpSkillSpec,
    parse_http_skill,
)
from ops_platform.registry import SkillRegistry, SkillSpec


logger = logging.getLogger(__name__)


class HttpSkillLoader:
    """运行时持有；admin 改动后调 reload() 即可。"""

    def __init__(self, runtime) -> None:
        self.runtime = runtime
        self._lock = threading.RLock()

    # ---------- 加载 ----------

    def reload(self) -> dict[str, list[str]]:
        """重新从 DB 加载所有 HTTP skill；返回 ``{code: [errs]}``，正常项 errs 为空列表。"""
        store = self.runtime.store
        if not hasattr(store, "list_http_skills"):
            return {}

        records = store.list_http_skills()
        registry: SkillRegistry = self.runtime.skill_registry

        with self._lock:
            removed = registry.unregister_by_source("http")
            errors: dict[str, list[str]] = {}
            ok_count = 0
            for r in records:
                if not r.get("enabled", True):
                    continue
                code = r.get("key") or r.get("code")
                try:
                    spec = parse_http_skill(r["definition"])
                except HttpSkillLoadError as exc:
                    errors[code or "<?>"] = [str(exc)]
                    continue

                if spec.code in registry._skills:
                    # 跟 Python skill 同名 → 拒绝（避免覆盖原生能力）
                    errors[spec.code] = [f"code 与已注册 skill 冲突，HTTP skill 跳过"]
                    continue

                try:
                    registry.register(self._build_spec(spec, source_id=str(r.get("id") or r.get("key") or spec.code)))
                    ok_count += 1
                    errors.setdefault(spec.code, [])
                except Exception as exc:  # pragma: no cover
                    errors[spec.code] = [f"注册失败：{exc}"]

            logger.info("HTTP skill 加载：移除旧 %d，新增 %d，错误 %d",
                        removed, ok_count,
                        sum(1 for v in errors.values() if v))
            return errors

    # ---------- 构造 SkillSpec ----------

    def _build_spec(self, http_spec: HttpSkillSpec, *, source_id: str) -> SkillSpec:
        runtime = self.runtime
        runner = HttpSkillRunner(runtime)

        # 闭包捕获 http_spec
        def _handler(ctx, **params):
            return runner.run(http_spec, params, ctx)

        # 自动给 params_schema 加上 connection_id 字段（如果用户没显式指定）
        schema = dict(http_spec.params_schema or {"type": "object", "properties": {}})
        schema.setdefault("type", "object")
        props = dict(schema.get("properties") or {})
        if "connection_id" not in props:
            props["connection_id"] = {
                "type": "string",
                "description": "可选；指定一个 http_api connection。不传则用 spec.connection_id 或平台默认。",
            }
        schema["properties"] = props

        return SkillSpec(
            code=http_spec.code,
            name=http_spec.name,
            description=http_spec.description,
            category=http_spec.category,
            required_connection_type="http_api",
            read_only=http_spec.read_only,
            visibility=http_spec.visibility,
            params_schema=schema,
            handler=_handler,
            version=str(int(time.time())),
            enabled=http_spec.enabled,
            requires_admin_approval=http_spec.requires_admin_approval,
            confirmation_ttl_seconds=http_spec.confirmation_ttl_seconds,
            source="http",
            source_id=source_id,
        )


import time  # 末尾 import 避免 spec module-level 循环

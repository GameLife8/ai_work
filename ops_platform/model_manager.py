from __future__ import annotations

import threading
from typing import Any

from ops_agent.model_client import OpsModelClient


class ModelManager:
    """模型配置 CRUD + OpsModelClient 缓存。"""

    def __init__(self, store) -> None:
        self.store = store
        self._client_cache: dict[str, OpsModelClient] = {}
        self._lock = threading.RLock()

    def list(self) -> list[dict[str, Any]]:
        return self.store.list_model_configs()

    def get(self, model_id: str) -> dict[str, Any] | None:
        return self.store.get_model_config(model_id)

    def create(self, *, provider: str, name: str, base_url: str, api_key: str,
               model: str, is_default: bool = False, created_by: str | None = None) -> dict[str, Any]:
        return self.store.create_model_config(
            provider=provider,
            name=name,
            base_url=base_url,
            api_key=api_key,
            model=model,
            is_default=is_default,
            created_by=created_by,
        )

    def update(self, model_id: str, **fields) -> dict[str, Any]:
        record = self.store.update_model_config(model_id, **fields)
        with self._lock:
            self._client_cache.pop(model_id, None)
        return record

    def delete(self, model_id: str) -> None:
        self.store.delete_model_config(model_id)
        with self._lock:
            self._client_cache.pop(model_id, None)

    def get_default(self) -> dict[str, Any] | None:
        for item in self.list():
            if item.get("is_default") and item.get("enabled", True):
                return item
        items = [m for m in self.list() if m.get("enabled", True)]
        return items[0] if items else None

    def get_client(self, model_id: str | None = None) -> OpsModelClient:
        record = self.get(model_id) if model_id else self.get_default()
        if not record:
            raise RuntimeError("没有可用的模型配置；请先在管理员后台创建。")
        cache_key = record["id"]
        with self._lock:
            if cache_key in self._client_cache:
                return self._client_cache[cache_key]
            client = OpsModelClient(
                base_url=record["base_url"],
                api_key=record["api_key"],
                model=record["model"],
                timeout_seconds=int(record.get("timeout_seconds") or 120),
            )
            self._client_cache[cache_key] = client
            return client

    def ensure_bootstrap(self, config_cls: Any) -> None:
        if self.list():
            return
        if config_cls.AI_BASE_URL and config_cls.AI_API_KEY:
            self.create(
                provider=config_cls.AI_PROVIDER,
                name=f"default-{config_cls.AI_PROVIDER}",
                base_url=config_cls.AI_BASE_URL,
                api_key=config_cls.AI_API_KEY,
                model=config_cls.AI_MODEL,
                is_default=True,
                created_by="bootstrap",
            )

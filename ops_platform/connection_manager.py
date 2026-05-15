from __future__ import annotations

import json
import logging
import threading
from typing import Any

from ops_platform import drivers


logger = logging.getLogger(__name__)


class ConnectionManager:
    """Connection 的 CRUD + client 缓存。

    数据存在 SQLStore 的 ``platform_connection`` 表。每个 Connection 是一份某种 type 的
    具体接入配置（凭证落库时建议加密；当前版本以明文 JSON 存放，留待后续接 KMS）。
    """

    def __init__(self, store, runtime_ref: Any | None = None) -> None:
        self.store = store
        self._runtime_ref = runtime_ref
        self._client_cache: dict[str, Any] = {}
        self._lock = threading.RLock()

    def attach_runtime(self, runtime: Any) -> None:
        self._runtime_ref = runtime

    # ---------- CRUD ----------

    def list(self, type_code: str | None = None) -> list[dict[str, Any]]:
        return self.store.list_connections(type_code=type_code)

    def get(self, connection_id: str) -> dict[str, Any] | None:
        return self.store.get_connection(connection_id)

    def create(self, *, type_code: str, name: str, alias: str = "", config: dict[str, Any],
               is_default: bool = False, created_by: str | None = None,
               tags: list[str] | None = None) -> dict[str, Any]:
        drivers.get(type_code)  # 校验 driver 存在
        record = self.store.create_connection(
            type_code=type_code,
            name=name,
            alias=alias,
            config=config,
            is_default=is_default,
            created_by=created_by,
            tags=tags or [],
        )
        return record

    def update(self, connection_id: str, **fields) -> dict[str, Any]:
        record = self.store.update_connection(connection_id, **fields)
        with self._lock:
            self._client_cache.pop(connection_id, None)
        return record

    def delete(self, connection_id: str) -> None:
        self.store.delete_connection(connection_id)
        with self._lock:
            self._client_cache.pop(connection_id, None)

    # ---------- client 解析 ----------

    def get_client(self, connection_id: str) -> Any:
        with self._lock:
            if connection_id in self._client_cache:
                return self._client_cache[connection_id]

        record = self.get(connection_id)
        if not record:
            raise KeyError(f"connection 不存在：{connection_id}")

        type_code = record["type_code"]

        # 内部 driver：直接从 runtime 取
        if type_code == "alert_analysis":
            client = self._runtime_ref.alert_analysis_service if self._runtime_ref else None
        else:
            driver = drivers.get(type_code)
            client = driver.build_client(record["config"])

        with self._lock:
            self._client_cache[connection_id] = client
        return client

    def get_default(self, type_code: str) -> dict[str, Any] | None:
        for item in self.list(type_code=type_code):
            if item.get("is_default"):
                return item
        # 没显式标默认就返回第一个 enabled 的
        items = [c for c in self.list(type_code=type_code) if c.get("enabled", True)]
        return items[0] if items else None

    def validate(self, connection_id: str) -> dict[str, Any]:
        record = self.get(connection_id)
        if not record:
            return {"ok": False, "message": "connection 不存在"}
        try:
            driver = drivers.get(record["type_code"])
        except KeyError as exc:
            return {"ok": False, "message": str(exc)}
        return driver.validate(record["config"])

    def validate_config(self, type_code: str, config: dict[str, Any]) -> dict[str, Any]:
        try:
            driver = drivers.get(type_code)
        except KeyError as exc:
            return {"ok": False, "message": str(exc)}
        return driver.validate(config)

    # ---------- bootstrap ----------

    def ensure_bootstrap(self, config_cls: Any) -> None:
        """首启时把旧的 .env 配置写成默认 connection，保持向后兼容。"""
        if self.list():
            return

        if config_cls.ZABBIX_BASE_URL:
            self.create(
                type_code="zabbix",
                name="default-zabbix",
                alias="默认 Zabbix",
                config={
                    "base_url": config_cls.ZABBIX_BASE_URL,
                    "username": config_cls.ZABBIX_USERNAME,
                    "password": config_cls.ZABBIX_PASSWORD,
                    "timeout_seconds": config_cls.ZABBIX_TIMEOUT_SECONDS,
                    "use_stub": config_cls.USE_STUB_ZABBIX,
                },
                is_default=True,
                created_by="bootstrap",
            )
        if config_cls.DOCKER_HOST:
            self.create(
                type_code="swarm",
                name="default-swarm",
                alias="默认 Swarm",
                config={
                    "docker_host": config_cls.DOCKER_HOST,
                    "docker_bin": config_cls.DOCKER_BIN,
                    "docker_tls_verify": config_cls.DOCKER_TLS_VERIFY,
                    "docker_cert_path": config_cls.DOCKER_CERT_PATH,
                    "log_default_tail": config_cls.DOCKER_LOG_DEFAULT_TAIL,
                    "log_max_tail": config_cls.DOCKER_LOG_MAX_TAIL,
                },
                is_default=True,
                created_by="bootstrap",
            )
        self.create(
            type_code="alert_analysis",
            name="builtin-alert-analysis",
            alias="内置告警分析",
            config={},
            is_default=True,
            created_by="bootstrap",
        )

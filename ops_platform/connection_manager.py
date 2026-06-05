from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

from ops_platform import drivers


logger = logging.getLogger(__name__)

# 按节点名路由 host_agent 用的索引缓存参数。
# host 命令的路由真相是「这个 node 物理上属于哪个集群」——node 名天然唯一归属一个
# host_agent 连接,拿 node 反查连接比靠"会话 tag / 默认连接"稳得多。
_NODE_INDEX_TTL = 300.0          # 正常缓存 5min(agent DaemonSet/service 的节点很稳定)
_NODE_INDEX_MIN_REFRESH = 15.0   # 强刷也至少隔 15s,防"不存在的 node"把索引刷爆


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
        # node → connection_id 索引(按节点名路由 host_agent),带 TTL,见 find_host_agent_for_node
        self._node_index: dict[str, str] = {}
        self._node_index_ts: float = 0.0

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
        with self._lock:
            self._node_index_ts = 0.0   # 新增连接 → 下次按 node 路由时重建索引
        return record

    def update(self, connection_id: str, **fields) -> dict[str, Any]:
        record = self.store.update_connection(connection_id, **fields)
        with self._lock:
            self._client_cache.pop(connection_id, None)
            self._node_index_ts = 0.0
        return record

    def delete(self, connection_id: str) -> None:
        self.store.delete_connection(connection_id)
        with self._lock:
            self._client_cache.pop(connection_id, None)
            self._node_index_ts = 0.0

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

    # ---------- 按节点名路由 host_agent ----------

    def find_host_agent_for_node(self, node: str, *, refresh: bool = False) -> str | None:
        """按节点名找它所属的 host_agent ``connection_id``;找不到返回 None（**全程不抛**）。

        host 命令的路由真相是「这个 node 物理上在哪个集群」。遍历各 host_agent 连接的
        ``list_nodes()`` 建 node→connection 索引并缓存(TTL ``_NODE_INDEX_TTL``)。
        ``refresh=True`` 强制重建(受 ``_NODE_INDEX_MIN_REFRESH`` 节流)——给"缓存未命中
        再确认一次,抓刚加入的节点"用。任何单连接列举失败都跳过,不影响整体路由。
        """
        if not node:
            return None
        node = str(node).strip()
        now = time.monotonic()
        with self._lock:
            age = now - self._node_index_ts
            need = (self._node_index_ts == 0.0) or (age >= _NODE_INDEX_TTL)
            if refresh and age >= _NODE_INDEX_MIN_REFRESH:
                need = True
        if need:
            index = self._build_node_index()   # 不持锁:内部含 kubectl/docker 网络调用
            with self._lock:
                self._node_index = index
                self._node_index_ts = time.monotonic()
        with self._lock:
            return self._node_index.get(node)

    def _build_node_index(self) -> dict[str, str]:
        """列各 host_agent 连接的节点,建 node→connection_id 索引。

        单个连接列举失败(集群不可达 / 凭证失效等)只 ``warning`` + 跳过,绝不拖垮其它
        连接的路由;node 名先到先得(理应唯一归属一个集群)。
        """
        index: dict[str, str] = {}
        for conn in self.list(type_code="host_agent"):
            if not conn.get("enabled", True):
                continue
            cid = conn["id"]
            try:
                nodes = self.get_client(cid).list_nodes()
            except Exception as exc:   # noqa: BLE001 —— 一个集群挂了不能拖垮按 node 路由
                logger.warning("node 索引:连接 %s 列举节点失败,跳过:%s",
                               conn.get("name") or cid, exc)
                continue
            for n in nodes or []:
                index.setdefault(str(n), cid)
        return index

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
        """首启时把旧的 .env 配置写成默认 connection，保持向后兼容。

        **并发安全(双重检查锁)**:多个 chainlit worker / 多容器同时首启空库时,
        check-then-act 会各自看到空库,各建一遍种子 → 重复。这里:
          1. 快路径:已有 connection 直接返回(无锁开销)
          2. 慢路径:拿 ``store.bootstrap_lock``(MySQL GET_LOCK 跨进程) → **锁内
             再查一次** list()(可能别的 worker 刚建完)→ 没有才建。
        """
        if self.list():    # 快路径
            return

        with self.store.bootstrap_lock():
            if self.list():    # 双重检查:锁外到锁内之间可能已被别的 worker 种好
                return
            self._do_bootstrap(config_cls)

    def _do_bootstrap(self, config_cls: Any) -> None:
        """实际建种子。调用方必须已持有 bootstrap_lock + 双重检查过 list()。"""
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

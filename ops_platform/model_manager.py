"""模型配置 CRUD + OpsModelClient 缓存 + 变更通知。

设计要点
========
**ModelManager 是模型配置 mutation 的唯一权威入口**。所有变更（create / update /
delete）都必须经过本 manager，禁止直接调 ``store.update_model_config(...)``——
否则会绕过 ``_client_cache`` 失效 + ``on_change`` 回调，导致：

- ``ModelManager._client_cache`` 里的 ``OpsModelClient`` 实例还是旧 model 字符串
  / base_url / api_key → chat 链路调用旧模型
- alert pipeline 的 ``runtime.ai_client`` 还是旧的派生实例 → 告警分析用旧凭证
- admin UI 看到的"已更新"跟实际生效之间有窗口（必须等进程重启）

变更通知机制
============
``register_on_change(callback)`` 让外部（最典型是 ``runtime.refresh_legacy_clients``）
注册回调。任何 create / update / delete 完成后，manager 会按注册顺序触发所有回调。
回调签名 ``callback(action: str, model_id: str | None, record: dict | None)``，
``action`` ∈ ``"create" / "update" / "delete"``；deletion 时 ``record=None``。

回调里抛异常不影响 mutation 本身（落库已经成功），只记日志——一个下游崩了不应
该让其他下游也收不到通知。
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

from ops_agent.model_client import OpsModelClient


logger = logging.getLogger(__name__)


# on_change 回调签名：(action, model_id, record_or_None)
ChangeCallback = Callable[[str, "str | None", "dict | None"], None]


class ModelManager:
    """模型配置 CRUD + OpsModelClient 缓存 + 下游变更通知。

    所有 mutation 都通过 ``create`` / ``update`` / ``delete`` 进行；这三个方法保证：
    1. 落 DB
    2. 失效 ``_client_cache``（chat 链路下次拿到的就是新 client）
    3. 触发所有 ``on_change`` 回调（alert pipeline 等下游一并刷新）
    """

    def __init__(self, store) -> None:
        self.store = store
        self._client_cache: dict[str, OpsModelClient] = {}
        self._lock = threading.RLock()
        self._on_change: list[ChangeCallback] = []

    # ---------- 变更通知 ----------

    def register_on_change(self, callback: ChangeCallback) -> None:
        """注册变更回调。多次调用按注册顺序触发。同一个 callable 重复注册会去重。"""
        with self._lock:
            if callback not in self._on_change:
                self._on_change.append(callback)

    def unregister_on_change(self, callback: ChangeCallback) -> None:
        """移除已注册的变更回调。用于热重载场景。"""
        with self._lock:
            try:
                self._on_change.remove(callback)
            except ValueError:
                pass

    def _notify(self, action: str, model_id: str | None, record: dict | None) -> None:
        """触发所有回调；单个回调抛错不影响其他回调。"""
        # snapshot 一份，避免回调里再注册/取消导致迭代异常
        with self._lock:
            callbacks = list(self._on_change)
        for cb in callbacks:
            try:
                cb(action, model_id, record)
            except Exception as exc:    # noqa: BLE001
                logger.warning(
                    "model_manager on_change 回调 %r 失败：%s（不影响其他回调）",
                    getattr(cb, "__name__", cb), exc,
                )

    # ---------- 缓存失效 ----------

    def refresh_cache(self, model_id: str | None = None) -> None:
        """手动失效 ``_client_cache``。

        Args:
            model_id: 单条 id；传 ``None`` 表示**全部清空**（admin 改配置后
                不知道哪些 id 被影响时用，比如 ``is_default`` 切换会改两行）。

        正常 create / update / delete 流程会自动失效，本方法是给外部手动兜底用的——
        比如直接改了 DB（不推荐！）需要手动通知 manager。
        """
        with self._lock:
            if model_id is None:
                self._client_cache.clear()
            else:
                self._client_cache.pop(model_id, None)

    # ---------- 读 ----------

    def list(self) -> list[dict[str, Any]]:
        return self.store.list_model_configs()

    def get(self, model_id: str) -> dict[str, Any] | None:
        return self.store.get_model_config(model_id)

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
                # tool_choice_preference 默认 None → OpsModelClient 用 "auto"
                # admin 在管理后台按"模型实测行为"调:
                # - 'required' 强制每轮必调 tool（qwen3-thinking 类"过度调用"模型不需要,
                #   但豆包 seed-2 偶发空手回的可以试）
                # - 'none' 禁止调 tool（生成式输出场景）
                # - 'auto' 默认（由模型自己决定）
                tool_choice_default=record.get("tool_choice_preference"),
            )
            self._client_cache[cache_key] = client
            return client

    # ---------- 写（唯一入口）----------

    def create(self, *, provider: str, name: str, base_url: str, api_key: str,
               model: str, is_default: bool = False,
               created_by: str | None = None) -> dict[str, Any]:
        record = self.store.create_model_config(
            provider=provider, name=name, base_url=base_url, api_key=api_key,
            model=model, is_default=is_default, created_by=created_by,
        )
        # is_default=True 会把其他行 is_default 改成 0（见 store.create_model_config），
        # 那些行的 OpsModelClient 实例没变（cache_key=id 没变），技术上不需要清掉；
        # 但 alert pipeline 派生的 ai_client 是基于 default 的，要通知它重建。
        # 干脆 cache 也全清，让"新建默认模型"这个事件统一刷干净，比记忆"什么时候该清什么"安全。
        if is_default:
            self.refresh_cache(None)
        self._notify("create", record.get("id") if record else None, record)
        return record

    def update(self, model_id: str, **fields) -> dict[str, Any]:
        record = self.store.update_model_config(model_id, **fields)
        # 改自己这一行 → 至少清自己的缓存
        # 切默认 (is_default=True) → 同时影响"原默认行"（被 SQL 强制改为 0），
        # 但 OpsModelClient 实例本身不变；alert pipeline 才是真正受影响的下游。
        # 简化策略：动到这行就清这行；切默认就把整池子清掉，让所有 lazy build 重新算。
        if fields.get("is_default"):
            self.refresh_cache(None)
        else:
            self.refresh_cache(model_id)
        self._notify("update", model_id, record)
        return record

    def delete(self, model_id: str) -> None:
        # 先抓一下"删的是不是当前默认"——删完再读就读不到了
        was_default = False
        existing = self.get(model_id)
        if existing and existing.get("is_default"):
            was_default = True
        self.store.delete_model_config(model_id)
        self.refresh_cache(model_id)
        if was_default:
            # 默认模型被删，alert pipeline 引用的就是悬空的——通知重建
            self.refresh_cache(None)
        self._notify("delete", model_id, None)

    # ---------- bootstrap ----------

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

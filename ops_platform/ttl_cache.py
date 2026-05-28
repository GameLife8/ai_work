"""线程安全的 TTL 缓存——给 hot read 路径降 DB 压力用。

用例
====
``UnifiedOpsAgent.ask()`` 每次都要：

1. ``store.list_prompt_segments()`` 拼 system prompt
2. ``connection_manager.list()`` 拼 cluster_registry prompt

这两个查询在并发 25 个 session 同时活跃时,每秒 25 次打 DB,完全没意义——
admin 改完配置 5s 内生效对运维场景是可接受延迟。

设计要点
========
- **TTL 5s**：admin UI 改完最多等 5s 生效;高并发期 cache hit 率 >90%
- **失效靠时间**而不是主动 bust：简化代码,代价是 5s 内的旧值会被读到
  （对 prompt / connection 列表这种"几乎不变"的数据,完全可接受）
- **线程安全**：RLock + 时间戳判断;读多写少,锁竞争极小
- **fail-open**：底层 loader 抛错时,直接抛给上层,**不**返回上一次的 stale
  cache。失败重试逻辑由上层决定（更安全）。
- **不缓存空结果**？看场景。本模块默认**会**缓存空结果——避免"一直空"的
  loader 反复被打。如果业务希望空结果立即重试,自己在上层判 None 处理。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Generic, TypeVar


T = TypeVar("T")


class TTLCache(Generic[T]):
    """单值 TTL 缓存。每个实例缓存一个 key→value（适合"全局唯一查询"场景）。

    多 key 场景请用 ``TTLDict``（按 key 维度独立 TTL，不在本模块第一版实现）。
    """

    def __init__(self, ttl_seconds: float = 5.0) -> None:
        self.ttl_seconds = float(ttl_seconds)
        self._value: T | None = None
        self._expires_at: float = 0.0
        self._lock = threading.RLock()
        self._hits = 0
        self._misses = 0

    def get(self, loader: Callable[[], T]) -> T:
        """命中则返回缓存；过期则用 loader 重算并存。

        Args:
            loader: 无参 callable，返回新值。**异常会向上传播**，不会被吞。

        Returns:
            缓存值（或新值）。
        """
        now = time.monotonic()
        with self._lock:
            if self._value is not None and now < self._expires_at:
                self._hits += 1
                return self._value
        # loader 在锁外跑——避免长 loader 阻塞所有读
        # 代价是高并发期可能并发跑多次 loader（thundering herd），但 ttl=5s 下
        # 这个窗口很短，运维 DB 查询 < 50ms 也撑得住。
        value = loader()
        with self._lock:
            self._value = value
            self._expires_at = time.monotonic() + self.ttl_seconds
            self._misses += 1
        return value

    def invalidate(self) -> None:
        """主动清空缓存。admin 显式触发 reload 时用。"""
        with self._lock:
            self._value = None
            self._expires_at = 0.0

    def stats(self) -> dict[str, Any]:
        """读 hit/miss 计数，监控面板用。"""
        with self._lock:
            total = self._hits + self._misses
            return {
                "hits": self._hits,
                "misses": self._misses,
                "total": total,
                "hit_rate": (self._hits / total) if total else 0.0,
                "ttl_seconds": self.ttl_seconds,
                "is_fresh": self._value is not None and time.monotonic() < self._expires_at,
            }

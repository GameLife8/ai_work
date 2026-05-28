"""ModelManager 缓存失效 + on_change 观察者契约。

设计契约
========
1. ``create`` / ``update`` / ``delete`` 是模型配置 mutation 的**唯一入口**。
2. 任何 mutation 都自动失效 ``_client_cache``——下次 ``get_client`` 一定拿到
   基于最新 DB 值构造的 ``OpsModelClient``。
3. 任何 mutation 都触发 ``on_change`` 回调（按注册顺序），允许下游（如
   ``runtime.refresh_legacy_clients``）一并刷新。
4. 单个回调抛异常**不影响**其他回调，也不影响 mutation 本身——mutation 已落库。
5. ``refresh_cache(None)`` 清空所有缓存；``refresh_cache(model_id)`` 只清单条。

为什么重要
==========
没有这套机制时：

- ``scripts/switch_model.py`` 改完 DB，运行中的 chat 进程仍用旧模型（cache 未失效）
- admin UI 切默认模型，alert pipeline 仍引用旧 AIClient（legacy refresh 漏调）
- 用户看到"已保存"，但实际生效要等进程重启——典型的"幽灵 bug"
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from ops_platform.model_manager import ModelManager


# ---------- 模拟一个最小可用的 store ---------- #


class _FakeStore:
    """内存版的 store，只实现 ModelManager 用到的几个方法。"""

    def __init__(self) -> None:
        self._rows: dict[str, dict] = {}
        self._seq = 0

    def _next_id(self) -> str:
        self._seq += 1
        return f"id-{self._seq:04d}"

    def list_model_configs(self) -> list[dict]:
        return list(self._rows.values())

    def get_model_config(self, model_id: str) -> dict | None:
        return self._rows.get(model_id)

    def create_model_config(self, **kwargs) -> dict:
        # 模拟 SQL 的"is_default=1 时其他行被清零"
        if kwargs.get("is_default"):
            for r in self._rows.values():
                r["is_default"] = False
        rec = {
            "id": self._next_id(),
            "enabled": True,
            "timeout_seconds": 120,
            **kwargs,
        }
        self._rows[rec["id"]] = rec
        return rec

    def update_model_config(self, model_id: str, **fields) -> dict:
        rec = self._rows.get(model_id)
        if rec is None:
            return None
        if fields.get("is_default"):
            for rid, r in self._rows.items():
                if rid != model_id:
                    r["is_default"] = False
        rec.update(fields)
        return rec

    def delete_model_config(self, model_id: str) -> None:
        self._rows.pop(model_id, None)


@pytest.fixture
def mm() -> ModelManager:
    store = _FakeStore()
    # 预置一条默认行，模拟 bootstrap 后的状态
    store.create_model_config(
        provider="volcengine_ark",
        name="default",
        base_url="https://ark.example.com/api/coding/v3",
        api_key="sk-old",
        model="doubao-seed-2.0-pro",
        is_default=True,
        created_by="bootstrap",
    )
    return ModelManager(store)


# ---------- 缓存失效契约 ---------- #


def test_update_invalidates_own_cache(mm: ModelManager) -> None:
    """update 同一行 → 下次 get_client 必须基于新 DB 值重建。"""
    default = mm.get_default()
    client_before = mm.get_client()
    assert client_before.model == "doubao-seed-2.0-pro"
    # 同一个对象会被缓存
    assert mm.get_client() is client_before

    mm.update(default["id"], model="doubao-seed-2-0-pro-260215")
    client_after = mm.get_client()
    assert client_after is not client_before, "update 后必须返回新构造的 client"
    assert client_after.model == "doubao-seed-2-0-pro-260215"


def test_update_is_default_clears_all_cache(mm: ModelManager) -> None:
    """切默认涉及多行（原默认行被改 is_default=0），全清更安全。"""
    old_default = mm.get_default()
    # 加一条非默认行 + 把它缓存进 _client_cache
    new_row = mm.create(
        provider="volcengine_ark", name="alt",
        base_url="https://ark.example.com/api/coding/v3",
        api_key="sk-alt", model="doubao-seed-1-6-251015",
        is_default=False, created_by="test",
    )
    # 加 create 时如果不是 is_default，cache 不会被清；预热
    old_client = mm.get_client(old_default["id"])
    new_client = mm.get_client(new_row["id"])
    assert mm._client_cache  # 至少有缓存

    # 把 new_row 切成默认
    mm.update(new_row["id"], is_default=True)
    assert mm._client_cache == {}, "切默认 → 全清缓存"


def test_delete_invalidates_cache(mm: ModelManager) -> None:
    default = mm.get_default()
    _ = mm.get_client()
    assert default["id"] in mm._client_cache
    mm.delete(default["id"])
    assert default["id"] not in mm._client_cache


def test_create_is_default_clears_cache(mm: ModelManager) -> None:
    """新建一条 is_default=True 的行——会把原默认改成 0，整个缓存池应清空。"""
    _ = mm.get_client()
    assert mm._client_cache
    mm.create(
        provider="qwen", name="new-default",
        base_url="https://dashscope.example.com/v1",
        api_key="sk-new", model="qwen3-32b",
        is_default=True, created_by="test",
    )
    assert mm._client_cache == {}


def test_create_non_default_does_not_clear_cache(mm: ModelManager) -> None:
    """新建普通行不应该影响别的缓存——避免不必要的重建。"""
    _ = mm.get_client()
    before_size = len(mm._client_cache)
    mm.create(
        provider="qwen", name="alt",
        base_url="x", api_key="k", model="m",
        is_default=False, created_by="test",
    )
    assert len(mm._client_cache) == before_size


def test_refresh_cache_all(mm: ModelManager) -> None:
    _ = mm.get_client()
    assert mm._client_cache
    mm.refresh_cache(None)
    assert mm._client_cache == {}


def test_refresh_cache_single(mm: ModelManager) -> None:
    default = mm.get_default()
    _ = mm.get_client(default["id"])
    assert default["id"] in mm._client_cache
    mm.refresh_cache(default["id"])
    assert default["id"] not in mm._client_cache


# ---------- on_change 回调契约 ---------- #


def test_on_change_fires_on_create(mm: ModelManager) -> None:
    cb = MagicMock()
    mm.register_on_change(cb)
    rec = mm.create(provider="qwen", name="x", base_url="u", api_key="k",
                    model="m", is_default=False, created_by="test")
    cb.assert_called_once_with("create", rec["id"], rec)


def test_on_change_fires_on_update(mm: ModelManager) -> None:
    cb = MagicMock()
    mm.register_on_change(cb)
    default = mm.get_default()
    rec = mm.update(default["id"], model="new-model-id")
    cb.assert_called_once_with("update", default["id"], rec)


def test_on_change_fires_on_delete(mm: ModelManager) -> None:
    cb = MagicMock()
    mm.register_on_change(cb)
    default = mm.get_default()
    mm.delete(default["id"])
    cb.assert_called_once_with("delete", default["id"], None)


def test_on_change_multiple_callbacks_all_fire(mm: ModelManager) -> None:
    cb1, cb2, cb3 = MagicMock(), MagicMock(), MagicMock()
    mm.register_on_change(cb1)
    mm.register_on_change(cb2)
    mm.register_on_change(cb3)
    default = mm.get_default()
    mm.update(default["id"], model="x")
    cb1.assert_called_once()
    cb2.assert_called_once()
    cb3.assert_called_once()


def test_on_change_callbacks_fire_in_registration_order(mm: ModelManager) -> None:
    """回调顺序要稳定——某些下游可能依赖另一个下游先跑完。"""
    order: list[str] = []
    mm.register_on_change(lambda *a, **k: order.append("first"))
    mm.register_on_change(lambda *a, **k: order.append("second"))
    mm.register_on_change(lambda *a, **k: order.append("third"))
    default = mm.get_default()
    mm.update(default["id"], model="x")
    assert order == ["first", "second", "third"]


def test_on_change_one_callback_failing_does_not_block_others(mm: ModelManager) -> None:
    """单个回调抛异常不应影响其他回调，也不应影响 mutation 本身。"""
    cb_ok_before = MagicMock()
    cb_fail = MagicMock(side_effect=RuntimeError("legacy refresh crashed"))
    cb_ok_after = MagicMock()
    mm.register_on_change(cb_ok_before)
    mm.register_on_change(cb_fail)
    mm.register_on_change(cb_ok_after)

    default = mm.get_default()
    # mutation 本身不应抛
    rec = mm.update(default["id"], model="resilient-test")
    assert rec["model"] == "resilient-test"

    cb_ok_before.assert_called_once()
    cb_fail.assert_called_once()
    cb_ok_after.assert_called_once()


def test_on_change_register_dedupes(mm: ModelManager) -> None:
    """同一个 callable 重复注册只生效一次，避免下游收两份事件。"""
    cb = MagicMock()
    mm.register_on_change(cb)
    mm.register_on_change(cb)
    mm.register_on_change(cb)
    default = mm.get_default()
    mm.update(default["id"], model="x")
    assert cb.call_count == 1


def test_on_change_unregister_works(mm: ModelManager) -> None:
    cb = MagicMock()
    mm.register_on_change(cb)
    mm.unregister_on_change(cb)
    default = mm.get_default()
    mm.update(default["id"], model="x")
    cb.assert_not_called()


def test_on_change_unregister_missing_is_silent(mm: ModelManager) -> None:
    """取消未注册的回调不应抛——简化 admin UI 卸载组件的逻辑。"""
    cb = MagicMock()
    mm.unregister_on_change(cb)  # 不应抛


# ---------- 集成：回调可以正确触发 legacy refresh ---------- #


def test_integration_callback_observes_default_change_only(mm: ModelManager) -> None:
    """模拟 runtime._on_model_change：只关心默认模型相关事件。

    这复现了 runtime.py 注册的真实回调逻辑——避免每次 mutation 都白跑一次
    legacy refresh。
    """
    legacy_refresh_calls: list[str] = []

    def fake_legacy_refresh():
        legacy_refresh_calls.append("refreshed")

    def on_change(action: str, model_id: str | None, record: dict | None) -> None:
        is_default_change = (
            action == "delete"
            or (record is not None and record.get("is_default"))
        )
        if is_default_change:
            fake_legacy_refresh()

    mm.register_on_change(on_change)

    # 1) 更新默认行的 model 字段 → 应触发
    default = mm.get_default()
    mm.update(default["id"], model="x")
    assert len(legacy_refresh_calls) == 1

    # 2) 新建一条非默认行 → 不应触发
    new_row = mm.create(provider="q", name="n", base_url="u", api_key="k",
                        model="m", is_default=False, created_by="t")
    assert len(legacy_refresh_calls) == 1

    # 3) 把这条切成默认 → 应触发
    mm.update(new_row["id"], is_default=True)
    assert len(legacy_refresh_calls) == 2

    # 4) 删除当前默认 → 应触发
    mm.delete(new_row["id"])
    assert len(legacy_refresh_calls) == 3

    # 5) 删除已经不是默认的旧行 → 当前实现是 "任何 delete 都触发"（保守），允许
    mm.delete(default["id"])
    assert len(legacy_refresh_calls) == 4

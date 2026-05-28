"""Medium 级修复的回归测试。

#9 store.py 拆分（兄弟模块 + 兼容 re-export）
#10 tool_choice per-model 配置
#11 TTL cache for hot reads
#12 intent 关键词补全
#13 runtime.shutdown 优雅停机
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest


# ============================================================
# #9 store.py 拆分 —— import 兼容
# ============================================================


def test_store_classes_re_exported_for_backward_compat():
    """拆完后 ``from ops_platform.store import X`` 仍能导出两个类。"""
    from ops_platform.store import (
        InMemoryPlatformStore,
        SQLPlatformStore,
        attach_platform_store,
    )
    assert InMemoryPlatformStore is not None
    assert SQLPlatformStore is not None
    assert callable(attach_platform_store)


def test_store_memory_module_is_importable():
    """兄弟模块 store_memory 也能直接 import（admin 工具脚本可能用）。"""
    from ops_platform.store_memory import InMemoryPlatformStore as Cls1
    from ops_platform.store import InMemoryPlatformStore as Cls2
    assert Cls1 is Cls2, "兄弟模块和 re-export 应指向同一个类"


def test_store_sql_module_is_importable():
    from ops_platform.store_sql import SQLPlatformStore as Cls1
    from ops_platform.store import SQLPlatformStore as Cls2
    assert Cls1 is Cls2


def test_store_py_no_longer_oversized():
    """store.py 不该再回到 1000+ 行;拆分目的是改善可维护性。"""
    import ops_platform.store as mod
    import inspect
    lines = len(inspect.getsource(mod).splitlines())
    assert lines < 300, f"store.py 又胀到 {lines} 行,可能逻辑漏进了入口模块"


# ============================================================
# #10 tool_choice per-model 配置
# ============================================================


def test_model_client_uses_tool_choice_default():
    """构造时传入的 tool_choice_default 必须出现在请求 payload 里。"""
    from unittest.mock import patch
    from ops_agent.model_client import OpsModelClient

    client = OpsModelClient(
        base_url="https://x/api/v3", api_key="k", model="m",
        tool_choice_default="required",
    )
    fake_resp = MagicMock(status_code=200)
    fake_resp.json.return_value = {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
    fake_resp.raise_for_status = MagicMock()

    captured = {}
    def fake_post(url, json=None, **kw):
        captured["payload"] = json
        return fake_resp
    with patch("ops_agent.model_client.requests.post", side_effect=fake_post):
        client.create_completion(
            messages=[{"role": "user", "content": "x"}],
            tools=[{"type": "function", "function": {"name": "t"}}],
        )
    assert captured["payload"]["tool_choice"] == "required"


def test_model_client_call_level_tool_choice_wins_over_default():
    """调用方显式传 tool_choice → 覆盖模型默认。"""
    from unittest.mock import patch
    from ops_agent.model_client import OpsModelClient

    client = OpsModelClient(
        base_url="https://x/api/v3", api_key="k", model="m",
        tool_choice_default="required",
    )
    fake_resp = MagicMock(status_code=200)
    fake_resp.json.return_value = {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
    fake_resp.raise_for_status = MagicMock()

    captured = {}
    def fake_post(url, json=None, **kw):
        captured["payload"] = json
        return fake_resp
    with patch("ops_agent.model_client.requests.post", side_effect=fake_post):
        client.create_completion(
            messages=[{"role": "user", "content": "x"}],
            tools=[{"type": "function", "function": {"name": "t"}}],
            tool_choice="none",
        )
    assert captured["payload"]["tool_choice"] == "none"


def test_model_client_falls_back_to_auto_without_default():
    """既没配置默认也没传调用值 → "auto"（OpenAI 协议默认）。"""
    from unittest.mock import patch
    from ops_agent.model_client import OpsModelClient

    client = OpsModelClient(base_url="https://x/api/v3", api_key="k", model="m")
    fake_resp = MagicMock(status_code=200)
    fake_resp.json.return_value = {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
    fake_resp.raise_for_status = MagicMock()
    captured = {}
    def fake_post(url, json=None, **kw):
        captured["payload"] = json
        return fake_resp
    with patch("ops_agent.model_client.requests.post", side_effect=fake_post):
        client.create_completion(
            messages=[{"role": "user", "content": "x"}],
            tools=[{"type": "function", "function": {"name": "t"}}],
        )
    assert captured["payload"]["tool_choice"] == "auto"


def test_model_client_rejects_invalid_tool_choice_default():
    """不合法的 tool_choice_default → 降级为 None,日志 warning。"""
    from ops_agent.model_client import OpsModelClient
    client = OpsModelClient(
        base_url="https://x/api/v3", api_key="k", model="m",
        tool_choice_default="invalid-value",
    )
    assert client.tool_choice_default is None


# ============================================================
# #11 TTL cache
# ============================================================


def test_ttl_cache_basic_hit_miss():
    from ops_platform.ttl_cache import TTLCache
    cache = TTLCache(ttl_seconds=5)
    counter = {"n": 0}
    def loader():
        counter["n"] += 1
        return "value"
    assert cache.get(loader) == "value"
    assert cache.get(loader) == "value"
    assert counter["n"] == 1, "第二次 get 应命中缓存,不再调 loader"


def test_ttl_cache_expires():
    from ops_platform.ttl_cache import TTLCache
    cache = TTLCache(ttl_seconds=0.1)
    counter = {"n": 0}
    def loader():
        counter["n"] += 1
        return counter["n"]
    assert cache.get(loader) == 1
    time.sleep(0.15)
    assert cache.get(loader) == 2, "过期后应重新调 loader"


def test_ttl_cache_invalidate():
    from ops_platform.ttl_cache import TTLCache
    cache = TTLCache(ttl_seconds=60)
    counter = {"n": 0}
    def loader():
        counter["n"] += 1
        return counter["n"]
    assert cache.get(loader) == 1
    cache.invalidate()
    assert cache.get(loader) == 2


def test_ttl_cache_loader_exception_propagates():
    """loader 抛错应向上传播,不该返回 stale cache。"""
    from ops_platform.ttl_cache import TTLCache
    cache = TTLCache(ttl_seconds=60)
    def loader_ok():
        return "fresh"
    cache.get(loader_ok)   # 先放一个进缓存
    cache.invalidate()
    def loader_fail():
        raise RuntimeError("DB down")
    with pytest.raises(RuntimeError, match="DB down"):
        cache.get(loader_fail)


def test_ttl_cache_stats_track_hit_rate():
    from ops_platform.ttl_cache import TTLCache
    cache = TTLCache(ttl_seconds=60)
    cache.get(lambda: "x")
    cache.get(lambda: "x")
    cache.get(lambda: "x")
    s = cache.stats()
    assert s["hits"] == 2
    assert s["misses"] == 1
    assert s["total"] == 3
    assert s["hit_rate"] > 0.6


# ============================================================
# #12 intent 关键词扩展
# ============================================================


def test_classify_intent_picks_up_show_variants():
    from ops_agent.agent import _classify_intent
    # 之前不命中的几个高频说法,现在应当落到 list_state
    assert _classify_intent("显示一下当前跑了什么容器") == "list_state"
    assert _classify_intent("打印下所有 deployment") == "list_state"
    assert _classify_intent("看下都有啥服务") == "list_state"
    assert _classify_intent("这台机器上跑了哪些 pod") == "list_state"


def test_classify_intent_picks_up_monitor_variants():
    from ops_agent.agent import _classify_intent
    assert _classify_intent("CPU 飙到 99% 了") == "monitor"
    assert _classify_intent("内存吃满了") == "monitor"


def test_classify_intent_picks_up_diagnose_variants():
    from ops_agent.agent import _classify_intent
    assert _classify_intent("为啥服务起不来") == "diagnose"
    assert _classify_intent("这个 pod 啥情况") == "diagnose"


# ============================================================
# #13 runtime.shutdown
# ============================================================


def test_shutdown_hook_is_idempotent():
    """多次调用 runtime.shutdown 只执行一次（atexit + signal handler 可能都触发）。"""
    from runtime import _attach_shutdown_hook

    runtime = MagicMock()
    runtime.async_task_service = None
    runtime.connection_manager = MagicMock(spec=[])  # 无 iter_clients_for_shutdown
    runtime.store = MagicMock(engine=None)

    _attach_shutdown_hook(runtime)
    runtime.shutdown(timeout=1)
    runtime.shutdown(timeout=1)
    runtime.shutdown(timeout=1)
    # 不该抛,也不该有异常


def test_shutdown_stops_async_task_poller():
    """shutdown 必须调到 async_task_service.stop_poller()。"""
    from runtime import _attach_shutdown_hook

    runtime = MagicMock()
    ats = MagicMock()
    ats._poller_thread = None   # 模拟 poller 没启动
    runtime.async_task_service = ats
    runtime.connection_manager = MagicMock(spec=[])
    runtime.store = MagicMock(engine=None)

    _attach_shutdown_hook(runtime)
    runtime.shutdown(timeout=1)
    ats.stop_poller.assert_called_once()


def test_shutdown_disposes_db_engine():
    """SQLAlchemy 连接池要 dispose,避免连接泄漏。"""
    from runtime import _attach_shutdown_hook

    runtime = MagicMock()
    runtime.async_task_service = None
    runtime.connection_manager = MagicMock(spec=[])
    engine = MagicMock()
    runtime.store = MagicMock(engine=engine)

    _attach_shutdown_hook(runtime)
    runtime.shutdown(timeout=1)
    engine.dispose.assert_called_once()


def test_shutdown_tolerates_subsystem_failures():
    """单个子系统挂掉不能阻止其他清理路径——比如 poller stop 抛错不影响 engine dispose。"""
    from runtime import _attach_shutdown_hook

    runtime = MagicMock()
    ats = MagicMock()
    ats.stop_poller.side_effect = RuntimeError("poller borked")
    ats._poller_thread = None
    runtime.async_task_service = ats
    runtime.connection_manager = MagicMock(spec=[])
    engine = MagicMock()
    runtime.store = MagicMock(engine=engine)

    _attach_shutdown_hook(runtime)
    runtime.shutdown(timeout=1)
    # engine dispose 仍被调到
    engine.dispose.assert_called_once()

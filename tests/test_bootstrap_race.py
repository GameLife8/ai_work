"""ensure_bootstrap 并发竞态回归测试。

背景
====
线上踩坑:chat 容器多 worker 同时首启**空库**,每个 worker 的
``ensure_bootstrap`` 各自 ``if self.list(): return`` 都看到空 → 各建一遍种子
→ DB 里出现 ×N 重复的 default-zabbix / default-swarm / builtin-alert-analysis。

修复:双重检查锁——快路径无锁查 list();空了才拿 ``store.bootstrap_lock``
(MySQL GET_LOCK 跨进程),锁内**再查一次** list(),没有才建。

本测试用 InMemoryStore(bootstrap_lock = 进程内 RLock)+ 多线程并发跑
ensure_bootstrap,断言种子只建一份。
"""

from __future__ import annotations

import threading

from ops_platform.connection_manager import ConnectionManager
from ops_platform.model_manager import ModelManager
from ops_platform.store import attach_platform_store


class _Cfg:
    """最小 config，触发所有种子分支。"""
    ZABBIX_BASE_URL = "http://zbx/api_jsonrpc.php"
    ZABBIX_USERNAME = "u"
    ZABBIX_PASSWORD = "p"
    ZABBIX_TIMEOUT_SECONDS = 10
    USE_STUB_ZABBIX = True
    DOCKER_HOST = "tcp://1.2.3.4:2375"
    DOCKER_BIN = "docker"
    DOCKER_TLS_VERIFY = ""
    DOCKER_CERT_PATH = ""
    DOCKER_LOG_DEFAULT_TAIL = 100
    DOCKER_LOG_MAX_TAIL = 1000
    AI_BASE_URL = "https://x/api/v3"
    AI_API_KEY = "sk-test"
    AI_PROVIDER = "volcengine_ark"
    AI_MODEL = "ep-xxx"


def _make_store():
    class _Bare:
        pass
    store = _Bare()
    attach_platform_store(store)   # 绑定 InMemoryPlatformStore（无 engine → memory）
    return store


def test_bootstrap_lock_bound_to_store():
    """bootstrap_lock 必须能从外层 store 调到（attach 绑定了）。"""
    store = _make_store()
    assert hasattr(store, "bootstrap_lock")
    with store.bootstrap_lock() as got:
        assert got is True


def test_connection_bootstrap_no_duplicate_under_concurrency():
    """20 线程并发跑 ensure_bootstrap，种子只建一份。"""
    store = _make_store()
    cm = ConnectionManager(store)

    barrier = threading.Barrier(20)
    errors: list[Exception] = []

    def worker():
        try:
            barrier.wait()        # 让所有线程尽量同时冲进 ensure_bootstrap
            cm.ensure_bootstrap(_Cfg)
        except Exception as exc:   # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"并发 bootstrap 抛错: {errors}"

    conns = store.list_connections()
    names = [c["name"] for c in conns]
    # 每个种子只能有一份
    assert names.count("default-zabbix") == 1, f"default-zabbix 重复: {names}"
    assert names.count("default-swarm") == 1, f"default-swarm 重复: {names}"
    assert names.count("builtin-alert-analysis") == 1, f"alert-analysis 重复: {names}"
    assert len(conns) == 3, f"应只有 3 个种子，实际 {len(conns)}: {names}"


def test_model_bootstrap_no_duplicate_under_concurrency():
    """模型 ensure_bootstrap 同样并发安全。"""
    store = _make_store()
    mm = ModelManager(store)

    barrier = threading.Barrier(20)
    errors: list[Exception] = []

    def worker():
        try:
            barrier.wait()
            mm.ensure_bootstrap(_Cfg)
        except Exception as exc:   # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"并发 model bootstrap 抛错: {errors}"
    models = store.list_model_configs()
    assert len(models) == 1, f"模型种子应只有 1 个，实际 {len(models)}"


def test_bootstrap_idempotent_on_resume():
    """已有数据时再调 ensure_bootstrap（快路径）不重复建、不报错。"""
    store = _make_store()
    cm = ConnectionManager(store)
    cm.ensure_bootstrap(_Cfg)
    n1 = len(store.list_connections())
    # 再调几次
    cm.ensure_bootstrap(_Cfg)
    cm.ensure_bootstrap(_Cfg)
    assert len(store.list_connections()) == n1, "重复调用不该再建种子"


def test_bootstrap_double_check_skips_when_seeded_inside_lock(monkeypatch):
    """模拟:快路径看到空,但进锁后别的 worker 已种好 → 锁内双重检查跳过。"""
    store = _make_store()
    cm = ConnectionManager(store)

    # 先正常种一份(模拟"别的 worker 已建")
    cm.ensure_bootstrap(_Cfg)
    seeded = len(store.list_connections())
    assert seeded == 3

    # 强制让 list() 第一次返回空(骗过快路径),第二次(锁内)返回真实值
    real_list = store.list_connections
    calls = {"n": 0}

    def fake_list(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return []        # 快路径:骗它说空
        return real_list(*a, **k)   # 锁内双重检查:真实(已种)

    monkeypatch.setattr(store, "list_connections", fake_list)
    cm.ensure_bootstrap(_Cfg)
    monkeypatch.undo()

    # 双重检查应拦住,不重复建
    assert len(store.list_connections()) == seeded, "锁内双重检查没拦住重复种子"

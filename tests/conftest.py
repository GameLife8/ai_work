"""pytest 全局 fixture + 测试环境 bootstrap。

**关键时序**：本文件**顶部**就 setenv,**必须在** ``from app import create_app``
**之前**完成。否则 ``Config`` 类被 ``app.py`` 首次 import 时,
``STORE_BACKEND = os.getenv("STORE_BACKEND", "sql")`` 这行**已经在类定义阶段求值**
（Python 类属性在 class body 执行时绑定）—— Config.STORE_BACKEND 永远是
import 时的快照,后续 ``monkeypatch.setenv`` 怎么调都没用。

之前 3 个 pre-existing 失败（``test_system_api`` / ``test_alert_api`` /
``test_admin_maintenance``）的 root cause 就是这个时序坑:它们在 fixture 内
``monkeypatch.setenv("STORE_BACKEND", "memory")``,但此时 ``Config`` 早已被
import 时冻结成 "sql",``create_store(Config)`` 拿到 "sql" 去连真实 TiDB。

修复
----
1. 本文件最顶部强制设 env,**在任何 app/config import 之前**
2. ``app.py`` 在 PYTEST_CURRENT_TEST 等 env 出现时跳过 ``atexit.register``,
   防止 500 个 test ×500 个 atexit handler 在 pytest 退出时全触发
3. ``client`` fixture 不再 setenv (顶部统一控制),只负责创建隔离的 app instance
"""

from __future__ import annotations

import os

# ========================================================================
# !!! 必须在任何 ``from app import ...`` / ``from config import Config`` 之前 !!!
# ========================================================================
# 强制 memory store + stub clients —— 让单测**完全脱离**网络 / TiDB /
# 真实模型 API。想测真实集成走 integration test 套件,别用这套 fixture。
os.environ["STORE_BACKEND"] = "memory"
os.environ["USE_STUB_AI"] = "true"
os.environ["USE_STUB_ZABBIX"] = "true"
# 防止外部 env 误设 STRICT_ENCRYPTION=true 导致 ensure_strict_encryption fail-fast
os.environ["STRICT_ENCRYPTION"] = "false"
# 显式标记 pytest 上下文(app.py 据此跳过 atexit/signal 注册,避免 atexit 累积)
os.environ.setdefault("PYTEST_VERSION", "running")
# ========================================================================

import pytest  # noqa: E402  (import order is intentional — env first)

from app import create_app  # noqa: E402


@pytest.fixture()
def client():
    """Flask test client + 隔离的 InMemoryStore（每 test 一份新 runtime）。

    用法::

        def test_health(client):
            response = client.get("/health")
            assert response.status_code == 200
    """
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as test_client:
        yield test_client


@pytest.fixture()
def client_with_runtime():
    """同 ``client``,但同时返回 ``runtime`` —— 给需要直接操作 runtime
    （比如先 ``runtime.connection_manager.create(...)`` 注入 fake 接入）的测试用。

    用法::

        def test_with_conn(client_with_runtime):
            client, runtime = client_with_runtime
            runtime.connection_manager.create(...)
            response = client.get("/api/...")
    """
    app = create_app()
    app.config["TESTING"] = True
    runtime = app.extensions["runtime"]
    with app.test_client() as test_client:
        yield test_client, runtime

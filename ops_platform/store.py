"""Platform store —— 用户 / 接入 / 模型 / 审计等的统一持久化层。

入口
====
``attach_platform_store(store)``:
    根据传入 ``store`` 对象有没有 ``engine`` 属性（SQLAlchemy engine）自动选
    SQL 实现或内存实现,把所有 platform 操作（``list_users`` / ``save_skill_call``
    等）的方法**反向 bind 到原 store**——保持调用方代码不变。

设计分层
========
本模块原本是 1795 行的"上帝文件",拆成 3 个文件后:

- ``store.py``（本文件,~70 行）—— 入口 + re-export + ``attach_platform_store``
- ``store_memory.py``（~510 行）—— ``InMemoryPlatformStore`` 实现,开发/单测用
- ``store_sql.py``（~1280 行）—— ``SQLPlatformStore`` 实现,生产用（TiDB / MySQL / SQLite）

为什么不做成 ``store/`` 包目录:Python 不允许同时存在 ``store.py`` 和 ``store/__init__.py``
的过渡期;改成兄弟文件能保持 ``from ops_platform.store import X`` 的所有导入兼容,
零迁移成本。

加密 / 解密
===========
- ``PLATFORM_ENCRYPTION_KEY`` 设了:所有敏感字段（``api_key`` / ``config_json``
  里的密码）以 ``enc:v1:...`` 密文落库,读取自动解密
- 未设密钥:明文落库（仅限开发/CI;生产用 ``STRICT_ENCRYPTION=true`` 强制启用）
- 详见 ``ops_platform/crypto.py``

迁移
====
- SQL 表 schema 在 ``SQLPlatformStore.initialize()`` 里 ``CREATE TABLE IF NOT EXISTS``
- 后续加列走 ``SQLPlatformStore._ensure_columns()`` —— SELECT 探测 + ALTER ADD COLUMN,
  幂等,兼容 MySQL 5.7 / TiDB 不支持 ``ALTER ... IF NOT EXISTS`` 的限制
- 历史明文 → 密文一次性迁移:``SQLPlatformStore.migrate_encrypt_existing()``
"""

from __future__ import annotations

from typing import Any

# Re-export 两个实现类,让 ``from ops_platform.store import InMemoryPlatformStore`` 类用法仍然成立。
# 类的真实定义在兄弟模块——本文件只做入口编排。
from ops_platform.store_memory import InMemoryPlatformStore
from ops_platform.store_sql import SQLPlatformStore


__all__ = [
    "InMemoryPlatformStore",
    "SQLPlatformStore",
    "attach_platform_store",
]


def attach_platform_store(store: Any) -> Any:
    """根据已有 store 类型创建对应的 platform store，并把方法 bind 到原 store。

    Args:
        store: 业务侧的 store 对象（InMemoryStore 或 SQLStore）。

    Returns:
        同一个 store 对象,但已经 bind 了 platform 方法（``list_users``,
        ``save_skill_call`` 等等);同时 ``store.platform`` 指向新创建的
        platform store 实例。
    """
    if hasattr(store, "engine"):
        platform = SQLPlatformStore(store.engine)
        platform.initialize()
    else:
        platform = InMemoryPlatformStore()

    method_names = [
        "list_users", "get_user", "get_user_by_username", "create_user", "update_user", "delete_user",
        "list_connections", "get_connection", "create_connection", "update_connection", "delete_connection",
        "list_model_configs", "get_model_config", "create_model_config", "update_model_config", "delete_model_config",
        "save_skill_call", "list_skill_calls",
        "create_pending_action", "get_pending_action", "list_pending_actions", "update_pending_action_status",
        "list_prompt_segments", "get_prompt_segment", "upsert_prompt_segment", "delete_prompt_segment",
        "list_runbooks", "get_runbook", "upsert_runbook", "delete_runbook",
        "save_runbook_execution", "list_runbook_executions", "get_runbook_execution",
        "list_http_skills", "get_http_skill", "upsert_http_skill", "delete_http_skill",
        "create_async_task", "get_async_task", "update_async_task",
        "list_async_tasks", "list_running_async_tasks",
        # 并发安全:ensure_bootstrap 双重检查锁用
        "bootstrap_lock",
    ]
    for name in method_names:
        setattr(store, name, getattr(platform, name))
    store.platform = platform
    return store

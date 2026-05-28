"""把 DEFAULT_RUNBOOKS 最新内容强制刷新到 DB,只覆盖 ``updated_by`` 在
``{'', 'bootstrap', 'bootstrap-sync'}`` 内的行,admin 改过的(其他 updated_by)不动。

为什么需要这个工具
==================
``seed_default_runbooks`` 当前是"按 key 增量"——已有的 key 一律跳过,不会更新。
所以每次改 ``DEFAULT_RUNBOOKS`` 里的 ``final_report_prompt`` / ``triggers`` /
``nodes`` 都得手动跑这个脚本。

后续 ``seed_default_runbooks`` 会改成"启动时自动同步 bootstrap-owned 行"
(见对应 spawn_task),那时这个脚本可以退役。在此之前它是部署流程的一部分。

用法
====
::

    docker cp scripts/sync_default_runbooks.py ai-ops-backend:/tmp/sync.py
    docker exec ai-ops-backend python /tmp/sync.py

或者直接在已经装好平台的进程内 import + main()。
"""
from __future__ import annotations

import logging
import sys

sys.path.insert(0, "/app")
sys.path.insert(0, ".")

from config import Config
from runtime import create_runtime
from ops_platform.runbook_seeds import DEFAULT_RUNBOOKS


logger = logging.getLogger("sync_default_runbooks")
_PLATFORM_OWNED = frozenset({"", "bootstrap", "bootstrap-sync"})


def main() -> int:
    rt = create_runtime(Config)
    store = rt.store
    existing = {r["key"]: r for r in store.list_runbooks()}

    inserted: list[str] = []
    synced: list[str] = []
    skipped_admin: list[str] = []

    for definition in DEFAULT_RUNBOOKS:
        key = definition["key"]
        row = existing.get(key)
        if row is None:
            inserted.append(key)
        else:
            owner = (row.get("updated_by") or "").strip()
            if owner not in _PLATFORM_OWNED:
                skipped_admin.append(key)
                print(f"  ⊘ admin-owned   {key:35s}  updated_by={owner!r}")
                continue
            synced.append(key)

        store.upsert_runbook(
            key=key,
            title=definition["title"],
            description=definition.get("description", ""),
            triggers=definition.get("triggers") or [],
            inputs=definition.get("inputs") or [],
            definition=definition,
            enabled=True,
            updated_by="bootstrap-sync",
        )
        prefix = "+ insert" if row is None else "↻ sync   "
        print(f"  {prefix}     {key:35s}")

    # 触发 in-memory registry 重新加载 —— 但只对**当前进程**的 registry 生效
    # 如果是另一个容器(chat / backend)内的 server,需要单独 restart 或调它的 reload API
    rt.runbook_registry.reload()

    print()
    print(f"inserted={len(inserted)}  synced={len(synced)}  admin-skipped={len(skipped_admin)}")
    print(f"内存 registry: {rt.runbook_registry.list_keys()}")
    if skipped_admin:
        print()
        print("注意: admin 改过的 runbook 没被覆盖。要强制覆盖,先在 admin UI 删掉再跑一遍。")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    sys.exit(main())

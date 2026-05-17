"""异步任务架构 end-to-end 烟雾测试。

在 ai-ops-backend 容器里跑：
    docker exec ai-ops-backend python3 -m scripts.smoke_async_task
"""

from __future__ import annotations

import sys
import time


def main() -> int:
    from runtime import create_runtime
    from config import Config

    runtime = create_runtime(Config)
    service = runtime.async_task_service
    cm = runtime.connection_manager

    # 找 bigdata host_agent
    bigdata = next(
        (c for c in cm.list(type_code="host_agent") if "bigdata" in c["name"]),
        None,
    )
    if not bigdata:
        print("no bigdata host_agent connection")
        return 1

    print(f"submitting task on bigdata6.chinasws.com via {bigdata['name']} ...")
    rec = service.submit(
        connection_id=bigdata["id"],
        node="bigdata6.chinasws.com",
        command="du -sh /etc /usr /var 2>/dev/null",
        argv=["sh", "-c", "du -sh /etc /usr /var 2>/dev/null"],
        nsenter="muinp",
        max_runtime_sec=120,
        submitted_by="smoketest",
        session_id="smoke-001",
    )
    task_id = rec["task_id"]
    print(f"submitted: {task_id} status={rec['status']}")
    if rec.get("last_poll_error"):
        print(f"  init error: {rec['last_poll_error']}")

    for i in range(30):
        time.sleep(2)
        cur = service.get(task_id, refresh=True)
        status = cur["status"]
        exit_code = cur.get("exit_code")
        print(f"poll {i+1}: status={status} exit={exit_code} poll_count={cur.get('poll_count')}")
        if status not in ("submitting", "running"):
            print("=== stdout ===")
            print(cur.get("stdout") or "(empty)")
            print("=== stderr ===")
            print(cur.get("stderr") or "(empty)")
            print(f"=== duration_ms: {cur.get('duration_ms')} ===")
            return 0
    print("timed out waiting")
    return 2


if __name__ == "__main__":
    sys.exit(main())

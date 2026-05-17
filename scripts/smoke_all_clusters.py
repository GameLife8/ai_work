"""向所有 host_agent 接入 (4 个集群) 各提交一个轻量 async task，确认 1.3 路径全通。"""
from __future__ import annotations

import sys
import time


def main() -> int:
    from runtime import create_runtime
    from config import Config

    runtime = create_runtime(Config)
    service = runtime.async_task_service
    cm = runtime.connection_manager

    conns = cm.list(type_code="host_agent")
    if not conns:
        print("no host_agent connections")
        return 1

    results = []
    for c in conns:
        cfg = c.get("config") or {}
        transport = cfg.get("transport") or cfg.get("agent_transport") or "exec"
        if transport != "http":
            results.append((c["name"], "(skip, transport != http)", None))
            continue

        client = cm.get_client(c["id"])
        try:
            nodes = client.list_nodes()
        except Exception as exc:
            results.append((c["name"], f"list_nodes failed: {exc}", None))
            continue

        if not nodes:
            results.append((c["name"], "no nodes", None))
            continue

        # 选第一个节点跑个 echo
        target = nodes[0]
        print(f"[{c['name']}] submitting on {target} ...")
        rec = service.submit(
            connection_id=c["id"],
            node=target,
            command="echo 'hello from 1.3 async path' && uname -n && date",
            argv=["sh", "-c", "echo 'hello from 1.3 async path' && uname -n && date"],
            nsenter="muinp",
            max_runtime_sec=30,
            submitted_by="cluster_smoke",
            session_id="cluster_smoke",
        )
        task_id = rec["task_id"]
        if rec["status"] == "error":
            results.append((c["name"], f"submit failed: {rec.get('last_poll_error')}", task_id))
            continue

        # poll up to 15s
        final = None
        for _ in range(10):
            time.sleep(1.5)
            cur = service.get(task_id, refresh=True)
            if cur["status"] not in ("submitting", "running"):
                final = cur
                break
        if not final:
            results.append((c["name"], "still running 15s", task_id))
            continue
        results.append((
            c["name"],
            f"status={final['status']} exit={final.get('exit_code')} dur={final.get('duration_ms')}ms",
            task_id,
        ))

    print()
    print("=" * 60)
    print("Cluster async path verification")
    print("=" * 60)
    for name, info, tid in results:
        print(f"  {name:25s} {info}   ({tid or '-'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

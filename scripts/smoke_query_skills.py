"""验证 kube_query / swarm_query / host_query 三把通用查询都通。"""
from __future__ import annotations
import sys


def main() -> int:
    from runtime import create_runtime
    from config import Config
    from ops_platform.context import SkillContext

    runtime = create_runtime(Config)
    cm = runtime.connection_manager
    invoker = runtime.skill_invoker

    print(f"registered skills: {len(runtime.skill_registry.list())} (expect ~22)")

    results = []

    # --- kube_query 在 codewave 上跑 ---
    cw = next((c for c in cm.list(type_code="k8s") if "codewave" in c["name"]), None)
    if cw:
        ctx = SkillContext(runtime=runtime, user={"username": "smoke", "role": "admin"},
                           selected_connections={"k8s": cw["id"]})
        env = invoker.invoke("kube_query", {
            "verb": "get", "resource": "pods", "namespace": "kube-system", "output": "json"
        }, ctx)
        rec = env.get("result") or {}
        ok = env.get("status") == "ok" and rec.get("ok")
        n = len((rec.get("parsed") or {}).get("items", [])) if rec.get("parsed") else 0
        results.append(("kube_query@codewave kube-system pods", ok, f"pods={n} status={env.get('status')}"))

    # --- swarm_query 在 bigdata 上跑 ---
    bd = next((c for c in cm.list(type_code="swarm") if "bigdata" in c["name"]), None)
    if bd:
        ctx = SkillContext(runtime=runtime, user={"username": "smoke", "role": "admin"},
                           selected_connections={"swarm": bd["id"]})
        env = invoker.invoke("swarm_query", {
            "category": "service", "verb": "ls"
        }, ctx)
        rec = env.get("result") or {}
        ok = env.get("status") == "ok" and rec.get("ok")
        parsed = rec.get("parsed") or []
        n = len(parsed) if isinstance(parsed, list) else (1 if parsed else 0)
        results.append(("swarm_query@bigdata services ls", ok, f"services={n} status={env.get('status')}"))

    # --- host_query 在 bigdata 上跑 ss ---
    ha = next((c for c in cm.list(type_code="host_agent") if "bigdata" in c["name"]), None)
    if ha:
        ctx = SkillContext(runtime=runtime, user={"username": "smoke", "role": "admin"},
                           selected_connections={"host_agent": ha["id"]})
        client = cm.get_client(ha["id"])
        node = client.list_nodes()[0]
        env = invoker.invoke("host_query", {
            "node": node, "command": "ss -ltn"
        }, ctx)
        rec = env.get("result") or {}
        ok = env.get("status") == "ok" and rec.get("ok")
        lines = len((rec.get("stdout") or "").splitlines())
        results.append((f"host_query@{node} 'ss -ltn'", ok, f"lines={lines} status={env.get('status')}"))

        # --- host_query: 应当拒绝 shell 元字符 ---
        env = invoker.invoke("host_query", {
            "node": node, "command": "ss -ltn | grep 22"
        }, ctx)
        rec = env.get("result") or {}
        err = env.get("status") == "error" or not rec.get("ok") or "禁用字符" in (rec.get("error") or "")
        results.append(("host_query rejects '| grep'", err, f"status={env.get('status')} err={rec.get('error','')[:60]}"))

        # --- host_query: 应当拒绝非白名单 binary ---
        env = invoker.invoke("host_query", {
            "node": node, "command": "rm -rf /tmp/foo"
        }, ctx)
        rec = env.get("result") or {}
        err = env.get("status") == "error" or "白名单" in (rec.get("error") or "")
        results.append(("host_query rejects 'rm'", err, f"status={env.get('status')} err={rec.get('error','')[:60]}"))

    print()
    print("=" * 80)
    for name, ok, info in results:
        status = "✅" if ok else "❌"
        print(f"  {status}  {name:55s} {info}")
    return 0 if all(o for _, o, _ in results) else 1


if __name__ == "__main__":
    sys.exit(main())

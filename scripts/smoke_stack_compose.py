"""验证 swarm_query service inspect 自动反推 compose_yaml。"""
from __future__ import annotations
import sys, uuid


def main() -> int:
    from runtime import create_runtime
    from config import Config
    from ops_platform.context import SkillContext

    runtime = create_runtime(Config)
    invoker = runtime.skill_invoker
    cm = runtime.connection_manager

    sws = next((c for c in cm.list(type_code="swarm") if "sws" in c["name"].lower()), None)
    if not sws:
        print("no sws swarm connection")
        return 1

    # 找一个 swarm 服务名（任意都行，拿 ls 第一个）
    ctx = SkillContext(runtime=runtime, user={"username": "smoke", "role": "admin"},
                       selected_connections={"swarm": sws["id"]})
    env = invoker.invoke("swarm_query",
                          {"category": "service", "verb": "ls"}, ctx)
    parsed = (env.get("result") or {}).get("parsed") or []
    if not parsed:
        print("no services")
        return 1
    sample = parsed[0]["Name"] if isinstance(parsed, list) and parsed else None
    if not sample:
        print("can't pick a service name")
        return 1
    print(f"Inspecting service: {sample}")

    env = invoker.invoke("swarm_query",
                          {"category": "service", "verb": "inspect", "name": sample}, ctx)
    rec = env.get("result") or {}
    compose_yaml = rec.get("compose_yaml")
    compose_note = rec.get("compose_note")

    if not compose_yaml:
        print("❌ 没有 compose_yaml 字段")
        return 1
    print("✅ 拿到 compose_yaml，长度", len(compose_yaml), "chars")
    print()
    print("=== compose_yaml 前 1500 chars ===")
    print(compose_yaml[:1500])
    print()
    print("=== compose_note ===")
    print(compose_note)
    return 0


if __name__ == "__main__":
    sys.exit(main())

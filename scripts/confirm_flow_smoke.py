"""端到端冒烟：写操作 prepare → confirm 链路。

不依赖真实 docker，跑 in-memory store + 假装 ctx 直接绕过 connection。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["STORE_BACKEND"] = "memory"

from config import Config
from ops_platform.context import SkillContext
from runtime import create_runtime


def main() -> None:
    rt = create_runtime(Config)

    # 取一个 swarm connection 出来；run() 会真的去执行 docker，所以我们 monkeypatch 它
    swarm_conn = rt.connection_manager.list(type_code="swarm")[0]
    client = rt.connection_manager.get_client(swarm_conn["id"])

    class FakeResult:
        ok = True
        stdout = "service_xxx updated"
        stderr = ""
        returncode = 0

    client.run = lambda args: FakeResult()  # type: ignore[assignment]

    ctx = SkillContext(
        runtime=rt,
        user={"username": "smoketester", "role": "admin"},
        session_id="smoke-1",
        selected_connections={},
    )

    # 1. 第一次调用：写 skill 应该返回 needs_confirmation
    env1 = rt.skill_invoker.invoke(
        "swarm_force_update_service",
        {"service_name": "demo", "connection_id": swarm_conn["id"]},
        ctx,
    )
    print("step1 status:", env1["status"])
    print("step1 token :", env1.get("pending_token"))
    assert env1["status"] == "needs_confirmation"
    token = env1["pending_token"]

    # 2. confirm: 真正执行
    env2 = rt.skill_invoker.confirm(token, ctx)
    print("step2 status:", env2["status"])
    print("step2 result:", env2.get("result"))
    assert env2["status"] == "ok"

    # 3. 重复 confirm 应该被拒
    env3 = rt.skill_invoker.confirm(token, ctx)
    print("step3 status:", env3["status"], env3.get("error_code"))
    assert env3["status"] == "error"

    # 4. reject 一个新的
    env4 = rt.skill_invoker.invoke(
        "swarm_force_update_service",
        {"service_name": "demo2", "connection_id": swarm_conn["id"]},
        ctx,
    )
    rejected = rt.skill_invoker.reject(env4["pending_token"], ctx, reason="dry_run")
    print("step4 rejected status:", rejected["status"])
    assert rejected["status"] == "rejected"

    # 5. 只读 skill 不走确认
    env5 = rt.skill_invoker.invoke("swarm_list_services", {"connection_id": swarm_conn["id"]}, ctx)
    print("step5 read-only status:", env5["status"])
    assert env5["status"] in {"ok", "error"}  # 这里因为 fake client.run，list_services 调 client.json 会走真实 docker

    print("\nALL OK")


if __name__ == "__main__":
    main()

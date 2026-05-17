"""重现 400 错误，dump 完整请求 payload。"""
from __future__ import annotations
import json, sys


def main():
    from runtime import create_runtime
    from config import Config
    from ops_agent.agent import UnifiedOpsAgent

    runtime = create_runtime(Config)
    agent = UnifiedOpsAgent(runtime)

    session = "debug-400-1"
    msg = "能帮我展示一下低代码平台的配置文件么"

    runtime.store.save_chat_message(session, "user", msg)
    try:
        out = agent.ask(msg, session_id=session,
                        user={"username": "smoketest", "role": "admin"})
        print("=== SUCCESS ===")
        print(out.message[:500])
    except Exception as exc:
        print(f"=== FAILED: {exc} ===")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()

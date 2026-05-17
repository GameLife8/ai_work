"""真打模型，验证同一 session 多轮 ask() 之间上下文确实在传。"""
from __future__ import annotations
import sys, time, uuid


def main() -> int:
    from runtime import create_runtime
    from config import Config
    from ops_agent.agent import UnifiedOpsAgent

    runtime = create_runtime(Config)
    agent = UnifiedOpsAgent(runtime)
    session = "smoke-mem-" + uuid.uuid4().hex[:8]
    store = runtime.store

    # 第 1 轮：先 save user（模拟 chainlit 流程），再 ask
    print(f"--- session={session} ---")
    print("[1/2] 我叫张三，记住我。")
    store.save_chat_message(session, "user", "我叫张三，记住我。")
    out1 = agent.ask("我叫张三，记住我。", session_id=session,
                     user={"username": "smoketest", "role": "admin"})
    print("  ↳", (out1.message or "")[:200])
    store.save_chat_message(session, "assistant", out1.message)

    time.sleep(1)

    # 第 2 轮：换问"我叫什么"，看模型记不记得
    print("[2/2] 我叫什么名字？")
    store.save_chat_message(session, "user", "我叫什么名字？")
    out2 = agent.ask("我叫什么名字？", session_id=session,
                     user={"username": "smoketest", "role": "admin"})
    print("  ↳", (out2.message or "")[:200])
    store.save_chat_message(session, "assistant", out2.message)

    # 验证
    msg = (out2.message or "").lower()
    if "张三" in (out2.message or ""):
        print("\n✅ 会话记忆生效：模型在第 2 轮回忆出了'张三'")
        return 0
    print("\n❌ 模型在第 2 轮没回忆出'张三'，记忆未生效")
    print(f"out2: {out2.message!r}")
    return 1


if __name__ == "__main__":
    sys.exit(main())

"""真打模型，验证"展示配置详细内容"类问题会先贴 raw 再翻译。"""
from __future__ import annotations
import sys, uuid


def main() -> int:
    from runtime import create_runtime
    from config import Config
    from ops_agent.agent import UnifiedOpsAgent

    runtime = create_runtime(Config)
    agent = UnifiedOpsAgent(runtime)
    session = "smoke-show-" + uuid.uuid4().hex[:8]
    store = runtime.store

    # 找一个 sws-swarm 上存在的 haitu 服务
    print("Q: 看一下 swarm 里 haitu 服务的完整配置")
    msg = "看一下 swarm 里 haitu 服务的完整配置，要原始 YAML，然后给我翻译"
    store.save_chat_message(session, "user", msg)
    out = agent.ask(msg, session_id=session,
                    user={"username": "smoketest", "role": "admin"})
    print()
    print("=== 模型回答 ===")
    print(out.message)
    print()
    print(f"=== 调了 {len(out.trace)} 个 skill ===")
    for t in out.trace:
        print(f"  - {t.get('tool_name')} status={t.get('status')}  latency={t.get('latency_ms')}ms")

    # 检查回答里有没有代码块（``` 或 yaml/json）
    has_codeblock = "```" in (out.message or "")
    has_raw_data = any(k in (out.message or "")
                       for k in ("Spec", "Image:", "image:", "TaskTemplate", "Endpoint",
                                 "Labels", "labels:", "Mounts"))
    print()
    print(f"  含代码块: {has_codeblock}")
    print(f"  含 raw 字段名: {has_raw_data}")
    if has_codeblock or has_raw_data:
        print("\n✅ 模型按新 prompt 给出了配置原文")
        return 0
    print("\n❌ 没看到原始配置内容")
    return 1


if __name__ == "__main__":
    sys.exit(main())

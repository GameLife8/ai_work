"""验证新 prompt 的几种输出模式都生效。"""
from __future__ import annotations
import sys, uuid


def main() -> int:
    from runtime import create_runtime
    from config import Config
    from ops_agent.agent import UnifiedOpsAgent

    runtime = create_runtime(Config)
    agent = UnifiedOpsAgent(runtime)
    user = {"username": "smoketest", "role": "admin"}

    cases = [
        ("config_view", "看一下 swarm 里 haitu 服务的完整配置，要原始 YAML 然后翻译"),
        ("list_state",  "BigData swarm 里有哪些服务，列一下"),
    ]
    results = []
    for tag, q in cases:
        session = f"smoke-{tag}-" + uuid.uuid4().hex[:6]
        runtime.store.save_chat_message(session, "user", q)
        out = agent.ask(q, session_id=session, user=user)
        msg = out.message or ""
        results.append((tag, q, msg, out.trace))

    print()
    for tag, q, msg, trace in results:
        print("=" * 80)
        print(f"[{tag}] Q: {q}")
        print(f"   skills: {[t.get('tool_name') for t in trace]}")
        print(f"   --- 回答头 500 字符 ---")
        print(msg[:500])
        print(f"   ... (total {len(msg)} chars)")
        # 检查特征
        if tag == "config_view":
            has_code = "```" in msg
            has_yaml_kw = "yaml" in msg.lower() or "image:" in msg or "Image" in msg
            print(f"   ✅ 含代码块={has_code} 含 yaml 字段={has_yaml_kw}")
        elif tag == "list_state":
            has_table = "|" in msg and "---" in msg
            print(f"   ✅ 含 markdown 表格={has_table}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

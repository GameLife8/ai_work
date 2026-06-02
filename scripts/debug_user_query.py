"""真打模型重现用户问题，验证现在能贴 yaml。"""
from __future__ import annotations
import sys, uuid


def main() -> int:
    from runtime import create_runtime
    from config import Config
    from ops_agent.agent import UnifiedOpsAgent, _classify_intent

    runtime = create_runtime(Config)
    agent = UnifiedOpsAgent(runtime)
    user = {"username": "smoketest", "role": "admin"}

    q = "低代码平台的coredns 的配置是什么，并给我解释一下"
    print(f"Q: {q}")
    print(f"意图分类: {_classify_intent(q)}")
    print()

    session = "debug-user-" + uuid.uuid4().hex[:6]
    runtime.store.save_chat_message(session, "user", q)
    out = agent.ask(q, session_id=session, user=user)

    msg = out.message or ""
    # 检测最终输出
    final = msg
    has_code = "```" in final
    has_corefile = "Corefile" in final
    print(f"\n=== 最终输出 ({len(final)} 字符) 含代码块={has_code} 含 Corefile={has_corefile} ===")
    print(final[-1800:])
    print()
    print(f"=== 调了 {len(out.trace)} 个 skill ===")
    for t in out.trace:
        tool_name = t.get('tool_name')
        args = t.get('tool_args') or {}
        status = t.get('status')
        result = t.get('tool_result') or {}
        stdout_len = len(result.get("stdout") or "")
        parsed = result.get("parsed")
        parsed_kind = type(parsed).__name__ if parsed is not None else "None"
        parsed_size = len(str(parsed)) if parsed is not None else 0
        result_keys = list(result.keys())
        print(f"  - {tool_name} args={dict(args)} status={status}")
        print(f"    result_keys={result_keys}")
        print(f"    stdout_chars={stdout_len}  parsed_kind={parsed_kind}  parsed_size={parsed_size}")
        if result.get("stdout"):
            print(f"    stdout 前 200 字: {(result.get('stdout') or '')[:200]!r}")
    print()
    # NOTE:raw-first 的代码兜底已删(改为 system prompt 铁律 #2 硬约束),
    # 这里直接看模型最终 message 自己有没有贴 raw。
    print("=== 模型最终 message（看它自己有没有贴 raw 表格/代码块）===")
    print(out.message[:800])
    return 0


if __name__ == "__main__":
    sys.exit(main())

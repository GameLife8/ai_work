"""量一下 system prompt + tools schema 的总大小。"""
from __future__ import annotations
import json


def main():
    from runtime import create_runtime
    from config import Config
    from ops_agent.agent import (
        UnifiedOpsAgent, _classify_intent_hint,
    )

    runtime = create_runtime(Config)
    agent = UnifiedOpsAgent(runtime)

    # 模拟一次 ask 的 payload 拼装
    user_message = "能帮我展示一下低代码平台的配置文件么"

    sys_prompt = agent._load_system_prompt()
    cluster_reg = agent._build_cluster_registry_prompt(selected_connections={})
    intent_hint = _classify_intent_hint(user_message) or ""
    tools = agent.registry.openai_tools(visibility=None)

    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "system", "content": cluster_reg},
    ]
    if intent_hint:
        messages.append({"role": "system", "content": intent_hint})
    messages.append({"role": "user", "content": user_message})

    payload = {
        "model": "ark-code-latest",
        "temperature": 0.1,
        "messages": messages,
        "tools": tools,
        "tool_choice": "auto",
    }
    total_json = json.dumps(payload, ensure_ascii=False)
    print(f"=== Payload size ===")
    print(f"  system_prompt:   {len(sys_prompt):>7d} chars")
    print(f"  cluster_reg:     {len(cluster_reg):>7d} chars")
    print(f"  intent_hint:     {len(intent_hint):>7d} chars")
    print(f"  user_message:    {len(user_message):>7d} chars")
    print(f"  tools count:     {len(tools)}")
    print(f"  tools schema:    {len(json.dumps(tools, ensure_ascii=False)):>7d} chars")
    print(f"  TOTAL payload:   {len(total_json):>7d} chars  ≈ {len(total_json)//4} tokens")
    print()
    # 看每个 tool 大小
    print("Top 5 largest tools:")
    sized = sorted([(t["function"]["name"], len(json.dumps(t, ensure_ascii=False))) for t in tools],
                   key=lambda x: -x[1])[:5]
    for name, sz in sized:
        print(f"  {sz:>5d}  {name}")


if __name__ == "__main__":
    main()

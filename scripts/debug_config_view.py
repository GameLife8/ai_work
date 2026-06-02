"""实测:config_view 查询时模型到底贴不贴 raw。

跑真实 agent.ask(打真实模型 + 真实 k8s),dump:
  - 意图分类
  - kube_query 返回里有没有可展示的 Corefile 原文(result.parsed)
  - 模型最终 message 有没有贴 ``` 代码块(raw)
  - 如果没贴,说明 prompt 没拦住 → 需要代码契约兜底
"""
from __future__ import annotations

import sys
sys.path.insert(0, "/app")

import json
import os
from runtime import create_runtime
from config import Config
from ops_agent import UnifiedOpsAgent
from ops_agent.agent import _classify_intent
from ops_agent.model_client import OpsModelClient

QUERY = os.getenv("DEBUG_QUERY", "codewave 这台机器的coredns 的配置是什么，请展示一下")
FORCE_MODEL = os.getenv("DEBUG_MODEL", "")   # 强制用某模型测(不改 DB 默认)


def main() -> None:
    rt = create_runtime(Config)
    agent = UnifiedOpsAgent(rt, max_steps=4)
    if FORCE_MODEL:
        d = rt.model_manager.get_default()
        agent.model = OpsModelClient(base_url=d["base_url"], api_key=d["api_key"],
                                     model=FORCE_MODEL, timeout_seconds=60)
        print(f"[强制模型] {FORCE_MODEL}")

    print(f"查询: {QUERY}")
    print(f"意图: {_classify_intent(QUERY)}")
    print("=" * 70)

    outcome = agent.ask(
        QUERY,
        user={"username": "admin", "role": "admin"},
        session_id="debug-config-view",
    )

    # 1. trace 里 kube_query 返回了什么
    print("\n=== TRACE(skill 调用)===")
    for item in outcome.trace:
        tool = item.get("tool_name")
        status = item.get("status")
        result = item.get("tool_result") or {}
        print(f"  [{tool}] status={status}")
        if tool == "kube_query":
            parsed = result.get("parsed")
            stdout = result.get("stdout", "")
            # ConfigMap 的 Corefile 在哪
            if isinstance(parsed, dict):
                data = parsed.get("data", {})
                if "Corefile" in data:
                    cf = data["Corefile"]
                    print(f"    ✓ result.parsed.data.Corefile 存在({len(cf)} 字符)")
                    print(f"    前 200 字: {cf[:200]!r}")
                else:
                    print(f"    parsed.data keys: {list(data.keys()) if isinstance(data,dict) else type(data)}")
            print(f"    stdout 长度: {len(stdout)}")

    # 2. 模型最终输出有没有贴 raw + 是模型自贴还是平台补
    msg = outcome.message
    has_code_block = "```" in msg
    platform_appended = "平台自动展示" in msg
    print("\n=== 模型最终 message ===")
    print(f"  长度: {len(msg)} 字符")
    print(f"  含 ``` 代码块(贴了 raw)? {has_code_block}")
    print(f"  含 'Corefile' 字样? {'Corefile' in msg}")
    model_name = getattr(agent.model, "model", "?")
    print("\n=== 判定 ===")
    if platform_appended:
        print(f"  ⚠️ 模型({model_name})**没自己贴**,靠平台 raw-first 契约补的")
    elif has_code_block:
        print(f"  ✅ 模型({model_name})**自己贴了** raw(指令遵循好,无需平台兜底)")
    else:
        print(f"  ❌ 模型({model_name})没贴 raw,平台契约也没补(异常,查 trace)")
    print("\n--- message 全文 ---")
    print(msg)

    # token
    print("\n=== token usage ===")
    print(json.dumps(outcome.usage, ensure_ascii=False))


if __name__ == "__main__":
    main()

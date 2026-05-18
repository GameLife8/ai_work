"""量化 system prompt + tools + 意图词典占用。"""
from __future__ import annotations
import json
from ops_platform.prompts import DEFAULTS
from ops_agent.agent import (
    _CONFIG_VIEW_VERBS, _CONFIG_VIEW_NOUNS,
    _INTENT_KEYWORDS, _INTENT_HINTS,
)


def main():
    print("=" * 70)
    print("意图分类词典（**仅在 Python 端做匹配，不进 prompt**）")
    print("=" * 70)
    print(f"config_view verbs: {len(_CONFIG_VIEW_VERBS)} 个  → {sum(len(v) for v in _CONFIG_VIEW_VERBS)} chars")
    print(f"config_view nouns: {len(_CONFIG_VIEW_NOUNS)} 个  → {sum(len(n) for n in _CONFIG_VIEW_NOUNS)} chars")
    other_kw = sum(len(kw) for kws in _INTENT_KEYWORDS.values() for kw in kws)
    other_n = sum(len(kws) for kws in _INTENT_KEYWORDS.values())
    print(f"其它 7 个意图: {other_n} 个  → {other_kw} chars")
    print(f"全部词典: {len(_CONFIG_VIEW_VERBS)+len(_CONFIG_VIEW_NOUNS)+other_n} 个，{sum(len(v) for v in _CONFIG_VIEW_VERBS)+sum(len(n) for n in _CONFIG_VIEW_NOUNS)+other_kw} chars")
    print()
    print("=" * 70)
    print("System prompt 段落分布（**这是真正进 prompt 的部分**）")
    print("=" * 70)
    total = 0
    for k, v in DEFAULTS.items():
        sz = len(v["content"])
        total += sz
        title = v.get("title", "")[:40]
        print(f"  {k:25s} {sz:>7d} chars   ← {title}")
    print(f"  {'TOTAL':25s} {total:>7d} chars  ≈ {total//4} tokens")
    print()
    print("=" * 70)
    print("Intent hints (动态注入 1 段)")
    print("=" * 70)
    for k, v in _INTENT_HINTS.items():
        print(f"  {k:15s} {len(v):>5d} chars")
    print(f"  每轮注入: 1 段 (~250 chars)")
    print()
    print("=" * 70)
    print("Tools schema (27 个 skill 全部 manifest)")
    print("=" * 70)
    from runtime import create_runtime
    from config import Config
    rt = create_runtime(Config)
    tools = rt.skill_registry.openai_tools(visibility=None)
    ts = sorted([(t["function"]["name"], len(json.dumps(t, ensure_ascii=False))) for t in tools], key=lambda x: -x[1])
    for n, sz in ts:
        print(f"  {sz:>5d}  {n}")
    tools_total = sum(s for _, s in ts)
    print(f"\n  Total: {tools_total} chars ≈ {tools_total//4} tokens")
    print()
    print("=" * 70)
    print("按意图过滤 tools 后的 schema 大小")
    print("=" * 70)
    from ops_agent.agent import _filter_tools_by_intent
    for intent in ("config_view", "list_state", "monitor", "write_action",
                   "async_task", "knowledge", "chat_intro", "diagnose", None):
        filtered, was = _filter_tools_by_intent(tools, intent)
        fsize = sum(len(json.dumps(t, ensure_ascii=False)) for t in filtered)
        skills = [t["function"]["name"] for t in filtered]
        print(f"  {str(intent):15s} → {len(filtered):2d} 个, {fsize:>6d} chars  (filtered={was})")
    print()

    print("=" * 70)
    print("汇总（单次请求）—— 用 config_view 意图举例")
    print("=" * 70)
    cluster_reg = 3670   # 实测
    user_msg = 30
    config_hint = len(__import__("ops_agent.agent", fromlist=["_INTENT_HINTS"])._INTENT_HINTS["config_view"])
    config_filtered, _ = _filter_tools_by_intent(tools, "config_view")
    config_tools_size = sum(len(json.dumps(t, ensure_ascii=False)) for t in config_filtered)

    print("【优化前】")
    payload_before = 12221 + cluster_reg + 250 + 20828 + user_msg
    print(f"  system prompt:    12221 chars")
    print(f"  cluster registry: {cluster_reg:>7d} chars")
    print(f"  intent hint:         250 chars (旧版)")
    print(f"  tools schema:     20828 chars (全 27 个)")
    print(f"  user message:        {user_msg:>4d} chars")
    print(f"  TOTAL:            {payload_before:>7d} chars  ≈ {payload_before//4} tokens")
    print()
    print("【优化后 - config_view 意图】")
    payload_after = total + cluster_reg + config_hint + config_tools_size + user_msg
    print(f"  system prompt:    {total:>7d} chars  (拆出 output_format 详细模式)")
    print(f"  cluster registry: {cluster_reg:>7d} chars")
    print(f"  intent hint:      {config_hint:>7d} chars (增强版含完整格式)")
    print(f"  tools schema:     {config_tools_size:>7d} chars ({len(config_filtered)} 个)")
    print(f"  user message:        {user_msg:>4d} chars")
    print(f"  TOTAL:            {payload_after:>7d} chars  ≈ {payload_after//4} tokens")
    print()
    saved = payload_before - payload_after
    print(f"  节省: {saved} chars  ≈ {saved//4} tokens  ({saved*100//payload_before}%)")


if __name__ == "__main__":
    main()

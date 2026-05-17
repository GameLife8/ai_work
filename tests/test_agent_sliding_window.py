"""Sliding-window 压缩 + signal-driven fallback 的回归测试。

这两条机制都不依赖真实 LLM/skill——纯测纯函数 + 用 FakeModel/FakeInvoker
拼出"模型空手而归"和"messages 巨大"的场景。
"""

from __future__ import annotations

import json
from typing import Any

from ops_agent.agent import (
    KEEP_TAIL_MESSAGES,
    MAX_MESSAGE_CHARS,
    UnifiedOpsAgent,
    _compress_history,
    _maybe_compress,
)


# ---------- _compress_history ---------- #

def _make_turn(idx: int, payload_size: int = 500) -> list[dict]:
    """造一个 turn 组：assistant(有 tool_calls) + 两个 tool 消息。

    payload_size 控制 tool 消息体长度（用来撑 messages 大小）。
    """
    return [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": f"c{idx}_a", "type": "function",
             "function": {"name": f"skill_a_{idx}", "arguments": "{}"}},
            {"id": f"c{idx}_b", "type": "function",
             "function": {"name": f"skill_b_{idx}", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": f"c{idx}_a", "content": "X" * payload_size},
        {"role": "tool", "tool_call_id": f"c{idx}_b", "content": "Y" * payload_size},
    ]


def test_compress_collapses_earliest_turn_when_room_to_spare():
    messages = (
        [{"role": "system", "content": "sys"}, {"role": "user", "content": "q"}]
        + _make_turn(1)            # 3 条
        + _make_turn(2)            # 3 条
        + _make_turn(3)            # 3 条
        + _make_turn(4)            # 3 条 → tail 至少 6 条
    )
    compressed = _compress_history(messages)
    # 第一轮 (turn 1) 应该被替换成单条 system 摘要
    assert len(compressed) == len(messages) - 3 + 1
    summary = compressed[2]
    assert summary["role"] == "system"
    assert "[历史摘要]" in summary["content"]
    assert "skill_a_1" in summary["content"]
    # head 保持不变
    assert compressed[0]["content"] == "sys"
    assert compressed[1]["content"] == "q"


def test_compress_no_op_when_tail_too_short():
    """如果压完后 tail 少于 KEEP_TAIL_MESSAGES，就保持原样——别压得过狠。"""
    messages = (
        [{"role": "system", "content": "sys"}, {"role": "user", "content": "q"}]
        + _make_turn(1)            # 唯一一轮
    )
    assert len(messages) - 2 < KEEP_TAIL_MESSAGES   # tail 不足
    assert _compress_history(messages) == messages


def test_compress_no_op_when_no_turn_group():
    """只有 system + user 时没东西可压。"""
    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "q"}]
    assert _compress_history(messages) == messages


def test_maybe_compress_iterates_until_under_threshold():
    """巨大 messages 应被多次压缩直到达标（或没法再压）。"""
    big_payload = MAX_MESSAGE_CHARS // 3      # 一个 turn 就够撑爆阈值
    messages = (
        [{"role": "system", "content": "sys"}, {"role": "user", "content": "q"}]
        + _make_turn(1, payload_size=big_payload)
        + _make_turn(2, payload_size=big_payload)
        + _make_turn(3, payload_size=big_payload)
        + _make_turn(4, payload_size=big_payload)
    )
    assert len(json.dumps(messages, ensure_ascii=False)) > MAX_MESSAGE_CHARS

    compressed = _maybe_compress(messages)

    # 已经被压过——尺寸应该明显小（不一定低于阈值，因为还要保留 tail）
    assert len(compressed) < len(messages)
    # 摘要 system 消息至少有一条
    assert any(m.get("role") == "system" and "[历史摘要]" in (m.get("content") or "")
               for m in compressed)


# ---------- _pick_fallback ---------- #

def test_pick_fallback_first_turn_uses_heuristic():
    out = UnifiedOpsAgent._pick_fallback(
        user_message="P-L-TIDB09 这台主机情况怎么样",
        trace=[],
        all_signals=[],
        force_routed_keys=set(),
    )
    assert out is not None
    name, args, reason = out
    assert name == "zabbix_get_host_overview"
    assert args == {"host_query": "P-L-TIDB09"}
    assert "启发式" in reason


def test_pick_fallback_mid_session_uses_critical_signal():
    """有 trace 时优先读 critical signal 推荐的 next_skill。"""
    out = UnifiedOpsAgent._pick_fallback(
        user_message="web 起不来",
        trace=[{"tool_name": "swarm_query", "tool_args": {}}],
        all_signals=[
            # 一条 warning + 一条 critical —— 应该选 critical
            {"type": "permission_denied", "severity": "warning",
             "next_skill": "swarm_query",
             "next_args": {"service_name": "web", "keyword": "denied"}},
            {"type": "oom_kill", "severity": "critical",
             "next_skill": "zabbix_get_host_overview",
             "next_args": {"host_query": "node-3"}},
        ],
        force_routed_keys=set(),
    )
    assert out is not None
    name, args, reason = out
    assert name == "zabbix_get_host_overview"
    assert args == {"host_query": "node-3"}
    assert "critical" in reason


def test_pick_fallback_skips_already_routed_keys():
    """同一个 (skill, args) 已经被 force-route 过，下次空轮不再选它。"""
    routed = {("zabbix_get_host_overview",
               json.dumps({"host_query": "node-3"}, sort_keys=True, ensure_ascii=False))}
    out = UnifiedOpsAgent._pick_fallback(
        user_message="web 起不来",
        trace=[{"tool_name": "swarm_query", "tool_args": {}}],
        all_signals=[
            {"type": "oom_kill", "severity": "critical",
             "next_skill": "zabbix_get_host_overview",
             "next_args": {"host_query": "node-3"}},
        ],
        force_routed_keys=routed,
    )
    # 没有别的可选 → 返回 None（模型继续给 final report）
    assert out is None


def test_pick_fallback_falls_through_warning_when_no_critical():
    out = UnifiedOpsAgent._pick_fallback(
        user_message="web",
        trace=[{"tool_name": "swarm_query", "tool_args": {}}],
        all_signals=[
            {"type": "permission_denied", "severity": "warning",
             "next_skill": "swarm_query",
             "next_args": {"service_name": "web", "keyword": "denied"}},
        ],
        force_routed_keys=set(),
    )
    assert out is not None
    name, _args, _reason = out
    assert name == "swarm_query"


def test_pick_fallback_skips_signals_without_next_skill():
    """没有 next_skill 的 signal（仅证据）不能驱动 pivot——避免给模型乱递空 args。"""
    out = UnifiedOpsAgent._pick_fallback(
        user_message="web",
        trace=[{"tool_name": "swarm_query", "tool_args": {}}],
        all_signals=[
            {"type": "high_cpu", "severity": "critical",
             "evidence": "host 'mystery' CPU 95%"},
            # 注意没有 next_skill
        ],
        force_routed_keys=set(),
    )
    assert out is None


def test_pick_fallback_returns_none_when_no_trace_and_no_keyword_match():
    """首轮模型空手 + user 没说出可启发式触发的关键词 → 不强行兜底。"""
    out = UnifiedOpsAgent._pick_fallback(
        user_message="你好",      # 不包含主机/服务关键词、没 JSON payload
        trace=[],
        all_signals=[],
        force_routed_keys=set(),
    )
    assert out is None


# ---------- end-to-end: agent uses signal fallback mid-session ---------- #


class _ModelEmitsToolThenGoesQuiet:
    """两轮：第一轮 emit OOM 信号；第二轮模型空手不调 tool。

    期望：agent 在第二轮命中 signal-driven fallback，自动调
    zabbix_get_host_overview。
    """

    def __init__(self) -> None:
        self.calls = 0

    def create_completion(self, *, messages, tools=None, tool_choice=None):
        self.calls += 1
        if self.calls == 1:
            return {
                "content": "",
                "tool_calls": [{
                    "id": "c1", "type": "function",
                    "function": {"name": "swarm_query",
                                 "arguments": json.dumps({"service_name": "web"})},
                }],
            }
        # 第二轮 / 第三轮：模型空手——agent 应该 force-route 到 OOM 信号建议
        if self.calls == 2:
            return {"content": "", "tool_calls": []}
        # 第三轮：上下文里已经有了 host_overview 结果——模型给最终报告
        return {"content": "host node-3 内存压力是根因", "tool_calls": []}


class _SignalEmittingInvoker:
    """第一次调 swarm_query 返回 OOM 信号；
    后续 zabbix_get_host_overview 返回 memory_used_percent=94。"""

    def __init__(self) -> None:
        self.invocations: list[tuple[str, dict]] = []

    def invoke(self, name, args, ctx):
        self.invocations.append((name, dict(args)))
        if name == "swarm_query":
            return {"skill": name, "status": "ok", "latency_ms": 1, "result": {
                "service_name": args.get("service_name"),
                "_signals": [{
                    "type": "oom_kill", "severity": "critical",
                    "evidence": "任务 web.1 退出码 137",
                    "next_skill": "zabbix_get_host_overview",
                    "next_args": {"host_query": "node-3"},
                    "context": {"node": "node-3"},
                }],
            }}
        if name == "zabbix_get_host_overview":
            return {"skill": name, "status": "ok", "latency_ms": 1, "result": {
                "host": {"host_name": args.get("host_query")},
                "memory_used_percent": 94,
            }}
        return {"skill": name, "status": "ok", "latency_ms": 1, "result": {}}

    @staticmethod
    def serialize_for_model(envelope):
        return json.dumps(envelope, ensure_ascii=False, default=str)


class _Reg:
    def openai_tools(self, visibility=None):
        return [{"type": "function", "function": {"name": "_any", "parameters": {}}}]


class _Store:
    def list_prompt_segments(self):
        return []


class _Runtime:
    def __init__(self) -> None:
        self.store = _Store()
        self.skill_registry = _Reg()
        self.skill_invoker = _SignalEmittingInvoker()
        self.model_manager = self
        self.connection_manager = None

    def get_client(self, _=None):
        return _ModelEmitsToolThenGoesQuiet()


def test_agent_force_routes_on_critical_signal_when_model_silent():
    runtime = _Runtime()
    agent = UnifiedOpsAgent(runtime, max_steps=5)
    agent.model = _ModelEmitsToolThenGoesQuiet()    # 覆盖掉 get_client 拿的实例
    outcome = agent.ask("web 起不来", user={"role": "admin"}, session_id="s1")

    invoked_names = [n for n, _ in runtime.skill_invoker.invocations]
    # 第一次模型调 swarm_query；第二次模型空手——agent 应该 force-route 到 host_overview
    assert invoked_names == ["swarm_query", "zabbix_get_host_overview"]
    # final report 来自第三轮（模型基于完整上下文给的总结）
    assert "node-3" in outcome.message or "内存压力" in outcome.message

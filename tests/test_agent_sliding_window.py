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

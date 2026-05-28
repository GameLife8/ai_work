"""OpsModelClient 边界契约：剥离厂商噪音字段，避免思考链跨轮泄漏。

背景
====
国产推理模型（doubao seed-2 / qwen3-thinking / deepseek-r1 等）会在响应里
返回 ``reasoning_content`` / ``thinking`` 等思考链字段，**单条可能上千 token**。

agent.py 当前调用方代码（``messages.append({"role":"assistant","content":...,
"tool_calls":...})``）已经显式过滤。但作为防御性设计，model_client 在边界处
**统一剥掉**——这样无论调用方未来怎么改，都不会再把噪音回写历史导致 token
雪崩。

本测试固化以下契约：
1. 返回的 message dict 永远不含 ``reasoning_content`` / ``thinking`` /
   ``thinking_content`` / ``chain_of_thought`` / ``cot`` 这类字段。
2. OpenAI 标准字段（``role`` / ``content`` / ``tool_calls`` / ``refusal``
   等）原样保留。
3. 即使响应里完全没有思考链，行为也正常（不抛错、不丢字段）。
"""

from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

from ops_agent.model_client import (
    OpsModelClient,
    _ASSISTANT_MESSAGE_ALLOWED_FIELDS,
    _strip_noise,
)


# ---------- _strip_noise 纯函数单测 ---------- #


def test_strip_removes_reasoning_content() -> None:
    """``reasoning_content``（doubao seed-2 / qwen3-thinking）必须剥掉。"""
    msg = {
        "role": "assistant",
        "content": "pong",
        "reasoning_content": "用户让我回复 pong，那我就回复 pong。" * 50,
    }
    out = _strip_noise(msg)
    assert "reasoning_content" not in out
    assert out["content"] == "pong"
    assert out["role"] == "assistant"


def test_strip_removes_thinking_variants() -> None:
    """覆盖 thinking / thinking_content / chain_of_thought / cot 各种别名。"""
    msg = {
        "role": "assistant",
        "content": "answer",
        "thinking": "deepseek-r1 风格",
        "thinking_content": "另一种命名",
        "chain_of_thought": "cot 完整文本",
        "cot": "短别名",
    }
    out = _strip_noise(msg)
    for noise in ("thinking", "thinking_content", "chain_of_thought", "cot"):
        assert noise not in out, f"字段 {noise!r} 未被剥离"


def test_strip_preserves_openai_standard_fields() -> None:
    """OpenAI 协议标准字段必须原样保留。"""
    msg = {
        "role": "assistant",
        "content": "调 tool 看看",
        "tool_calls": [{
            "id": "call_1",
            "type": "function",
            "function": {"name": "host_query", "arguments": '{"node":"n1"}'},
        }],
        "refusal": None,
        "reasoning_content": "这条应该被剥",
    }
    out = _strip_noise(msg)
    assert out["role"] == "assistant"
    assert out["content"] == "调 tool 看看"
    assert out["tool_calls"][0]["function"]["name"] == "host_query"
    assert out["refusal"] is None
    assert "reasoning_content" not in out


def test_strip_is_noop_when_no_noise() -> None:
    """没有噪音字段时，行为等价于浅拷贝。"""
    msg = {"role": "assistant", "content": "hi", "tool_calls": None}
    out = _strip_noise(msg)
    assert out == msg
    assert out is not msg, "应返回新 dict，避免调用方意外原位修改"


def test_strip_handles_empty_noise_values() -> None:
    """空字符串 / None 的噪音字段也要剥掉（防止下游误以为有内容）。"""
    msg = {
        "role": "assistant",
        "content": "ok",
        "reasoning_content": "",
        "thinking": None,
    }
    out = _strip_noise(msg)
    assert "reasoning_content" not in out
    assert "thinking" not in out


def test_strip_drops_unknown_vendor_fields() -> None:
    """任何不在白名单里的字段——哪怕是没列名的新厂商字段——都剥掉。"""
    msg = {
        "role": "assistant",
        "content": "x",
        "audio": {"transcript": "x", "data": "AAAA..." * 1000},   # 多模态噪音
        "vendor_internal_token": "敏感字段",
    }
    out = _strip_noise(msg)
    assert "audio" not in out
    assert "vendor_internal_token" not in out


def test_allowed_fields_constant_covers_openai_essentials() -> None:
    """白名单常量必须覆盖 OpenAI 协议核心字段——防止后续误改导致回归。"""
    must_keep = {"role", "content", "tool_calls", "tool_call_id", "refusal"}
    assert must_keep.issubset(_ASSISTANT_MESSAGE_ALLOWED_FIELDS)


# ---------- create_completion 端到端：mock requests 验证调用链 ---------- #


def _mock_response(message: dict, status_code: int = 200) -> MagicMock:
    fake = MagicMock()
    fake.status_code = status_code
    fake.json.return_value = {"choices": [{"message": message}]}
    fake.raise_for_status = MagicMock()
    return fake


def test_create_completion_strips_noise_end_to_end() -> None:
    """端到端：mock 一个带 reasoning_content 的响应，验证返回值已经剥干净。"""
    client = OpsModelClient(
        base_url="https://ark.example.com/api/v3",
        api_key="sk-test",
        model="doubao-seed-2-0-pro-260215",
        timeout_seconds=30,
    )
    fake_msg = {
        "role": "assistant",
        "content": "pong",
        "reasoning_content": "用户让我回复 pong" * 200,   # 模拟大段思考链
        "thinking": "另一种厂商命名",
    }
    with patch("ops_agent.model_client.requests.post",
               return_value=_mock_response(fake_msg)) as mock_post:
        out = client.create_completion(messages=[{"role": "user", "content": "ping"}])

    assert mock_post.called
    assert "reasoning_content" not in out, "create_completion 必须在边界剥噪音"
    assert "thinking" not in out
    assert out["content"] == "pong"


def test_create_completion_preserves_tool_calls() -> None:
    """剥噪音时不能误伤 tool_calls——它是 agent 循环的命脉。"""
    client = OpsModelClient(
        base_url="https://ark.example.com/api/v3",
        api_key="sk-test",
        model="doubao-seed-2-0-pro-260215",
    )
    fake_msg = {
        "role": "assistant",
        "content": "",
        "reasoning_content": "决定先查容器",
        "tool_calls": [{
            "id": "call_42",
            "type": "function",
            "function": {"name": "host_run_command",
                         "arguments": '{"node":"P-TPM-01","command":"docker ps"}'},
        }],
    }
    with patch("ops_agent.model_client.requests.post",
               return_value=_mock_response(fake_msg)):
        out = client.create_completion(messages=[{"role": "user", "content": "查容器"}])
    assert "reasoning_content" not in out
    assert out["tool_calls"][0]["id"] == "call_42"
    assert out["tool_calls"][0]["function"]["name"] == "host_run_command"


def test_create_completion_returns_clean_dict_for_appending_to_history() -> None:
    """**核心契约**：返回的 dict 直接 ``messages.append(out)`` 也不会带入噪音。

    这是 agent.py 未来重构时的安全网——哪怕调用方一时手快没拆开取字段，
    history 也不会被污染。
    """
    client = OpsModelClient(base_url="https://x/api/v3", api_key="k", model="m")
    fake_msg = {
        "role": "assistant",
        "content": "final answer",
        "reasoning_content": "千字思考链" * 500,
    }
    with patch("ops_agent.model_client.requests.post",
               return_value=_mock_response(fake_msg)):
        out = client.create_completion(messages=[{"role": "user", "content": "?"}])

    # 模拟"未来某次重构"把整条消息回写历史
    history: list[dict] = []
    history.append(out)

    # 即便整条回写，下一轮 API payload 也不会包含 reasoning_content
    serialized = str(history)
    assert "reasoning_content" not in serialized
    assert "千字思考链" not in serialized

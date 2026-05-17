"""会话记忆（同会话多轮上下文 + 自动摘要）回归测试。

跑独立的 InMemoryStore + 假 model client，验证：
  1. ask() 二次提问时，messages 里能看到上一轮的 user/assistant 原文
  2. 历史超过 SUMMARIZE_AT 阈值后，自动调模型生成摘要并落库 role=summary
  3. 摘要后的下一轮 ask()，messages 里有 system 摘要段 + 最近 N 条原文
  4. tool_calls / tool 响应不带到下一轮（避免撑爆 context + 旧数据误导）
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from models.db import InMemoryStore
from ops_agent.agent import (
    CHAT_HISTORY_RECENT,
    CHAT_HISTORY_SUMMARIZE_AT,
    UnifiedOpsAgent,
)
from ops_platform import (
    ConnectionManager,
    ModelManager,
    SkillInvoker,
    SkillRegistry,
)
from ops_platform.runbook_engine import RunbookRegistry
from ops_platform.store import attach_platform_store


class FakeModel:
    """记录每次调用 messages；可预置返回值。"""

    def __init__(self, replies: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.replies = list(replies or [])

    def create_completion(self, messages, tools=None, tool_choice=None):
        self.calls.append({"messages": [dict(m) for m in messages]})
        if self.replies:
            return self.replies.pop(0)
        # 默认：assistant 直接给一段文本，不调 tool
        return {"role": "assistant", "content": "(default fake reply)"}


def _make_runtime(model: FakeModel):
    """造一个最小可用 runtime：InMemoryStore + fake model + 空 skill registry。"""
    store = InMemoryStore()
    attach_platform_store(store)

    class _Runtime:
        pass

    runtime = _Runtime()
    runtime.store = store
    runtime.skill_registry = SkillRegistry()
    runtime.skill_invoker = SkillInvoker(runtime.skill_registry, store)
    runtime.connection_manager = ConnectionManager(store)
    runtime.connection_manager.attach_runtime(runtime)
    runtime.model_manager = ModelManager(store)
    runtime.runbook_registry = RunbookRegistry(runtime)
    return runtime, store


def _make_agent(runtime, model: FakeModel) -> UnifiedOpsAgent:
    """跳过正常构造（要 ModelManager 拉 default model），直接拼好对象。"""
    agent = object.__new__(UnifiedOpsAgent)
    agent.runtime = runtime
    agent.max_steps = 3
    agent.model = model
    agent.registry = runtime.skill_registry
    agent.invoker = runtime.skill_invoker
    return agent


def test_history_is_injected_into_messages_after_second_ask():
    """第二轮 ask() 应该把第一轮的 user + assistant 消息塞进 messages。"""
    model = FakeModel(replies=[
        {"role": "assistant", "content": "好的，我已记下：你说集群 X 出问题"},
        {"role": "assistant", "content": "已综合上下文给出建议"},
    ])
    runtime, store = _make_runtime(model)
    agent = _make_agent(runtime, model)
    session = "s-mem-1"

    # 模拟 chainlit 流程：先 save user，再 ask
    store.save_chat_message(session, "user", "查一下集群 X 状态")
    out1 = agent.ask("查一下集群 X 状态", session_id=session)
    store.save_chat_message(session, "assistant", out1.message)

    store.save_chat_message(session, "user", "刚才那个集群再看下磁盘")
    agent.ask("刚才那个集群再看下磁盘", session_id=session)

    # 第二次模型被调时，messages 里应该出现第一轮的内容
    msgs = model.calls[-1]["messages"]
    flat = "\n".join(f"{m.get('role')}: {m.get('content', '')}" for m in msgs)
    assert "查一下集群 X 状态" in flat
    assert "我已记下" in flat or "集群 X" in flat
    # 当前用户消息必然是最后一条
    assert msgs[-1]["role"] == "user"
    assert msgs[-1]["content"] == "刚才那个集群再看下磁盘"


def test_history_excludes_tool_call_messages():
    """tool_calls / tool 响应不应进入下一轮 history（防 context 爆炸 + 旧数据误导）。"""
    model = FakeModel(replies=[{"role": "assistant", "content": "查完了"}])
    runtime, store = _make_runtime(model)
    agent = _make_agent(runtime, model)
    session = "s-mem-2"

    # 模拟一段假的对话历史：用户、助手（含 tool_calls 痕迹）、tool 响应、最终助手回复
    store.save_chat_message(session, "user", "查 pod")
    store.save_chat_message(session, "assistant", "已查到 5 个 pod",
                            trace=[{"tool_name": "kube_query", "tool_args": {}}])

    store.save_chat_message(session, "user", "再确认一下")
    agent.ask("再确认一下", session_id=session)

    msgs = model.calls[-1]["messages"]
    # 不应该出现 role=tool 的消息
    assert not any(m.get("role") == "tool" for m in msgs)
    # 但 assistant 文本应该带进来
    assert any("已查到 5 个 pod" in (m.get("content") or "") for m in msgs)


def test_summary_triggers_when_history_exceeds_threshold():
    """攒满 30+ 条历史后，应该自动调模型生成摘要并落库。"""
    summary_reply = "用户先后查了 X / Y 服务，定位到 OOM；用户偏好维护窗口外不重启。"
    model = FakeModel(replies=[
        {"role": "assistant", "content": summary_reply},   # 第一次调用是摘要
        {"role": "assistant", "content": "已综合摘要回复"},  # 第二次调用是真正的回答
    ])
    runtime, store = _make_runtime(model)
    agent = _make_agent(runtime, model)
    session = "s-mem-3"

    # 灌入 32 条对话历史（超过 CHAT_HISTORY_SUMMARIZE_AT=30）
    for i in range(16):
        store.save_chat_message(session, "user", f"用户第 {i+1} 个问题")
        store.save_chat_message(session, "assistant", f"助手第 {i+1} 个回答")

    store.save_chat_message(session, "user", "现在再问一个")
    agent.ask("现在再问一个", session_id=session)

    # 验证 summary 已落库
    latest = store.get_latest_chat_summary(session)
    assert latest is not None
    assert latest["content"] == summary_reply
    meta = latest.get("metadata_json") or {}
    assert meta.get("covers_until_id", 0) > 0
    assert meta.get("summarized_count", 0) > 0

    # 验证最后一次模型调用的 messages 里有 system 注入的摘要 + 最近 N 条
    msgs = model.calls[-1]["messages"]
    has_summary_system = any(
        m.get("role") == "system" and summary_reply in (m.get("content") or "")
        for m in msgs
    )
    assert has_summary_system, "摘要应该作为 system 消息注入"
    # 最近 N 条原文里应该有"用户第 16 个问题" / "助手第 16 个回答"
    flat = "\n".join((m.get("content") or "") for m in msgs)
    assert "用户第 16 个问题" in flat
    assert "助手第 16 个回答" in flat


def test_no_history_no_summary_for_fresh_session():
    """全新会话不应该读到任何 history / summary，messages 长度只有基础 system + 当前 user。"""
    model = FakeModel(replies=[{"role": "assistant", "content": "你好"}])
    runtime, _ = _make_runtime(model)
    agent = _make_agent(runtime, model)

    # 注意：fresh session 不调 save_chat_message 模拟"刚开会话"
    agent.ask("你好", session_id="s-fresh-1")

    msgs = model.calls[-1]["messages"]
    # 基础 prompt 2 条 system + 当前 1 条 user
    assert msgs[-1]["role"] == "user"
    assert msgs[-1]["content"] == "你好"
    # 之前的 messages 应该都是 system（无历史 user/assistant）
    assert all(m.get("role") == "system" for m in msgs[:-1])

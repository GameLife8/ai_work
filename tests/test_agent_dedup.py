"""验证 agent 在同一轮内对重复 tool_call 做了去重。

场景：模型偶尔会在一个 turn 里 emit 两个完全相同的 ``tool_calls`` 条目
（参数完全一样）。之前 agent 对每个都跑一次 ``invoke``，造成：
  - 重复审计行
  - 重复 LLM token 消耗
  - 写操作潜在重复执行风险（虽然 pending_action 机制兜底了）

agent.py 现在用 ``turn_call_cache`` 缓存 ``(name, args_json)`` → envelope；
本测试守住该行为。
"""

from __future__ import annotations

import json
from typing import Any

from ops_agent.agent import UnifiedOpsAgent, AgentOutcome


class _FakeModel:
    """两段式：第一轮发两个相同 tool_call，第二轮直接给文本。"""

    def __init__(self) -> None:
        self.calls = 0

    def create_completion(self, *, messages, tools=None, tool_choice=None):
        self.calls += 1
        if self.calls == 1:
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "function": {"name": "echo", "arguments": json.dumps({"x": 1})},
                    },
                    {
                        # 同 name + 同 args；agent 应当从缓存里复用
                        "id": "call_2",
                        "function": {"name": "echo", "arguments": json.dumps({"x": 1})},
                    },
                ],
            }
        return {"content": "done", "tool_calls": []}


class _FakeInvoker:
    """记录每次真正进入 ``invoke`` 的调用，方便验证去重。"""

    def __init__(self) -> None:
        self.real_invocations: list[tuple[str, dict[str, Any]]] = []

    def invoke(self, name, args, ctx):
        self.real_invocations.append((name, dict(args)))
        return {"skill": name, "status": "ok", "result": {"echoed": args}, "latency_ms": 1}

    @staticmethod
    def serialize_for_model(envelope):
        return json.dumps(envelope, ensure_ascii=False, default=str)


class _FakeRegistry:
    def openai_tools(self, visibility=None):
        return [{"type": "function", "function": {"name": "echo", "parameters": {}}}]


class _FakeStore:
    def list_prompt_segments(self):
        return []


class _FakeRuntime:
    def __init__(self) -> None:
        self.store = _FakeStore()
        self.skill_registry = _FakeRegistry()
        self.skill_invoker = _FakeInvoker()
        self.model_manager = self
        self.connection_manager = None  # SkillContext 不会真用

    def get_client(self, _id=None):
        return _FakeModel()


def test_agent_dedups_duplicate_tool_calls_in_same_turn():
    runtime = _FakeRuntime()
    agent = UnifiedOpsAgent(runtime, max_steps=3)
    # 替换 model 为我们的 fake——避免 model_manager.get_client 在测试期搜真模型
    fake_model = _FakeModel()
    agent.model = fake_model

    outcome = agent.ask("test", user={"role": "admin"}, session_id="s1")

    assert isinstance(outcome, AgentOutcome)
    # 两次 tool_call 同 name+args → 实际 invoke 只发生 1 次（去重命中）
    assert len(runtime.skill_invoker.real_invocations) == 1
    # 但 trace 里仍记录 2 条（追溯模型行为）
    assert len(outcome.trace) == 2
    assert outcome.trace[0]["tool_args"] == outcome.trace[1]["tool_args"]

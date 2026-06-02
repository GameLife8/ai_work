"""Agent runbook 强路由的端到端测试。

核心契约
========

用户消息命中某个 enabled runbook 的 trigger 关键词时:

  1. **绕过 tool loop**——模型一次 ``create_completion`` 都不调
  2. **强制调** ``platform_run_runbook(user_query=用户原话, inputs={})``
  3. ``AgentOutcome.message`` = runbook 跑完后的 ``final_report``
  4. ``trace`` 只有 1 条:``platform_run_runbook``
  5. 没命中任何 runbook 的消息走原 tool loop(模型自由发挥)

这条路由就是对"国产模型不主动调 runbook"病根的硬修复——
让标准化流程靠平台代码强制,而不是靠 prompt 提醒模型。
"""

from __future__ import annotations

import json
from typing import Any

from ops_agent.agent import AgentOutcome, UnifiedOpsAgent


# ---------- fakes ---------- #


class _StubRunbook:
    """RunbookRegistry.get / match_by_query 返回的对象只用到 ``key``。"""

    def __init__(self, key: str) -> None:
        self.key = key


class _Registry:
    """match_by_query 按预设响应 ——既能装命中也能装不命中。"""

    def __init__(self, match: _StubRunbook | None = None) -> None:
        self._match = match
        self.calls: list[str] = []

    def match_by_query(self, q: str) -> _StubRunbook | None:
        self.calls.append(q)
        return self._match

    def match_all_by_query(self, q: str) -> list:
        self.calls.append(q)
        return [self._match] if self._match else []


class _Invoker:
    """记录所有 invoke 调用,可装 platform_run_runbook 的 envelope 返回。"""

    def __init__(self, *, final_report: str = "stub-report") -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._final_report = final_report

    def invoke(self, name: str, args: dict, ctx):
        self.calls.append((name, dict(args)))
        if name == "platform_run_runbook":
            return {
                "skill": "platform_run_runbook",
                "status": "ok",
                "latency_ms": 1234,
                "result": {
                    "runbook_key": "cluster_health_audit_swarm",
                    "global_status": "done",
                    "abort_reason": None,
                    "total_ms": 1200,
                    "node_states": [],
                    "_signals": [],
                    "final_report": self._final_report,
                },
            }
        return {"skill": name, "status": "ok", "result": {}, "latency_ms": 1}

    @staticmethod
    def serialize_for_model(envelope: dict) -> str:
        return json.dumps(envelope, ensure_ascii=False, default=str)


class _SkillRegistry:
    def openai_tools(self, visibility=None):
        return [{"type": "function", "function": {"name": "swarm_query", "parameters": {}}}]


class _Store:
    def list_prompt_segments(self):
        return []

    def save_chat_message(self, *a, **kw):
        pass

    def list_chat_messages(self, *a, **kw):
        return []

    def count_chat_messages(self, *a, **kw):
        return 0

    def get_latest_chat_summary(self, *a, **kw):
        return None


class _Model:
    """记录 create_completion 是否被调用——预路由命中时应该 0 次。"""

    def __init__(self) -> None:
        self.completions = 0

    def create_completion(self, *, messages, tools=None, tool_choice=None):
        self.completions += 1
        return {"content": "(fallback path)", "tool_calls": []}


class _Runtime:
    def __init__(self, *, match: _StubRunbook | None,
                 final_report: str = "stub-report") -> None:
        self.store = _Store()
        self.skill_registry = _SkillRegistry()
        self.skill_invoker = _Invoker(final_report=final_report)
        self.runbook_registry = _Registry(match=match)
        self.connection_manager = None
        # model_manager.get_client(...) 在 __init__ 时被调,返回 self._model
        self._model = _Model()

    def get_client(self, _id=None):
        return self._model

    @property
    def model_manager(self):
        return self


# ---------- 测试用例 ---------- #


def test_preroute_hits_runbook_skips_tool_loop() -> None:
    """触发命中:模型 0 次 completion,只调一次 platform_run_runbook,trace 1 条。"""
    rt = _Runtime(
        match=_StubRunbook("cluster_health_audit_swarm"),
        final_report="# 表格化报告 ...",
    )
    agent = UnifiedOpsAgent(rt)
    out = agent.ask(
        "sws-swarm 集群巡检一下,看看宿主机内存cpu 硬盘",
        user={"role": "admin"}, session_id="s1",
    )

    assert isinstance(out, AgentOutcome)
    # 1) 没经过 model.create_completion(模型零参与决策)
    assert rt._model.completions == 0, "预路由命中时不该调模型"
    # 2) invoker 只被调了一次,而且是 platform_run_runbook
    assert len(rt.skill_invoker.calls) == 1
    name, args = rt.skill_invoker.calls[0]
    assert name == "platform_run_runbook"
    assert args["user_query"] == "sws-swarm 集群巡检一下,看看宿主机内存cpu 硬盘"
    assert args["inputs"] == {}
    # 3) trace 只有 1 条,就是这次 platform_run_runbook
    assert len(out.trace) == 1
    assert out.trace[0]["tool_name"] == "platform_run_runbook"
    # 4) message = runbook 的 final_report,直接传给用户
    assert out.message == "# 表格化报告 ..."
    # 5) 没有任何 pending_action
    assert out.pending_actions == []


def test_preroute_misses_falls_through_to_tool_loop() -> None:
    """未命中:走原 tool loop,模型会被调用至少一次。"""
    rt = _Runtime(match=None)  # registry 永远 None
    agent = UnifiedOpsAgent(rt, max_steps=1)
    agent.ask("帮我看下某 pod 的日志", user={"role": "admin"}, session_id="s2")

    # 没命中 → 模型至少跑了一轮(因为我们的 _Model 返回空 tool_calls 让循环立刻结束)
    assert rt._model.completions >= 1
    # invoker 没被调 platform_run_runbook
    assert all(c[0] != "platform_run_runbook" for c in rt.skill_invoker.calls)


def test_preroute_runbook_no_final_report_returns_friendly_fallback() -> None:
    """Runbook 跑了但 final_report 为空 → 给用户友好兜底,不是空白。"""
    rt = _Runtime(
        match=_StubRunbook("cluster_health_audit_swarm"),
        final_report="",   # 模拟报告生成失败
    )
    agent = UnifiedOpsAgent(rt)
    out = agent.ask("巡检", user={"role": "admin"}, session_id="s3")

    assert out.message  # 不能空白
    # 兜底文案应该提到 runbook key 和让用户去查审计
    assert "cluster_health_audit_swarm" in out.message
    assert ("global_status" in out.message
            or "审计" in out.message
            or "node_states" in out.message)


def test_preroute_handles_registry_exception_gracefully() -> None:
    """RunbookRegistry.match_by_query 抛异常时不能崩——降级回 tool loop。"""

    class _FlakyRegistry:
        def match_by_query(self, q):
            raise RuntimeError("DB 临时挂了")

        def match_all_by_query(self, q):
            raise RuntimeError("DB 临时挂了")

    rt = _Runtime(match=None)
    rt.runbook_registry = _FlakyRegistry()
    agent = UnifiedOpsAgent(rt, max_steps=1)
    # 不应抛异常
    out = agent.ask("巡检", user={"role": "admin"}, session_id="s4")
    assert isinstance(out, AgentOutcome)
    # 降级走 tool loop,模型应该被调
    assert rt._model.completions >= 1

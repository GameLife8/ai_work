from __future__ import annotations

import json
from typing import Any

from ops_agent.agent import UnifiedOpsAgent


class _ResumeModel:
    def __init__(self, script: list[dict[str, Any]]) -> None:
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []

    def create_completion(self, *, messages, tools=None, tool_choice=None):
        self.requests.append({
            "messages": json.loads(json.dumps(messages, ensure_ascii=False, default=str)),
            "tools": tools,
            "tool_choice": tool_choice,
        })
        if not self.script:
            return {"content": "done", "tool_calls": []}
        return self.script.pop(0)


class _ResumeInvoker:
    def __init__(self) -> None:
        self.real_invocations: list[tuple[str, dict[str, Any]]] = []

    def invoke(self, name, args, ctx):
        self.real_invocations.append((name, dict(args)))
        if name == "host_run_command":
            return {
                "status": "needs_confirmation",
                "pending_token": "tok-write",
                "preview": {
                    "skill_code": name,
                    "skill_name": name,
                    "args": args,
                },
                "latency_ms": None,
            }
        return {
            "status": "ok",
            "result": {"checked_after_write": True, "args": args},
            "latency_ms": 7,
        }

    @staticmethod
    def serialize_for_model(envelope):
        return json.dumps(envelope, ensure_ascii=False, default=str)


class _ResumeRegistry:
    def openai_tools(self, visibility=None):
        return [
            {"type": "function", "function": {"name": "host_run_command", "parameters": {}}},
            {"type": "function", "function": {"name": "swarm_query", "parameters": {}}},
        ]


class _ResumeStore:
    def __init__(self, history: list[dict[str, Any]] | None = None) -> None:
        self.history = history or []

    def list_prompt_segments(self):
        return []

    def count_chat_messages(self, *args, **kwargs):
        return len(self.history)

    def get_latest_chat_summary(self, *args, **kwargs):
        return None

    def list_chat_messages(self, *args, **kwargs):
        return list(self.history)


class _ConnectionManager:
    def __init__(self) -> None:
        self.records = {
            "conn-swarm": {
                "id": "conn-swarm",
                "type_code": "swarm",
                "name": "SWS Swarm",
                "alias": "SWS",
            },
        }

    def list(self):
        return []

    def get(self, connection_id):
        return self.records.get(connection_id)


class _RunbookRegistry:
    def match_all_by_query(self, _query):
        return []


class _ResumeRuntime:
    def __init__(self, model: _ResumeModel, *, history: list[dict[str, Any]] | None = None) -> None:
        self.store = _ResumeStore(history)
        self.skill_registry = _ResumeRegistry()
        self.skill_invoker = _ResumeInvoker()
        self.model_manager = self
        self.connection_manager = _ConnectionManager()
        self.runbook_registry = _RunbookRegistry()
        self._model = model

    def get_client(self, _id=None):
        return self._model


def _tool_call(call_id: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
    }


def test_connectivity_probe_forces_host_confirmation_after_swarm_ps():
    model = _ResumeModel([
        {"content": "", "tool_calls": [
            _tool_call(
                "call_ps", "swarm_query",
                {"category": "service", "verb": "ps", "name": "iiot-resource_resource-service"},
            ),
        ]},
        {"content": "", "tool_calls": [
            _tool_call(
                "call_host", "host_run_command",
                {
                    "node": "worker4.chinasws.com",
                    "command": (
                        "PID=$(docker inspect -f '{{.State.Pid}}' "
                        "$(docker ps -q --filter name=iiot-resource_resource-service.1 | head -1)); "
                        "timeout 6 nsenter -t $PID -n nc -zv -w3 169.24.7.36 4000 2>&1"
                    ),
                },
            ),
        ]},
        {"content": "请在下方点击 ✅ 确认 或 ❌ 取消", "tool_calls": []},
    ])
    runtime = _ResumeRuntime(model)

    class _ConnectivityInvoker(_ResumeInvoker):
        def invoke(self, name, args, ctx):
            self.real_invocations.append((name, dict(args)))
            if name == "swarm_query":
                return {
                    "status": "ok",
                    "result": {
                        "stdout": json.dumps({
                            "Name": "iiot-resource_resource-service.1",
                            "Node": "worker4.chinasws.com",
                            "DesiredState": "Running",
                            "CurrentState": "Running 3 weeks ago",
                        }, ensure_ascii=False),
                    },
                    "latency_ms": 9,
                }
            return super().invoke(name, args, ctx)

    runtime.skill_invoker = _ConnectivityInvoker()
    agent = UnifiedOpsAgent(runtime, max_steps=3)

    out = agent.ask(
        "sws-swarm 中 resource 的服务能够访问 169.24.7.36 4000 这个接口么",
        user={"username": "admin", "role": "admin"},
        session_id="sess-connectivity",
    )

    assert out.pending_actions
    assert runtime.skill_invoker.real_invocations[-1][0] == "host_run_command"
    forced = model.requests[1]
    assert forced["tool_choice"] == "required"
    assert [t["function"]["name"] for t in forced["tools"]] == ["host_run_command"]
    forced_prompt = json.dumps(forced["messages"], ensure_ascii=False)
    assert "容器/服务连通性探测必须进入 host_run_command" in forced_prompt


def test_resume_after_confirm_continues_tool_loop_and_updates_trace():
    model = _ResumeModel([
        {"content": "", "tool_calls": [
            _tool_call("call_write", "host_run_command", {"node": "n1", "command": "true"}),
        ]},
        {"content": "please confirm", "tool_calls": []},
        {"content": "", "tool_calls": [
            _tool_call("call_read", "swarm_query", {"query": "service ps"}),
        ]},
        {"content": "ready to summarize", "tool_calls": []},
        {"content": "final after resumed tool loop", "tool_calls": []},
    ])
    runtime = _ResumeRuntime(model)
    agent = UnifiedOpsAgent(runtime, max_steps=4)

    first = agent.ask(
        "run command then verify",
        user={"username": "admin", "role": "admin"},
        session_id="s1",
    )

    assert first.pending_actions
    assert first.resume_state
    assert runtime.skill_invoker.real_invocations == [
        ("host_run_command", {"node": "n1", "command": "true"}),
    ]

    confirmed = {
        "status": "ok",
        "result": {"stdout": "command completed"},
        "latency_ms": 13,
    }
    resumed = agent.resume(
        first.resume_state,
        [("tok-write", confirmed)],
        user={"username": "admin", "role": "admin"},
    )

    assert resumed.message == "final after resumed tool loop"
    assert runtime.skill_invoker.real_invocations[-1] == (
        "swarm_query", {"query": "service ps"},
    )
    assert resumed.trace[0]["status"] == "ok"
    assert resumed.trace[0]["tool_result"] == {"stdout": "command completed"}
    assert resumed.trace[0]["pending_token"] is None
    assert resumed.trace[0]["resolved_pending_token"] == "tok-write"

    resume_request = model.requests[2]
    write_tool_messages = [
        m for m in resume_request["messages"]
        if m.get("role") == "tool" and m.get("tool_call_id") == "call_write"
    ]
    assert write_tool_messages
    assert json.loads(write_tool_messages[0]["content"])["status"] == "ok"


def test_resume_state_maps_duplicate_pending_to_all_tool_call_ids():
    model = _ResumeModel([
        {"content": "", "tool_calls": [
            _tool_call("call_write_1", "host_run_command", {"node": "n1", "command": "true"}),
            _tool_call("call_write_2", "host_run_command", {"node": "n1", "command": "true"}),
        ]},
        {"content": "please confirm", "tool_calls": []},
        {"content": "after confirm", "tool_calls": []},
        {"content": "summary", "tool_calls": []},
    ])
    runtime = _ResumeRuntime(model)
    agent = UnifiedOpsAgent(runtime, max_steps=2)

    first = agent.ask(
        "run duplicate command",
        user={"username": "admin", "role": "admin"},
        session_id="s1",
    )

    assert len(first.pending_actions) == 1
    assert first.resume_state["pending_map"]["tok-write"] == [
        "call_write_1",
        "call_write_2",
    ]
    assert len(runtime.skill_invoker.real_invocations) == 1

    agent.resume(
        first.resume_state,
        [("tok-write", {"status": "ok", "result": {"stdout": "done"}})],
        user={"username": "admin", "role": "admin"},
    )

    resume_request = model.requests[2]
    write_tool_messages = [
        m for m in resume_request["messages"]
        if m.get("role") == "tool" and m.get("tool_call_id") in {"call_write_1", "call_write_2"}
    ]
    assert len(write_tool_messages) == 2
    assert {json.loads(m["content"])["status"] for m in write_tool_messages} == {"ok"}


def test_resume_state_strips_session_history_but_ask_still_uses_it():
    history = [
        {"role": "user", "content": "上一轮用户问题：看一下 old-service"},
        {"role": "assistant", "content": "上一轮助手回答：old-service 在 worker1"},
    ]
    model = _ResumeModel([
        {"content": "", "tool_calls": [
            _tool_call("call_write", "host_run_command", {"node": "n1", "command": "true"}),
        ]},
        {"content": "please confirm", "tool_calls": []},
        {"content": "after confirm", "tool_calls": []},
        {"content": "summary", "tool_calls": []},
    ])
    runtime = _ResumeRuntime(model, history=history)
    agent = UnifiedOpsAgent(runtime, max_steps=2)

    first = agent.ask(
        "当前这一轮要执行命令",
        user={"username": "admin", "role": "admin"},
        session_id="s-with-history",
        selected_connections={"swarm": "conn-swarm"},
    )

    first_request_text = json.dumps(model.requests[0]["messages"], ensure_ascii=False)
    assert "上一轮用户问题" in first_request_text
    assert "上一轮助手回答" in first_request_text

    resume_state_text = json.dumps(first.resume_state["messages"], ensure_ascii=False)
    assert "上一轮用户问题" not in resume_state_text
    assert "上一轮助手回答" not in resume_state_text
    assert "当前这一轮要执行命令" in resume_state_text
    assert "SWS" in resume_state_text
    assert first.resume_state["resume_slimmed"] is True
    assert first.resume_state["resume_message_chars"] < first.resume_state["original_message_chars"]

    agent.resume(
        first.resume_state,
        [("tok-write", {"status": "ok", "result": {"stdout": "done"}})],
        user={"username": "admin", "role": "admin"},
    )

    resume_request_text = json.dumps(model.requests[2]["messages"], ensure_ascii=False)
    assert "上一轮用户问题" not in resume_request_text
    assert "上一轮助手回答" not in resume_request_text
    assert "当前这一轮要执行命令" in resume_request_text

from __future__ import annotations

import inspect

import admin_app.blueprint as blueprint


def test_agent_ask_returns_resume_state_for_external_clients():
    src = inspect.getsource(blueprint.agent_ask)
    assert '"resume_state"' in src
    assert 'getattr(outcome, "resume_state", None)' in src


def test_agent_resume_endpoint_confirms_tokens_server_side():
    assert hasattr(blueprint, "agent_resume")
    src = inspect.getsource(blueprint.agent_resume)
    assert "_runtime().skill_invoker.confirm" in src
    assert "_runtime().skill_invoker.reject" in src
    assert "agent.resume" in src
    assert "user=g.current_user" in src
    assert "decision.get(\"envelope\")" not in src, (
        "Admin resume API 不能接受客户端伪造的 envelope,只能接受 token + action"
    )

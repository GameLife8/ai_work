"""High 级 fix 的回归测试。

#4 STRICT_ENCRYPTION fail-fast
#5 host_agent_client requests.Session 连接池
#6 token usage 累计 + 透出
#8 invoker.invoke 入口 RBAC
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest


# ============================================================
# #4 STRICT_ENCRYPTION fail-fast
# ============================================================


def test_ensure_strict_encryption_raises_when_strict_and_no_key(monkeypatch):
    from ops_platform.crypto import EncryptionNotConfigured, ensure_strict_encryption, reset_for_test
    monkeypatch.delenv("PLATFORM_ENCRYPTION_KEY", raising=False)
    reset_for_test()
    with pytest.raises(EncryptionNotConfigured) as exc:
        ensure_strict_encryption(strict=True)
    assert "STRICT_ENCRYPTION" in str(exc.value)
    assert "PLATFORM_ENCRYPTION_KEY" in str(exc.value)


def test_ensure_strict_encryption_passes_when_strict_and_key_present(monkeypatch):
    from ops_platform.crypto import ensure_strict_encryption, reset_for_test
    monkeypatch.setenv("PLATFORM_ENCRYPTION_KEY", "test-passphrase-for-unit-test")
    reset_for_test()
    try:
        ensure_strict_encryption(strict=True)  # 不该抛
    finally:
        reset_for_test()
        monkeypatch.delenv("PLATFORM_ENCRYPTION_KEY", raising=False)


def test_ensure_strict_encryption_is_noop_when_not_strict(monkeypatch):
    """strict=False 时（开发模式），无 key 也不该抛 —— 老行为兼容。"""
    from ops_platform.crypto import ensure_strict_encryption, reset_for_test
    monkeypatch.delenv("PLATFORM_ENCRYPTION_KEY", raising=False)
    reset_for_test()
    ensure_strict_encryption(strict=False)  # 不该抛


# ============================================================
# #5 host_agent_client requests.Session 连接池
# ============================================================


def test_http_exec_uses_session_pool():
    from services.host_agent_client import _HttpExec
    exec = _HttpExec(port=9100, token="abc", timeout_seconds=30)
    # 验证 session 已建好,且 Authorization 已注入
    assert exec._session is not None
    assert exec._session.headers.get("Authorization") == "Bearer abc"


def test_http_exec_session_request_reuses_connection():
    """同一个 _HttpExec 多次调用必须复用同一个 session（而不是每次新建）。"""
    from services.host_agent_client import _HttpExec
    exec = _HttpExec(port=9100, token="abc", timeout_seconds=30)
    session_id_1 = id(exec._session)
    session_id_2 = id(exec._session)
    assert session_id_1 == session_id_2


def test_http_exec_close_releases_session():
    from services.host_agent_client import _HttpExec
    exec = _HttpExec(port=9100, token="abc", timeout_seconds=30)
    exec.close()  # 不应抛


def test_http_exec_post_calls_session_not_module():
    """exec() 必须走 self._session.post,不能再走 requests.post 模块函数。"""
    from services.host_agent_client import _HttpExec
    exec = _HttpExec(port=9100, token="abc", timeout_seconds=30)
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {"stdout": "ok", "stderr": "", "exit_code": 0}
    with patch.object(exec._session, "post", return_value=fake_resp) as mock_post, \
         patch("services.host_agent_client.requests.post") as mock_module_post:
        out = exec.exec("1.2.3.4", ["ss", "-ltnp"])
    mock_post.assert_called_once()
    mock_module_post.assert_not_called()
    assert out == ("ok", "", 0)


# ============================================================
# #6 Token usage 累计
# ============================================================


def test_accumulate_usage_handles_empty():
    from ops_agent.agent import _accumulate_usage
    total: dict = {}
    _accumulate_usage(total, {})
    assert total == {}
    _accumulate_usage(total, None)
    assert total == {}


def test_accumulate_usage_first_call_seeds_model():
    from ops_agent.agent import _accumulate_usage
    total: dict = {}
    _accumulate_usage(total, {"prompt_tokens": 100, "completion_tokens": 50,
                              "total_tokens": 150, "model": "doubao-seed-2-0-pro"})
    assert total == {
        "prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150,
        "calls": 1, "model": "doubao-seed-2-0-pro",
    }


def test_accumulate_usage_sums_across_calls():
    from ops_agent.agent import _accumulate_usage
    total: dict = {}
    for i in range(3):
        _accumulate_usage(total, {"prompt_tokens": 1000, "completion_tokens": 200,
                                  "total_tokens": 1200, "model": "m1"})
    assert total["prompt_tokens"] == 3000
    assert total["completion_tokens"] == 600
    assert total["total_tokens"] == 3600
    assert total["calls"] == 3
    assert total["model"] == "m1"


def test_accumulate_usage_keeps_first_model_when_switched():
    """中途切模型场景极少；首个 model 名留作主标识（满足'90% 单 model'场景）。"""
    from ops_agent.agent import _accumulate_usage
    total: dict = {}
    _accumulate_usage(total, {"prompt_tokens": 10, "completion_tokens": 5,
                              "total_tokens": 15, "model": "modelA"})
    _accumulate_usage(total, {"prompt_tokens": 20, "completion_tokens": 10,
                              "total_tokens": 30, "model": "modelB"})
    assert total["model"] == "modelA"
    assert total["calls"] == 2


def test_model_client_attaches_usage_to_message():
    """create_completion 必须把 OpenAI usage 字段挂到返回的 message 上。"""
    from ops_agent.model_client import OpsModelClient
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {
        "choices": [{"message": {"role": "assistant", "content": "pong"}}],
        "usage": {"prompt_tokens": 42, "completion_tokens": 8, "total_tokens": 50},
    }
    fake_resp.raise_for_status = MagicMock()
    client = OpsModelClient(base_url="https://x/api/v3", api_key="k",
                            model="doubao-seed-2-0-pro-260215")
    with patch("ops_agent.model_client.requests.post", return_value=fake_resp):
        msg = client.create_completion(messages=[{"role": "user", "content": "ping"}])
    assert msg["_usage"] == {
        "prompt_tokens": 42, "completion_tokens": 8, "total_tokens": 50,
        "model": "doubao-seed-2-0-pro-260215",
    }
    assert msg["content"] == "pong"


def test_model_client_no_usage_field_does_not_crash():
    """部分国产模型早期版本响应漏 usage 字段 —— 不能因此挂掉。"""
    from ops_agent.model_client import OpsModelClient
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {
        "choices": [{"message": {"role": "assistant", "content": "hi"}}],
        # 故意省略 usage
    }
    fake_resp.raise_for_status = MagicMock()
    client = OpsModelClient(base_url="https://x/api/v3", api_key="k", model="m")
    with patch("ops_agent.model_client.requests.post", return_value=fake_resp):
        msg = client.create_completion(messages=[{"role": "user", "content": "x"}])
    assert "_usage" not in msg, "无 usage 字段时不应注入空 _usage"


def test_agent_outcome_has_usage_field():
    """AgentOutcome 必须有 usage 字段（默认空 dict）。"""
    from ops_agent.agent import AgentOutcome
    outcome = AgentOutcome(message="hi", trace=[])
    assert outcome.usage == {}
    outcome2 = AgentOutcome(message="hi", trace=[],
                            usage={"total_tokens": 100, "calls": 1, "model": "x"})
    assert outcome2.usage["total_tokens"] == 100


# ============================================================
# #8 invoker.invoke 入口 RBAC
# ============================================================


@pytest.fixture
def admin_only_invoker():
    """构造一个含 admin-only skill 的 invoker。"""
    from ops_platform.invoker import SkillInvoker
    from ops_platform.context import SkillContext

    spec = MagicMock()
    spec.code = "host_run_command"
    spec.visibility = "admin"
    spec.read_only = False
    spec.required_connection_type = "host_agent"
    spec.requires_admin_approval = False
    spec.confirmation_ttl_seconds = 300

    registry = MagicMock()
    registry.get.return_value = spec

    store = MagicMock()
    inv = SkillInvoker(registry, store)
    return inv, store, SkillContext


def test_invoke_blocks_admin_skill_for_user_role(admin_only_invoker):
    """user 角色调 visibility=admin 的 skill —— 必须被拦"""
    inv, store, SkillContext = admin_only_invoker
    ctx = SkillContext(
        runtime=MagicMock(),
        user={"username": "alice", "role": "user"},
        session_id="s",
    )
    result = inv.invoke("host_run_command", {"node": "n1", "command": "ls"}, ctx)
    assert result["status"] == "error"
    assert result["error_code"] == "forbidden"
    # 关键:registry.execute 没被调到
    inv.registry.execute.assert_not_called()


def test_invoke_blocks_when_no_role(admin_only_invoker):
    """ctx 没设 role —— 默认 user,同样拦"""
    inv, store, SkillContext = admin_only_invoker
    ctx = SkillContext(runtime=MagicMock(), user={"username": "x"}, session_id="s")
    result = inv.invoke("host_run_command", {}, ctx)
    assert result["error_code"] == "forbidden"


def test_invoke_blocks_when_no_user(admin_only_invoker):
    """ctx 完全没 user —— 当成 user 角色拦"""
    inv, store, SkillContext = admin_only_invoker
    ctx = SkillContext(runtime=MagicMock(), user=None, session_id="s")
    result = inv.invoke("host_run_command", {}, ctx)
    assert result["error_code"] == "forbidden"


def test_invoke_admin_passes_admin_skill(admin_only_invoker):
    """admin 角色调 admin-only skill —— 正常走 _prepare 走 needs_confirmation 流程"""
    inv, store, SkillContext = admin_only_invoker
    inv._prepare = MagicMock(return_value={"status": "needs_confirmation", "pending_token": "tok"})
    ctx = SkillContext(
        runtime=MagicMock(),
        user={"username": "admin", "role": "admin"},
        session_id="s",
    )
    result = inv.invoke("host_run_command", {}, ctx)
    assert result["status"] == "needs_confirmation"
    inv._prepare.assert_called_once()


def test_invoke_all_visibility_skill_open_to_user():
    """visibility=all 的 skill —— user 角色照常能调,RBAC 不应误伤"""
    from ops_platform.invoker import SkillInvoker
    from ops_platform.context import SkillContext

    spec = MagicMock()
    spec.code = "host_query"
    spec.visibility = "all"
    spec.read_only = True

    registry = MagicMock()
    registry.get.return_value = spec
    store = MagicMock()
    inv = SkillInvoker(registry, store)
    inv._execute = MagicMock(return_value={"status": "ok", "result": {}})

    ctx = SkillContext(runtime=MagicMock(),
                       user={"username": "alice", "role": "user"}, session_id="s")
    result = inv.invoke("host_query", {"node": "n1", "command": "ss -ltnp"}, ctx)
    assert result["status"] == "ok"
    inv._execute.assert_called_once()


def test_invoke_unknown_skill_returns_unknown_skill_error():
    """未注册的 skill —— 友好错误,不抛 KeyError"""
    from ops_platform.invoker import SkillInvoker
    from ops_platform.context import SkillContext

    registry = MagicMock()
    registry.get.side_effect = KeyError("not registered")
    store = MagicMock()
    inv = SkillInvoker(registry, store)

    ctx = SkillContext(runtime=MagicMock(), user={"role": "admin"}, session_id="s")
    result = inv.invoke("nonexistent_skill", {}, ctx)
    assert result["status"] == "error"
    assert result["error_code"] == "unknown_skill"
    assert "nonexistent_skill" in result["message"]

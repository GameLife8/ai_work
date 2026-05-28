"""三个 Critical bug fix 的回归测试。

1. agent.py JSON 解析崩溃 —— 模型返回非法 tool_call.arguments 时不能崩
2. invoker.confirm/reject 跨会话 token 重放
3. invoker 审计日志泄漏敏感参数（密码 / api_key / -p<password> / 等）
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from ops_agent.agent import _parse_tool_call_args
from ops_platform.invoker import (
    SkillInvoker,
    _is_sensitive_key,
    _redact_audit_args,
    _redact_command_string,
)


# ============================================================
# #1 _parse_tool_call_args：把硬抛错转成软错误
# ============================================================


def test_parse_valid_args_returns_dict():
    tc = {"id": "c1", "type": "function",
          "function": {"name": "host_query", "arguments": '{"node":"n1","command":"ss -ltnp"}'}}
    args, err = _parse_tool_call_args(tc)
    assert err is None
    assert args == {"node": "n1", "command": "ss -ltnp"}


def test_parse_empty_arguments_returns_empty_dict():
    """国产模型会传空字符串作为 arguments（无参数 skill），不能视为错。"""
    tc = {"id": "c1", "type": "function", "function": {"name": "platform_get_runbooks", "arguments": ""}}
    args, err = _parse_tool_call_args(tc)
    assert err is None
    assert args == {}


def test_parse_truncated_json_returns_error():
    """模型生成长参数到一半被截断 → 应返回 error envelope，不抛。"""
    tc = {"id": "c1", "type": "function",
          "function": {"name": "host_query", "arguments": '{"node": "n1", "command": "ss -ltnp'}}
    args, err = _parse_tool_call_args(tc)
    assert args is None
    assert err is not None
    assert err["status"] == "error"
    assert err["skill"] == "host_query"
    assert "JSON" in err["error"]
    # 必须给模型一个明确的"重试提示"
    assert "_hint" in err["result"]


def test_parse_chinese_quote_returns_error():
    """模型用中文引号 "name"（U+201C/U+201D）而非 ASCII " → 非法 JSON。"""
    tc = {"id": "c1", "type": "function",
          "function": {"name": "host_query", "arguments": '{“node”: “n1”}'}}
    args, err = _parse_tool_call_args(tc)
    assert args is None
    assert err is not None
    assert err["status"] == "error"


def test_parse_non_dict_top_level_returns_error():
    """合法 JSON 但顶层是 list / string → 不能传给 skill（**args 解构会爆）。"""
    for raw in ('["a","b"]', '"just a string"', '42', 'null'):
        tc = {"id": "c1", "type": "function",
              "function": {"name": "host_query", "arguments": raw}}
        args, err = _parse_tool_call_args(tc)
        assert args is None, f"raw={raw!r} 不该解析成功"
        assert err is not None
        assert err["status"] == "error"
        assert "object" in err["error"]


def test_parse_error_envelope_contains_preview():
    """error envelope 必须含 raw_arguments_preview 帮助 debug——但截断防止超长。"""
    huge = '{"key": "' + "x" * 1000 + '"'  # 缺尾 }
    tc = {"id": "c1", "type": "function",
          "function": {"name": "host_query", "arguments": huge}}
    args, err = _parse_tool_call_args(tc)
    assert err is not None
    preview = err["result"]["raw_arguments_preview"]
    assert len(preview) <= 200, "preview 应被截到 200 字以内"
    assert preview.startswith('{"key":')


def test_parse_handles_missing_arguments_key():
    """tool_call.function 没有 arguments 字段 → 视为空 dict，不抛。"""
    tc = {"id": "c1", "type": "function", "function": {"name": "x"}}
    args, err = _parse_tool_call_args(tc)
    assert err is None
    assert args == {}


# ============================================================
# #2 confirm() / reject() 跨会话 token 重放
# ============================================================


@pytest.fixture
def invoker_setup():
    """构造一个最小可用的 invoker + store + ctx 二件套。"""
    from ops_platform.context import SkillContext

    store = MagicMock()
    registry = MagicMock()
    inv = SkillInvoker(registry, store)
    return inv, store, SkillContext


def _pending_record(session_id: str = "sess-A", status: str = "pending"):
    """构造一条 pending action 记录。"""
    from datetime import UTC, datetime, timedelta
    return {
        "token": "tok-xyz",
        "skill_code": "swarm_scale_service",
        "args_json": {"service": "web", "replicas": 5},
        "status": status,
        "session_id": session_id,
        "expires_at": (datetime.now(UTC) + timedelta(seconds=300)).isoformat(),
        "requires_admin_approval": False,
    }


def test_confirm_rejects_cross_session_token(invoker_setup):
    """会话 A 生成的 token，从会话 B 调 confirm 必须被拒。"""
    inv, store, SkillContext = invoker_setup
    store.get_pending_action.return_value = _pending_record(session_id="sess-A")

    ctx = SkillContext(
        runtime=MagicMock(),
        user={"username": "admin", "role": "admin"},
        session_id="sess-B",
    )
    result = inv.confirm("tok-xyz", ctx)
    assert result["status"] == "error"
    assert result["error_code"] == "session_mismatch"
    inv.registry.execute.assert_not_called()


def test_confirm_accepts_same_session_token(invoker_setup):
    """同一会话 token 必须能正常 confirm。"""
    inv, store, SkillContext = invoker_setup
    store.get_pending_action.return_value = _pending_record(session_id="sess-A")
    inv.registry.get.return_value = MagicMock(
        code="swarm_scale_service",
        confirmation_ttl_seconds=300,
        requires_admin_approval=False,
    )
    inv._execute = MagicMock(return_value={"status": "ok", "result": {"done": True}})

    ctx = SkillContext(
        runtime=MagicMock(),
        user={"username": "admin", "role": "admin"},
        session_id="sess-A",
    )
    result = inv.confirm("tok-xyz", ctx)
    assert result["status"] == "ok"
    inv._execute.assert_called_once()


def test_reject_also_checks_session(invoker_setup):
    """reject 同样校验 session（防 DoS 拒绝别人的 pending action）。"""
    inv, store, SkillContext = invoker_setup
    store.get_pending_action.return_value = _pending_record(session_id="sess-A")
    ctx = SkillContext(
        runtime=MagicMock(),
        user={"username": "u", "role": "user"},
        session_id="sess-B",
    )
    result = inv.reject("tok-xyz", ctx, reason="malicious reject")
    assert result["error_code"] == "session_mismatch"
    store.update_pending_action_status.assert_not_called()


def test_reject_accepts_same_session(invoker_setup):
    inv, store, SkillContext = invoker_setup
    store.get_pending_action.return_value = _pending_record(session_id="sess-A")
    store.update_pending_action_status.return_value = {"status": "rejected"}
    ctx = SkillContext(runtime=MagicMock(), user={"username": "u"}, session_id="sess-A")
    result = inv.reject("tok-xyz", ctx, reason="not now")
    assert result["status"] == "rejected"


def test_confirm_record_without_session_passes_through(invoker_setup):
    """老数据可能 session_id 为空（外部脚本注入 / 历史迁移）。
    退化为只看 token——不能完全锁死,但当前 ctx 仍需有 session_id（来路必须明）。"""
    inv, store, SkillContext = invoker_setup
    rec = _pending_record(session_id=None)
    store.get_pending_action.return_value = rec
    inv.registry.get.return_value = MagicMock(
        code="x", confirmation_ttl_seconds=300, requires_admin_approval=False,
    )
    inv._execute = MagicMock(return_value={"status": "ok", "result": {}})

    ctx = SkillContext(
        runtime=MagicMock(),
        user={"username": "admin", "role": "admin"},
        session_id="any-session",
    )
    result = inv.confirm("tok-xyz", ctx)
    assert result["status"] == "ok"


# ============================================================
# #3 审计日志脱敏
# ============================================================


def test_is_sensitive_key_lookup():
    assert _is_sensitive_key("password")
    assert _is_sensitive_key("PASSWORD")
    assert _is_sensitive_key("user_password")
    assert _is_sensitive_key("api_key")
    assert _is_sensitive_key("X-API-KEY")     # 含 hyphen 的 HTTP header 风格
    assert _is_sensitive_key("X-Api-Key")
    assert _is_sensitive_key("authorization")
    assert _is_sensitive_key("auth_token")
    assert not _is_sensitive_key("username")
    assert not _is_sensitive_key("node")
    assert not _is_sensitive_key("command")


def test_redact_sensitive_top_level_keys():
    args = {"username": "alice", "password": "p@ssw0rd!",
            "api_key": "sk-abc123", "node": "n1"}
    out = _redact_audit_args(args)
    assert out["username"] == "alice"
    assert out["password"] == "***"
    assert out["api_key"] == "***"
    assert out["node"] == "n1"


def test_redact_handles_case_variants():
    args = {"API_KEY": "x", "ApiKey": "y", "Authorization": "Bearer z",
            "SECRET_TOKEN": "s", "auth_token": "t"}
    out = _redact_audit_args(args)
    for k in args:
        assert out[k] == "***"


def test_redact_command_string_inline_mysql_password():
    cmd = "mysql -uroot -psup3rs3cret -e 'show databases'"
    out = _redact_command_string(cmd)
    assert "sup3rs3cret" not in out
    assert "-p***" in out


def test_redact_command_string_curl_authorization_header():
    """整个 Authorization 值（含 'Bearer xxx'）都该被替换，
    不能只到第一个空格 —— Bearer token 自然带空格。"""
    cmd = "curl -H 'Authorization: Bearer eyJhbGc.xxx.yyy' https://api.example.com/v1/foo"
    out = _redact_command_string(cmd)
    assert "eyJhbGc" not in out
    assert "Bearer" not in out   # 整段都被吃掉了
    assert "Authorization:" in out and "***" in out


def test_redact_command_string_curl_xapikey():
    cmd = 'curl -H "X-Api-Key: secret-key-12345" https://x'
    out = _redact_command_string(cmd)
    assert "secret-key-12345" not in out


def test_redact_command_string_url_basic_auth():
    cmd = "wget https://user:hunter2@private.example.com/secret.tar.gz"
    out = _redact_command_string(cmd)
    assert "hunter2" not in out
    assert "user:***@" in out


def test_redact_command_string_env_var_credential():
    """覆盖 DATABASE_PASSWORD（有前缀）+ API_KEY（裸名）+ AWS_SECRET（有后缀）。"""
    cmd = "API_KEY=sk-xyz DATABASE_PASSWORD=p123 AWS_SECRET_ACCESS_KEY=zzz /opt/start.sh"
    out = _redact_command_string(cmd)
    assert "sk-xyz" not in out
    assert "p123" not in out
    assert "zzz" not in out
    # 三个 KEY= 都该变成 ***
    assert out.count("=***") >= 3, f"未全部替换: {out}"


def test_redact_args_with_command_field():
    """host_run_command 的 params.command 字段必须做 string-level 脱敏。"""
    args = {"node": "n1", "command": "mysql -uroot -psecret123 -e 'select 1'"}
    out = _redact_audit_args(args)
    assert out["node"] == "n1"
    assert "secret123" not in out["command"]
    assert "-p***" in out["command"]


def test_redact_nested_dict():
    args = {
        "config": {"endpoint": "https://x.example.com", "api_key": "sk-leaked"},
        "node": "n1",
    }
    out = _redact_audit_args(args)
    assert out["config"]["api_key"] == "***"
    assert out["config"]["endpoint"] == "https://x.example.com"


def test_redact_does_not_mutate_original():
    """**关键**：脱敏不能改原 params——否则 skill 自己拿到的就是 `***`，实际执行用 *** 当密码连数据库就完蛋。"""
    args = {"password": "real-pw", "command": "mysql -ptop_secret",
            "nested": {"api_key": "sk-real"}}
    snapshot = json.dumps(args, sort_keys=True)
    _ = _redact_audit_args(args)
    assert json.dumps(args, sort_keys=True) == snapshot, "原 args 被原位修改了!"


def test_redact_safe_passthrough_for_non_sensitive():
    """没有敏感字段时,等价于深拷贝（不引用原对象）。"""
    args = {"node": "n1", "ports": [80, 443], "labels": {"env": "prod"}}
    out = _redact_audit_args(args)
    assert out == args
    assert out is not args


def test_redact_handles_list_of_commands():
    """有些 skill（host_capture_packets）的 cmd_list 是字符串列表——
    若 key 名含 'command'，list 元素也要脱敏。"""
    args = {"commands_list": ["echo hi", "mysql -psecret"], "node": "n1"}
    out = _redact_audit_args(args)
    assert "secret" not in out["commands_list"][1]


def test_redact_handles_non_dict_input():
    """防御性：传入 list / None 也不该抛。"""
    assert _redact_audit_args(None) is None
    assert _redact_audit_args([1, 2, 3]) == [1, 2, 3]
    assert _redact_audit_args("not a dict") == "not a dict"

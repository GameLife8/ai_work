"""Jenkins skill 契约 + 客户端单测。

不打真实 Jenkins —— 用 mock 验证以下契约:
1. ``_encode_job_path`` 正确处理 folder 嵌套和 URL-encoding
2. ``jenkins_query`` skill 的 category × verb 路由
3. console 输出的 scanner 能捕获关键错误关键字
4. 返回 envelope 的字段在不同分支下都齐
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from services.jenkins_client import JenkinsClient, JenkinsResult
from skills.jenkins_query import (
    MANIFEST,
    _format_job_summary,
    _summarize_console_failure,
    _validate,
)
from skills.jenkins_query import run as jenkins_query_run


# ============================================================
# JenkinsClient 路径编码
# ============================================================


def test_encode_simple_job():
    """单 job 也要带 ``job/`` 前缀,这样 caller 拼 ``/{path}/api/json`` 才对。

    回归:之前漏了前缀,实测 Jenkins 直接 404。
    """
    assert JenkinsClient._encode_job_path("audit-management") == "job/audit-management"


def test_encode_folder_nested():
    """``app/sub-job`` → ``job/app/job/sub-job`` (Jenkins folder URL 约定)"""
    assert JenkinsClient._encode_job_path("app/sub-job") == "job/app/job/sub-job"


def test_encode_three_levels():
    assert (JenkinsClient._encode_job_path("app/team-a/build-something")
            == "job/app/job/team-a/job/build-something")


def test_encode_special_chars_url_quoted():
    """job 名含空格 / 中文要 URL-encode"""
    out = JenkinsClient._encode_job_path("我的 job")
    assert "%E6%88%91" in out or "%20" in out
    assert out.startswith("job/")


def test_encode_empty_raises():
    with pytest.raises(ValueError, match="job 名"):
        JenkinsClient._encode_job_path("")


# ============================================================
# Skill MANIFEST 契约（被 test_skill_manifest_quality 自动 lint,这里再 spot-check）
# ============================================================


def test_manifest_basic_shape():
    assert MANIFEST["code"] == "jenkins_query"
    assert MANIFEST["read_only"] is True
    assert MANIFEST["visibility"] == "all"
    assert MANIFEST["required_connection_type"] == "jenkins"


def test_manifest_description_includes_examples():
    """长描述必须带示例(被 test_skill_manifest_quality 强制,这里再守一道)"""
    desc = MANIFEST["description"]
    assert "示例" in desc or "::" in desc or "``" in desc
    assert "category=job" in desc
    assert "category=build" in desc


# ============================================================
# _validate
# ============================================================


def test_validate_unknown_category():
    err = _validate("nope", "ls", None, None)
    assert "unknown category" in err


def test_validate_wrong_verb_for_category():
    err = _validate("queue", "console", None, None)
    assert "queue 不支持 verb=console" in err


def test_validate_inspect_needs_name():
    err = _validate("job", "inspect", None, None)
    assert "name" in err


def test_validate_ok():
    assert _validate("job", "ls", None, None) is None
    assert _validate("build", "console", "x", "lastBuild") is None
    assert _validate("queue", "ls", None, None) is None


# ============================================================
# scanner: console 输出错误识别
# ============================================================


def test_console_scanner_picks_up_errors():
    text = """
[INFO] starting build
mvn clean install
[ERROR] Failed to execute goal compile
java.lang.NullPointerException at com.example.Foo.bar(Foo.java:42)
Build step 'Maven' marked build as FAILURE
exit code 1
"""
    out = _summarize_console_failure(text)
    assert out["total_lines"] >= 5
    hits = out["error_hits"]
    assert any("ERROR" in h["text"] for h in hits)
    assert any("FAILURE" in h["text"] for h in hits)
    assert any("exit code" in h["text"] for h in hits)


def test_console_scanner_empty_text():
    assert _summarize_console_failure("") == {}


def test_console_scanner_caps_evidence():
    """大量错误行不能让 evidence 数组爆炸"""
    huge = "\n".join("ERROR: line %d failed" % i for i in range(200))
    out = _summarize_console_failure(huge, max_evidence=10)
    assert len(out["error_hits"]) == 10


# ============================================================
# _format_job_summary
# ============================================================


def test_format_job_summary_basic():
    job = {
        "_class": "org.jenkinsci.plugins.workflow.job.WorkflowJob",
        "name": "audit-management",
        "url": "http://x/job/audit-management/",
        "color": "blue",
        "lastBuild": {"number": 19, "result": "SUCCESS", "timestamp": 12345},
    }
    out = _format_job_summary(job)
    assert out["name"] == "audit-management"
    assert out["type"] == "WorkflowJob"     # 取 _class 最后一段
    assert out["color"] == "blue"
    assert out["last_build_number"] == 19


def test_format_job_summary_folder_no_lastbuild():
    job = {"_class": "com.cloudbees.hudson.plugins.folder.Folder",
           "name": "app", "url": "http://x/job/app/"}
    out = _format_job_summary(job)
    assert out["type"] == "Folder"
    assert out["last_build_number"] is None


# ============================================================
# jenkins_query run() —— 用 mock client 走完整链路
# ============================================================


def _ctx_with_client(fake_client) -> MagicMock:
    ctx = MagicMock()
    ctx.connection_for.return_value = fake_client
    return ctx


def test_run_job_ls_picks_abnormal():
    fake_client = MagicMock()
    fake_client.list_jobs.return_value = JenkinsResult(
        ok=True, status_code=200, url="x",
        data={"jobs": [
            {"_class": "X.Job", "name": "ok-1", "color": "blue",
             "lastBuild": {"number": 1, "result": "SUCCESS"}},
            {"_class": "X.Job", "name": "failed-1", "color": "red",
             "lastBuild": {"number": 2, "result": "FAILURE"}},
            {"_class": "X.Job", "name": "unstable-1", "color": "yellow",
             "lastBuild": {"number": 3, "result": "UNSTABLE"}},
        ]},
    )
    out = jenkins_query_run(_ctx_with_client(fake_client),
                            category="job", verb="ls")
    assert out["ok"] is True
    assert out["total"] == 3
    assert out["abnormal_count"] == 2
    assert "failed-1" in out["abnormal_names"]
    assert "unstable-1" in out["abnormal_names"]


def test_run_build_console_runs_scanner():
    fake_client = MagicMock()
    fake_client.get_console.return_value = JenkinsResult(
        ok=True, status_code=200, url="x",
        data="ok\n[ERROR] something broke\nexit code 1\n",
    )
    out = jenkins_query_run(_ctx_with_client(fake_client),
                            category="build", verb="console",
                            name="x", build_number=42)
    assert out["ok"] is True
    assert "console" in out
    assert "scanner" in out
    assert len(out["scanner"]["error_hits"]) >= 1


def test_run_build_console_respects_tail_zero():
    """tail_lines=0 → 返回全文(no truncation)"""
    fake_client = MagicMock()
    fake_client.get_console.return_value = JenkinsResult(
        ok=True, status_code=200, url="x", data="full text",
    )
    jenkins_query_run(_ctx_with_client(fake_client),
                      category="build", verb="console",
                      name="x", tail_lines=0)
    # 验证传给 client 的 tail 是 None(=不截)
    args, kwargs = fake_client.get_console.call_args
    assert kwargs.get("tail") is None


def test_run_validation_error_returns_friendly_envelope():
    """坏参数不抛,返回 ok=False + error 描述 —— LLM 能基于此自纠正"""
    fake_client = MagicMock()
    out = jenkins_query_run(_ctx_with_client(fake_client),
                            category="job", verb="inspect")  # 漏 name
    assert out["ok"] is False
    assert "name" in out["error"]
    # 关键:**没有**调到 client(参数挂掉就别浪费 HTTP 调用)
    fake_client.get_job.assert_not_called()


def test_run_queue_ls_counts_stuck():
    fake_client = MagicMock()
    fake_client.list_queue.return_value = JenkinsResult(
        ok=True, status_code=200, url="x",
        data={"items": [
            {"id": 1, "stuck": False},
            {"id": 2, "stuck": True},
            {"id": 3, "stuck": True},
        ]},
    )
    out = jenkins_query_run(_ctx_with_client(fake_client),
                            category="queue", verb="ls")
    assert out["queue_size"] == 3
    assert out["stuck_count"] == 2


def test_run_node_ls_counts_offline():
    fake_client = MagicMock()
    fake_client.list_nodes.return_value = JenkinsResult(
        ok=True, status_code=200, url="x",
        data={"computer": [
            {"displayName": "master", "offline": False, "numExecutors": 4},
            {"displayName": "agent-1", "offline": True, "numExecutors": 2,
             "offlineCause": {"description": "agent disconnected"}},
        ]},
    )
    out = jenkins_query_run(_ctx_with_client(fake_client),
                            category="node", verb="ls")
    assert out["node_count"] == 2
    assert out["offline_count"] == 1
    offline = next(n for n in out["nodes"] if n["offline"])
    assert offline["offline_cause"] == "agent disconnected"


def test_run_propagates_http_error():
    """client 返回 ok=False 时 skill 也要原样标记 + 暴露 error 给模型"""
    fake_client = MagicMock()
    fake_client.list_jobs.return_value = JenkinsResult(
        ok=False, status_code=401, url="x",
        data=None, error="<html>HTTP ERROR 401</html>",
    )
    out = jenkins_query_run(_ctx_with_client(fake_client),
                            category="job", verb="ls")
    assert out["ok"] is False
    assert out["status_code"] == 401
    assert "401" in out["error"]


# ============================================================
# Driver 注册契约
# ============================================================


def test_driver_registered():
    """``ConnectionManager`` lookups jenkins 必须命中。"""
    from ops_platform.drivers import get as get_driver
    d = get_driver("jenkins")
    assert d.type_code == "jenkins"
    assert d.category == "automation"


def test_driver_schema_fields():
    from ops_platform.drivers import get as get_driver
    d = get_driver("jenkins")
    schema = d.schema()
    keys = {f["key"] for f in schema["fields"]}
    assert "base_url" in keys
    assert "username" in keys
    assert "api_token" in keys
    assert "password" in keys


def test_driver_build_client_uses_api_token_over_password():
    """api_token 和 password 都填时,API token 应该优先(更安全)"""
    from ops_platform.drivers.jenkins import JenkinsDriver
    d = JenkinsDriver()
    client = d.build_client({
        "base_url": "http://x:8080",
        "username": "admin",
        "api_token": "tok-abc",
        "password": "pw-xyz",
    })
    # JenkinsClient.__init__ 优先用 api_token
    assert client._secret == "tok-abc"


def test_driver_build_client_falls_back_to_password():
    from ops_platform.drivers.jenkins import JenkinsDriver
    d = JenkinsDriver()
    client = d.build_client({
        "base_url": "http://x:8080",
        "username": "admin",
        "api_token": "",   # 空
        "password": "pw-xyz",
    })
    assert client._secret == "pw-xyz"


# ============================================================
# JenkinsClient HTTP 层 (mock requests.Session)
# ============================================================


def _mock_response(status_code: int, body, ctype: str = "application/json"):
    r = MagicMock()
    r.status_code = status_code
    r.headers = {"Content-Type": ctype}
    r.url = "http://x"
    if "json" in ctype:
        r.json.return_value = body
        r.text = ""
    else:
        r.text = body
        r.json.side_effect = ValueError("not json")
    return r


def test_client_get_returns_parsed_json():
    client = JenkinsClient(base_url="http://x", username="u", api_token="t")
    fake_resp = _mock_response(200, {"key": "value"})
    with patch.object(client._session, "get", return_value=fake_resp) as get:
        r = client.get("/api/json")
    get.assert_called_once()
    assert r.ok
    assert r.data == {"key": "value"}


def test_client_get_console_returns_text():
    client = JenkinsClient(base_url="http://x", username="u", api_token="t")
    fake_resp = _mock_response(200, "line1\nline2\nline3", ctype="text/plain")
    with patch.object(client._session, "get", return_value=fake_resp):
        r = client.get_console("audit-management", "lastBuild")
    assert r.ok
    assert isinstance(r.data, str)
    assert "line1" in r.data


def test_client_get_console_tail_truncates():
    client = JenkinsClient(base_url="http://x", username="u", api_token="t")
    long_text = "\n".join(f"line {i}" for i in range(500))
    fake_resp = _mock_response(200, long_text, ctype="text/plain")
    with patch.object(client._session, "get", return_value=fake_resp):
        r = client.get_console("x", "1", tail=50)
    assert r.ok
    assert r.data.count("\n") == 49   # 50 行 = 49 个换行


def test_client_4xx_returns_not_ok():
    client = JenkinsClient(base_url="http://x", username="u", api_token="t")
    fake_resp = _mock_response(401, "Invalid password", ctype="text/html")
    fake_resp.text = "<html>HTTP ERROR 401</html>"
    with patch.object(client._session, "get", return_value=fake_resp):
        r = client.get("/api/json")
    assert not r.ok
    assert r.status_code == 401
    assert "401" in r.error


def test_client_network_error_returns_not_ok():
    import requests as _requests
    client = JenkinsClient(base_url="http://x", username="u", api_token="t")
    with patch.object(client._session, "get",
                      side_effect=_requests.ConnectionError("refused")):
        r = client.get("/api/json")
    assert not r.ok
    assert r.status_code == -1
    assert "refused" in r.error


def test_client_session_has_basic_auth():
    client = JenkinsClient(base_url="http://x", username="admin", api_token="tok")
    assert client._session.auth == ("admin", "tok")


def test_client_no_credentials_no_auth():
    """没填 username/token 时不设 auth(便于匿名 Jenkins 测试)"""
    client = JenkinsClient(base_url="http://x")
    assert client._session.auth is None


def test_client_close_is_idempotent():
    client = JenkinsClient(base_url="http://x", username="u", api_token="t")
    client.close()
    client.close()    # 不该抛

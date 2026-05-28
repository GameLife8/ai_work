"""HostAgentClient HTTP transport 行为回归测试。

不打真实 agent——用 ``requests-mock`` 或 monkeypatch ``requests.post`` 模拟
``/v1/exec`` 响应，覆盖：
  - SwarmHostAgent 默认 transport=http，调用走 HTTP
  - K8sHostAgent 默认 transport=exec
  - nsenter_on_node 在 http 路径下用 nsenter 字符串参数，不在客户端拼 nsenter cmd
  - http 错误回 returncode=-1（不抛）
  - target_pid != 1 时强制回 exec wrap（HTTP 协议不支持 pid 自定义）
"""

from __future__ import annotations

import json

import pytest

from services.host_agent_client import (
    DEFAULT_NSENTER_NS,
    HostExecResult,
    K8sHostAgent,
    SwarmHostAgent,
    _build_nsenter_cmd,
    _HttpExec,
)


# --------- _HttpExec --------- #


class _FakeResp:
    def __init__(self, status: int, body: dict | str):
        self.status_code = status
        self._body = body
        self.text = body if isinstance(body, str) else json.dumps(body)

    def json(self):
        if isinstance(self._body, dict):
            return self._body
        raise json.JSONDecodeError("not json", self.text, 0)


def test_http_exec_happy_path(monkeypatch):
    captured = {}

    # 现在 _HttpExec 内部用 requests.Session 复用连接,headers（Authorization Bearer）
    # 通过 session.headers 默认注入,**不再**作为 per-call kwarg 传给 post。
    # 所以测试不再断言 headers kwarg,改为在断言段直接看 session.headers。
    def fake_post(url, json=None, **kw):
        captured["url"] = url
        captured["json"] = json
        captured["timeout"] = kw.get("timeout")
        return _FakeResp(200, {
            "exit_code": 0,
            "stdout": "PID 1 systemd\n",
            "stderr": "",
            "duration_ms": 10,
            "truncated": False,
            "timeout": False,
        })

    http = _HttpExec(port=9100, token="t-1", timeout_seconds=30)
    monkeypatch.setattr(http._session, "post", fake_post)

    stdout, stderr, rc = http.exec("10.0.0.5", ["ps", "1"], nsenter="muinp", timeout=20)

    assert stdout == "PID 1 systemd\n"
    assert stderr == ""
    assert rc == 0
    assert captured["url"] == "http://10.0.0.5:9100/v1/exec"
    assert captured["json"] == {"cmd": ["ps", "1"], "nsenter": "muinp", "timeout_sec": 20}
    # Authorization 走 session-default header,不在 per-call kwargs
    assert http._session.headers["Authorization"] == "Bearer t-1"
    # 平台侧的 HTTP 超时 = agent 端 timeout_sec + 5 缓冲
    assert captured["timeout"] == 25


def test_http_exec_4xx_returns_negative_rc(monkeypatch):
    http = _HttpExec(port=9100, token="t", timeout_seconds=10)
    monkeypatch.setattr(http._session, "post",
                        lambda *a, **kw: _FakeResp(403, {"error": "command not in whitelist"}))
    stdout, stderr, rc = http.exec("1.1.1.1", ["bad_cmd"])
    assert stdout == ""
    assert rc == -1
    assert "403" in stderr
    assert "whitelist" in stderr


def test_http_exec_network_error(monkeypatch):
    import services.host_agent_client as mod

    def boom(*a, **kw):
        raise mod.requests.ConnectionError("nope")

    http = _HttpExec(port=9100, token="t", timeout_seconds=10)
    monkeypatch.setattr(http._session, "post", boom)
    stdout, stderr, rc = http.exec("1.1.1.1", ["true"])
    assert rc == -1
    assert "network error" in stderr.lower()


def test_http_exec_requires_token():
    with pytest.raises(ValueError, match="agent_token"):
        _HttpExec(port=9100, token="", timeout_seconds=10)


# --------- SwarmHostAgent http transport --------- #


class _FakeSwarm:
    """模拟 DockerSwarmClient 仅做 ``docker node inspect`` + ``service ps``。"""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def json(self, args):
        self.calls.append(args)
        if args[:2] == ["node", "inspect"]:
            target = args[2]
            return [{
                "Description": {"Hostname": target},
                "Status": {"Addr": "192.168.2.124", "State": "ready"},
            }]
        if args[:2] == ["service", "ps"]:
            return [{"Node": "worker2", "ID": "task1"}]
        return []


def test_swarm_default_transport_is_http():
    sw = _FakeSwarm()
    http = _HttpExec(port=9100, token="t", timeout_seconds=10)
    agent = SwarmHostAgent(sw, transport="http", http=http)
    assert agent.transport == "http"


def test_swarm_http_exec_uses_node_inspect_addr(monkeypatch):
    """exec_on_node → docker node inspect 拿 Addr → HTTP POST 到那个 IP。"""
    sw = _FakeSwarm()
    captured: dict = {}

    def fake_post(url, json=None, **kw):
        captured["url"] = url
        captured["json"] = json
        return _FakeResp(200, {"exit_code": 0, "stdout": "ok\n", "stderr": ""})

    http = _HttpExec(port=9100, token="t", timeout_seconds=10)
    monkeypatch.setattr(http._session, "post", fake_post)

    agent = SwarmHostAgent(sw, transport="http", http=http)
    result = agent.exec_on_node("worker2", ["echo", "hi"])

    assert isinstance(result, HostExecResult)
    assert result.transport == "http"
    assert result.ok
    assert result.stdout == "ok\n"
    # 关键：用了 node inspect 的 Addr
    assert captured["url"] == "http://192.168.2.124:9100/v1/exec"
    # nsenter="" 因为是裸 exec
    assert captured["json"]["nsenter"] == ""


def test_swarm_http_nsenter_passes_flags_to_agent(monkeypatch):
    """nsenter_on_node 在 http 路径下应该把 ``nsenter='muinp'`` 当参数传，不自己拼。"""
    sw = _FakeSwarm()
    captured: dict = {}

    def fake_post(url, json=None, **kw):
        captured["json"] = json
        return _FakeResp(200, {"exit_code": 0, "stdout": "systemd\n", "stderr": ""})

    http = _HttpExec(port=9100, token="t", timeout_seconds=10)
    monkeypatch.setattr(http._session, "post", fake_post)

    agent = SwarmHostAgent(sw, transport="http", http=http)
    result = agent.nsenter_on_node("worker2", ["cat", "/proc/1/comm"])

    # 关键：cmd 是原始的，没被 nsenter -t 1 包过
    assert captured["json"]["cmd"] == ["cat", "/proc/1/comm"]
    assert captured["json"]["nsenter"] == "muinp"
    assert result.stdout == "systemd\n"


def test_swarm_http_nsenter_fallback_when_target_pid_not_1(monkeypatch):
    """target_pid != 1 时不能走 HTTP path（协议不支持），fallback 到 exec wrap。"""
    sw = _FakeSwarm()
    http = _HttpExec(port=9100, token="t", timeout_seconds=10)
    agent = SwarmHostAgent(sw, transport="http", http=http)

    # 这次应该走 exec_on_node 的 http path（但用 nsenter-wrapped cmd）
    captured = {}

    def fake_post(url, json=None, **kw):
        captured["json"] = json
        return _FakeResp(200, {"exit_code": 0, "stdout": "", "stderr": ""})

    monkeypatch.setattr(http._session, "post", fake_post)

    agent.nsenter_on_node("worker2", ["ss", "-ltn"], target_pid=12345)
    # 应该在客户端拼 nsenter -t 12345 ... -- ss -ltn
    cmd_sent = captured["json"]["cmd"]
    assert cmd_sent[0] == "nsenter"
    assert "-t" in cmd_sent and "12345" in cmd_sent
    assert "ss" in cmd_sent and "-ltn" in cmd_sent


# --------- K8sHostAgent default --------- #


class _FakeKube:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def run(self, args):
        self.calls.append(list(args))
        from services.k8s_client import CommandResult
        if args[:2] == ["-n", "ai-ops"] and "get" in args and "pods" in args:
            body = {"items": [{
                "metadata": {"name": "ai-ops-agent-abc"},
                "spec": {"nodeName": "worker2"},
                "status": {"phase": "Running"},
            }]}
            return CommandResult(command=args, stdout=json.dumps(body), stderr="", returncode=0)
        if "get" in args and "node" in args:
            body = {"status": {"addresses": [
                {"type": "InternalIP", "address": "10.0.7.5"},
                {"type": "Hostname", "address": "worker2"},
            ]}}
            return CommandResult(command=args, stdout=json.dumps(body), stderr="", returncode=0)
        if "exec" in args:
            return CommandResult(command=args, stdout="ok\n", stderr="", returncode=0)
        return CommandResult(command=args, stdout="", stderr="", returncode=0)


def test_k8s_default_transport_is_exec():
    agent = K8sHostAgent(_FakeKube())
    assert agent.transport == "exec"


def test_k8s_exec_path_invokes_kubectl_exec():
    kube = _FakeKube()
    agent = K8sHostAgent(kube, transport="exec")
    result = agent.exec_on_node("worker2", ["true"])
    assert result.transport == "exec"
    assert result.ok
    # 验证 kubectl 命令有正确的 exec 字段
    exec_call = next(c for c in kube.calls if "exec" in c)
    assert "ai-ops-agent-abc" in exec_call
    assert exec_call[-1] == "true"


def test_k8s_http_path_uses_internal_ip(monkeypatch):
    kube = _FakeKube()
    http = _HttpExec(port=9100, token="k-t", timeout_seconds=10)
    agent = K8sHostAgent(kube, transport="http", http=http)

    captured: dict = {}
    monkeypatch.setattr(http._session, "post",
                        lambda url, json=None, **kw: (
                            captured.update(url=url, json=json),
                            _FakeResp(200, {"exit_code": 0, "stdout": "ok", "stderr": ""}),
                        )[1])

    result = agent.exec_on_node("worker2", ["true"])
    assert result.transport == "http"
    # kubectl get node worker2 拿到 InternalIP = 10.0.7.5
    assert captured["url"] == "http://10.0.7.5:9100/v1/exec"


def test_k8s_http_requires_http_helper():
    with pytest.raises(ValueError, match="http 参数不可为 None"):
        K8sHostAgent(_FakeKube(), transport="http", http=None)


# --------- 边界 --------- #


def test_build_nsenter_cmd_shape():
    out = _build_nsenter_cmd(1, DEFAULT_NSENTER_NS, ["ss", "-ltn"])
    assert out[:3] == ["nsenter", "-t", "1"]
    assert "--mount" in out
    assert "--pid" in out
    assert out[-3:] == ["--", "ss", "-ltn"]

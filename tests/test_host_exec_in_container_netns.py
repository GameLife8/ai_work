"""host_exec_in_container_netns —— 在目标容器 netns 跑网络诊断命令。

机制:skill 只把 ``container`` + 命令交给 ``client.exec_in_container_netns``;**容器 PID
解析 + nsenter 全在 agent 内部**(不走业务白名单、绕开 docker_proxy 对 docker 的硬拦)。

回归点:
  1. 容器名 + ``namespaces=("n",)``(只换网络) + 命令 透传给 agent。
  2. 只读白名单:非网络诊断命令直接拒,**不发起任何节点调用**。
  3. agent 侧错误(如容器 PID 找不到)如实透传。
"""

from __future__ import annotations


class _Result:
    def __init__(self, ok, stdout="", stderr="", rc=0):
        self.ok, self.stdout, self.stderr, self.returncode = ok, stdout, stderr, rc


class _Client:
    def __init__(self, result=None):
        self.calls = []
        self._result = result or _Result(True, stdout="Address: 1.2.3.4")

    def exec_in_container_netns(self, node, container, inner_cmd, *, namespaces=("n",), timeout=None):
        self.calls.append({
            "node": node, "container": container,
            "cmd": list(inner_cmd), "namespaces": tuple(namespaces),
        })
        return self._result


class _Ctx:
    def __init__(self, client):
        self._c = client

    def connection_for(self, _type, _cid):
        return self._c


def test_delegates_container_and_netns_to_agent():
    from skills.host_exec_in_container_netns import run
    client = _Client()
    out = run(_Ctx(client), node="192.168.2.44", container="csga-car-manage_ua",
              command="nslookup iiot.chinasws.com")
    assert out["ok"] is True
    assert len(client.calls) == 1
    c = client.calls[0]
    assert c["container"] == "csga-car-manage_ua"   # 容器名透传,PID 由 agent 内部解析
    assert c["namespaces"] == ("n",)                # 只换网络 namespace(mount 仍是 agent 镜像)
    assert c["cmd"] == ["nslookup", "iiot.chinasws.com"]


def test_rejects_non_whitelisted_command_without_calling_agent():
    from skills.host_exec_in_container_netns import run
    client = _Client()
    out = run(_Ctx(client), node="n1", container="csga", command="rm -rf /tmp")
    assert out["ok"] is False and "白名单" in out["error"]
    assert client.calls == []


def test_command_accepts_list_form():
    from skills.host_exec_in_container_netns import run
    client = _Client()
    out = run(_Ctx(client), node="n1", container="csga",
              command=["getent", "hosts", "iiot.chinasws.com"])
    assert out["ok"] is True
    assert client.calls[0]["cmd"] == ["getent", "hosts", "iiot.chinasws.com"]


def test_propagates_agent_side_error():
    from skills.host_exec_in_container_netns import run
    client = _Client(result=_Result(False, stderr="HTTP transport got 404: container_pid_not_found", rc=-1))
    out = run(_Ctx(client), node="n1", container="ghost", command="dig a.com")
    assert out["ok"] is False
    assert "container_pid_not_found" in out["stderr"]

"""host_run_command 进容器 namespace 的回归。

准则:**代码不解析、不限制命令** —— 任意命令原样塞 ``sh -c``;传 ``container`` 就走
agent 的 ``exec_in_container_netns``(进容器 netns、命令在工具镜像里跑),不传走宿主机。
"""

from __future__ import annotations


class _Result:
    def __init__(self, ok=True, stdout="", stderr="", rc=0):
        self.ok, self.stdout, self.stderr, self.returncode = ok, stdout, stderr, rc


class _Client:
    def __init__(self):
        self.netns_calls = []
        self.host_calls = []

    def exec_in_container_netns(self, node, container, inner_cmd, *, namespaces=("n",), timeout=None):
        self.netns_calls.append({
            "node": node, "container": container,
            "cmd": list(inner_cmd), "namespaces": tuple(namespaces),
        })
        return _Result(stdout="ok")

    def nsenter_on_node(self, node, inner_cmd, *, target_pid=1, namespaces=("m", "u", "i", "n", "p"), timeout=None):
        self.host_calls.append({"node": node, "cmd": list(inner_cmd), "namespaces": tuple(namespaces)})
        return _Result(stdout="host")


class _Ctx:
    def __init__(self, c):
        self._c = c

    def connection_for(self, _t, _c):
        return self._c


def test_container_runs_in_netns_default_net_only():
    from skills.host_run_command import run
    c = _Client()
    out = run(_Ctx(c), node="192.168.2.44", container="csga", command="nc -zv 1.2.3.4 8107")
    assert out["ok"] and not c.host_calls            # 没走宿主机路径
    call = c.netns_calls[0]
    assert call["container"] == "csga"
    assert call["namespaces"] == ("n",)              # 进容器默认只换网络 namespace
    assert call["cmd"] == ["sh", "-c", "nc -zv 1.2.3.4 8107"]   # 命令原样塞 sh -c


def test_no_container_runs_on_host_all_namespaces():
    from skills.host_run_command import run
    c = _Client()
    out = run(_Ctx(c), node="n1", command="systemctl status docker")
    assert out["ok"] and not c.netns_calls
    assert c.host_calls[0]["cmd"] == ["sh", "-c", "systemctl status docker"]
    assert c.host_calls[0]["namespaces"] == ("m", "u", "i", "n", "p")   # 宿主机默认全进


def test_command_not_rewritten_or_whitelisted():
    """准则:代码不解析/不限制命令——任意 shell(管道/重定向/写)原样进 sh -c。"""
    from skills.host_run_command import run
    c = _Client()
    cmd = "bash -c 'echo > /dev/tcp/h/9000' | grep -i ok && rm -f /tmp/z"
    run(_Ctx(c), node="n1", container="x", command=cmd)
    assert c.netns_calls[0]["cmd"] == ["sh", "-c", cmd]

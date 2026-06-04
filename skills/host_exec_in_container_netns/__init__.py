"""在目标容器的网络 namespace 里跑网络诊断命令（DNS / 连通性）。

机制（全部交给 agent 内部编排，skill 只传容器名 + 命令）
=====================================================
调 ``client.exec_in_container_netns(node, container, cmd, namespaces=("n",))``：

  1. **agent 内部**把容器名解析成宿主机主进程 PID（docker inspect → crictl → ctr）。
     这是 agent 自己的编排（用挂进来的 docker.sock / runtime socket），**不走业务命令
     白名单**，也绕开 docker_proxy 对 ``docker`` 业务命令的硬拦——所以 swarm/docker 节点
     也能拿到 PID。
  2. agent ``nsenter -t <pid> --net -- <cmd>``：只换**网络** namespace（不换 mount）。
     - direct 模式：agent 自己 nsenter。
     - docker_proxy 模式：agent 起一次性 ``--rm`` 特权 sibling 容器跑，跑完自动清。

关键：只换网络 namespace（``nsenter="n"``），命令仍在 **agent / tools 镜像的文件系统**
里执行——而该镜像自带 ``bind-tools``(dig/nslookup/host) + iproute2(ip/ss)，所以
``dig/nslookup`` 都在，**不会**像在宿主机直接跑那样 ``executable not found``；这些命令
本身也都在 agent 白名单里。

跟 ``host_inspect_container_netns`` 的区别
==========================================
那个进 netns 跑**固定的** ss/ip/iptables;本 skill 跑**你指定的网络诊断命令**
(nslookup/dig/getent/ping...)。

典型场景
========
- 「容器 X 里访问 ``iiot.chinasws.com`` 解析到的 IP 是什么」
- 「容器 X 连不连得通 ``10.0.0.5:5432``」
"""

from __future__ import annotations

import os
import shlex


# 只读网络诊断白名单 —— 本 skill 标 read_only=True（不走二次确认），命令首个二进制必须
# 在白名单里,杜绝借道跑写操作。这些也都在 agent 的 allowed.yml 里。
_ALLOWED_BINARIES = frozenset({
    "nslookup", "dig", "host", "getent", "resolvectl",
    "ping", "ping6", "ping4", "traceroute", "traceroute6", "tracepath", "mtr",
    "ss", "netstat", "ip", "arp", "ethtool",
})


MANIFEST = {
    "code": "host_exec_in_container_netns",
    "name": "容器网络视角诊断（DNS / 连通性）",
    "description": (
        "**在目标容器的网络 namespace 里跑网络诊断命令**(nslookup/dig/getent/ping/ss/ip...)。"
        "agent 内部把容器名解析成宿主机 PID,再 ``nsenter -t <PID> --net`` 进目标容器的网络栈、"
        "但命令在 **agent 镜像**里执行(自带 dig/nslookup/ip/ss)——所以**不会**像在宿主机直接 dig "
        "那样 ``executable not found``。用于「容器内访问某域名解析到哪个 IP / 容器能不能连通某地址」"
        "这类**从容器网络视角**的诊断。"
        "示例:``host_exec_in_container_netns(node='192.168.2.44', container='csga-car-manage_ua', "
        "command='nslookup iiot.chinasws.com')``。"
        "**何时用本 skill**:要从**某个容器的网络视角**做 DNS 解析 / 连通性测试。"
        "**何时不用**:看容器自己的监听端口/路由/iptables 请走 ``host_inspect_container_netns``;"
        "宿主机视角的网络命令请走 ``host_query``;非诊断类的任意命令请改用 ``host_run_command``。"
        "命令报 ``executable not found`` 时换等价工具重试(nslookup→dig→getent hosts),不要停下来问用户。"
    ),
    "category": "host",
    "required_connection_type": "host_agent",
    "read_only": True,
    "visibility": "all",
    "params_schema": {
        "type": "object",
        "properties": {
            "node": {"type": "string", "description": "容器所在节点(host_agent 能解析的节点名/IP)"},
            "container": {"type": "string", "description": "容器名或 ID 前缀(docker ps / crictl ps 看到的;Swarm 用完整 task 名如 svc.1.xxx)"},
            "command": {
                "type": "string",
                "description": "在容器网络里跑的诊断命令,如 ``nslookup iiot.chinasws.com`` / ``dig +short a.com`` / ``ping -c2 10.0.0.5``。只允许网络诊断类只读命令。",
            },
            "connection_id": {"type": "string", "description": "host_agent 接入 id;不传走会话/平台默认"},
        },
        "required": ["node", "container", "command"],
    },
}


def _normalize_command(command) -> list[str]:
    if isinstance(command, (list, tuple)):
        return [str(c) for c in command]
    return shlex.split(str(command or ""))


def run(
    ctx,
    *,
    node: str,
    container: str,
    command,
    connection_id: str | None = None,
) -> dict:
    client = ctx.connection_for("host_agent", connection_id)

    argv = _normalize_command(command)
    if not argv:
        return {"node": node, "container": container, "ok": False,
                "error": "command 为空", "command": command}

    binary = os.path.basename(argv[0])
    if binary not in _ALLOWED_BINARIES:
        return {
            "node": node, "container": container, "ok": False,
            "error": f"命令 ``{binary}`` 不在网络诊断白名单内(本 skill 只读,只允许 "
                     f"nslookup/dig/getent/ping/ss/ip 等)。要跑任意命令请用 host_run_command(需确认)。",
            "command": argv,
            "allowed": sorted(_ALLOWED_BINARIES),
        }

    # agent 内部解析容器 PID + ``nsenter -t <pid> --net`` 跑命令(命令在 agent/tools 镜像里,
    # 自带 dig/nslookup;docker_proxy 模式 agent 自己起 --rm 特权 sibling 跑完清掉)。
    try:
        r = client.exec_in_container_netns(node, container, argv, namespaces=("n",))
    except NotImplementedError as exc:
        return {
            "node": node, "container": container, "ok": False,
            "error": f"该 host_agent 接入不支持容器 netns 诊断(需 transport=http): {exc}",
            "command": argv,
        }

    return {
        "node": node,
        "container": container,
        "command": " ".join(argv),
        "ok": bool(r.ok),
        "stdout": r.stdout,
        "stderr": r.stderr,
        "returncode": r.returncode,
    }

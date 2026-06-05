"""在宿主机 / 某容器的 namespace 内执行**任意命令**（**逃生口**，需 admin 二次确认）。

设计准则
========
**代码不拼接、不限制命令** —— 模型给什么 shell 命令就执行什么（``sh -c <命令>``），
agent 的 ``allowed.yml`` + 平台的二次确认是唯一边界。给模型最大自由度。

进容器 namespace
================
传 ``container`` 就进**那个容器**的 namespace 跑命令（agent 内部解析容器宿主机 PID
再 ``nsenter``）。默认只换**网络** namespace（``namespaces="n"``）——命令在 agent/tools
镜像的文件系统里跑（自带 dig/nslookup/nc/ss/ip...），不会因容器/宿主机没装工具而
``executable not found``。要换其它 namespace 模型自己传 ``namespaces``。
"""

from __future__ import annotations


MANIFEST = {
    "code": "host_run_command",
    "name": "宿主机/容器任意命令（admin）",
    "description": (
        "在指定节点的宿主机、或**某个容器的 namespace** 中执行**任意 shell 命令**——"
        "**这是平台的'逃生口'**，白名单 skill 覆盖不到的临时排障都走这里。"
        "**代码不限制命令内容**，模型给什么跑什么（``sh -c``），由 admin 审批 + 全量审计兜底。"
        "传 ``container`` 就进该容器的 namespace（默认只换网络 namespace,命令在自带 dig/nslookup/"
        "nc/ss/ip 的 agent 镜像里跑,所以容器里没装工具也能从**容器网络视角**做 DNS 解析 / 连通性测试,"
        "如 ``command='nslookup iiot.chinasws.com'`` / ``command='nc -zv 10.0.0.5 5432'``)。"
        "不传 ``container`` = 在宿主机跑(默认进 m/u/i/n/p 全 namespace)。"
        "**当前会话用户(admin)审批后执行**,所有调用全审计。"
        "**何时不用**:常规只读诊断**请走专用 skill**(host_query / kube_query / swarm_query / "
        "zabbix_get_host_overview / host_inspect_container_netns),别拿这个逃生口替代它们。"
    ),
    "category": "host",
    "required_connection_type": "host_agent",
    "read_only": False,
    # ``visibility=admin`` 已经把访问限制在 admin 角色——审批人就是 admin 自己,
    # 不再需要 ``requires_admin_approval=True`` 自己审自己(语义冗余)。当前会话用户点确认即执行。
    "requires_admin_approval": False,
    "visibility": "admin",
    "confirmation_ttl_seconds": 600,
    "params_schema": {
        "type": "object",
        "properties": {
            "node": {"type": "string"},
            "command": {
                "type": "string",
                "description": "要执行的 shell 命令（原样喂给 ``sh -c``,模型可用管道/重定向/任意工具）,例如 ``systemctl status docker`` / ``nslookup a.com`` / ``nc -zv host port``",
            },
            "container": {
                "type": "string",
                "description": "可选:容器名或 ID 前缀。传了就**进该容器的 namespace 跑命令**(agent 内部解析容器宿主机 PID 再 nsenter);Swarm 用 service 名前缀即可。不传 = 在宿主机跑。",
            },
            "namespaces": {
                "type": "string",
                "description": "进入哪些 namespace；m=mnt u=uts i=ipc n=net p=pid。不传时:**进容器默认只 n**(网络;命令在 agent 镜像里跑,工具齐全),**宿主机默认 muinp**(全进)。",
            },
            "connection_id": {"type": "string"},
        },
        "required": ["node", "command"],
    },
}


def run(
    ctx,
    *,
    node: str,
    command: str,
    container: str | None = None,
    namespaces: str | None = None,
    connection_id: str | None = None,
) -> dict:
    client = ctx.connection_for("host_agent", connection_id)
    # 准则:不解析、不改写命令——原样塞进 sh -c,让模型用任意 shell 语法。
    inner = ["sh", "-c", command]

    # namespaces 默认值:进容器只换网络(命令用 agent 镜像的工具),宿主机全进。
    if namespaces is None:
        ns_str = "n" if container else "muinp"
    else:
        ns_str = namespaces
    ns_tuple = tuple(c for c in ns_str if c in {"m", "u", "i", "n", "p", "U", "C"})

    if container:
        result = client.exec_in_container_netns(
            node, container, inner, namespaces=ns_tuple or ("n",))
    else:
        result = client.nsenter_on_node(
            node, inner, namespaces=ns_tuple or ("m", "u", "i", "n", "p"))

    return {
        "node": node,
        "container": container,
        "command": command,
        "namespaces": "".join(ns_tuple) or ("n" if container else "muinp"),
        "ok": result.ok,
        "stdout": result.stdout[-16000:],
        "stderr": result.stderr[-4000:],
        "returncode": result.returncode,
    }

"""在指定节点上执行**任意命令**——平台唯一的命令执行口。

设计准则（大道至简）
====================
**代码不拼接、不限制、不封装命令** —— 模型给什么 shell 命令就执行什么
（``sh -c <命令>``）。agent 的 ``allowed.yml`` + **每次执行弹确认（当前会话用户点
允许/拒绝）** 是仅有的两道边界。给模型最大自由度。

命令在节点的**宿主机 namespace**（``nsenter -t 1``，全 namespace）里跑。要看某个
**容器内部**的视角（DNS / 连通性 / 进程），自己在命令里 ``nsenter`` 进那个容器的
namespace —— 具体套路调 ``platform_get_runbooks(name='container_netns_diag')`` 学。
平台不再为"进容器"单独造 skill。
"""

from __future__ import annotations


MANIFEST = {
    "code": "host_run_command",
    "name": "节点任意命令",
    "description": (
        "在指定节点上执行**任意 shell 命令**——**平台唯一的命令执行口**,主机排障、"
        "看容器、Swarm/K8s 写操作(docker/kubectl)、临时取证,全走这一个。"
        "**代码不限制命令内容**,模型给什么跑什么(原样 ``sh -c``,可用管道/重定向/任意工具),"
        "**每次执行都会弹确认(当前会话用户点允许/拒绝)**,全量审计兜底。"
        "命令默认在**宿主机 namespace**(nsenter -t 1)里跑;要从**某容器网络视角**查 DNS/连通性,"
        "在命令里自己 nsenter 进容器(套路见 ``platform_get_runbooks(name='container_netns_diag')``)。"
        "传 ``node`` 即可——平台按节点名自动路由到它所属集群,无需关心 connection_id。"
        "**只读查 K8s/Swarm 请用 kube_query / swarm_query**(更结构化、省 token);这个口留给主机命令、"
        "写操作、以及那俩覆盖不到的临时排障。"
    ),
    "category": "host",
    "required_connection_type": "host_agent",
    "read_only": False,
    # 每次执行弹确认,审批人就是当前会话用户——不需要单独的 admin 审批(语义冗余)。
    "requires_admin_approval": False,
    "visibility": "admin",
    "confirmation_ttl_seconds": 600,
    "params_schema": {
        "type": "object",
        "properties": {
            "node": {
                "type": "string",
                "description": "目标节点名(或 IP)。平台按节点名自动路由到所属集群。",
            },
            "command": {
                "type": "string",
                "description": (
                    "要执行的 shell 命令,**原样喂给 ``sh -c``**,模型可用管道/重定向/任意工具。"
                    "例:``docker ps -a | grep resource`` / ``nslookup oss.chinasws.com`` / "
                    "``kubectl get pods -n default`` / ``nsenter -t <PID> -n nslookup a.com``"
                ),
            },
            "connection_id": {
                "type": "string",
                "description": "可选,显式指定 host_agent 连接;不传则按 node 自动路由。",
            },
        },
        "required": ["node", "command"],
    },
}


def run(
    ctx,
    *,
    node: str,
    command: str,
    connection_id: str | None = None,
) -> dict:
    # 按 node 路由到它所属集群的 host_agent(连接解析在 ctx 内做)。
    client = ctx.connection_for("host_agent", connection_id, node=node)
    # 准则:不解析、不改写命令——原样塞进 sh -c,让模型用任意 shell 语法。
    # 在宿主机全 namespace(nsenter -t 1 -m -u -i -n -p)里跑;进容器由模型在命令里自己 nsenter。
    result = client.nsenter_on_node(
        node, ["sh", "-c", command], namespaces=("m", "u", "i", "n", "p"))
    return {
        "node": node,
        "command": command,
        "ok": result.ok,
        "stdout": result.stdout[-16000:],
        "stderr": result.stderr[-4000:],
        "returncode": result.returncode,
    }

"""进入指定容器的网络 namespace，看它自己的 socket 监听 / 路由 / iptables。

容器运行时适配
--------------
按以下顺序探测容器主进程的宿主机 PID：
  1. ``docker inspect``  —— 经典 docker / Swarm / K8s 1.23 及之前
  2. ``crictl ps + crictl inspect`` —— containerd / CRI-O 直接管理（K8s 1.24+）
  3. ``ctr -n k8s.io c info`` —— containerd 直连兜底（极少用）

agent 容器需要挂宿主机的 ``/var/run/docker.sock`` 或 ``/run/containerd/containerd.sock``，
具体 ``deploy/ai-ops-agent-k8s.yaml`` 里两个都挂了。
"""

from __future__ import annotations

import json
import re

MANIFEST = {
    "code": "host_inspect_container_netns",
    "name": "进容器网络 namespace 排障",
    "description": (
        "已知容器名（或容器 ID 前缀），定位到容器所在的宿主机进程 PID，"
        "然后 ``nsenter -t <pid> -n`` 进它的 **网络 namespace**，依次跑 "
        "``ss -tnlp / ip a / ip route / iptables-save``。"
        "用于排查「容器自己网络配错 / 容器内监听端口 / 容器看到的路由」。"
        "支持 docker / containerd（自动探测，containerd 集群挂 /run/containerd/containerd.sock 即可）。"
        "vs ``host_query(command='ss -ltnup')`` 看的是宿主机视角；本 skill 看的是容器视角。"
    ),
    "category": "host",
    "required_connection_type": "host_agent",
    "read_only": True,
    "visibility": "all",
    "params_schema": {
        "type": "object",
        "properties": {
            "node": {"type": "string"},
            "container": {"type": "string", "description": "容器名或 ID 前缀（docker ps / crictl ps 看到的）"},
            "connection_id": {"type": "string"},
        },
        "required": ["node", "container"],
    },
}


def _safe_int(s: str) -> int | None:
    s = (s or "").strip()
    try:
        return int(s)
    except ValueError:
        return None


def _try_docker(client, node: str, container: str) -> tuple[int | None, str]:
    r = client.exec_on_node(
        node,
        ["docker", "inspect", "--format", "{{.State.Pid}}", container],
    )
    if not r.ok:
        return None, (r.stderr or r.stdout or "").strip()
    pid = _safe_int(r.stdout)
    return (pid, "") if pid else (None, f"docker inspect 解析失败：{r.stdout!r}")


def _try_crictl(client, node: str, container: str) -> tuple[int | None, str]:
    """crictl 路径——containerd / CRI-O 通用。

    crictl 默认会去找 /run/containerd/containerd.sock 等几条标准 socket；
    如果不行，可以在 agent 镜像里设 CRI_RUNTIME_ENDPOINT 环境变量。
    """
    # 先按 name 查容器 id（crictl 的 --name 是子串匹配）
    r = client.exec_on_node(node, ["crictl", "ps", "-q", "--name", container])
    cid = ""
    if r.ok and r.stdout.strip():
        cid = r.stdout.strip().splitlines()[0]
    else:
        # 也许 user 直接给的是容器 id 前缀
        cid = container

    # 用 go-template 取 PID
    r2 = client.exec_on_node(
        node,
        ["crictl", "inspect", "-o", "go-template",
         "--template", "{{.info.pid}}", cid],
    )
    if r2.ok:
        pid = _safe_int(r2.stdout)
        if pid:
            return pid, ""

    # 退到 JSON 解析
    r3 = client.exec_on_node(node, ["crictl", "inspect", cid])
    if r3.ok and r3.stdout.strip().startswith("{"):
        try:
            data = json.loads(r3.stdout)
            pid = (data.get("info") or {}).get("pid")
            if isinstance(pid, int) and pid > 0:
                return pid, ""
        except json.JSONDecodeError:
            pass
    return None, (r3.stderr or r2.stderr or "crictl 拿不到 PID").strip()


def _try_ctr(client, node: str, container: str) -> tuple[int | None, str]:
    """ctr 是 containerd 的低层 CLI；agent 镜像里多半没有，但留着兜底。"""
    r = client.exec_on_node(
        node,
        ["ctr", "-n", "k8s.io", "container", "info", container],
    )
    if not r.ok or not r.stdout.strip().startswith("{"):
        return None, (r.stderr or r.stdout or "ctr 不可用")
    try:
        data = json.loads(r.stdout)
        pid = (data.get("Status") or {}).get("Pid")
        if isinstance(pid, int) and pid > 0:
            return pid, ""
    except json.JSONDecodeError:
        pass
    # 极少数版本输出 "pid": N（小写）
    m = re.search(r'"pid"\s*:\s*(\d+)', r.stdout)
    if m:
        return int(m.group(1)), ""
    return None, "ctr 输出无法解析"


def _find_container_pid(client, node: str, container: str) -> tuple[int | None, str, str]:
    """返回 (pid, runtime, error)；runtime 用于审计/调试。"""
    pid, err1 = _try_docker(client, node, container)
    if pid:
        return pid, "docker", ""
    pid, err2 = _try_crictl(client, node, container)
    if pid:
        return pid, "crictl", ""
    pid, err3 = _try_ctr(client, node, container)
    if pid:
        return pid, "ctr", ""
    return None, "", f"docker:[{err1}] crictl:[{err2}] ctr:[{err3}]"


def run(ctx, *, node: str, container: str, connection_id: str | None = None) -> dict:
    client = ctx.connection_for("host_agent", connection_id, node=node)
    pid, runtime, err = _find_container_pid(client, node, container)
    if not pid:
        return {
            "node": node,
            "container": container,
            "ok": False,
            "error": "找不到容器 PID（容器不存在 / runtime 不支持 / agent 镜像缺工具）",
            "detail": err,
        }

    sections = [
        ("sockets",    ["ss", "-tnlp"]),
        ("interfaces", ["ip", "-c=never", "addr"]),
        ("routes",     ["ip", "-c=never", "route"]),
        ("iptables",   ["iptables-save"]),
    ]
    out: dict[str, str] = {}
    for key, cmd in sections:
        r = client.nsenter_on_node(node, cmd, target_pid=pid, namespaces=("n",))
        out[key] = r.stdout if r.ok else f"[error] {r.stderr or r.stdout}"
    return {
        "node": node,
        "container": container,
        "pid": pid,
        "runtime": runtime,
        "sections": out,
    }

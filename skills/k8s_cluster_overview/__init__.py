"""K8s 集群巡检 / 体检 —— 一把口聚合 skill（swarm_cluster_overview 的 k8s 对应物）。

为什么单独做一个 skill
======================
跟 swarm 巡检同理:巡检是标准化确定性流程,模型自己一步步挑 skill 容易:
- 漏查某类资源(只看 pod 不看 node 容量)
- N 个节点 N 轮 tool 循环,token 拉满
- 用 k8s node hostname 直接查 Zabbix(查不到,Zabbix 多半按 IP 注册)

把"列节点 → 拿 InternalIP → 用 IP 反查 zabbix → 列异常 pod → 列副本不匹配
deployment"固化进一个 skill,**模型只调一次,平台保证全跑完**。配合
``cluster_health_audit_k8s`` runbook,模型末端只写中文表格化报告。

只采集不判定
============
跟 swarm 版一样:本 skill **不告诉你哪个节点/pod 是"坏"的**(异常判定留给模型),
但会把"明显异常"(pod phase != Running、重启 ≥5、CrashLoop/ImagePull;deployment
ready < desired)单列出来当 raw 线索,省得模型自己翻几百个 pod。

返回结构
========
::

    {
      "k8s": {"node_count", "ready_count", "namespace_count"},
      "nodes": [{"name","role","ready","internal_ip","kubelet_version",
                 "zabbix": {found, overview, storage}}, ...],
      "pods": {"total", "abnormal_count", "abnormal": [{namespace,name,phase,
               node,restart_count,reasons}]},
      "deployments": {"total", "abnormal_count", "abnormal": [{namespace,name,
                      desired,ready,available}]},
      "_signals": [...来自各节点 zabbix overview]
    }
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ops_platform.signals import attach as _attach


logger = logging.getLogger(__name__)


MANIFEST = {
    "code": "k8s_cluster_overview",
    "name": "K8s 集群原始数据采集",
    "description": (
        "**K8s 巡检的 raw 数据采集 skill**——一次调用拉:全部节点(含 InternalIP 反查的 "
        "Zabbix 监控 raw CPU/内存/磁盘) + 全集群异常 pod(CrashLoop/Pending/重启高/拉镜像失败) "
        "+ 副本不匹配的 deployment。"
        "**只采集不判定**:不告诉你哪个节点资源高 / 哪个 pod 该重启,只把 ``kubectl get "
        "nodes/pods/deployments -o json`` 的关键字段 + Zabbix overview 聚到一起返回,"
        "异常综合判断和报告由模型做。"
        "已自动处理 Zabbix hostname 不一致痛点:从 node ``status.addresses`` 提 InternalIP,"
        "用 IP 反查 Zabbix(直接传 k8s node 名多半查不到)。"
        "通常配合 ``cluster_health_audit_k8s`` runbook 触发。"
        "示例:``k8s_cluster_overview()`` 或 ``k8s_cluster_overview(skip_zabbix=true)`` 纯 k8s 巡检。"
        "**何时不用**:看单个 pod/deployment 详情请走 ``kube_query``(verb=describe/get);"
        "swarm 集群巡检请走 ``swarm_cluster_overview``;单主机监控请走 ``zabbix_get_host_overview``。"
    ),
    "category": "k8s",
    "required_connection_type": "k8s",
    "read_only": True,
    "visibility": "all",
    "params_schema": {
        "type": "object",
        "properties": {
            "connection_id": {
                "type": "string",
                "description": "K8s 接入 id;不传走会话/平台默认",
            },
            "zabbix_connection_id": {
                "type": "string",
                "description": "Zabbix 接入 id;不传走会话/平台默认",
            },
            "lookback_hours": {
                "type": "number", "default": 1, "minimum": 0.25, "maximum": 168,
                "description": "Zabbix 查询窗口(小时);默认 1",
            },
            "skip_zabbix": {
                "type": "boolean", "default": False,
                "description": "true=纯 K8s 巡检不查 Zabbix",
            },
            "restart_threshold": {
                "type": "integer", "default": 5, "minimum": 1,
                "description": "pod 重启次数 ≥ 该值算异常;默认 5",
            },
        },
    },
}


# 拉镜像 / 启动失败类的 waiting reason —— 算异常
_BAD_WAITING_REASONS = frozenset({
    "CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull",
    "ErrImageNeverPull", "CreateContainerError", "CreateContainerConfigError",
    "InvalidImageName",
})


def _kubectl_json(client, args: list[str]) -> dict:
    """跑 ``kubectl [--context X] <args>`` 并 JSON 解析;失败抛 RuntimeError。

    用于 cluster-scoped(nodes)和 -A(all namespaces)查询——不加 ``-n``。
    """
    full = (["--context", client.context] if getattr(client, "context", None) else []) + args
    result = client.run(full)
    if not result.ok:
        raise RuntimeError(
            f"kubectl {' '.join(args)} 失败 (rc={result.returncode}): "
            f"{result.stderr or result.stdout or '(无输出)'}"
        )
    if not (result.stdout or "").strip():
        return {}
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"kubectl {' '.join(args)} 输出非 JSON: {exc}")


def _parse_node(item: dict) -> dict:
    """从 ``kubectl get nodes -o json`` 的单个 item 提关键字段。"""
    meta = item.get("metadata", {}) or {}
    status = item.get("status", {}) or {}
    labels = meta.get("labels", {}) or {}

    roles = [k.split("/", 1)[-1] for k in labels
             if k.startswith("node-role.kubernetes.io/") and k.split("/", 1)[-1]]
    role = ",".join(sorted(roles)) or "worker"

    ready = "Unknown"
    for cond in status.get("conditions", []) or []:
        if cond.get("type") == "Ready":
            ready = "Ready" if cond.get("status") == "True" else "NotReady"
            break

    internal_ip = ""
    for addr in status.get("addresses", []) or []:
        if addr.get("type") == "InternalIP":
            internal_ip = (addr.get("address") or "").strip()
            break

    node_info = status.get("nodeInfo", {}) or {}
    return {
        "name": meta.get("name", ""),
        "role": role,
        "ready": ready,
        "internal_ip": internal_ip,
        "kubelet_version": node_info.get("kubeletVersion", ""),
        "os_image": node_info.get("osImage", ""),
    }


# 异常 pod 详情列表上限——防止集群里堆积的 Evicted/Failed pod 把报告表格和 token 撑爆。
# count 仍是准确总数,只截断**明细列表**。
_MAX_ABNORMAL_PODS = 40


def _parse_pods(items: list[dict], restart_threshold: int) -> dict[str, Any]:
    """列异常 pod —— phase 异常 / 重启高 / 拉镜像或启动失败。只挑明显异常,raw 线索。

    ``abnormal_count`` 是准确总数;``abnormal`` 明细最多 ``_MAX_ABNORMAL_PODS`` 条
    (按 severity 粗排:拉镜像/启动失败 > 重启高 > phase 异常),避免几百个 Evicted
    pod 把报告撑爆。``truncated`` 标记是否截断。
    """
    abnormal: list[dict] = []
    for p in items:
        meta = p.get("metadata", {}) or {}
        status = p.get("status", {}) or {}
        spec = p.get("spec", {}) or {}
        phase = status.get("phase", "")

        restart = 0
        reasons: list[str] = []
        for cs in status.get("containerStatuses", []) or []:
            restart += int(cs.get("restartCount", 0) or 0)
            waiting = (cs.get("state", {}) or {}).get("waiting", {}) or {}
            r = waiting.get("reason")
            if r:
                reasons.append(r)

        has_bad_reason = any(r in _BAD_WAITING_REASONS for r in reasons)
        is_abnormal = (
            phase not in ("Running", "Succeeded")
            or restart >= restart_threshold
            or has_bad_reason
        )
        if is_abnormal:
            # severity 粗排序权重:拉镜像/启动失败(2) > 重启高(1) > 仅 phase 异常(0)
            severity = 2 if has_bad_reason else (1 if restart >= restart_threshold else 0)
            abnormal.append({
                "namespace": meta.get("namespace", ""),
                "name": meta.get("name", ""),
                "phase": phase,
                "node": spec.get("nodeName", "") or status.get("hostIP", ""),
                "restart_count": restart,
                "reasons": sorted(set(reasons)),
                "_severity": severity,
            })

    total_abnormal = len(abnormal)
    abnormal.sort(key=lambda x: (-x["_severity"], -x["restart_count"]))
    shown = abnormal[:_MAX_ABNORMAL_PODS]
    for x in shown:
        x.pop("_severity", None)
    return {
        "total": len(items),
        "abnormal_count": total_abnormal,
        "abnormal": shown,
        "truncated": total_abnormal > _MAX_ABNORMAL_PODS,
    }


def _parse_deployments(items: list[dict]) -> dict[str, Any]:
    """列副本不匹配的 deployment(ready < desired)。"""
    abnormal: list[dict] = []
    for d in items:
        meta = d.get("metadata", {}) or {}
        spec = d.get("spec", {}) or {}
        status = d.get("status", {}) or {}
        desired = int(spec.get("replicas", 0) or 0)
        ready = int(status.get("readyReplicas", 0) or 0)
        if ready < desired:
            abnormal.append({
                "namespace": meta.get("namespace", ""),
                "name": meta.get("name", ""),
                "desired": desired,
                "ready": ready,
                "available": int(status.get("availableReplicas", 0) or 0),
            })
    return {"total": len(items), "abnormal_count": len(abnormal), "abnormal": abnormal}


def _zabbix_overview_for_ip(zabbix, ip: str, lookback_hours: float | int) -> dict:
    """单节点 Zabbix 反查(按 InternalIP),拼 ``{found, overview, storage}``。

    跟 swarm_cluster_overview 同逻辑:overview + storage 是两个独立 RPC。
    """
    if not ip:
        return {"found": False, "reason": "node 没拿到 InternalIP"}
    try:
        overview = zabbix.get_host_overview(ip, lookback_hours=lookback_hours)
    except ValueError as exc:
        return {"found": False, "reason": str(exc)}
    except Exception as exc:    # noqa: BLE001
        logger.warning("get_host_overview(%s) 失败:%s", ip, exc)
        return {"found": False, "reason": f"zabbix overview 异常: {exc}"}

    out: dict = {"found": True, "overview": overview}
    try:
        out["storage"] = zabbix.get_host_storage_overview(ip, lookback_hours=lookback_hours)
    except Exception as exc:    # noqa: BLE001
        out["storage_error"] = str(exc)
        logger.warning("get_host_storage_overview(%s) 失败:%s", ip, exc)
    return out


def _enrich_with_zabbix(ctx, nodes: list[dict], zabbix_connection_id: str | None,
                        lookback_hours: float | int) -> list[dict]:
    """给每个 node 加 ``zabbix`` 字段;返回累积的 signals。"""
    try:
        zabbix = ctx.connection_for("zabbix", zabbix_connection_id)
    except Exception as exc:    # noqa: BLE001
        for n in nodes:
            n["zabbix"] = {"found": False, "reason": f"未配置 Zabbix 接入: {exc}"}
        return []

    signals: list[dict] = []
    for n in nodes:
        result = _zabbix_overview_for_ip(zabbix, n.get("internal_ip") or "", lookback_hours)
        n["zabbix"] = result
        if result.get("found"):
            signals.extend((result.get("overview") or {}).get("_signals") or [])
    return signals


def run(
    ctx,
    *,
    connection_id: str | None = None,
    zabbix_connection_id: str | None = None,
    lookback_hours: float | int = 1,
    skip_zabbix: bool = False,
    restart_threshold: int = 5,
) -> dict[str, Any]:
    client = ctx.connection_for("k8s", connection_id)

    # 1. 列节点
    nodes_raw = (_kubectl_json(client, ["get", "nodes", "-o", "json"]).get("items") or [])
    nodes_out = [_parse_node(n) for n in nodes_raw]

    # 2. 用 InternalIP 反查 Zabbix(可关)
    all_signals: list[dict] = []
    if not skip_zabbix:
        all_signals = _enrich_with_zabbix(ctx, nodes_out, zabbix_connection_id, lookback_hours)
    else:
        for n in nodes_out:
            n["zabbix"] = {"found": False, "reason": "skip_zabbix=True"}

    # 3. 全集群异常 pod
    pods_raw = (_kubectl_json(client, ["get", "pods", "-A", "-o", "json"]).get("items") or [])
    pods_block = _parse_pods(pods_raw, restart_threshold)

    # 4. 副本不匹配 deployment
    deploys_raw = (_kubectl_json(client, ["get", "deployments", "-A", "-o", "json"]).get("items") or [])
    deploys_block = _parse_deployments(deploys_raw)

    # 命名空间数(从 pod namespace 去重粗估;不额外发一次 get ns)
    namespaces = {(p.get("metadata", {}) or {}).get("namespace", "") for p in pods_raw}
    namespaces.discard("")

    payload: dict[str, Any] = {
        "k8s": {
            "node_count": len(nodes_out),
            "ready_count": sum(1 for n in nodes_out if n["ready"] == "Ready"),
            "namespace_count": len(namespaces),
        },
        "nodes": nodes_out,
        "pods": pods_block,
        "deployments": deploys_block,
    }
    return _attach(payload, all_signals)

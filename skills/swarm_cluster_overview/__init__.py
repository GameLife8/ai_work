"""Swarm 集群巡检 / 体检 —— 一把口聚合 skill。

为什么单独做一个 skill 而不是让模型自己挑
========================================
巡检 = 标准化、确定性的流程,模型自己一步一步挑 skill 会出问题:

- 容易拍脑袋写一个 ``worker1.chinasws.com`` 直接查 Zabbix(查不到,因为 Zabbix
  注册主机时多半用 IP 而不是 hostname)
- 失败了不知道回头从 ``swarm node inspect`` 拿 ``Status.Addr`` 反查
- N 个节点要 N 轮 tool 循环,token 拉满

把"列节点 → 每节点 inspect 拿 IP → 用 IP 反查 zabbix → 列服务 → 异常筛选"
这条路径固化到一个 skill 里,**模型只调一次,平台保证全跑完**。配合
``cluster_health_audit_swarm`` runbook,模型在末端只写中文表格化报告。

K8s 等价 skill 后续做(``k8s_cluster_overview``)。

返回数据结构
============
::

    {
      "swarm": {"node_count", "manager_count", "worker_count"},
      "nodes": [
        {
          "hostname", "id", "role" ("manager"|"worker"),
          "availability", "state", "manager_status",
          "addr",            # Status.Addr,通常就是节点 IP
          "zabbix": {
            "found": True,
            "overview": {...,zabbix_get_host_overview 完整返回...}
          }
          # 或 {"found": False, "reason": "..."}
        },
        ...
      ],
      "services": {
        "total", "abnormal_count",
        "abnormal": [{"name","mode","replicas","replicas_running",
                      "replicas_desired","image","reason"}],
        "all": [...,可选]
      },
      "_signals": [...,从各节点 zabbix overview 累积]
    }
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ops_platform.signals import attach as _attach


logger = logging.getLogger(__name__)


MANIFEST = {
    "code": "swarm_cluster_overview",
    "name": "Swarm 集群原始数据采集",
    "description": (
        "**Swarm 巡检的 raw 数据采集 skill**——一次调用拉:全部节点(含 IP 反查的 "
        "Zabbix 监控 raw 数据 CPU/内存/磁盘) + 全部服务清单。"
        "**只采集不判定**:本 skill **不告诉你哪个节点资源高 / 哪个服务异常**,只把"
        "``docker node ls / inspect`` 和 ``zabbix.get_host_overview`` 的原始字段聚到一起返回。"
        "**异常判断由调用方(模型)自己做**——看每个节点的 metric_summary.cpu_avg、"
        "memory_summary.memory_used_percent、storage_summary,看每个服务的 Replicas 字段。"
        "已自动处理 Zabbix hostname 不一致的痛点:平台从 ``docker node inspect`` 提 "
        "``Status.Addr`` 是节点 IP,用 IP 反查 Zabbix(直接传 swarm hostname 多半查不到)。"
        "通常配合 ``cluster_health_audit_swarm`` runbook 触发,模型基于本 skill 的 raw 数据"
        "自行综合 + 写报告。"
    ),
    "category": "swarm",
    "required_connection_type": "swarm",
    "read_only": True,
    "visibility": "all",
    "params_schema": {
        "type": "object",
        "properties": {
            "connection_id": {
                "type": "string",
                "description": "Swarm 接入 id;不传走会话/平台默认",
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
                "description": "true=纯 Swarm 巡检不查 Zabbix",
            },
        },
    },
}


def _parse_docker_json_lines(stdout: str) -> list[dict]:
    """``docker ... --format '{{json .}}'`` 输出每行一个 JSON,合并为 list。"""
    rows: list[dict] = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _list_nodes(client) -> list[dict]:
    result = client.run(["node", "ls", "--format", "{{json .}}"])
    if not result.ok:
        raise RuntimeError(
            f"docker node ls 失败 (rc={result.returncode}): "
            f"{result.stderr or result.stdout or '(无输出)'}"
        )
    return _parse_docker_json_lines(result.stdout)


def _inspect_node(client, node_ref: str) -> dict | None:
    """``docker node inspect <ref> --format '{{json .}}'`` —— 失败返回 None,不抛。"""
    if not node_ref:
        return None
    result = client.run(["node", "inspect", node_ref, "--format", "{{json .}}"])
    if not result.ok:
        logger.warning("docker node inspect %s 失败: %s", node_ref, result.stderr)
        return None
    rows = _parse_docker_json_lines(result.stdout)
    return rows[0] if rows else None


def _list_services(client) -> list[dict]:
    result = client.run(["service", "ls", "--format", "{{json .}}"])
    if not result.ok:
        raise RuntimeError(
            f"docker service ls 失败 (rc={result.returncode}): "
            f"{result.stderr or result.stdout or '(无输出)'}"
        )
    return _parse_docker_json_lines(result.stdout)


def _classify_role(node_raw: dict) -> str:
    """从 docker node ls 行判断 manager / worker。"""
    manager_status = node_raw.get("ManagerStatus") or ""
    if manager_status:
        return "manager"
    return "worker"


def _collect_node_addrs(swarm, nodes_raw: list[dict]) -> list[dict]:
    """对每个 node ls 行调一次 inspect 拿 IP / 完整 role / 状态。"""
    out: list[dict] = []
    for n in nodes_raw:
        node_id = n.get("ID") or ""
        hostname = n.get("Hostname") or ""
        availability = n.get("Availability") or ""
        state = n.get("Status") or ""
        manager_status = n.get("ManagerStatus") or ""
        role = _classify_role(n)

        inspect = _inspect_node(swarm, node_id or hostname)
        addr = ""
        if isinstance(inspect, dict):
            addr = ((inspect.get("Status") or {}).get("Addr") or "").strip()

        out.append({
            "hostname": hostname,
            "id": node_id,
            "role": role,
            "availability": availability,
            "state": state,
            "manager_status": manager_status,
            "addr": addr,
        })
    return out


def _zabbix_overview_for_addr(zabbix, addr: str, lookback_hours: float | int) -> dict:
    """单节点 Zabbix 反查,拼装 ``{found, overview, storage}`` 结构。

    overview / storage 是 **两个独立的 Zabbix RPC**:
      - ``get_host_overview``        → CPU / 内存 / 可用性
      - ``get_host_storage_overview`` → 每挂载点的 ``used_percent`` / ``free_gb``

    单独调,因为 ``get_host_overview`` 不返回 filesystems list。skill 把两个 raw
    拼在一起返回,模型自己从中提"最高磁盘挂载点"等聚合视图。
    """
    if not addr:
        return {"found": False, "reason": "swarm node inspect 没拿到 Status.Addr"}
    try:
        overview = zabbix.get_host_overview(addr, lookback_hours=lookback_hours)
    except ValueError as exc:
        # ZabbixClient.find_host 没找到 → ValueError("未找到主机: ...")
        return {"found": False, "reason": f"Zabbix 中找不到 IP={addr} 的主机: {exc}"}
    except Exception as exc:    # noqa: BLE001
        return {"found": False, "reason": f"Zabbix 查询失败: {exc}"}

    # 拉 storage 独立 try,失败不影响 overview 已经成功的事实
    storage: dict | None = None
    storage_error: str | None = None
    try:
        storage = zabbix.get_host_storage_overview(addr, lookback_hours=lookback_hours)
    except Exception as exc:    # noqa: BLE001
        storage_error = str(exc)
        logger.warning("get_host_storage_overview(%s) 失败:%s", addr, exc)

    out: dict = {"found": True, "overview": overview}
    if storage is not None:
        out["storage"] = storage
    if storage_error is not None:
        out["storage_error"] = storage_error
    return out


def _enrich_with_zabbix(
    ctx, nodes: list[dict], zabbix_connection_id: str | None,
    lookback_hours: float | int,
) -> list[dict]:
    """给 nodes 列表每条加 ``zabbix`` 字段;同时返回累积的 signals。"""
    try:
        zabbix = ctx.connection_for("zabbix", zabbix_connection_id)
    except Exception as exc:    # noqa: BLE001
        for n in nodes:
            n["zabbix"] = {"found": False, "reason": f"未配置 Zabbix 接入: {exc}"}
        return []

    signals: list[dict] = []
    for n in nodes:
        result = _zabbix_overview_for_addr(zabbix, n.get("addr") or "", lookback_hours)
        n["zabbix"] = result
        if result.get("found"):
            sigs = (result.get("overview") or {}).get("_signals") or []
            signals.extend(sigs)
    return signals


def _build_services_block(services_raw: list[dict]) -> dict[str, Any]:
    """只采集,不判定:把 ``docker service ls`` 的字段原样转成 list。

    设计哲学:**异常判定由模型做**(看到 Replicas="0/3" 自己判全宕),不在 skill
    里偷偷筛 abnormal —— 那会剥夺模型的解析空间,违反"runbook 给思路 + skill 给
    原料 + 模型综合"的分层。
    """
    items: list[dict] = []
    for s in services_raw:
        items.append({
            "name": s.get("Name") or "",
            "mode": s.get("Mode") or "",
            "replicas": s.get("Replicas") or "",     # 原文 "2/2" / "0/3" 等
            "image": s.get("Image") or "",
            "ports": s.get("Ports") or "",
        })
    return {"total": len(items), "items": items}


def run(
    ctx,
    *,
    connection_id: str | None = None,
    zabbix_connection_id: str | None = None,
    lookback_hours: float | int = 1,
    skip_zabbix: bool = False,
) -> dict[str, Any]:
    swarm = ctx.connection_for("swarm", connection_id)

    # 1. 列节点 + 2. 每节点 inspect 拿 IP
    nodes_raw = _list_nodes(swarm)
    nodes_out = _collect_node_addrs(swarm, nodes_raw)

    # 3. 用 IP 反查 Zabbix(可关) —— raw overview 数据塞进 node 的 zabbix 字段
    all_signals: list[dict] = []
    if not skip_zabbix:
        all_signals = _enrich_with_zabbix(
            ctx, nodes_out, zabbix_connection_id, lookback_hours,
        )
    else:
        for n in nodes_out:
            n["zabbix"] = {"found": False, "reason": "skip_zabbix=True"}

    # 4. 列服务(raw,不判异常)
    services_raw = _list_services(swarm)
    services_block = _build_services_block(services_raw)

    # 5. 组装 — 只汇总元信息,不做任何 "好/坏" 判定
    manager_count = sum(1 for n in nodes_out if n["role"] == "manager")
    payload: dict[str, Any] = {
        "swarm": {
            "node_count": len(nodes_out),
            "manager_count": manager_count,
            "worker_count": len(nodes_out) - manager_count,
        },
        "nodes": nodes_out,
        "services": services_block,
    }
    return _attach(payload, all_signals)

from __future__ import annotations

import logging

from ops_platform.signals import (
    SEV_CRITICAL, SEV_WARNING,
    SIG_AGENT_DOWN, SIG_HIGH_CPU, SIG_HIGH_DISK, SIG_HIGH_MEM,
    SIG_HOST_UNREACHABLE,
    attach, signal,
)


logger = logging.getLogger(__name__)


MANIFEST = {
    "code": "zabbix_get_host_overview",
    "name": "主机概览（CPU + 内存 + 磁盘 + 可用性）",
    "description": (
        "获取主机的**全量基础信息**：可用性（ping / agent）+ CPU 用率 + 内存用率 + "
        "**所有挂载点磁盘容量**。一次调用就能回答「主机基础信息 / 主机当前状态 / 主机概览」。"
        "**两类典型场景**："
        "(1) 用户直接问「基础信息 / 主机概况 / 主机情况」——直接调本 skill 即可；"
        "(2) 容器/Pod 排障时把 swarm task.Node / k8s pod.node 当作 host_query 反查——"
        "    验证容器异常是不是宿主机 CPU/MEM/DISK 扛不住引起的。"
        "返回里 cpu_avg/max/min/p95、memory_avg/max/p95 全部基于窗口内**全量原始点**精确算出，"
        "跟 Zabbix dashboard 完全一致；尖峰、长尾都不会被采样错过。"
        "信号判定：CPU avg ≥ 80%（持续满载，critical）或 p95 ≥ 80%（长尾压力，warning）；"
        "MEM 当前 ≥ 90% 或窗口内 max ≥ 90%；任一挂载点 used ≥ 90% → 宿主机层根因。"
        "agent 不可用 / ping 不通 → 主机宕机或网络隔离，要换另一条排查路径。"
    ),
    "category": "zabbix",
    "required_connection_type": "zabbix",
    "read_only": True,
    "visibility": "all",
    "params_schema": {
        "type": "object",
        "properties": {
            "host_query": {"type": "string"},
            "connection_id": {"type": "string"},
            "include_storage": {
                "type": "boolean",
                "default": True,
                "description": "是否一并返回磁盘信息；默认 true",
            },
            "lookback_hours": {
                "type": "number",
                "default": 1,
                "minimum": 0.25,
                "maximum": 720,
                "description": (
                    "分析回看窗口（小时）。影响 metric_summary / memory_summary 的 avg/max/trend 是基于多长时间算的。"
                    "默认 1；用户问「24 小时数据」传 24，「近一周」传 168。窗口越大平台自动用越大的采样间隔（24h→1h间隔，"
                    "7d→6h 间隔），始终保持 12-30 个采样点。"
                ),
            },
        },
        "required": ["host_query"],
    },
}


def _extract_signals(result: dict) -> list[dict]:
    """从聚合结果里识别异常信号——驱动跨域 pivot 用。"""
    sigs: list[dict] = []
    host = (result.get("host") or {}).get("host_name") or ""

    avail = result.get("availability_summary") or {}
    if avail.get("ping_status") == "down" or avail.get("agent_status") == "down":
        last_seen = avail.get("last_seen_minutes_ago")
        sigs.append(signal(
            SIG_HOST_UNREACHABLE if avail.get("ping_status") == "down" else SIG_AGENT_DOWN,
            severity=SEV_CRITICAL,
            evidence=(
                f"主机 {host} ping={avail.get('ping_status')} agent={avail.get('agent_status')}"
                + (f"，最后心跳 {last_seen} 分钟前" if last_seen is not None else "")
            ),
        ))

    mem = result.get("memory_summary") or {}
    used_pct = mem.get("memory_used_percent")
    mem_max = mem.get("memory_max_percent")
    if isinstance(used_pct, (int, float)) and used_pct >= 90:
        sigs.append(signal(
            SIG_HIGH_MEM, severity=SEV_CRITICAL,
            evidence=f"主机 {host} 当前内存使用率 {used_pct}%（>= 90%）",
        ))
    elif isinstance(mem_max, (int, float)) and mem_max >= 90:
        # 当前回落但窗口内出现过 90%+：内存压力近期发生过，仍值得关注
        sigs.append(signal(
            SIG_HIGH_MEM, severity=SEV_WARNING,
            evidence=(
                f"主机 {host} 窗口内最高内存 {mem_max}%（当前 {used_pct}% 已回落，"
                f"近期发生过内存压力）"
            ),
        ))

    metric = result.get("metric_summary") or {}
    cpu_avg = metric.get("cpu_avg")
    cpu_p95 = metric.get("cpu_p95")
    cpu_max = metric.get("cpu_max")
    if isinstance(cpu_avg, (int, float)) and cpu_avg >= 80:
        # 平均 ≥ 80：持续满载，根因级
        sigs.append(signal(
            SIG_HIGH_CPU, severity=SEV_CRITICAL,
            evidence=f"主机 {host} CPU 平均用率 {cpu_avg}%（>= 80%，持续满载）",
        ))
    elif isinstance(cpu_p95, (int, float)) and cpu_p95 >= 80:
        # 平均不高但 5% 时间在告急：长尾尖峰、突发压力
        sigs.append(signal(
            SIG_HIGH_CPU, severity=SEV_WARNING,
            evidence=(
                f"主机 {host} CPU p95 {cpu_p95}%（avg {cpu_avg}% / max {cpu_max}%，"
                f"窗口内 5% 时间持续高负载）"
            ),
        ))

    for fs in result.get("filesystems") or []:
        used = fs.get("used_percent")
        if isinstance(used, (int, float)) and used >= 90:
            sigs.append(signal(
                SIG_HIGH_DISK, severity=SEV_CRITICAL,
                evidence=(
                    f"主机 {host} 挂载点 {fs.get('mount_point')} "
                    f"使用率 {used}%（剩 {fs.get('free_gb')} GB / "
                    f"共 {fs.get('total_gb')} GB）"
                ),
                context={"node": host, "mount_point": fs.get("mount_point")},
            ))
    return sigs


def run(ctx, *, host_query: str, connection_id: str | None = None,
        include_storage: bool = True, lookback_hours: float = 1) -> dict:
    client = ctx.connection_for("zabbix", connection_id)
    overview = client.get_host_overview(host_query, lookback_hours=lookback_hours)

    # 一并拿磁盘信息：放在同一份结果里，admin 看一次就够了
    if include_storage:
        try:
            storage = client.get_host_storage_overview(host_query, lookback_hours=lookback_hours)
            overview["filesystems"] = storage.get("filesystems") or []
        except Exception as exc:
            logger.warning("zabbix_get_host_overview 拿磁盘失败：%s", exc)
            overview["filesystems"] = []
            overview["storage_error"] = str(exc)

    return attach(overview, _extract_signals(overview))

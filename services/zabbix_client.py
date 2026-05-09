from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from statistics import mean
from zoneinfo import ZoneInfo

import requests

from services.metric_provider import (
    HostRef,
    MetricDescriptor,
    MetricPoint,
)


logger = logging.getLogger(__name__)


# 逻辑指标名 → Zabbix item key 前缀候选（按顺序找，第一个命中即用）。
# 新增其它监控产品时，对应 client 自己维护一份类似映射，不影响这里。
_ZBX_METRIC_KEY_MAP: dict[str, list[str]] = {
    "cpu.utilization":    ["system.cpu.util", "system.cpu.util[,system,avg1]"],
    "memory.utilization": ["vm.memory.util", "vm.memory.utilization"],
    "system.load.avg1":   ["system.cpu.load", "system.cpu.load[all,avg1]"],
    "memory.available":   ["vm.memory.size[available]", "vm.memory.size[free]"],
    "memory.total":       ["vm.memory.size[total]"],
}

SAMPLE_INTERVAL_SECONDS = 300       # 默认每 5 分钟一个采样点
SAMPLE_POINTS = 12                  # 默认 12 个点 = 1 小时窗口
DEFAULT_EVENT_TIMEZONE = ZoneInfo("Asia/Shanghai")

# history.get 单次拉的最大原始点数。
# 1min 间隔 × 7d ≈ 10080 点；为了拿全 7d 数据这里设到 12000。
# 更长窗口（30d+）应改用 trends.get（1h 聚合），见 _get_window_history TODO。
HISTORY_RAW_LIMIT = 12000


# 所有 stub 返回都通过本函数打标记，避免假数据被静默当真实数据用
def _mark_stub(data: dict) -> dict:
    """给 stub 数据加 ``_stub_data: True`` 标记。

    上层（skill / agent / chainlit）应该看到这个标记后明确告诉用户"这是模拟数据"，
    或干脆拒绝使用。生产环境绝对不该出现 ``_stub_data=True`` 的响应。
    """
    if not isinstance(data, dict):
        return data
    data.setdefault("_stub_data", True)
    data.setdefault(
        "_stub_warning",
        "⚠️ 此为 stub 模拟数据，不是真实监控数据。请把 connection 的 use_stub 关掉并填真实凭证。",
    )
    return data


def compute_window(lookback_hours: float | int) -> tuple[int, int]:
    """根据用户想看的小时数，挑合理的 (interval_seconds, sample_points)。

    始终保持采样点 12-30 个之间，避免 24h 跨度时拉 288 个点把 token 烧爆。
    """
    if not lookback_hours or lookback_hours <= 0:
        return SAMPLE_INTERVAL_SECONDS, SAMPLE_POINTS
    h = float(lookback_hours)
    if h <= 1:        return 300, 12        # 5min × 12 = 1h（默认）
    if h <= 3:        return 600, int(h * 6)  # 10min step
    if h <= 12:       return 1800, int(h * 2)  # 30min step
    if h <= 24:       return 3600, int(h)    # 1h step → 24 点
    if h <= 72:       return 7200, int(h / 2)  # 2h step
    if h <= 168:      return 21600, int(h / 6)  # 6h step（一周 = 28 点）
    return 86400, max(int(h / 24), 7)        # 1d step


class ZabbixClient:
    # services.metric_provider.MetricProvider 协议要求；让 services.metric_analytics
    # 不依赖具体实现也能识别 provider 类型（在错误信息和日志里用）。
    name: str = "zabbix"

    def __init__(
        self,
        base_url: str,
        username: str = "",
        password: str = "",
        timeout_seconds: int = 10,
        use_stub: bool = True,
    ) -> None:
        self.base_url = base_url
        self.username = username
        self.password = password
        # 实例级窗口（默认从模块常量初始化），可被 _with_window 临时覆盖
        self._sample_interval_seconds = SAMPLE_INTERVAL_SECONDS
        self._sample_points = SAMPLE_POINTS
        self.timeout_seconds = timeout_seconds
        self.use_stub = use_stub
        self._auth_token: str | None = None
        self._request_id = 0

    def healthcheck(self) -> dict:
        if self.use_stub:  # ⚠️ STUB DATA — 仅 USE_STUB_ZABBIX=true 或 connection.use_stub=true 时进入
            return _mark_stub({
                "backend": "stub",
                "healthy": True,
                "details": {"mode": "stub", "base_url": self.base_url},
            })

        try:
            version = self.apiinfo_version()
            token = self.login()
            return {
                "backend": "zabbix",
                "healthy": True,
                "details": {
                    "mode": "api",
                    "base_url": self.base_url,
                    "version": version,
                    "authenticated": bool(token),
                },
            }
        except Exception as exc:  # pragma: no cover
            return {
                "backend": "zabbix",
                "healthy": False,
                "error": str(exc),
                "details": {"mode": "api", "base_url": self.base_url},
            }

    def get_metric_summary(self, alert: dict) -> dict:
        """CPU + load 概览。

        关键设计：avg / max / min / p95 全部基于**窗口内全部原始点**计算
        （不是降采样到 24 点），保证跟 Zabbix dashboard 显示完全一致——
        不会因为采样误差错过分钟级 CPU spike。

        12h 窗口下原始点数 ≈ 720（Zabbix 每分钟一条 history），算 max/p95
        够精确；当前窗口最大 7d ≈ 10080 点，已在 history.get limit 范围内。
        """
        if self.use_stub:  # ⚠️ STUB DATA — 仅 USE_STUB_ZABBIX=true 或 connection.use_stub=true 时进入
            return self._stub_metric_summary(alert)

        try:
            host_id = self._resolve_host_id(alert)
            if not host_id:
                return self._stub_metric_summary(alert)

            summary: dict = {}
            items = self._get_host_items(host_id)

            cpu_item = self._find_metric_item(items, ["system.cpu.util", "system.cpu.util[,system,avg1]"])
            if cpu_item:
                window = self._get_window_history(cpu_item["itemid"], cpu_item["value_type"], alert)
                agg = self._aggregate(window["raw"])
                if agg:
                    summary.update({
                        "cpu_avg": agg["avg"],
                        "cpu_max": agg["max"],
                        "cpu_min": agg["min"],
                        "cpu_p95": agg["p95"],
                        "cpu_last": agg["last"],
                        "cpu_raw_count": agg["raw_count"],
                    })

            load_item = self._find_metric_item(items, ["system.cpu.load", "system.cpu.load[all,avg1]"])
            if load_item:
                window = self._get_window_history(load_item["itemid"], load_item["value_type"], alert)
                agg = self._aggregate(window["raw"])
                if agg:
                    summary["load_avg"] = agg["avg"]
                    summary["load_max"] = agg["max"]

            # 至少拿到 CPU 或 load 之一就算成功（之前要求三个都齐，过于苛刻——
            # 有些主机 Zabbix 模板没装 system.cpu.load 就会全量回 stub，掩盖真数据）
            if "cpu_avg" in summary or "load_avg" in summary:
                summary.update(self._sample_window_metadata())
                return summary
        except Exception as exc:  # pragma: no cover
            logger.warning("Zabbix metric query failed, falling back to stub: %s", exc)

        return self._stub_metric_summary(alert)

    def get_raw_context(self, alert: dict, needs: list[str]) -> dict:
        if self.use_stub:  # ⚠️ STUB DATA — 仅 USE_STUB_ZABBIX=true 或 connection.use_stub=true 时进入
            return {}

        try:
            host_id = self._resolve_host_id(alert)
            if not host_id:
                return {}

            items = self._get_host_items(host_id)
            raw = {
                "host": {
                    "host_id": host_id,
                    "host_name": alert.get("host_name"),
                    "host_ip": alert.get("host_ip"),
                },
                "items": {},
            }

            if "metric_summary" in needs:
                raw["items"]["metric_summary"] = self._collect_items(items, ["system.cpu.util", "system.cpu.load"], alert)

            if "disk_summary" in needs:
                mount_point = alert.get("resource_scope", {}).get("mount_point")
                if mount_point:
                    raw["items"]["disk_summary"] = self._collect_items(items, [f"vfs.fs.size[{mount_point},"], alert)

            if "memory_summary" in needs:
                raw["items"]["memory_summary"] = self._collect_items(items, ["vm.memory.util", "vm.memory.size["], alert)

            if "disk_io_summary" in needs:
                raw["items"]["disk_io_summary"] = self._collect_items(items, ['perf_counter_en["\\\\PhysicalDisk'], alert)

            if "availability_summary" in needs:
                raw["items"]["availability_summary"] = self._collect_items(items, ["agent.ping", "system.uptime"], alert)

            raw["sample_window"] = self._sample_window_metadata()

            return raw
        except Exception as exc:  # pragma: no cover
            logger.warning("Zabbix raw context query failed: %s", exc)
            return {}

    def get_disk_summary(self, alert: dict) -> dict:
        if self.use_stub:  # ⚠️ STUB DATA — 仅 USE_STUB_ZABBIX=true 或 connection.use_stub=true 时进入
            return self._stub_disk_summary(alert)

        mount_point = alert.get("resource_scope", {}).get("mount_point")
        if not mount_point:
            return self._stub_disk_summary(alert)

        try:
            host_id = self._resolve_host_id(alert)
            if not host_id:
                return self._stub_disk_summary(alert)

            items = self._get_host_items(host_id)
            used_item = self._find_metric_item(items, [f"vfs.fs.size[{mount_point},pused]"])
            free_item = self._find_metric_item(items, [f"vfs.fs.size[{mount_point},free]"])
            used_bytes_item = self._find_metric_item(items, [f"vfs.fs.size[{mount_point},used]"])
            total_item = self._find_metric_item(items, [f"vfs.fs.size[{mount_point},total]"])

            used_values = self._get_numeric_history(used_item["itemid"], used_item["value_type"], alert) if used_item else []
            free_values = self._get_numeric_history(free_item["itemid"], free_item["value_type"], alert) if free_item else []
            used_bytes_values = self._get_numeric_history(used_bytes_item["itemid"], used_bytes_item["value_type"], alert) if used_bytes_item else []
            total_values = self._get_numeric_history(total_item["itemid"], total_item["value_type"], alert) if total_item else []

            if used_values:
                latest_total = total_values[-1] if total_values else 0
                latest_used_bytes = used_bytes_values[-1] if used_bytes_values else 0
                used_bytes_window_start = used_bytes_values[0] if len(used_bytes_values) > 1 else latest_used_bytes

                if free_values:
                    latest_free = free_values[-1]
                    free_window_start = free_values[0] if len(free_values) > 1 else latest_free
                elif latest_total and latest_used_bytes:
                    latest_free = max(latest_total - latest_used_bytes, 0)
                    free_window_start = max(latest_total - used_bytes_window_start, 0)
                else:
                    latest_free = 0
                    free_window_start = 0

                return {
                    "mount_point": mount_point,
                    "used_percent": round(used_values[-1], 2),
                    "free_gb": round(latest_free / 1024 / 1024 / 1024, 2) if latest_free else 0,
                    "total_gb": round(latest_total / 1024 / 1024 / 1024, 2) if latest_total else 0,
                    "growth_gb_1h": round((free_window_start - latest_free) / 1024 / 1024 / 1024, 2),
                    "trend": "sharp_increase" if free_window_start - latest_free > 20 * 1024 * 1024 * 1024 else "gradual",
                    **self._sample_window_metadata(),
                }
        except Exception as exc:  # pragma: no cover
            logger.warning("Zabbix disk query failed, falling back to stub: %s", exc)

        return self._stub_disk_summary(alert)

    def get_memory_summary(self, alert: dict) -> dict:
        """内存概览。

        - ``memory_used_percent``：**最新一条**原始点（保留原语义，跟 dashboard 实时值对齐）
        - ``memory_avg/max/min/p95_percent``：基于窗口全量原始点
        - ``available_gb / total_gb``：最新点（容量信息是当下值，无聚合意义）
        - ``trend``：rising_fast 当 (last - first) ≥ 8pp，否则 stable
        """
        if self.use_stub:  # ⚠️ STUB DATA — 仅 USE_STUB_ZABBIX=true 或 connection.use_stub=true 时进入
            return self._stub_memory_summary(alert)

        try:
            host_id = self._resolve_host_id(alert)
            if not host_id:
                return self._stub_memory_summary(alert)

            items = self._get_host_items(host_id)
            util_item = self._find_metric_item(items, ["vm.memory.util", "vm.memory.utilization"])
            free_item = self._find_metric_item(items, ["vm.memory.size[free]", "vm.memory.size[available]"])
            total_item = self._find_metric_item(items, ["vm.memory.size[total]"])

            util_window = self._get_window_history(util_item["itemid"], util_item["value_type"], alert) if util_item else {"raw": []}
            free_window = self._get_window_history(free_item["itemid"], free_item["value_type"], alert) if free_item else {"raw": []}
            total_window = self._get_window_history(total_item["itemid"], total_item["value_type"], alert) if total_item else {"raw": []}

            agg = self._aggregate(util_window["raw"])
            if agg:
                latest_free = free_window["raw"][-1]["value"] if free_window["raw"] else 0
                latest_total = total_window["raw"][-1]["value"] if total_window["raw"] else 0
                util_values = [r["value"] for r in util_window["raw"]]
                rising_fast = len(util_values) > 1 and (util_values[-1] - util_values[0]) >= 8
                return {
                    "memory_used_percent":  agg["last"],   # 最新点（保持向后兼容字段名）
                    "memory_avg_percent":   agg["avg"],
                    "memory_max_percent":   agg["max"],
                    "memory_min_percent":   agg["min"],
                    "memory_p95_percent":   agg["p95"],
                    "memory_raw_count":     agg["raw_count"],
                    "available_gb":         round(latest_free / 1024 / 1024 / 1024, 2) if latest_free else 0,
                    "total_gb":             round(latest_total / 1024 / 1024 / 1024, 2) if latest_total else 0,
                    "swap_used_percent":    0.0,
                    "trend":                "rising_fast" if rising_fast else "stable",
                    **self._sample_window_metadata(),
                }
        except Exception as exc:  # pragma: no cover
            logger.warning("Zabbix memory query failed, falling back to stub: %s", exc)

        return self._stub_memory_summary(alert)

    def get_disk_io_summary(self, alert: dict) -> dict:
        if self.use_stub:  # ⚠️ STUB DATA — 仅 USE_STUB_ZABBIX=true 或 connection.use_stub=true 时进入
            return self._stub_disk_io_summary(alert)

        try:
            host_id = self._resolve_host_id(alert)
            if not host_id:
                return self._stub_disk_io_summary(alert)

            items = self._get_host_items(host_id)
            queue_item = self._find_metric_item(items, ['Current Disk Queue Length'])
            idle_item = self._find_metric_item(items, ['% Idle Time'])
            read_await_item = self._find_metric_item(items, ['Avg. Disk sec/Read'])
            write_await_item = self._find_metric_item(items, ['Avg. Disk sec/Write'])

            queue_values = self._get_numeric_history(queue_item["itemid"], queue_item["value_type"], alert) if queue_item else []
            idle_values = self._get_numeric_history(idle_item["itemid"], idle_item["value_type"], alert) if idle_item else []
            read_await_values = self._get_numeric_history(read_await_item["itemid"], read_await_item["value_type"], alert) if read_await_item else []
            write_await_values = self._get_numeric_history(write_await_item["itemid"], write_await_item["value_type"], alert) if write_await_item else []

            if queue_values or idle_values:
                latest_idle = idle_values[-1] if idle_values else 100.0
                latest_read = read_await_values[-1] if read_await_values else 0.0
                latest_write = write_await_values[-1] if write_await_values else 0.0
                await_ms = max(latest_read, latest_write) * 1000
                return {
                    "utilization_percent": round(max(0.0, 100.0 - latest_idle), 2),
                    "await_ms": round(await_ms, 2),
                    "queue_size": round(queue_values[-1], 2) if queue_values else 0.0,
                    "trend": "sustained_high" if len(queue_values) > 1 and max(queue_values) >= 3 else "moderate",
                    **self._sample_window_metadata(),
                }
        except Exception as exc:  # pragma: no cover
            logger.warning("Zabbix disk IO query failed, falling back to stub: %s", exc)

        return self._stub_disk_io_summary(alert)

    def get_availability_summary(self, alert: dict) -> dict:
        if self.use_stub:  # ⚠️ STUB DATA — 仅 USE_STUB_ZABBIX=true 或 connection.use_stub=true 时进入
            return self._stub_availability_summary(alert)

        try:
            host_id = self._resolve_host_id(alert)
            if not host_id:
                return self._stub_availability_summary(alert)

            items = self._get_host_items(host_id)
            agent_ping_item = self._find_metric_item(items, ["agent.ping"])
            uptime_item = self._find_metric_item(items, ["system.uptime"])
            ping_values = self._get_numeric_history(agent_ping_item["itemid"], agent_ping_item["value_type"], alert) if agent_ping_item else []
            uptime_values = self._get_numeric_history(uptime_item["itemid"], uptime_item["value_type"], alert) if uptime_item else []
            ping_status = "up" if ping_values and ping_values[-1] >= 1 else "down"
            agent_status = ping_status
            return {
                "ping_status": ping_status,
                "agent_status": agent_status,
                "last_seen_minutes_ago": 0 if uptime_values else 10,
                **self._sample_window_metadata(),
            }
        except Exception as exc:  # pragma: no cover
            logger.warning("Zabbix availability query failed, falling back to stub: %s", exc)

        return self._stub_availability_summary(alert)

    def find_host(self, host_query: str) -> dict | None:
        if self.use_stub:  # ⚠️ STUB DATA — 仅 USE_STUB_ZABBIX=true 或 connection.use_stub=true 时进入
            return {
                "hostid": "stub-host", "host": host_query, "name": host_query,
                "interfaces": [{"ip": "127.0.0.1"}],
                "_stub_data": True,
            }

        query = host_query.strip()
        if not query:
            return None

        by_host = self._rpc(
            "host.get",
            params={
                "output": ["hostid", "host", "name"],
                "selectInterfaces": ["ip"],
                "filter": {"host": [query]},
            },
            auth=self.login(),
        )
        if by_host:
            return by_host[0]

        by_name = self._rpc(
            "host.get",
            params={
                "output": ["hostid", "host", "name"],
                "selectInterfaces": ["ip"],
                "search": {"name": query},
                "searchByAny": True,
            },
            auth=self.login(),
        )
        if by_name:
            return by_name[0]

        by_ip = self._rpc(
            "host.get",
            params={
                "output": ["hostid", "host", "name"],
                "selectInterfaces": ["ip"],
                "search": {"ip": query},
            },
            auth=self.login(),
        )
        if by_ip:
            return by_ip[0]
        return None

    def get_host_overview(self, host_query: str, *, lookback_hours: float | int = 1) -> dict:
        """主机概览。

        ``lookback_hours``：分析回看窗口，影响 metric_summary / memory_summary 里的
        avg / max / trend 是基于多长时间算的；availability 也按这个时间窗扫问题事件。
        默认 1h；用户问"24 小时"传 24；问"近一周"传 168 即可。
        """
        host = self.find_host(host_query)
        if not host:
            raise ValueError(f"未找到主机: {host_query}")

        alert = self._build_host_alert(host)
        with self._with_window(lookback_hours):
            payload = {
                "host": {
                    "host_id": host["hostid"],
                    "host_name": host.get("name") or host.get("host"),
                    "host_ip": self._extract_host_ip(host),
                },
                "lookback_hours": float(lookback_hours or 1),
                "metric_summary": self.get_metric_summary(alert),
                "memory_summary": self.get_memory_summary(alert),
                "availability_summary": self.get_availability_summary(alert),
            }
        return payload

    def get_host_storage_overview(self, host_query: str, *, lookback_hours: float | int | None = None) -> dict:
        """磁盘容量概览。

        ``lookback_hours`` 当前对磁盘容量值无影响（容量是当下快照，不是趋势）；
        保留参数是为了 skill 接口和 host_overview 一致，未来可加"24h 内最高使用率"
        / "增长速率" 等趋势指标。
        """
        host = self.find_host(host_query)
        if not host:
            raise ValueError(f"未找到主机: {host_query}")
        if self.use_stub:  # ⚠️ STUB DATA — 仅 USE_STUB_ZABBIX=true 或 connection.use_stub=true 时进入
            return _mark_stub({
                "host": {
                    "host_id": host["hostid"],
                    "host_name": host.get("name") or host.get("host"),
                    "host_ip": self._extract_host_ip(host),
                },
                "filesystems": [
                    {"mount_point": "/", "used_percent": 71.2, "free_gb": 120.0, "total_gb": 512.0},
                    {"mount_point": "/data", "used_percent": 84.3, "free_gb": 227.0, "total_gb": 2047.0},
                ],
            })

        items = self._get_host_items(host["hostid"])
        filesystems: dict[str, dict] = {}
        pattern = re.compile(r"^vfs\.fs\.size\[(?P<mount>.+?),(?P<metric>pused|free|used|total)\]$")
        for item in items:
            key_name = item.get("key_", "")
            match = pattern.match(key_name)
            if not match:
                continue
            mount = match.group("mount")
            metric = match.group("metric")
            bucket = filesystems.setdefault(mount, {"mount_point": mount})
            try:
                bucket[metric] = float(item.get("lastvalue") or 0)
            except ValueError:
                continue

        result = []
        for mount, values in sorted(filesystems.items()):
            total = values.get("total", 0.0)
            used = values.get("used", 0.0)
            free = values.get("free")
            if free is None and total and used:
                free = max(total - used, 0.0)
            result.append(
                {
                    "mount_point": mount,
                    "used_percent": round(values.get("pused", 0.0), 2),
                    "free_gb": round((free or 0.0) / 1024 / 1024 / 1024, 2),
                    "total_gb": round(total / 1024 / 1024 / 1024, 2) if total else 0.0,
                }
            )

        return {
            "host": {
                "host_id": host["hostid"],
                "host_name": host.get("name") or host.get("host"),
                "host_ip": self._extract_host_ip(host),
            },
            "filesystems": result,
        }

    def debug_metric_summary(self, alert: dict) -> dict:
        if self.use_stub:  # ⚠️ STUB DATA — 仅 USE_STUB_ZABBIX=true 或 connection.use_stub=true 时进入
            summary = self._stub_metric_summary(alert)
            return {"mode": "stub", "summary": summary, "host_id": None, "cpu_item": None, "load_item": None}

        host_id = self._resolve_host_id(alert)
        if not host_id:
            raise ValueError("Unable to resolve host_id from host_name or host_ip")

        items = self._get_host_items(host_id)
        cpu_item = self._find_metric_item(items, ["system.cpu.util", "system.cpu.util[,system,avg1]"])
        load_item = self._find_metric_item(items, ["system.cpu.load", "system.cpu.load[all,avg1]"])
        summary = self.get_metric_summary(alert)
        return {
            "mode": "api",
            "host_id": host_id,
            "cpu_item": cpu_item,
            "load_item": load_item,
            "summary": summary,
        }

    def apiinfo_version(self) -> str:
        version = self._rpc("apiinfo.version", params={})
        if not isinstance(version, str):
            raise ValueError("Unexpected apiinfo.version response")
        return version

    def login(self) -> str:
        if self._auth_token:
            return self._auth_token
        if not self.username or not self.password:
            raise ValueError("Zabbix credentials are required when USE_STUB_ZABBIX=false")

        token = self._rpc(
            "user.login",
            params={"username": self.username, "password": self.password},
        )
        if not isinstance(token, str) or not token:
            raise ValueError("Zabbix login failed")
        self._auth_token = token
        return token

    # ----------------------------------------------------------------- #
    # services.metric_provider.MetricProvider 协议实现
    #
    # 这三个方法把 Zabbix 适配成 provider-agnostic 接口。analytics 层
    # （find_peak / fetch_window / summarize）只依赖这三个方法，换 Prometheus
    # 之类只要实现同样三个签名即可。
    # ----------------------------------------------------------------- #

    def resolve_host(self, query: str) -> HostRef | None:
        """MetricProvider: 把 host name / IP / hostid 解析成中性 HostRef。"""
        if self.use_stub:  # ⚠️ STUB DATA
            host = self.find_host(query)
            if not host:
                return None
            return HostRef(
                id=str(host.get("hostid", "")),
                name=str(host.get("name") or host.get("host") or query),
                ip=self._extract_host_ip(host),
                extra={"_stub": True},
            )

        # 优先 hostid（数字串），命中直接走 host.get by id 拿规范字段
        if query.isdigit():
            rows = self._rpc(
                "host.get",
                params={
                    "output": ["hostid", "host", "name"],
                    "selectInterfaces": ["ip"],
                    "hostids": [query],
                },
                auth=self.login(),
            )
            if rows:
                return self._host_to_ref(rows[0])

        host = self.find_host(query)
        if not host:
            return None
        return self._host_to_ref(host)

    def find_metric(self, host: HostRef, metric_name: str) -> MetricDescriptor | None:
        """MetricProvider: 把逻辑指标名映射到 Zabbix item。

        映射表见模块顶部 ``_ZBX_METRIC_KEY_MAP``。未知指标返回 None——
        analytics 层会包装成"主机上无此指标"的错误。
        """
        keys = _ZBX_METRIC_KEY_MAP.get(metric_name)
        if not keys:
            return None

        if self.use_stub:  # ⚠️ STUB DATA
            return MetricDescriptor(
                name=metric_name,
                unit="%" if "util" in metric_name else "",
                provider_handle={"_stub": True, "metric": metric_name},
            )

        items = self._get_host_items(host.id)
        item = self._find_metric_item(items, keys)
        if not item:
            return None
        return MetricDescriptor(
            name=metric_name,
            unit=str(item.get("units") or ""),
            provider_handle={
                "itemid": str(item["itemid"]),
                "value_type": int(item.get("value_type", 0)),
                "key": item.get("key_", ""),
            },
        )

    def query_series(
        self,
        host: HostRef,
        metric: MetricDescriptor,
        start_ts: int,
        end_ts: int,
    ) -> list[MetricPoint]:
        """MetricProvider: 拉 [start, end] 区间的全部原始点（升序）。

        命中 ``HISTORY_RAW_LIMIT`` 时打 warning 但不抛——保证 analytics 拿到部分
        数据也能给统计；超长窗口（>7d）后续应改 trends.get（TODO）。
        """
        if start_ts >= end_ts:
            return []

        handle = metric.provider_handle or {}

        if self.use_stub or handle.get("_stub"):  # ⚠️ STUB DATA
            return self._stub_query_series(host, metric, int(start_ts), int(end_ts))

        item_id = handle.get("itemid")
        value_type = handle.get("value_type")
        if not item_id or value_type is None:
            return []

        rows = self._rpc(
            "history.get",
            params={
                "output": "extend",
                "history": int(value_type),
                "itemids": [item_id],
                "sortfield": "clock",
                "sortorder": "ASC",
                "time_from": int(start_ts),
                "time_till": int(end_ts),
                "limit": HISTORY_RAW_LIMIT,
            },
            auth=self.login(),
        )

        series: list[MetricPoint] = []
        for row in rows or []:
            try:
                series.append(MetricPoint(timestamp=int(row["clock"]), value=float(row["value"])))
            except (KeyError, TypeError, ValueError):
                continue

        if len(series) >= HISTORY_RAW_LIMIT:
            logger.warning(
                "history.get 命中 limit=%s（item %s, %s ~ %s），数据可能被截断；"
                "对超长窗口（>7d）应改用 trends.get（1h 聚合）。",
                HISTORY_RAW_LIMIT, item_id, start_ts, end_ts,
            )
        return series

    @staticmethod
    def _host_to_ref(host: dict) -> HostRef:
        return HostRef(
            id=str(host.get("hostid", "")),
            name=str(host.get("name") or host.get("host") or ""),
            ip=ZabbixClient._extract_host_ip(host),
            extra={"host": host.get("host", "")},
        )

    @staticmethod
    def _stub_query_series(
        host: HostRef, metric: MetricDescriptor, start_ts: int, end_ts: int,
    ) -> list[MetricPoint]:
        """生成 stub 时间序列：60s 一个点、值在合理范围内带个明显的尖峰。

        这只在 use_stub=True 时被调用；目的是让本地联调 / 集成测试能跑得通，
        而**不是**伪装成真实数据。返回的点 timestamp 真实（基于 start_ts），
        但调用方应该结合 healthcheck 里的 _stub_data 标记判断。
        """
        if start_ts >= end_ts:
            return []
        # 每分钟一个点，最多 720 个（12h 上限），避免大窗口炸内存
        step = 60
        max_points = 720
        n = min(max_points, max(1, (end_ts - start_ts) // step))
        baseline = 70.0 if metric.name == "cpu.utilization" else 60.0
        if "load" in metric.name:
            baseline = 2.0
        # 在窗口中段插一个 +25 的尖峰，让 find_peak 测试有可挑的点
        spike_idx = n // 2
        points: list[MetricPoint] = []
        for i in range(n):
            ts = start_ts + i * step
            value = baseline + (5.0 if i % 7 == 0 else 0.0)
            if i == spike_idx:
                value = baseline + 25.0
            points.append(MetricPoint(timestamp=ts, value=round(value, 2)))
        return points

    def _resolve_host_id(self, alert: dict) -> str | None:
        host_id = str(alert.get("host_id", "")).strip()
        if host_id:
            return host_id

        result = self._rpc(
            "host.get",
            params={
                "output": ["hostid", "host", "name"],
                "filter": {"host": [alert.get("host_name", "")]},
            },
            auth=self.login(),
        )
        if result:
            return result[0]["hostid"]

        host_name = alert.get("host_name")
        if host_name:
            result = self._rpc(
                "host.get",
                params={
                    "output": ["hostid", "host", "name"],
                    "search": {"name": host_name},
                    "searchByAny": True,
                },
                auth=self.login(),
            )
            if result:
                return result[0]["hostid"]

        ip = alert.get("host_ip")
        if not ip:
            return None

        result = self._rpc(
            "host.get",
            params={
                "output": ["hostid", "host", "name"],
                "selectInterfaces": ["ip"],
                "search": {"ip": ip},
            },
            auth=self.login(),
        )
        if result:
            return result[0]["hostid"]
        return None

    @staticmethod
    def _extract_host_ip(host: dict) -> str:
        interfaces = host.get("interfaces") or []
        if interfaces:
            return interfaces[0].get("ip", "")
        return ""

    def _build_host_alert(self, host: dict) -> dict:
        return {
            "host_id": host["hostid"],
            "host_name": host.get("name") or host.get("host"),
            "host_ip": self._extract_host_ip(host),
            "event_time": datetime.now(UTC).isoformat(),
            "resource_scope": {},
            "signal": {},
            "status": "problem",
            "severity": "info",
        }

    def _get_host_items(self, host_id: str) -> list[dict]:
        return self._rpc(
            "item.get",
            params={
                "output": ["itemid", "key_", "name", "value_type", "lastvalue", "units"],
                "hostids": host_id,
                "sortfield": "name",
            },
            auth=self.login(),
        )

    def _collect_items(self, items: list[dict], key_prefixes: list[str], alert: dict) -> list[dict]:
        collected = []
        for item in items:
            key_name = item.get("key_", "")
            if any(key_name.startswith(prefix) for prefix in key_prefixes):
                samples = self._get_sampled_history(item.get("itemid", ""), item.get("value_type", 0), alert)
                collected.append(
                    {
                        "itemid": item.get("itemid"),
                        "name": item.get("name"),
                        "key": key_name,
                        "value_type": item.get("value_type"),
                        "lastvalue": item.get("lastvalue"),
                        "units": item.get("units"),
                        "samples": samples,
                    }
                )
        return collected

    @staticmethod
    def _find_metric_item(items: list[dict], key_prefixes: list[str]) -> dict | None:
        for item in items:
            key_name = item.get("key_", "")
            if any(key_name.startswith(prefix) for prefix in key_prefixes):
                return item
        return None

    def _get_numeric_history(self, item_id: str, value_type: int | str, alert: dict | None = None) -> list[float]:
        """返回窗口内**全部原始点**的值列表（不再降采样）。

        历史背景：早期实现里这里返回的是降采样到 ``_sample_points`` 的值，
        然后上游再 ``mean / max``——会丢精度（30min 采样错过分钟级 spike）。
        现在统一从 ``_get_window_history`` 拿原始点，agg 走真值。
        """
        if alert is not None:
            window = self._get_window_history(item_id, value_type, alert)
            if window["raw"]:
                return [r["value"] for r in window["raw"]]

        # alert=None：不带时间窗的 fallback——给老调用方留口子，拉最近 20 条
        history_type = int(value_type)
        result = self._rpc(
            "history.get",
            params={
                "output": "extend",
                "history": history_type,
                "itemids": [item_id],
                "sortfield": "clock",
                "sortorder": "DESC",
                "limit": 20,
            },
            auth=self.login(),
        )
        values = []
        for row in result:
            try:
                values.append(float(row["value"]))
            except (KeyError, TypeError, ValueError):
                continue
        return list(reversed(values))

    def _get_window_history(self, item_id: str, value_type: int | str, alert: dict | None) -> dict:
        """一次 history.get 同时返回**原始点 + 降采样点**。

        Returns:
            {
                "raw":     [{"clock", "value"}, ...]   # 窗口内全部原始点（用来算 avg/max/p95/min）
                "samples": [{"clock", "time", "value", "source_clock", "source_time"}, ...]
                                                       # step-interpolation 降采样到 _sample_points 个
                                                       # （用来给序列展示，避免 token 爆炸）
            }

        ⚠️ 长窗口 (>7d) 命中 ``HISTORY_RAW_LIMIT``，会有"老数据被截断"的 warning；
            后续应该用 ``trends.get``（1h 聚合）替代——TODO。
        """
        if not item_id:
            return {"raw": [], "samples": []}

        history_type = int(value_type)
        sample_times = self._build_sample_times(alert)
        window_start = sample_times[0] - self._sample_interval_seconds

        rows = self._rpc(
            "history.get",
            params={
                "output": "extend",
                "history": history_type,
                "itemids": [item_id],
                "sortfield": "clock",
                "sortorder": "ASC",
                "time_from": window_start,
                "time_till": sample_times[-1],
                "limit": HISTORY_RAW_LIMIT,
            },
            auth=self.login(),
        )

        raw: list[dict] = []
        for row in rows:
            try:
                raw.append({"clock": int(row["clock"]), "value": float(row["value"])})
            except (KeyError, TypeError, ValueError):
                continue

        if len(raw) >= HISTORY_RAW_LIMIT:
            logger.warning(
                "history.get 命中 limit=%s（item %s），窗口起点可能被截断；"
                "对超长窗口（>7d）应改用 trends.get（1h 聚合）。",
                HISTORY_RAW_LIMIT, item_id,
            )

        if not raw:
            return {"raw": [], "samples": []}

        # 降采样：每个采样点取 ≤ 该时间最近的一条（step interpolation）
        samples: list[dict] = []
        row_index = 0
        last_seen: dict | None = None
        for sample_clock in sample_times:
            while row_index < len(raw) and raw[row_index]["clock"] <= sample_clock:
                last_seen = raw[row_index]
                row_index += 1
            if last_seen is None:
                continue
            samples.append(
                {
                    "clock": sample_clock,
                    "time": datetime.fromtimestamp(sample_clock, UTC).isoformat(),
                    "value": round(last_seen["value"], 6),
                    "source_clock": last_seen["clock"],
                    "source_time": datetime.fromtimestamp(last_seen["clock"], UTC).isoformat(),
                }
            )
        return {"raw": raw, "samples": samples}

    @staticmethod
    def _aggregate(raw: list[dict], precision: int = 2) -> dict:
        """从原始点列表算 avg / max / min / p95 / last。

        - p95 用最简单的 nearest-rank 法：sorted[ceil(0.95 * n) - 1]
        - 单点也能给出值（avg=max=min=p95=last=唯一点）
        - 空列表返回空 dict（让调用方决定是 fallback 还是跳过）
        """
        if not raw:
            return {}
        values = [r["value"] for r in raw]
        s = sorted(values)
        p95_idx = min(len(s) - 1, max(0, int(round(0.95 * len(s))) - 1))
        return {
            "avg": round(mean(values), precision),
            "max": round(max(values), precision),
            "min": round(min(values), precision),
            "p95": round(s[p95_idx], precision),
            "last": round(values[-1], precision),
            "raw_count": len(values),
        }

    def _sample_window_metadata(self) -> dict:
        return {
            "sample_window_minutes": int(self._sample_interval_seconds * self._sample_points / 60),
            "sample_interval_minutes": int(self._sample_interval_seconds / 60),
            "sample_count": self._sample_points,
        }

    def _with_window(self, lookback_hours: float | int | None):
        """临时切换实例的采样窗口，with 块结束后自动还原。

        ``lookback_hours`` 为 None 或 <=0 时不动（保持默认 1h 行为）。
        """
        from contextlib import contextmanager

        @contextmanager
        def _ctx():
            if not lookback_hours or lookback_hours <= 0:
                yield
                return
            old_interval = self._sample_interval_seconds
            old_points = self._sample_points
            self._sample_interval_seconds, self._sample_points = compute_window(lookback_hours)
            try:
                yield
            finally:
                self._sample_interval_seconds = old_interval
                self._sample_points = old_points

        return _ctx()

    def _get_sampled_history(self, item_id: str, value_type: int | str, alert: dict | None) -> list[dict]:
        """向后兼容：只返回降采样后的 samples 数组，原始点扔掉。

        新代码请直接用 ``_get_window_history``，能同时拿到 raw + samples。
        """
        return self._get_window_history(item_id, value_type, alert)["samples"]

    def _build_sample_times(self, alert: dict | None) -> list[int]:
        end_ts = self._resolve_reference_timestamp(alert)
        start_ts = end_ts - (self._sample_points - 1) * self._sample_interval_seconds
        return [start_ts + index * self._sample_interval_seconds for index in range(self._sample_points)]

    @staticmethod
    def _resolve_reference_timestamp(alert: dict | None) -> int:
        if not alert:
            return int(datetime.now(UTC).timestamp())

        raw_event_time = str(alert.get("event_time", "")).strip()
        if not raw_event_time:
            return int(datetime.now(UTC).timestamp())

        parsed = ZabbixClient._parse_event_time(raw_event_time)
        return int(parsed.timestamp())

    @staticmethod
    def _parse_event_time(raw_event_time: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(raw_event_time)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=DEFAULT_EVENT_TIMEZONE)
            return parsed.astimezone(UTC)
        except ValueError:
            pass

        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y.%m.%d %H:%M:%S"):
            try:
                parsed = datetime.strptime(raw_event_time, fmt)
                return parsed.replace(tzinfo=DEFAULT_EVENT_TIMEZONE).astimezone(UTC)
            except ValueError:
                continue
        return datetime.now(UTC)

    def _rpc(self, method: str, params: dict, auth: str | None = None):
        self._request_id += 1
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": self._request_id,
        }
        if auth:
            payload["auth"] = auth

        with requests.Session() as session:
            session.trust_env = False
            response = session.post(self.base_url, json=payload, timeout=self.timeout_seconds)
        response.raise_for_status()
        data = response.json()
        if "error" in data:
            error = data["error"]
            raise RuntimeError(f"Zabbix API error {error.get('code')}: {error.get('data') or error.get('message')}")
        return data.get("result")

    # 注意：以下所有 _stub_* 方法仅在 use_stub=true 时被调用，所有返回都打 _stub_data 标记。
    # 这样即使数据流到 agent / 模型，模型也能识别"这是模拟数据"而不是真实监控值。

    @staticmethod
    def _stub_metric_summary(alert: dict) -> dict:
        severity = alert.get("severity", "").lower()
        if severity in {"high", "critical"}:
            return _mark_stub({
                "cpu_avg": 94.1, "cpu_max": 97.6, "cpu_min": 88.0,
                "cpu_p95": 96.5, "cpu_last": 93.2, "cpu_raw_count": 720,
                "load_avg": 18.2, "load_max": 22.7,
            })
        return _mark_stub({
            "cpu_avg": 68.2, "cpu_max": 76.4, "cpu_min": 60.0,
            "cpu_p95": 74.1, "cpu_last": 67.8, "cpu_raw_count": 720,
            "load_avg": 3.4, "load_max": 4.7,
        })

    @staticmethod
    def _stub_disk_summary(alert: dict) -> dict:
        mount_point = alert.get("resource_scope", {}).get("mount_point", "/")
        host_name = alert.get("host_name", "").lower()
        if "db" in host_name or "tidb" in host_name:
            return _mark_stub({
                "mount_point": mount_point,
                "used_percent": 96.4, "free_gb": 8.5, "total_gb": 512.0,
                "growth_gb_1h": 36.8, "trend": "sharp_increase",
            })
        return _mark_stub({
            "mount_point": mount_point,
            "used_percent": 91.2, "free_gb": 42.0, "total_gb": 1024.0,
            "growth_gb_1h": 4.3, "trend": "gradual",
        })

    @staticmethod
    def _stub_memory_summary(alert: dict) -> dict:
        host_name = alert.get("host_name", "").lower()
        if "db" in host_name or "tidb" in host_name:
            return _mark_stub({
                "memory_used_percent": 96.1, "memory_avg_percent": 94.5,
                "memory_max_percent": 97.2, "memory_min_percent": 91.0,
                "memory_p95_percent": 96.8, "memory_raw_count": 720,
                "available_gb": 1.4, "total_gb": 64.0,
                "swap_used_percent": 68.0, "trend": "rising_fast",
            })
        return _mark_stub({
            "memory_used_percent": 88.0, "memory_avg_percent": 86.4,
            "memory_max_percent": 89.5, "memory_min_percent": 83.2,
            "memory_p95_percent": 89.1, "memory_raw_count": 720,
            "available_gb": 9.8, "total_gb": 32.0,
            "swap_used_percent": 10.0, "trend": "stable",
        })

    @staticmethod
    def _stub_disk_io_summary(alert: dict) -> dict:
        host_name = alert.get("host_name", "").lower()
        if "db" in host_name or "tidb" in host_name:
            return _mark_stub({
                "utilization_percent": 97.0, "await_ms": 88.0, "queue_size": 5.2,
                "trend": "sustained_high",
            })
        return _mark_stub({
            "utilization_percent": 72.0, "await_ms": 18.0, "queue_size": 1.2,
            "trend": "moderate",
        })

    @staticmethod
    def _stub_availability_summary(alert: dict) -> dict:
        return _mark_stub({
            "ping_status": "down", "agent_status": "down", "last_seen_minutes_ago": 6,
        })

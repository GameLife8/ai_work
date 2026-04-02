from __future__ import annotations

import logging
from datetime import UTC, datetime
from statistics import mean
from zoneinfo import ZoneInfo

import requests


logger = logging.getLogger(__name__)

SAMPLE_INTERVAL_SECONDS = 300
SAMPLE_POINTS = 12
DEFAULT_EVENT_TIMEZONE = ZoneInfo("Asia/Shanghai")


class ZabbixClient:
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
        self.timeout_seconds = timeout_seconds
        self.use_stub = use_stub
        self._auth_token: str | None = None
        self._request_id = 0

    def healthcheck(self) -> dict:
        if self.use_stub:
            return {
                "backend": "stub",
                "healthy": True,
                "details": {"mode": "stub", "base_url": self.base_url},
            }

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
        if self.use_stub:
            return self._stub_metric_summary(alert)

        try:
            host_id = self._resolve_host_id(alert)
            if not host_id:
                return self._stub_metric_summary(alert)

            summary = {}
            items = self._get_host_items(host_id)

            item = self._find_metric_item(items, ["system.cpu.util", "system.cpu.util[,system,avg1]"])
            if item:
                values = self._get_numeric_history(item["itemid"], item["value_type"], alert)
                if values:
                    summary["cpu_avg"] = round(mean(values), 2)
                    summary["cpu_max"] = round(max(values), 2)

            load_item = self._find_metric_item(items, ["system.cpu.load", "system.cpu.load[all,avg1]"])
            if load_item:
                values = self._get_numeric_history(load_item["itemid"], load_item["value_type"], alert)
                if values:
                    summary["load_avg"] = round(mean(values), 2)

            if {"cpu_avg", "cpu_max", "load_avg"} <= summary.keys():
                summary.update(self._sample_window_metadata())
                return summary
        except Exception as exc:  # pragma: no cover
            logger.warning("Zabbix metric query failed, falling back to stub: %s", exc)

        return self._stub_metric_summary(alert)

    def get_raw_context(self, alert: dict, needs: list[str]) -> dict:
        if self.use_stub:
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
        if self.use_stub:
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
        if self.use_stub:
            return self._stub_memory_summary(alert)

        try:
            host_id = self._resolve_host_id(alert)
            if not host_id:
                return self._stub_memory_summary(alert)

            items = self._get_host_items(host_id)
            util_item = self._find_metric_item(items, ["vm.memory.util", "vm.memory.utilization"])
            free_item = self._find_metric_item(items, ["vm.memory.size[free]", "vm.memory.size[available]"])
            total_item = self._find_metric_item(items, ["vm.memory.size[total]"])

            util_values = self._get_numeric_history(util_item["itemid"], util_item["value_type"], alert) if util_item else []
            free_values = self._get_numeric_history(free_item["itemid"], free_item["value_type"], alert) if free_item else []
            total_values = self._get_numeric_history(total_item["itemid"], total_item["value_type"], alert) if total_item else []
            if util_values:
                latest_free = free_values[-1] if free_values else 0
                latest_total = total_values[-1] if total_values else 0
                return {
                    "memory_used_percent": round(util_values[-1], 2),
                    "available_gb": round(latest_free / 1024 / 1024 / 1024, 2) if latest_free else 0,
                    "total_gb": round(latest_total / 1024 / 1024 / 1024, 2) if latest_total else 0,
                    "swap_used_percent": 0.0,
                    "trend": "rising_fast" if len(util_values) > 1 and util_values[-1] - util_values[0] >= 8 else "stable",
                    **self._sample_window_metadata(),
                }
        except Exception as exc:  # pragma: no cover
            logger.warning("Zabbix memory query failed, falling back to stub: %s", exc)

        return self._stub_memory_summary(alert)

    def get_disk_io_summary(self, alert: dict) -> dict:
        if self.use_stub:
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
        if self.use_stub:
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

    def debug_metric_summary(self, alert: dict) -> dict:
        if self.use_stub:
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
        samples = self._get_sampled_history(item_id, value_type, alert)
        if samples:
            return [sample["value"] for sample in samples]

        if alert is None:
            return []

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

    @staticmethod
    def _sample_window_metadata() -> dict:
        return {
            "sample_window_minutes": int(SAMPLE_INTERVAL_SECONDS * SAMPLE_POINTS / 60),
            "sample_interval_minutes": int(SAMPLE_INTERVAL_SECONDS / 60),
            "sample_count": SAMPLE_POINTS,
        }

    def _get_sampled_history(self, item_id: str, value_type: int | str, alert: dict | None) -> list[dict]:
        if not item_id:
            return []

        history_type = int(value_type)
        sample_times = self._build_sample_times(alert)
        window_start = sample_times[0] - SAMPLE_INTERVAL_SECONDS
        result = self._rpc(
            "history.get",
            params={
                "output": "extend",
                "history": history_type,
                "itemids": [item_id],
                "sortfield": "clock",
                "sortorder": "ASC",
                "time_from": window_start,
                "time_till": sample_times[-1],
                "limit": 2000,
            },
            auth=self.login(),
        )

        rows = []
        for row in result:
            try:
                rows.append({"clock": int(row["clock"]), "value": float(row["value"])})
            except (KeyError, TypeError, ValueError):
                continue

        if not rows:
            return []

        samples = []
        row_index = 0
        last_seen = None
        for sample_clock in sample_times:
            while row_index < len(rows) and rows[row_index]["clock"] <= sample_clock:
                last_seen = rows[row_index]
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
        return samples

    def _build_sample_times(self, alert: dict | None) -> list[int]:
        end_ts = self._resolve_reference_timestamp(alert)
        start_ts = end_ts - (SAMPLE_POINTS - 1) * SAMPLE_INTERVAL_SECONDS
        return [start_ts + index * SAMPLE_INTERVAL_SECONDS for index in range(SAMPLE_POINTS)]

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

        response = requests.post(self.base_url, json=payload, timeout=self.timeout_seconds)
        response.raise_for_status()
        data = response.json()
        if "error" in data:
            error = data["error"]
            raise RuntimeError(f"Zabbix API error {error.get('code')}: {error.get('data') or error.get('message')}")
        return data.get("result")

    @staticmethod
    def _stub_metric_summary(alert: dict) -> dict:
        severity = alert.get("severity", "").lower()
        if severity in {"high", "critical"}:
            return {"cpu_avg": 94.1, "cpu_max": 97.6, "load_avg": 18.2}
        return {"cpu_avg": 68.2, "cpu_max": 76.4, "load_avg": 3.4}

    @staticmethod
    def _stub_disk_summary(alert: dict) -> dict:
        mount_point = alert.get("resource_scope", {}).get("mount_point", "/")
        host_name = alert.get("host_name", "").lower()
        if "db" in host_name or "tidb" in host_name:
            return {
                "mount_point": mount_point,
                "used_percent": 96.4,
                "free_gb": 8.5,
                "total_gb": 512.0,
                "growth_gb_1h": 36.8,
                "trend": "sharp_increase",
            }
        return {
            "mount_point": mount_point,
            "used_percent": 91.2,
            "free_gb": 42.0,
            "total_gb": 1024.0,
            "growth_gb_1h": 4.3,
            "trend": "gradual",
        }

    @staticmethod
    def _stub_memory_summary(alert: dict) -> dict:
        host_name = alert.get("host_name", "").lower()
        if "db" in host_name or "tidb" in host_name:
            return {
                "memory_used_percent": 96.1,
                "available_gb": 1.4,
                "total_gb": 64.0,
                "swap_used_percent": 68.0,
                "trend": "rising_fast",
            }
        return {
            "memory_used_percent": 88.0,
            "available_gb": 9.8,
            "total_gb": 32.0,
            "swap_used_percent": 10.0,
            "trend": "stable",
        }

    @staticmethod
    def _stub_disk_io_summary(alert: dict) -> dict:
        host_name = alert.get("host_name", "").lower()
        if "db" in host_name or "tidb" in host_name:
            return {
                "utilization_percent": 97.0,
                "await_ms": 88.0,
                "queue_size": 5.2,
                "trend": "sustained_high",
            }
        return {
            "utilization_percent": 72.0,
            "await_ms": 18.0,
            "queue_size": 1.2,
            "trend": "moderate",
        }

    @staticmethod
    def _stub_availability_summary(alert: dict) -> dict:
        return {
            "ping_status": "down",
            "agent_status": "down",
            "last_seen_minutes_ago": 6,
        }

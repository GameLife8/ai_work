from __future__ import annotations

import logging
from statistics import mean

import requests


logger = logging.getLogger(__name__)


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
                values = self._get_numeric_history(item["itemid"], item["value_type"])
                if values:
                    summary["cpu_avg"] = round(mean(values), 2)
                    summary["cpu_max"] = round(max(values), 2)

            load_item = self._find_metric_item(items, ["system.cpu.load", "system.cpu.load[all,avg1]"])
            if load_item:
                values = self._get_numeric_history(load_item["itemid"], load_item["value_type"])
                if values:
                    summary["load_avg"] = round(mean(values), 2)

            if {"cpu_avg", "cpu_max", "load_avg"} <= summary.keys():
                return summary
        except Exception as exc:  # pragma: no cover
            logger.warning("Zabbix metric query failed, falling back to stub: %s", exc)

        return self._stub_metric_summary(alert)

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
            total_item = self._find_metric_item(items, [f"vfs.fs.size[{mount_point},total]"])

            used_values = self._get_numeric_history(used_item["itemid"], used_item["value_type"]) if used_item else []
            free_values = self._get_numeric_history(free_item["itemid"], free_item["value_type"]) if free_item else []
            total_values = self._get_numeric_history(total_item["itemid"], total_item["value_type"]) if total_item else []

            if used_values:
                latest_free = free_values[-1] if free_values else 0
                free_24h_ago = free_values[0] if len(free_values) > 1 else latest_free
                latest_total = total_values[-1] if total_values else 0
                return {
                    "mount_point": mount_point,
                    "used_percent": round(used_values[-1], 2),
                    "free_gb": round(latest_free / 1024 / 1024 / 1024, 2) if latest_free else 0,
                    "total_gb": round(latest_total / 1024 / 1024 / 1024, 2) if latest_total else 0,
                    "growth_gb_24h": round((free_24h_ago - latest_free) / 1024 / 1024 / 1024, 2),
                    "trend": "sharp_increase" if free_24h_ago - latest_free > 20 * 1024 * 1024 * 1024 else "gradual",
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
            util_item = self._find_metric_item(items, ["vm.memory.util"])
            free_item = self._find_metric_item(items, ["vm.memory.size[free]"])
            total_item = self._find_metric_item(items, ["vm.memory.size[total]"])

            util_values = self._get_numeric_history(util_item["itemid"], util_item["value_type"]) if util_item else []
            free_values = self._get_numeric_history(free_item["itemid"], free_item["value_type"]) if free_item else []
            total_values = self._get_numeric_history(total_item["itemid"], total_item["value_type"]) if total_item else []
            if util_values:
                latest_free = free_values[-1] if free_values else 0
                latest_total = total_values[-1] if total_values else 0
                return {
                    "memory_used_percent": round(util_values[-1], 2),
                    "available_gb": round(latest_free / 1024 / 1024 / 1024, 2) if latest_free else 0,
                    "total_gb": round(latest_total / 1024 / 1024 / 1024, 2) if latest_total else 0,
                    "swap_used_percent": 0.0,
                    "trend": "rising_fast" if len(util_values) > 1 and util_values[-1] - util_values[0] >= 8 else "stable",
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

            queue_values = self._get_numeric_history(queue_item["itemid"], queue_item["value_type"]) if queue_item else []
            idle_values = self._get_numeric_history(idle_item["itemid"], idle_item["value_type"]) if idle_item else []
            read_await_values = self._get_numeric_history(read_await_item["itemid"], read_await_item["value_type"]) if read_await_item else []
            write_await_values = self._get_numeric_history(write_await_item["itemid"], write_await_item["value_type"]) if write_await_item else []

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
            ping_values = self._get_numeric_history(agent_ping_item["itemid"], agent_ping_item["value_type"]) if agent_ping_item else []
            uptime_values = self._get_numeric_history(uptime_item["itemid"], uptime_item["value_type"]) if uptime_item else []
            ping_status = "up" if ping_values and ping_values[-1] >= 1 else "down"
            agent_status = ping_status
            return {
                "ping_status": ping_status,
                "agent_status": agent_status,
                "last_seen_minutes_ago": 0 if uptime_values else 10,
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
                "output": ["itemid", "key_", "name", "value_type"],
                "hostids": host_id,
                "sortfield": "name",
            },
            auth=self.login(),
        )

    @staticmethod
    def _find_metric_item(items: list[dict], key_prefixes: list[str]) -> dict | None:
        for item in items:
            key_name = item.get("key_", "")
            if any(key_name.startswith(prefix) for prefix in key_prefixes):
                return item
        return None

    def _get_numeric_history(self, item_id: str, value_type: int | str) -> list[float]:
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
                "growth_gb_24h": 36.8,
                "trend": "sharp_increase",
            }
        return {
            "mount_point": mount_point,
            "used_percent": 91.2,
            "free_gb": 42.0,
            "total_gb": 1024.0,
            "growth_gb_24h": 4.3,
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

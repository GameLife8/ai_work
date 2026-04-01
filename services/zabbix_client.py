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

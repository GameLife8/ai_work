from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from typing import Any


@dataclass
class CommandResult:
    command: list[str]
    stdout: str
    stderr: str
    returncode: int

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class DockerSwarmClient:
    """直接调本地 ``docker`` 二进制 + 远端 DOCKER_HOST 操作 Swarm。

    部署形态：本服务跑在容器里，容器镜像内置 docker CLI（仅 client，不带 daemon），
    所有命令通过环境变量 DOCKER_HOST 指向真实 Swarm manager 节点。
    """

    def __init__(
        self,
        docker_bin: str,
        docker_host: str,
        docker_tls_verify: str = "",
        docker_cert_path: str = "",
        log_default_tail: int = 100,
        log_max_tail: int = 1000,
    ) -> None:
        self.docker_bin = docker_bin
        self.docker_host = docker_host
        self.docker_tls_verify = docker_tls_verify
        self.docker_cert_path = docker_cert_path
        self.log_default_tail = log_default_tail
        self.log_max_tail = log_max_tail

    def healthcheck(self) -> dict:
        # 历史遗留：原本有个 ``self.docker_runner`` 字段（用来跑命令的辅助对象），
        # 后来重构成直接 ``docker_bin + DOCKER_HOST`` 走 subprocess，这里字段一直
        # 没删，admin 点验证按钮时报 AttributeError。现在改成回显 docker_bin
        # 让运维知道实际用的哪个 CLI binary。
        result = self.run(["info", "--format", "{{json .Swarm}}"])
        return {
            "healthy": result.ok,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "docker_bin": self.docker_bin,
            "docker_host": self.docker_host,
        }

    def list_services(self, filter_name: str | None = None) -> dict:
        rows = self.json(["service", "ls", "--format", "{{json .}}"])
        if isinstance(rows, dict):
            rows = [rows]
        if filter_name:
            needle = filter_name.lower()
            rows = [row for row in rows if needle in row.get("Name", "").lower()]
        return {"services": rows}

    def get_service_detail(self, service_name: str) -> dict:
        payload = self.json(["service", "inspect", service_name])
        return {"service": payload[0] if payload else {}}

    def get_service_status(self, service_name: str) -> dict:
        rows = self.json(["service", "ls", "--filter", f"name={service_name}", "--format", "{{json .}}"])
        if isinstance(rows, dict):
            rows = [rows]
        return {"status": rows[0] if rows else {}}

    def get_service_tasks(self, service_name: str) -> dict:
        rows = self.json(["service", "ps", service_name, "--no-trunc", "--format", "{{json .}}"])
        if isinstance(rows, dict):
            rows = [rows]
        return {"tasks": rows}

    def get_failed_tasks(self, service_name: str, limit: int = 10) -> dict:
        rows = self.get_service_tasks(service_name)["tasks"]
        failed = []
        for row in rows:
            state = (row.get("CurrentState") or "").lower()
            error = row.get("Error") or ""
            if "failed" in state or error:
                failed.append(row)
        return {"service_name": service_name, "failed_tasks": failed[:limit]}

    def check_service_health(self, service_name: str) -> dict:
        detail = self.get_service_detail(service_name)["service"]
        status = self.get_service_status(service_name)["status"]
        tasks = self.get_service_tasks(service_name)["tasks"]
        failed = self.get_failed_tasks(service_name)["failed_tasks"]
        task_template = detail.get("Spec", {}).get("TaskTemplate", {})
        return {
            "service_name": service_name,
            "status": status,
            "update_status": detail.get("UpdateStatus", {}),
            "restart_policy": task_template.get("RestartPolicy", {}),
            "image": task_template.get("ContainerSpec", {}).get("Image"),
            "constraints": task_template.get("Placement", {}).get("Constraints", []),
            "failed_tasks": failed,
            "task_count": len(tasks),
        }

    def get_service_logs(self, service_name: str, tail: int | None = None, since: str | None = None) -> dict:
        final_tail = min(tail or self.log_default_tail, self.log_max_tail)
        args = ["service", "logs", service_name, "--tail", str(final_tail), "--raw"]
        if since:
            args.extend(["--since", since])
        result = self.run(args)
        if not result.ok:
            combined = "\n".join([part for part in [result.stdout, result.stderr] if part]).strip()
            if not combined:
                raise RuntimeError("获取服务日志失败")
            return {
                "service_name": service_name,
                "tail": final_tail,
                "since": since,
                "logs": combined,
                "partial": True,
            }
        return {
            "service_name": service_name,
            "tail": final_tail,
            "since": since,
            "logs": result.stdout,
            "partial": False,
        }

    def get_service_logs_filter(
        self,
        service_name: str,
        keyword: str,
        tail: int | None = None,
        since: str | None = None,
    ) -> dict:
        payload = self.get_service_logs(service_name=service_name, tail=tail, since=since)
        lines = payload["logs"].splitlines()
        needle = keyword.lower()
        matched = [line for line in lines if needle in line.lower()]
        return {
            "service_name": service_name,
            "keyword": keyword,
            "tail": payload["tail"],
            "since": payload["since"],
            "matched_count": len(matched),
            "matched_logs": "\n".join(matched[:200]),
            "partial": payload.get("partial", False),
        }

    def run(self, args: list[str]) -> CommandResult:
        command = [self.docker_bin] + args
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=self._native_env(),
        )
        return CommandResult(
            command=command,
            stdout=completed.stdout.strip(),
            stderr=completed.stderr.strip(),
            returncode=completed.returncode,
        )

    def json(self, args: list[str]) -> Any:
        result = self.run(args)
        if not result.ok:
            raise RuntimeError(result.stderr or result.stdout or "docker command failed")
        payload = result.stdout.strip()
        if not payload:
            return []
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            rows = []
            for line in payload.splitlines():
                line = line.strip()
                if not line:
                    continue
                rows.append(json.loads(line))
            return rows

    def _native_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["DOCKER_HOST"] = self.docker_host
        if self.docker_tls_verify:
            env["DOCKER_TLS_VERIFY"] = self.docker_tls_verify
        if self.docker_cert_path:
            env["DOCKER_CERT_PATH"] = self.docker_cert_path
        return env

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from typing import Any

from config import get_settings


@dataclass
class CommandResult:
    command: list[str]
    stdout: str
    stderr: str
    returncode: int

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class DockerCLI:
    def __init__(self) -> None:
        self.settings = get_settings()

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["DOCKER_HOST"] = self.settings.docker_host
        if self.settings.docker_tls_verify:
            env["DOCKER_TLS_VERIFY"] = self.settings.docker_tls_verify
        if self.settings.docker_cert_path:
            env["DOCKER_CERT_PATH"] = self.settings.docker_cert_path
        return env

    def run(self, args: list[str]) -> CommandResult:
        command = [self.settings.docker_bin] + args
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=self._env(),
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
        except json.JSONDecodeError as exc:
            items = []
            for line in payload.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    items.append(json.loads(line))
                except json.JSONDecodeError:
                    raise RuntimeError(f"命令返回的内容不是合法 JSON: {payload}") from exc
            return items

    def lines(self, args: list[str]) -> list[str]:
        result = self.run(args)
        if not result.ok:
            raise RuntimeError(result.stderr or result.stdout or "docker command failed")
        return [line for line in result.stdout.splitlines() if line.strip()]

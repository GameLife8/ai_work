"""K8s client：本地 kubectl + 远端 kubeconfig，行为对齐 docker_swarm_client。

我们不直接登录 server，而是把每个 K8s 集群的 kubeconfig 落到平台 Connection 表里，
执行时把它写到一个临时文件，调用本地 ``kubectl --kubeconfig=<tmp> ...`` 操作远端集群。

为什么不用 python kubernetes client：
- 已有 Swarm 走 docker CLI 的成熟模式，保持一致；
- kubectl 输出最丰富（rollout/describe/logs），调试方便；
- 私有化部署节点上一般已经装好 kubectl；
- 出错时可以直接给运维一条可复制的 kubectl 命令。
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
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


class K8sClient:
    """通过 kubectl 间接操作远端集群。"""

    def __init__(
        self,
        *,
        kubeconfig: str,
        kubectl_bin: str = "kubectl",
        context: str = "",
        namespace: str = "default",
        log_default_tail: int = 200,
        log_max_tail: int = 5000,
    ) -> None:
        if not kubeconfig or not kubeconfig.strip():
            raise ValueError("kubeconfig 不能为空")
        self.kubectl_bin = kubectl_bin
        self.context = context
        self.namespace = namespace or "default"
        self.log_default_tail = log_default_tail
        self.log_max_tail = log_max_tail

        # kubeconfig 写到临时文件（只在本进程生命周期内存在）
        self._lock = threading.Lock()
        fd, self._kubeconfig_path = tempfile.mkstemp(prefix="ai-ops-kubeconfig-", suffix=".yaml")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(kubeconfig)
        os.chmod(self._kubeconfig_path, 0o600)

    def __del__(self) -> None:  # pragma: no cover
        try:
            if getattr(self, "_kubeconfig_path", None):
                os.unlink(self._kubeconfig_path)
        except Exception:
            pass

    # ---------- 健康检查 ----------

    def healthcheck(self) -> dict:
        result = self.run(["version", "--output=json"])
        return {
            "healthy": result.ok,
            "stdout": result.stdout[:500],
            "stderr": result.stderr[:500],
            "context": self.context,
            "namespace": self.namespace,
        }

    # ---------- 只读 ----------

    def list_pods(self, namespace: str | None = None, label_selector: str | None = None) -> dict:
        cmd = self._scoped(["get", "pods", "-o", "json"], namespace)
        if label_selector:
            cmd.extend(["-l", label_selector])
        rows = self._json(cmd)
        items = rows.get("items", []) if isinstance(rows, dict) else []
        slim = [self._summarize_pod(p) for p in items]
        return {"namespace": namespace or self.namespace, "count": len(slim), "pods": slim}

    def describe_pod(self, name: str, namespace: str | None = None) -> dict:
        cmd = self._scoped(["describe", "pod", name], namespace)
        result = self.run(cmd)
        return {
            "name": name,
            "namespace": namespace or self.namespace,
            "ok": result.ok,
            "describe": result.stdout,
            "stderr": result.stderr,
        }

    def get_pod_logs(
        self,
        name: str,
        *,
        namespace: str | None = None,
        container: str | None = None,
        tail: int | None = None,
        since: str | None = None,
        previous: bool = False,
    ) -> dict:
        capped = min(tail or self.log_default_tail, self.log_max_tail)
        cmd = self._scoped(["logs", name, "--tail", str(capped)], namespace)
        if container:
            cmd.extend(["-c", container])
        if since:
            cmd.extend(["--since", since])
        if previous:
            cmd.append("--previous")
        result = self.run(cmd)
        return {
            "name": name,
            "namespace": namespace or self.namespace,
            "container": container,
            "tail": capped,
            "ok": result.ok,
            "logs": result.stdout,
            "stderr": result.stderr,
        }

    def list_deployments(self, namespace: str | None = None) -> dict:
        cmd = self._scoped(["get", "deploy", "-o", "json"], namespace)
        rows = self._json(cmd)
        items = rows.get("items", []) if isinstance(rows, dict) else []
        slim = [self._summarize_deployment(d) for d in items]
        return {"namespace": namespace or self.namespace, "count": len(slim), "deployments": slim}

    # ---------- 写 ----------

    def restart_deployment(self, name: str, namespace: str | None = None) -> CommandResult:
        return self.run(self._scoped(["rollout", "restart", f"deployment/{name}"], namespace))

    def scale_deployment(self, name: str, replicas: int, namespace: str | None = None) -> CommandResult:
        return self.run(
            self._scoped(["scale", f"deployment/{name}", f"--replicas={int(replicas)}"], namespace)
        )

    def rollout_undo(self, name: str, namespace: str | None = None) -> CommandResult:
        return self.run(self._scoped(["rollout", "undo", f"deployment/{name}"], namespace))

    # ---------- 底层 ----------

    def run(self, args: list[str]) -> CommandResult:
        cmd = self._build_command(args)
        completed = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        return CommandResult(
            command=cmd,
            stdout=(completed.stdout or "").strip(),
            stderr=(completed.stderr or "").strip(),
            returncode=completed.returncode,
        )

    def _json(self, args: list[str]) -> Any:
        result = self.run(args)
        if not result.ok:
            raise RuntimeError(result.stderr or result.stdout or "kubectl command failed")
        if not result.stdout:
            return {}
        return json.loads(result.stdout)

    def _scoped(self, args: list[str], namespace: str | None) -> list[str]:
        ns = namespace or self.namespace
        scoped = ["-n", ns] if ns else []
        if self.context:
            scoped = ["--context", self.context] + scoped
        return scoped + args

    def _build_command(self, args: list[str]) -> list[str]:
        return [self.kubectl_bin, "--kubeconfig", self._kubeconfig_path] + args

    # ---------- 数据精简 ----------

    @staticmethod
    def _summarize_pod(pod: dict) -> dict:
        meta = pod.get("metadata", {})
        status = pod.get("status", {})
        cs = status.get("containerStatuses", []) or []
        ready = sum(1 for c in cs if c.get("ready"))
        restarts = sum(int(c.get("restartCount", 0)) for c in cs)
        waiting_reasons = [
            (c.get("state", {}).get("waiting") or {}).get("reason")
            for c in cs if c.get("state", {}).get("waiting")
        ]
        return {
            "name": meta.get("name"),
            "namespace": meta.get("namespace"),
            "node": (pod.get("spec") or {}).get("nodeName"),
            "phase": status.get("phase"),
            "ready": f"{ready}/{len(cs)}" if cs else "0/0",
            "restarts": restarts,
            "waiting_reasons": [r for r in waiting_reasons if r],
            "start_time": status.get("startTime"),
        }

    @staticmethod
    def _summarize_deployment(d: dict) -> dict:
        meta = d.get("metadata", {})
        spec = d.get("spec", {})
        status = d.get("status", {})
        return {
            "name": meta.get("name"),
            "namespace": meta.get("namespace"),
            "replicas_desired": spec.get("replicas"),
            "replicas_ready": status.get("readyReplicas", 0),
            "replicas_available": status.get("availableReplicas", 0),
            "updated": status.get("updatedReplicas", 0),
            "image": next(
                (c.get("image") for c in (spec.get("template", {}).get("spec", {}).get("containers", []) or [])),
                None,
            ),
        }

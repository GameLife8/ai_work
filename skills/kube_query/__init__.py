"""K8s 通用只读查询 skill —— 一把口替代所有"kubectl get/describe/logs/top"。

为什么用一个 skill 而不是给每种资源单独建 skill
================================================
之前平台为每种 K8s 查询都做了独立 skill（k8s_list_pods / k8s_describe_pod /
k8s_get_pod_logs / k8s_list_deployments ...），结果：

1. 模型问"看 coredns 的 ConfigMap" → 没有 k8s_get_configmap，只能绕道 host_run_command
   塞 kubectl exec，最后因为 distroless 容器没 cat 而失败
2. 想查 ingress / service / endpoints / pvc / role / secret … 任何资源都得加 skill
3. 同一类操作（信号扫描）散落在 4 个 skill 里，重复维护

重构思路：**执行通用化，信号扫描独立**。本 skill 接受 ``verb`` + ``resource`` + ``name?``
等参数，组装成 ``kubectl <verb> ...`` 调用；执行完按 (verb, resource) 路由到
``ops_platform/scanners/*`` 里的扫描器自动 attach signals。

支持的 verb（**只读**，写操作走专用 skill）
- ``get``           列 / 拉资源（默认 yaml；可改 json/wide/jsonpath=...）
- ``describe``      human-readable 详情（含 Events）
- ``logs``          应用日志
- ``top``           ``kubectl top pod/node`` 资源占用（需 metrics-server）
- ``api-resources`` 列出可用资源类型（探索用）
- ``explain``       某资源类型的字段说明
- ``events``        ``kubectl get events`` 的便捷别名

**禁止**：``exec`` / ``port-forward`` / ``edit`` / ``apply`` / ``delete`` /
``scale`` / ``patch`` / ``rollout`` / ``-w``/``--watch``（长连接） —— 这些是写
操作或可能阻塞，走专用写 skill 或 host_run_command。

参数 schema
-----------
- ``verb``        必填，上面 7 个之一
- ``resource``    取决于 verb：get/describe 必填 (pods/svc/deploy/cm/secret/...)，
                  logs 不用（直接传 name），其他可选
- ``name``        可选；具体资源名
- ``namespace``   可选；不传走 connection 默认
- ``output``      可选 (yaml/json/wide/name/jsonpath=...)；默认 yaml
- ``selector``    可选；``-l app=foo`` 标签过滤
- ``all_namespaces``  bool；等价于 ``-A``
- ``container``   logs 用；多容器 pod 指定容器
- ``previous``    logs 用；看上次 crash 前日志
- ``tail``        logs 用；行数（默认 200，硬上限 5000）
- ``since``       logs 用；如 5m / 1h
- ``extra_args``  字符串列表，附加 kubectl 参数（不允许 -w/--watch）

返回
----
``{ok, command, stdout, stderr, returncode, _signals}``
"""

from __future__ import annotations

import shlex

from ops_platform.scanners import k8s_describe as scan_describe
from ops_platform.scanners import k8s_logs as scan_logs
from ops_platform.scanners import k8s_pod_list as scan_pod_list
from ops_platform.signals import attach


MANIFEST = {
    "code": "kube_query",
    "name": "K8s 通用查询",
    "description": (
        "**K8s 只读查询的唯一入口**——任意 ``kubectl get/describe/logs/top/api-resources/events``。示例：\n"
        "  - pod 列表 ``verb=get, resource=pods, namespace=default``；详情 ``verb=describe, resource=pod, name=xxx``\n"
        "  - 配置 ``verb=get, resource=configmap, name=coredns, namespace=kube-system``\n"
        "  - 日志 ``verb=logs, name=xxx``（**CrashLoopBackOff 必须带 ``previous=True``**；多容器用 ``container`` 指定）\n"
        "  - 节点占用 ``verb=top, resource=nodes``；资源类型 ``verb=api-resources``\n"
        "日志正常但 pod 仍异常 → 回 ``verb=describe`` 看系统层（OOM/调度/资源）。"
        "**写操作请走专用 skill**（k8s_scale_deployment / k8s_restart_deployment / k8s_rollout_undo）；"
        "看容器内文件优先 ``verb=get, resource=configmap`` 看挂载的 ConfigMap。"
    ),
    "category": "k8s",
    "required_connection_type": "k8s",
    "read_only": True,
    "visibility": "all",
    "params_schema": {
        "type": "object",
        "properties": {
            "verb": {"type": "string",
                     "enum": ["get", "describe", "logs", "top", "api-resources", "explain", "events"]},
            "resource": {"type": "string",
                         "description": "资源类型：pods/svc/deploy/cm/secret/ing/pvc/node/sa/role/...（logs 不用）"},
            "name": {"type": "string"},
            "namespace": {"type": "string"},
            "output": {"type": "string",
                       "description": "yaml/json/wide/name/jsonpath=...；默认 yaml"},
            "selector": {"type": "string", "description": "``-l`` 标签选择器，例如 ``app=foo,env=prod``"},
            "all_namespaces": {"type": "boolean", "default": False},
            "container": {"type": "string", "description": "logs 用，多容器 pod 指定容器"},
            "previous": {"type": "boolean", "default": False, "description": "logs 用，看上次 crash 前日志"},
            "tail": {"type": "integer", "minimum": 1, "maximum": 5000,
                     "description": "logs 用；默认 200"},
            "since": {"type": "string", "description": "logs 用；如 5m / 1h"},
            "extra_args": {"type": "array", "items": {"type": "string"},
                           "description": "附加 kubectl 参数（禁止 -w/--watch）"},
            "connection_id": {"type": "string"},
        },
        "required": ["verb"],
    },
}


# --- 安全约束 --- #

# 禁止透传给 kubectl 的危险/写参数
_BLOCKED_FLAGS = {
    "-w", "--watch", "--watch-only",
    "-f",                                  # 部分 verb 下 -f 是文件输入（apply -f）
    "--patch", "--patch-file",
}
# 资源名不允许的字符——防注入（kubectl 自己也会校验，但提早 reject 更友好）
_BAD_NAME_CHARS = set(";|&`$()<>\n\r\t\"'")


def _check_safe_token(s: str, field: str) -> None:
    if not isinstance(s, str):
        raise ValueError(f"{field} 必须是字符串")
    if any(c in _BAD_NAME_CHARS for c in s):
        raise ValueError(f"{field} 含非法字符：{s!r}")


def _build_cmd(*, verb: str, resource: str | None, name: str | None,
               namespace: str | None, output: str | None, selector: str | None,
               all_namespaces: bool, container: str | None, previous: bool,
               tail: int | None, since: str | None, extra_args: list[str] | None) -> list[str]:
    """组装最终 ``kubectl`` 命令（不含 ``kubectl --kubeconfig=...`` 前缀，
    那部分由 K8sClient.run 自己包）。"""
    cmd: list[str] = [verb]

    if verb == "events":
        cmd = ["get", "events"]                  # events 是 get 的别名
    if verb == "api-resources":
        # api-resources 不接 resource/name，纯探索
        if resource or name:
            raise ValueError("api-resources 不接 resource/name")
        if extra_args:
            cmd.extend(extra_args)
        return cmd
    if verb == "explain":
        if not resource:
            raise ValueError("verb=explain 需要 resource，例如 explain pod.spec.containers")
        _check_safe_token(resource, "resource")
        cmd.append(resource)
        return cmd

    if verb == "logs":
        if not name:
            raise ValueError("verb=logs 需要 name（pod 名）")
        _check_safe_token(name, "name")
        cmd = ["logs", name, "--tail", str(int(tail or 200))]
        if container:
            _check_safe_token(container, "container")
            cmd.extend(["-c", container])
        if since:
            _check_safe_token(since, "since")
            cmd.extend(["--since", since])
        if previous:
            cmd.append("--previous")
    elif verb in ("get", "describe", "top", "events"):
        if verb != "events" and not resource:
            raise ValueError(f"verb={verb} 需要 resource，例如 ``resource=pods``")
        if resource:
            _check_safe_token(resource, "resource")
            cmd.append(resource)
        if name:
            _check_safe_token(name, "name")
            cmd.append(name)
        if selector:
            _check_safe_token(selector, "selector")
            cmd.extend(["-l", selector])
        if verb == "get":
            cmd.extend(["-o", (output or "yaml")])
    else:
        raise ValueError(f"未知 verb={verb}")

    # 通用：命名空间
    if all_namespaces and not name:
        cmd.append("--all-namespaces")
    # namespace 由 K8sClient._scoped 注入；本函数不重复加，避免双 -n

    # extra_args 透传（禁危险 flag）
    for arg in (extra_args or []):
        if not isinstance(arg, str):
            raise ValueError("extra_args 元素必须是字符串")
        if arg in _BLOCKED_FLAGS or arg.startswith("--watch"):
            raise ValueError(f"禁用参数：{arg}")
        if any(c in _BAD_NAME_CHARS for c in arg):
            raise ValueError(f"extra_args 含非法字符：{arg!r}")
        cmd.append(arg)

    return cmd


def _route_scanner(*, verb: str, resource: str | None, name: str | None,
                   namespace: str | None, stdout: str, parsed_json) -> list[dict]:
    """按 (verb, resource) 路由到合适的 scanner；找不到就空列表（不发信号）。"""
    if not stdout:
        return []
    res = (resource or "").lower().rstrip("s")   # pods → pod
    if verb == "describe" and res in ("pod", "po"):
        return scan_describe.scan(stdout, name=name or "", namespace=namespace)
    if verb == "logs":
        return scan_logs.scan(stdout, pod=name or "", namespace=namespace)
    if verb == "get" and res in ("pod", "po"):
        # kubectl get pods 返回的 yaml 难解析；只有 output=json 我们才能跑 pod-list scanner
        if isinstance(parsed_json, dict):
            items = parsed_json.get("items") or []
            slim = []
            for it in items:
                meta = it.get("metadata", {})
                spec = it.get("spec", {})
                status = it.get("status", {})
                cs = status.get("containerStatuses", []) or []
                waiting = [(c.get("state", {}).get("waiting") or {}).get("reason")
                           for c in cs if c.get("state", {}).get("waiting")]
                slim.append({
                    "name": meta.get("name"),
                    "namespace": meta.get("namespace"),
                    "phase": status.get("phase"),
                    "restarts": sum(int(c.get("restartCount", 0)) for c in cs),
                    "waiting_reasons": [r for r in waiting if r],
                    "node": spec.get("nodeName"),
                })
            return scan_pod_list.scan(slim, namespace=namespace)
    return []


def run(
    ctx,
    *,
    verb: str,
    resource: str | None = None,
    name: str | None = None,
    namespace: str | None = None,
    output: str | None = None,
    selector: str | None = None,
    all_namespaces: bool = False,
    container: str | None = None,
    previous: bool = False,
    tail: int | None = None,
    since: str | None = None,
    extra_args: list[str] | None = None,
    connection_id: str | None = None,
) -> dict:
    client = ctx.connection_for("k8s", connection_id)

    args = _build_cmd(
        verb=verb, resource=resource, name=name, namespace=namespace,
        output=output, selector=selector, all_namespaces=all_namespaces,
        container=container, previous=previous, tail=tail, since=since,
        extra_args=extra_args,
    )

    # 走 K8sClient._scoped 自动加 -n + --context
    scoped = client._scoped(args, namespace) if not all_namespaces else (
        ["--context", client.context] if client.context else []
    ) + args
    result = client.run(scoped)

    payload = {
        "verb": verb, "resource": resource, "name": name, "namespace": namespace or client.namespace,
        "command": " ".join(shlex.quote(c) for c in result.command),
        "ok": result.ok,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "returncode": result.returncode,
    }

    # 尝试 JSON 解析（output=json 或者隐式有 items 字段）
    parsed_json = None
    if result.ok and (output == "json" or (result.stdout or "").lstrip().startswith("{")):
        try:
            import json as _json
            parsed_json = _json.loads(result.stdout)
            payload["parsed"] = parsed_json
        except Exception:
            pass

    sigs = _route_scanner(verb=verb, resource=resource, name=name,
                          namespace=namespace, stdout=result.stdout, parsed_json=parsed_json)
    return attach(payload, sigs)

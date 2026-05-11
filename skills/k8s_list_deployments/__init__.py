from __future__ import annotations

from ops_platform.signals import (
    SEV_CRITICAL,
    SEV_WARNING,
    SIG_REPLICAS_INSUFFICIENT,
    attach,
    signal,
)


MANIFEST = {
    "code": "k8s_list_deployments",
    "name": "列出 K8s Deployment",
    "description": (
        "列出指定 namespace 下的 Deployment，含期望/就绪/可用副本和当前镜像。"
        "**用途**：(1) 先确认 deployment 名拼写 / 在哪个 namespace；"
        "(2) 扫一眼有哪些副本不齐 (ready < desired)；"
        "(3) 找到目标后 pivot 到 k8s_describe_pod 看具体 pod 状态。"
        "返回的 ``_signals`` 会自动标出副本不齐的 deployment，建议下一步调 k8s_describe_pod。"
    ),
    "category": "k8s",
    "required_connection_type": "k8s",
    "read_only": True,
    "visibility": "all",
    "params_schema": {
        "type": "object",
        "properties": {
            "namespace": {"type": "string"},
            "connection_id": {"type": "string"},
        },
    },
}


def _safe_int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _extract_signals(deployments: list[dict], namespace: str | None) -> list[dict]:
    """ready < desired 时挂 SIG_REPLICAS_INSUFFICIENT，让 agent 自动钻到具体 pod。

    severity 阈值：
      - ready == 0：CRITICAL（服务完全不可用）
      - 0 < ready < desired：WARNING（部分降级）
    """
    sigs: list[dict] = []
    for d in deployments or []:
        # 不同 k8s_client 实现字段名略有差异：兼容 desired/replicas、ready/readyReplicas、name/Name
        name = d.get("name") or d.get("Name") or ""
        ns = d.get("namespace") or d.get("Namespace") or namespace or "default"
        desired = _safe_int(d.get("desired") or d.get("replicas") or d.get("Replicas"))
        ready = _safe_int(d.get("ready") or d.get("readyReplicas") or d.get("ReadyReplicas"))

        if desired <= 0 or ready >= desired:
            continue

        if ready == 0:
            sev = SEV_CRITICAL
            evidence = f"Deployment {ns}/{name} 0/{desired} 就绪——服务完全不可用"
        else:
            sev = SEV_WARNING
            evidence = f"Deployment {ns}/{name} {ready}/{desired} 就绪——部分副本异常"

        sigs.append(signal(
            SIG_REPLICAS_INSUFFICIENT,
            severity=sev,
            evidence=evidence,
            # describe_pod 需要 pod 名，但这里只有 deployment 名；让模型先调 k8s_list_pods 拿 pod 名再 describe
            next_skill="k8s_list_pods",
            next_args={"namespace": ns},
            context={
                "deployment": name,
                "namespace": ns,
                "desired": desired,
                "ready": ready,
            },
        ))
    return sigs


def run(ctx, *, namespace: str | None = None, connection_id: str | None = None) -> dict:
    result = ctx.connection_for("k8s", connection_id).list_deployments(namespace=namespace)
    # 兼容两种返回：直接 list[dict] 或 {"deployments": [...], ...}
    if isinstance(result, list):
        deployments = result
        wrapped = {"deployments": result}
    else:
        deployments = (result or {}).get("deployments") or (result or {}).get("items") or []
        wrapped = result if isinstance(result, dict) else {"deployments": deployments}
    return attach(wrapped, _extract_signals(deployments, namespace))

"""返回平台沉淀的诊断剧本。

模型在面对不熟悉的场景时，调一次本 skill 拿到结构化 runbook，按里面 step 顺序选择
其它 skill 调用即可，比直接靠模型记忆要稳很多。
"""

from __future__ import annotations

from ops_platform.runbooks import GENERAL_GUIDANCE, RUNBOOKS

MANIFEST = {
    "code": "platform_get_runbooks",
    "name": "查询诊断剧本",
    "description": (
        "返回平台沉淀的「诊断剧本」——每个剧本是「触发场景 → 应该按什么顺序调哪些 skill 取证 → "
        "看到什么信号要切到哪个跨域 skill 」的结构化指引。"
        "**面对「服务起不来 / Pod CrashLoop / 高延迟 / 主机告警 / 发布失败 / 告警研判」等"
        "复合问题时，建议先调用本 skill 拿剧本，再按 step 取证**，避免漏查或重复查。"
        "可选 ``name`` 过滤单个剧本；不传则返回全部 + 通用指引。"
    ),
    "category": "platform",
    "required_connection_type": None,
    "read_only": True,
    "visibility": "all",
    "params_schema": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": (
                    "可选；剧本名（如 swarm_service_not_starting / k8s_pod_crashloop）。"
                    "不传返回全部剧本概览 + 跨域跳转规则。"
                ),
            },
        },
    },
}


def run(ctx, *, name: str | None = None, **_kwargs) -> dict:
    if name:
        runbook = RUNBOOKS.get(name)
        if not runbook:
            return {
                "error": f"runbook '{name}' 不存在",
                "available": sorted(RUNBOOKS.keys()),
            }
        return {"name": name, "runbook": runbook}

    summary = {
        n: {"title": rb.get("title"), "triggers": rb.get("triggers", [])}
        for n, rb in RUNBOOKS.items()
    }
    return {
        "runbooks_summary": summary,
        "general_guidance": GENERAL_GUIDANCE,
        "tip": "调用 platform_get_runbooks(name='xxx') 获取某个剧本的完整 step 列表",
    }

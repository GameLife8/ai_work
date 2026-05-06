"""按 runbook 图自动取证（平台决定调啥 / 怎么调 / 参数从哪来）。

调用方式
--------
::

    platform_run_runbook(
        name="swarm_service_not_starting",        # 可选，不传则按 user_query 自动匹配
        user_query="iiot-haitu_seatable 起不来",   # 必填，给最终报告用 + 自动匹配
        inputs={"service_name": "iiot-haitu_seatable"},  # runbook.inputs 要求的字段
    )

为什么不让模型自己调一连串 skill
--------------------------------
模型现想现做的诊断路径**每次都不太一样**——同一个问题可能漏查主机层、可能反复试关键词、
可能在 token 用尽前没收完证据。runbook 把"运维老司机的查问题套路"代码化成可执行图，
平台保证：
  1. 跨域 pivot 一定发生（OOM 信号 → 必查 zabbix host）
  2. 同一个 skill 不会被重复调
  3. 失败节点按 on_error 策略优雅降级
  4. 每一步都落审计，事后可回放

模型在末端只做**语言层**：把所有证据转成中文五段式报告。
"""

from __future__ import annotations

import logging

from ops_platform.runbook_engine import (
    RunbookExecutor,
    render_final_report,
)


logger = logging.getLogger(__name__)


MANIFEST = {
    "code": "platform_run_runbook",
    "name": "执行诊断剧本",
    "description": (
        "按预定义的诊断剧本（DAG）自动取证。**适合复合问题**——服务起不来、Pod CrashLoop、"
        "网络不通、主机告警等场景。比一步步靠模型自己挑 skill 更稳、更快、可审计。"
        "name 不传则按用户原话匹配 triggers 关键词；inputs 提供 runbook 需要的字段。"
        "返回里 ``node_states`` 是每节点详细执行结果；``_signals`` 是全局收集的信号；"
        "``final_report`` 是模型基于上述材料写的中文五段式报告——直接给用户看就行，"
        "**不要再追加任何工具调用**。"
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
                "description": "runbook key；不传则按 user_query 自动匹配",
            },
            "user_query": {
                "type": "string",
                "description": "用户原话，必填——用于自动匹配 + 让模型出最终报告",
            },
            "inputs": {
                "type": "object",
                "description": "runbook.inputs 要求的字段，例如 {service_name, namespace, node, host_query}",
            },
        },
        "required": ["user_query"],
    },
}


def run(ctx, *, user_query: str, name: str | None = None, inputs: dict | None = None,
        connection_id: str | None = None, **_kwargs) -> dict:
    runtime = ctx.runtime
    registry = getattr(runtime, "runbook_registry", None)
    if registry is None:
        return {"error": "runbook_registry 未初始化（runtime 未装配）"}

    rb = registry.find(name=name, user_query=user_query or "")
    if rb is None:
        return {
            "error": "no_matching_runbook",
            "message": (f"未找到名为 {name!r} 的剧本" if name
                        else "user_query 没匹配到任何剧本 triggers"),
            "available": registry.list_keys(),
        }

    user_inputs = dict(inputs or {})
    user_inputs.setdefault("user_query", user_query)

    executor = RunbookExecutor(runtime)
    exec_result = executor.execute(rb, user_inputs=user_inputs, skill_ctx=ctx)

    # 模型生成最终中文报告（这是 runbook 的"语言层"环节）
    final_report = ""
    try:
        model_client = runtime.model_manager.get_client()
        final_report = render_final_report(
            exec_result, user_message=user_query,
            model_client=model_client,
            custom_prompt=rb.final_report_prompt or "",
        )
    except Exception as exc:
        logger.exception("生成最终报告失败")
        final_report = f"（生成报告失败：{exc}；原始证据见 node_states）"

    # 落库，可在 admin UI 回放
    try:
        runtime.store.save_runbook_execution(
            execution_id=exec_result.execution_id,
            runbook_key=exec_result.runbook_key,
            runbook_version=exec_result.runbook_version,
            triggered_by=(ctx.user or {}).get("username"),
            session_id=ctx.session_id,
            user_inputs=user_inputs,
            global_status=exec_result.global_status,
            abort_reason=exec_result.abort_reason,
            total_ms=exec_result.total_ms,
            node_states=exec_result.node_states,
            signals=exec_result.all_signals,
            final_report=final_report,
            started_at=exec_result.started_at,
            ended_at=exec_result.ended_at,
        )
    except Exception:  # pragma: no cover
        logger.exception("写 runbook execution 审计失败")

    out = exec_result.to_skill_result()
    out["final_report"] = final_report
    return out

from __future__ import annotations

import logging
import threading
from typing import Any

from services.alert_parser import AlertParser


logger = logging.getLogger(__name__)


class AlertService:
    """旧告警 pipeline：webhook → 解析 → AI 决策 → incident 联动。

    新增：``handle_alert`` 末尾会 **best-effort** 触发一次 runbook 自动诊断
    （需 ``runtime_ref`` + ``auto_runbook_enabled``）。这是为了让 webhook 告警
    自动跑跨域诊断剧本（OOM 信号→主机概览、5xx→服务+主机+网络一连串），而不只是
    停留在「AI 单步决策」。

    runbook 失败/超时不会污染原 alert decision 返回结构——只是 ``auto_diagnosis``
    字段会带错误信息；alert_event / alert_decision 落库照常。
    """

    def __init__(
        self,
        store,
        ai_client,
        context_fetcher,
        incident_service,
        decision_engine,
        default_needs: list[str],
        *,
        runtime_ref: Any | None = None,
        auto_runbook_enabled: bool = False,
        auto_runbook_timeout_seconds: int = 120,
    ) -> None:
        self.store = store
        self.ai_client = ai_client
        self.context_fetcher = context_fetcher
        self.incident_service = incident_service
        self.decision_engine = decision_engine
        self.default_needs = default_needs
        self.runtime_ref = runtime_ref
        self.auto_runbook_enabled = auto_runbook_enabled
        self.auto_runbook_timeout_seconds = max(10, int(auto_runbook_timeout_seconds))

    def handle_alert(self, raw_payload: dict) -> dict:
        alert = AlertParser.parse(raw_payload)
        alert_event_id = self.store.save_alert_event(alert, raw_payload)

        plan = self.ai_client.plan_context(alert)
        needs = plan.get("needs") or self.default_needs

        context = self.context_fetcher.fetch_context(needs, alert)
        decision = self.ai_client.judge_alert(alert, context)
        decision["ai_plan"] = plan

        action_result = self.decision_engine.execute_action(alert, decision)
        merged_decision = {**decision, **action_result}

        incident_result = self.incident_service.apply_decision(alert_event_id, alert, merged_decision)
        merged_decision.update(incident_result)

        self.store.save_alert_decision(alert_event_id, merged_decision, plan=plan, context=context)

        # best-effort 自动诊断：触发 runbook，失败不抛
        auto_diagnosis = self._maybe_run_runbook(alert, alert_event_id)

        return {
            "alert_event_id": alert_event_id,
            "decision": merged_decision,
            "context": context,
            "plan": plan,
            "alert": alert,
            "auto_diagnosis": auto_diagnosis,
        }

    # ---------- runbook 自动诊断 ---------- #

    def _maybe_run_runbook(self, alert: dict, alert_event_id: int) -> dict | None:
        """从 alert 内容匹配 runbook 并执行；返回 ExecutionResult.to_dict() 或 None。

        约定：
        - ``runtime_ref`` 没注入 / 开关关 → 直接 None（不尝试，不报错）。
        - 没匹配上任何 runbook → 返回 {"status": "skipped", "reason": "no_match"}
        - 执行抛异常 / 超时 → 返回 {"status": "error", "error": "..."}（仍 best-effort）
        - 正常 → 返回带 execution_id / runbook_key / global_status 的轻量 dict
        """
        if not self.auto_runbook_enabled or not self.runtime_ref:
            return None

        registry = getattr(self.runtime_ref, "runbook_registry", None)
        invoker = getattr(self.runtime_ref, "skill_invoker", None)
        if registry is None or invoker is None:
            return None

        # 把 AlertParser 解析后的字段拼成一个 trigger 查询串（runbook triggers 模糊匹配）。
        # AlertParser 输出字段：alert_name / alert_message / host_name / host_ip / tags / resource_scope
        # ——没有 summary / service_name，service 信息通常落在 tags 或 resource_scope 里。
        tags = alert.get("tags") or {}
        scope = alert.get("resource_scope") or {}
        service_name = (
            tags.get("service") or tags.get("service_name")
            or scope.get("service") or scope.get("service_name")
            or ""
        )
        query_parts = [
            alert.get("alert_name") or "",
            alert.get("alert_message") or "",
            alert.get("host_name") or "",
            service_name,
        ]
        query = " ".join(p for p in query_parts if p).strip()
        if not query or query == "unknown alert":
            # alert_name 默认是 "unknown alert"——这种情况下也算"没什么可匹配的"
            return {"status": "skipped", "reason": "empty_alert_summary"}

        runbook = registry.match_by_query(query)
        if runbook is None:
            return {"status": "skipped", "reason": "no_match", "query": query}

        # 准备 runbook inputs：从 AlertParser 输出的字段映射到 runbook 常见参数名
        # （runbook 自己声明的 inputs 列表是权威；这里只是按"常见键"探测式填）
        candidate_inputs = {
            "service_name": service_name,
            "host_query": alert.get("host_name") or alert.get("host_ip"),
            "host_name": alert.get("host_name"),
            "namespace": tags.get("namespace") or scope.get("namespace"),
            "pod_name": tags.get("pod") or scope.get("pod"),
            "mount_point": scope.get("mount_point"),
            "summary": alert.get("alert_name") or alert.get("alert_message"),
        }
        user_inputs = {k: v for k, v in candidate_inputs.items() if v}
        # 必填字段缺失时直接放弃——避免送进 runbook 后才报错
        missing = [k for k in runbook.inputs if k not in user_inputs]
        if missing:
            return {
                "status": "skipped",
                "reason": "missing_required_inputs",
                "runbook_key": runbook.key,
                "missing_inputs": missing,
            }

        # 跑 runbook（带总超时；超时后 ExecutionResult 已经能反映 timeout 状态）
        try:
            return self._execute_runbook_with_timeout(runbook, user_inputs, alert_event_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("auto-runbook 执行失败 (alert_event_id=%s)", alert_event_id)
            return {"status": "error", "runbook_key": runbook.key, "error": str(exc)}

    def _execute_runbook_with_timeout(
        self, runbook, user_inputs: dict[str, Any], alert_event_id: int,
    ) -> dict:
        """用线程 + Event 兜一层超时，保证哪怕 runbook 引擎自己卡死也不阻塞 webhook 响应。

        ``runbook.max_total_seconds`` 也是执行器内部的硬上限；这里再加一层是"防御性"——
        如果某个 skill handler 在 ThreadPoolExecutor 之外卡死（极少），整体仍能脱困。
        """
        from ops_platform.context import SkillContext
        from ops_platform.runbook_engine import RunbookExecutor

        result_box: dict[str, Any] = {}

        def _runner():
            try:
                executor = RunbookExecutor(self.runtime_ref)
                # alert pipeline 是系统侧动作；用 system actor + 无人工 selected_connections
                skill_ctx = SkillContext(
                    runtime=self.runtime_ref,
                    user={"username": "auto-runbook", "role": "admin"},
                    session_id=f"alert-{alert_event_id}",
                    selected_connections={},
                )
                result = executor.execute(runbook, user_inputs=user_inputs, skill_ctx=skill_ctx)
                # 写一份 runbook execution 行到 DB（如果 store 支持）
                if hasattr(self.store, "save_runbook_execution"):
                    try:
                        self.store.save_runbook_execution(
                            execution_id=result.execution_id,
                            runbook_key=result.runbook_key,
                            runbook_version=result.runbook_version,
                            global_status=result.global_status,
                            started_at=result.started_at,
                            ended_at=result.ended_at,
                            total_ms=result.total_ms,
                            user_inputs=user_inputs,
                            node_states=result.node_states,
                            all_signals=result.all_signals,
                            abort_reason=result.abort_reason,
                            triggered_by=f"alert:{alert_event_id}",
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("save_runbook_execution 失败：%s", exc)
                result_box["result"] = result
            except Exception as exc:  # noqa: BLE001
                result_box["error"] = str(exc)

        t = threading.Thread(target=_runner, name=f"auto-runbook-{alert_event_id}", daemon=True)
        t.start()
        t.join(timeout=self.auto_runbook_timeout_seconds)
        if t.is_alive():
            return {
                "status": "timeout",
                "runbook_key": runbook.key,
                "timeout_seconds": self.auto_runbook_timeout_seconds,
            }
        if "error" in result_box:
            return {"status": "error", "runbook_key": runbook.key, "error": result_box["error"]}

        exec_result = result_box["result"]
        # 给 alert API 调用方返回轻量版（详细 node_states 已经入库）；UI 需要完整的可以另查
        return {
            "status": exec_result.global_status,
            "execution_id": exec_result.execution_id,
            "runbook_key": exec_result.runbook_key,
            "runbook_version": exec_result.runbook_version,
            "total_ms": exec_result.total_ms,
            "abort_reason": exec_result.abort_reason,
            "node_count": len(exec_result.node_states),
            "signals_count": len(exec_result.all_signals),
            "top_signals": exec_result.all_signals[:5],   # 前 5 个，UI 上一目了然
        }

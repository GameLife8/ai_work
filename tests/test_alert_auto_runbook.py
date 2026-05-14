"""AlertService → runbook 自动诊断的单元测试。

不打真实 runbook engine / skill；用 FakeRegistry + FakeExecutor 模拟边界行为，
覆盖：
  1. 开关关 → 不触发，不污染原响应
  2. 没匹配上 runbook → status=skipped/no_match
  3. 必填 inputs 缺失 → status=skipped/missing_required_inputs
  4. 正常执行 → 返回 execution 概要
  5. 异常 → 用 error 兜住，不抛
"""

from __future__ import annotations

from typing import Any

from services.alert_service import AlertService


# ---------- 极简 dependency fakes ---------- #

class _FakeStore:
    def __init__(self) -> None:
        self.saved = []
        self.runbook_executions = []

    def save_alert_event(self, alert, raw_payload):
        rec = {"id": len(self.saved) + 1, "alert": alert, "raw": raw_payload}
        self.saved.append(rec)
        return rec["id"]

    def save_alert_decision(self, alert_event_id, decision, **_):
        return alert_event_id

    def save_runbook_execution(self, **fields):
        self.runbook_executions.append(fields)


class _AI:
    def plan_context(self, alert):
        return {"needs": []}

    def judge_alert(self, alert, context):
        return {"severity": "warning"}


class _Fetcher:
    def fetch_context(self, needs, alert):
        return {}


class _Incidents:
    def apply_decision(self, alert_event_id, alert, decision):
        return {}


class _DecisionEngine:
    def execute_action(self, alert, decision):
        return {}


class _FakeRunbook:
    """模拟 Runbook 对象——只读 ``key`` / ``inputs`` / ``max_total_seconds``。"""

    def __init__(self, key: str, inputs: list[str] | None = None) -> None:
        self.key = key
        self.inputs = inputs or []
        self.max_total_seconds = 60


class _FakeRegistry:
    def __init__(self, runbook: _FakeRunbook | None) -> None:
        self._rb = runbook

    def match_by_query(self, query: str):
        return self._rb


class _FakeExecResult:
    """模拟 ExecutionResult 给 _execute_runbook_with_timeout 转换。"""

    def __init__(self, status: str = "done") -> None:
        self.execution_id = "exec-1"
        self.runbook_key = "fake"
        self.runbook_version = 1
        self.global_status = status
        self.started_at = "2026-04-02T12:00:00+00:00"
        self.ended_at = "2026-04-02T12:00:01+00:00"
        self.total_ms = 1000
        self.node_states = [{"node_id": "n1", "status": "done"}]
        self.all_signals = [{"type": "oom_kill", "severity": "critical"}]
        self.abort_reason = None


class _FakeRuntime:
    """提供 runbook_registry / skill_invoker 供 AlertService 探针。"""

    def __init__(self, runbook: _FakeRunbook | None) -> None:
        self.runbook_registry = _FakeRegistry(runbook)
        self.skill_invoker = object()    # _maybe_run_runbook 只检查存在性


def _make_service(*, runtime_ref=None, enabled: bool, store=None) -> AlertService:
    return AlertService(
        store=store or _FakeStore(),
        ai_client=_AI(),
        context_fetcher=_Fetcher(),
        incident_service=_Incidents(),
        decision_engine=_DecisionEngine(),
        default_needs=[],
        runtime_ref=runtime_ref,
        auto_runbook_enabled=enabled,
        auto_runbook_timeout_seconds=5,
    )


def _payload(**overrides):
    """构造 webhook raw payload —— AlertParser 会重新解析字段。

    AlertParser 用 ``alert_name`` 和 ``alert_message`` 而不是 ``summary``，
    ``service`` 通常落到 ``tags`` 或 ``resource_scope``。我们这里直接构造
    parser 能识别的字段名（同名透传）。
    """
    base = {
        "alert_name": "host node-3 OOM",
        "alert_message": "memory usage exceeded threshold",
        "host_name": "node-3",
        "tags": {"service": "web"},
        "event_time": "2026-04-02 12:00:00",
        "severity": "critical",
    }
    base.update(overrides)
    return base


# ---------- 测试用例 ---------- #

def test_auto_runbook_disabled_returns_none_in_response():
    """开关关 → response 不带 auto_diagnosis 字段（path 直接 None）。"""
    svc = _make_service(runtime_ref=None, enabled=False)
    out = svc.handle_alert(_payload())
    assert out["auto_diagnosis"] is None


def test_auto_runbook_no_matching_runbook_yields_skipped():
    """AlertParser 会重写部分字段（alert_name 从 alert_message 派生等），所以这里只
    校验 query 含 host_name + tags.service —— 这俩是真正稳定的 passthrough 字段。"""
    svc = _make_service(runtime_ref=_FakeRuntime(runbook=None), enabled=True)
    out = svc.handle_alert(_payload())
    diag = out["auto_diagnosis"]
    assert diag["status"] == "skipped"
    assert diag["reason"] == "no_match"
    assert "node-3" in diag["query"]
    assert "web" in diag["query"]


def test_auto_runbook_missing_required_inputs_yields_skipped():
    """runbook 声明需要 mount_point 但 alert 没带 → skipped/missing_required_inputs。"""
    rb = _FakeRunbook("disk_diag", inputs=["mount_point"])
    svc = _make_service(runtime_ref=_FakeRuntime(runbook=rb), enabled=True)
    out = svc.handle_alert(_payload())     # 没带 mount_point
    diag = out["auto_diagnosis"]
    assert diag["status"] == "skipped"
    assert diag["reason"] == "missing_required_inputs"
    assert diag["missing_inputs"] == ["mount_point"]


def test_auto_runbook_executes_and_returns_summary(monkeypatch):
    """正常路径：匹配 runbook → 跑成功 → 响应里有 execution_id / status=done / signals。"""
    rb = _FakeRunbook("web_diag", inputs=["service_name"])
    runtime = _FakeRuntime(runbook=rb)
    store = _FakeStore()
    svc = _make_service(runtime_ref=runtime, enabled=True, store=store)

    # patch RunbookExecutor 让它直接返回 FakeExecResult，避免真跑引擎
    import services.alert_service as mod
    real_import = __import__

    class _FakeExecutor:
        def __init__(self, _runtime):
            pass

        def execute(self, runbook, *, user_inputs, skill_ctx):
            return _FakeExecResult(status="done")

    # 偷换 ops_platform.runbook_engine.RunbookExecutor（_execute_runbook_with_timeout 内部 import）
    import ops_platform.runbook_engine as rb_mod
    monkeypatch.setattr(rb_mod, "RunbookExecutor", _FakeExecutor)

    out = svc.handle_alert(_payload())
    diag = out["auto_diagnosis"]
    assert diag["status"] == "done"
    assert diag["execution_id"] == "exec-1"
    assert diag["runbook_key"] == "fake"
    assert diag["node_count"] == 1
    assert diag["signals_count"] == 1
    assert diag["top_signals"][0]["type"] == "oom_kill"
    # 入库
    assert len(store.runbook_executions) == 1
    assert store.runbook_executions[0]["triggered_by"].startswith("alert:")


def test_auto_runbook_swallows_executor_exception(monkeypatch):
    """RunbookExecutor 内部抛异常时，_maybe_run_runbook 应该返回 error 而不是冒泡。"""
    rb = _FakeRunbook("web_diag", inputs=["service_name"])
    runtime = _FakeRuntime(runbook=rb)
    svc = _make_service(runtime_ref=runtime, enabled=True)

    class _BoomExecutor:
        def __init__(self, _runtime):
            pass

        def execute(self, *_a, **_kw):
            raise RuntimeError("simulated executor crash")

    import ops_platform.runbook_engine as rb_mod
    monkeypatch.setattr(rb_mod, "RunbookExecutor", _BoomExecutor)

    out = svc.handle_alert(_payload())
    diag = out["auto_diagnosis"]
    assert diag["status"] == "error"
    assert "simulated executor crash" in diag["error"]


def test_auto_runbook_empty_query_yields_skipped():
    """没 alert_name / message / host / service —— 没法组 trigger 查询串，直接 skipped。

    AlertParser 默认会给 alert_name="unknown alert"——我们的 query 组装逻辑会把
    这种情况也识别为"没东西可匹配"。
    """
    rb = _FakeRunbook("anything")
    svc = _make_service(runtime_ref=_FakeRuntime(runbook=rb), enabled=True)
    out = svc.handle_alert({"event_time": "2026-04-02 12:00:00"})
    diag = out["auto_diagnosis"]
    assert diag["status"] == "skipped"
    assert diag["reason"] == "empty_alert_summary"

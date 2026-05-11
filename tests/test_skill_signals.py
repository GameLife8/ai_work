"""跨 skill 的 signal 行为回归测试。

这些测试不打 Zabbix/Swarm/K8s，只验证 ``_extract_signals`` 逻辑——
保证之后改 prompt / 改 client 不要回归这些守则：

  1. host_name 缺失时不挂 ``next_skill``（免得给模型一个空 host_query）
  2. metric_query_peak warning 区间也有 next_skill（之前只 critical 有）
  3. swarm_get_failed_tasks service_name 缺失时不挂 ``next_skill``
  4. k8s_get_pod_logs 命中 OOM / connection refused 关键词时发 signal
  5. k8s_list_deployments ready < desired 时发 SIG_REPLICAS_INSUFFICIENT
"""

from __future__ import annotations


# ---------- metric_query_peak ---------- #

def test_peak_warning_branch_attaches_next_skill():
    from skills.metric_query_peak import _extract_signals

    result = {
        "metric": "cpu.utilization",
        "peak": {"value": 85.0, "time": "2026-04-02T12:34:00+00:00"},
        "host": {"host_name": "web-01"},
        "lookback_seconds": 3600,
    }
    sigs = _extract_signals(result)
    assert len(sigs) == 1
    assert sigs[0]["severity"] == "warning"
    # 之前这里 warning 分支没挂 next_skill，模型拿到只能干瞪眼
    assert sigs[0]["next_skill"] == "zabbix_get_host_overview"
    assert sigs[0]["next_args"]["host_query"] == "web-01"


def test_peak_skips_next_skill_when_host_name_missing():
    """host_name 为空时不递空 host_query 给下游 skill。"""
    from skills.metric_query_peak import _extract_signals

    result = {
        "metric": "cpu.utilization",
        "peak": {"value": 95.0, "time": "2026-04-02T12:34:00+00:00"},
        "host": {"host_name": ""},        # 关键
        "lookback_seconds": 3600,
    }
    sigs = _extract_signals(result)
    assert len(sigs) == 1
    assert sigs[0]["severity"] == "critical"
    assert sigs[0].get("next_skill") is None
    assert sigs[0].get("next_args") is None


def test_peak_memory_critical_attaches_next_skill():
    from skills.metric_query_peak import _extract_signals

    result = {
        "metric": "memory.utilization",
        "peak": {"value": 92.0, "time": "2026-04-02T12:34:00+00:00"},
        "host": {"host_name": "db-01"},
        "lookback_seconds": 86400,
    }
    sigs = _extract_signals(result)
    assert sigs[0]["type"] == "high_memory"
    assert sigs[0]["next_skill"] == "zabbix_get_host_overview"


# ---------- swarm_get_failed_tasks ---------- #

def test_failed_tasks_skips_next_skill_when_service_name_missing():
    from skills.swarm_get_failed_tasks import _extract_signals

    sigs = _extract_signals([
        # ServiceName 缺失 且 Name 不带 "." 分割不出 service —— 不应递空 next_args
        {"Name": "", "ServiceName": "",
         "Error": "pull access denied for ghcr.io/foo:bar",
         "CurrentState": "rejected", "Node": "node-1"},
    ])
    assert len(sigs) == 1
    assert sigs[0]["type"] == "image_pull_fail"
    assert sigs[0].get("next_skill") is None


def test_failed_tasks_oom_emits_critical_with_node_pivot():
    from skills.swarm_get_failed_tasks import _extract_signals

    sigs = _extract_signals([
        {"Name": "web.1.abc", "ServiceName": "web", "Node": "node-1",
         "Error": "task: non-zero exit (137)", "CurrentState": "failed 30s ago"},
    ])
    assert len(sigs) == 1
    assert sigs[0]["type"] == "oom_kill"
    assert sigs[0]["severity"] == "critical"
    assert sigs[0]["next_skill"] == "zabbix_get_host_overview"
    assert sigs[0]["next_args"] == {"host_query": "node-1"}


# ---------- k8s_get_pod_logs ---------- #

def test_pod_logs_detects_oom_and_pivots_to_host_overview():
    from skills.k8s_get_pod_logs import _scan_logs

    sigs = _scan_logs(
        "2026-04-02 12:34:56 ERROR Out of memory: Killed process 1234 (java)",
        pod="api-0", namespace="prod",
    )
    types = [s["type"] for s in sigs]
    assert "oom_kill" in types
    oom = next(s for s in sigs if s["type"] == "oom_kill")
    assert oom["next_skill"] == "zabbix_get_host_overview"


def test_pod_logs_detects_connection_refused():
    from skills.k8s_get_pod_logs import _scan_logs

    sigs = _scan_logs(
        "dial tcp 10.0.0.5:6379: connect: connection refused",
        pod="api-0", namespace="prod",
    )
    types = [s["type"] for s in sigs]
    assert "connection_refused" in types


def test_pod_logs_empty_returns_no_signals():
    from skills.k8s_get_pod_logs import _scan_logs

    assert _scan_logs("", pod="api-0", namespace="prod") == []
    assert _scan_logs("everything is fine\n", pod="api-0", namespace="prod") == []


# ---------- k8s_list_deployments ---------- #

def test_list_deployments_critical_when_zero_ready():
    from skills.k8s_list_deployments import _extract_signals

    sigs = _extract_signals(
        [{"name": "api", "namespace": "prod", "desired": 3, "ready": 0}],
        namespace="prod",
    )
    assert len(sigs) == 1
    assert sigs[0]["type"] == "replicas_insufficient"
    assert sigs[0]["severity"] == "critical"
    assert sigs[0]["next_skill"] == "k8s_list_pods"
    assert sigs[0]["next_args"] == {"namespace": "prod"}


def test_list_deployments_warning_when_partial_ready():
    from skills.k8s_list_deployments import _extract_signals

    sigs = _extract_signals(
        [{"name": "api", "namespace": "prod", "desired": 3, "ready": 1}],
        namespace="prod",
    )
    assert sigs[0]["severity"] == "warning"


def test_list_deployments_clean_when_fully_ready():
    from skills.k8s_list_deployments import _extract_signals

    assert _extract_signals(
        [{"name": "api", "namespace": "prod", "desired": 3, "ready": 3}],
        namespace="prod",
    ) == []


def test_list_deployments_tolerates_alternate_field_casings():
    """不同 k8s_client 实现字段名不一致；signal 提取要兼容。"""
    from skills.k8s_list_deployments import _extract_signals

    sigs = _extract_signals(
        [{"Name": "api", "Namespace": "prod", "Replicas": 3, "ReadyReplicas": 0}],
        namespace=None,
    )
    assert len(sigs) == 1
    assert sigs[0]["context"]["deployment"] == "api"

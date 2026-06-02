"""跨 skill 的 signal 行为回归测试。

跑独立 scanner 函数（不打 Zabbix/Swarm/K8s），保证以后改 prompt / 改 client
不要回归这些守则：

  1. host_name 缺失时不挂 ``next_skill``（免得给模型空 host_query）
  2. metric_query_peak warning 区间也有 next_skill（之前只 critical 有）
  3. swarm_failed_tasks scanner: service_name 缺失时不挂 ``next_skill``
  4. k8s logs scanner 命中 OOM / connection refused 关键词时发 signal
  5. k8s pod_list scanner 在 CrashLoop / ImagePull 时 pivot 到 kube_query(describe)

skill 体系重构后说明
====================
原来扫描函数内嵌在各个 skill 模块里（``skills/k8s_list_pods._extract_pod_signals``
等）。重构后扫描器搬到 ``ops_platform/scanners/*``——本测试直接调那里。
"""

from __future__ import annotations


# ---------- metric_query_peak (skill 内置 scanner，保留) ---------- #

def test_peak_warning_branch_attaches_next_skill():
    from skills.metric_query import _extract_signals

    result = {
        "metric": "cpu.utilization",
        "peak": {"value": 85.0, "time": "2026-04-02T12:34:00+00:00"},
        "host": {"host_name": "web-01"},
        "lookback_seconds": 3600,
    }
    sigs = _extract_signals(result)
    assert len(sigs) == 1
    assert sigs[0]["severity"] == "warning"
    assert sigs[0]["next_skill"] == "zabbix_get_host_overview"
    assert sigs[0]["next_args"]["host_query"] == "web-01"


def test_peak_skips_next_skill_when_host_name_missing():
    """host_name 为空时不递空 host_query 给下游 skill。"""
    from skills.metric_query import _extract_signals

    result = {
        "metric": "cpu.utilization",
        "peak": {"value": 95.0, "time": "2026-04-02T12:34:00+00:00"},
        "host": {"host_name": ""},
        "lookback_seconds": 3600,
    }
    sigs = _extract_signals(result)
    assert len(sigs) == 1
    assert sigs[0]["severity"] == "critical"
    assert sigs[0].get("next_skill") is None
    assert sigs[0].get("next_args") is None


def test_peak_memory_critical_attaches_next_skill():
    from skills.metric_query import _extract_signals

    result = {
        "metric": "memory.utilization",
        "peak": {"value": 92.0, "time": "2026-04-02T12:34:00+00:00"},
        "host": {"host_name": "db-01"},
        "lookback_seconds": 86400,
    }
    sigs = _extract_signals(result)
    assert sigs[0]["type"] == "high_memory"
    assert sigs[0]["next_skill"] == "zabbix_get_host_overview"


# ---------- scanners.swarm_failed_tasks ---------- #

def test_failed_tasks_skips_next_skill_when_service_name_missing():
    """ServiceName 缺失 + Name 不带 . 分割不出 service —— 不应递空 next_args。"""
    from ops_platform.scanners.swarm_failed_tasks import scan

    sigs = scan([
        {"Name": "", "ServiceName": "",
         "Error": "pull access denied for ghcr.io/foo:bar",
         "CurrentState": "rejected", "Node": "node-1"},
    ])
    assert len(sigs) == 1
    assert sigs[0]["type"] == "image_pull_fail"
    assert sigs[0].get("next_skill") is None


def test_failed_tasks_oom_emits_critical_with_node_pivot():
    from ops_platform.scanners.swarm_failed_tasks import scan

    sigs = scan([
        {"Name": "web.1.abc", "ServiceName": "web", "Node": "node-1",
         "Error": "task: non-zero exit (137)", "CurrentState": "failed 30s ago"},
    ])
    assert len(sigs) == 1
    assert sigs[0]["type"] == "oom_kill"
    assert sigs[0]["severity"] == "critical"
    assert sigs[0]["next_skill"] == "zabbix_get_host_overview"
    assert sigs[0]["next_args"] == {"host_query": "node-1"}


# ---------- scanners.k8s_logs ---------- #

def test_pod_logs_detects_oom_and_pivots_to_host_overview():
    from ops_platform.scanners.k8s_logs import scan

    sigs = scan(
        "2026-04-02 12:34:56 ERROR Out of memory: Killed process 1234 (java)",
        pod="api-0", namespace="prod",
    )
    types = [s["type"] for s in sigs]
    assert "oom_kill" in types
    oom = next(s for s in sigs if s["type"] == "oom_kill")
    assert oom["next_skill"] == "zabbix_get_host_overview"


def test_pod_logs_detects_connection_refused():
    from ops_platform.scanners.k8s_logs import scan

    sigs = scan(
        "dial tcp 10.0.0.5:6379: connect: connection refused",
        pod="api-0", namespace="prod",
    )
    types = [s["type"] for s in sigs]
    assert "connection_refused" in types


def test_pod_logs_empty_returns_no_signals():
    from ops_platform.scanners.k8s_logs import scan

    assert scan("", pod="api-0", namespace="prod") == []
    assert scan("everything is fine\n", pod="api-0", namespace="prod") == []


# ---------- scanners.k8s_pod_list ---------- #

def test_pod_list_crashloop_pivots_to_kube_query_describe():
    from ops_platform.scanners.k8s_pod_list import scan

    sigs = scan([
        {"name": "api-0", "namespace": "prod", "restarts": 7,
         "waiting_reasons": ["CrashLoopBackOff"]},
    ], namespace="prod")
    assert len(sigs) == 1
    assert sigs[0]["type"] == "crash_loop_backoff"
    assert sigs[0]["next_skill"] == "kube_query"
    assert sigs[0]["next_args"]["verb"] == "describe"
    assert sigs[0]["next_args"]["resource"] == "pod"
    assert sigs[0]["next_args"]["name"] == "api-0"


def test_pod_list_imagepull_pivots_to_kube_query_describe():
    from ops_platform.scanners.k8s_pod_list import scan

    sigs = scan([
        {"name": "api-0", "namespace": "prod", "restarts": 0,
         "waiting_reasons": ["ImagePullBackOff"]},
    ], namespace="prod")
    assert len(sigs) == 1
    assert sigs[0]["type"] == "image_pull_fail"
    assert sigs[0]["next_skill"] == "kube_query"
    assert sigs[0]["next_args"]["verb"] == "describe"


# ---------- scanners.k8s_describe ---------- #

def test_describe_oomkilled_pivots_to_zabbix_overview():
    from ops_platform.scanners.k8s_describe import scan

    txt = """
Name:         api-0
Namespace:    prod
Node:         w1.example.com/10.0.0.1
Events:
  Type     Reason     Age   Message
  Warning  BackOff    1m    Back-off restarting failed container
  Normal   Killing    2m    Container died with last reason: OOMKilled
""".strip()
    sigs = scan(txt, name="api-0", namespace="prod")
    types = [s["type"] for s in sigs]
    assert "oom_kill" in types
    oom = next(s for s in sigs if s["type"] == "oom_kill")
    assert oom["next_skill"] == "zabbix_get_host_overview"
    assert oom["context"]["node"] == "w1.example.com"

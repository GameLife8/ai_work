"""k8s_cluster_overview skill 单测（不打真实 k8s，mock client）。"""

from __future__ import annotations

from unittest.mock import MagicMock

from skills.k8s_cluster_overview import (
    _parse_deployments,
    _parse_node,
    _parse_pods,
)


def test_parse_node_extracts_fields():
    item = {
        "metadata": {"name": "node-1", "labels": {"node-role.kubernetes.io/master": ""}},
        "status": {
            "conditions": [{"type": "Ready", "status": "True"}],
            "addresses": [{"type": "InternalIP", "address": "192.168.9.66"},
                          {"type": "Hostname", "address": "node-1"}],
            "nodeInfo": {"kubeletVersion": "v1.28.2", "osImage": "Ubuntu 22.04"},
        },
    }
    out = _parse_node(item)
    assert out["name"] == "node-1"
    assert out["role"] == "master"
    assert out["ready"] == "Ready"
    assert out["internal_ip"] == "192.168.9.66"
    assert out["kubelet_version"] == "v1.28.2"


def test_parse_node_notready():
    item = {"metadata": {"name": "n2", "labels": {}},
            "status": {"conditions": [{"type": "Ready", "status": "False"}],
                       "addresses": [], "nodeInfo": {}}}
    out = _parse_node(item)
    assert out["ready"] == "NotReady"
    assert out["role"] == "worker"   # 无 role label → worker


def test_parse_pods_flags_crashloop():
    items = [
        {"metadata": {"namespace": "ns1", "name": "ok-pod"},
         "spec": {"nodeName": "n1"},
         "status": {"phase": "Running", "containerStatuses": [{"restartCount": 0}]}},
        {"metadata": {"namespace": "ns1", "name": "crash-pod"},
         "spec": {"nodeName": "n2"},
         "status": {"phase": "Running", "containerStatuses": [
             {"restartCount": 12, "state": {"waiting": {"reason": "CrashLoopBackOff"}}}]}},
        {"metadata": {"namespace": "ns2", "name": "pending-pod"},
         "spec": {}, "status": {"phase": "Pending", "containerStatuses": []}},
    ]
    out = _parse_pods(items, restart_threshold=5)
    assert out["total"] == 3
    assert out["abnormal_count"] == 2   # crash + pending
    names = {p["name"] for p in out["abnormal"]}
    assert names == {"crash-pod", "pending-pod"}
    # crash-pod 有 bad reason → severity 高 → 排在前
    assert out["abnormal"][0]["name"] == "crash-pod"


def test_parse_pods_caps_abnormal_list():
    """几百个异常 pod → 明细截断到上限,但 count 准确。"""
    items = [
        {"metadata": {"namespace": "ns", "name": f"evicted-{i}"},
         "spec": {}, "status": {"phase": "Failed", "containerStatuses": []}}
        for i in range(100)
    ]
    out = _parse_pods(items, restart_threshold=5)
    assert out["abnormal_count"] == 100      # 总数准确
    assert len(out["abnormal"]) == 40        # 明细截断
    assert out["truncated"] is True


def test_parse_pods_running_succeeded_not_abnormal():
    items = [
        {"metadata": {"namespace": "ns", "name": "run"},
         "spec": {}, "status": {"phase": "Running", "containerStatuses": [{"restartCount": 1}]}},
        {"metadata": {"namespace": "ns", "name": "job-done"},
         "spec": {}, "status": {"phase": "Succeeded", "containerStatuses": []}},
    ]
    out = _parse_pods(items, restart_threshold=5)
    assert out["abnormal_count"] == 0


def test_parse_deployments_flags_replica_mismatch():
    items = [
        {"metadata": {"namespace": "ns", "name": "ok"},
         "spec": {"replicas": 3}, "status": {"readyReplicas": 3, "availableReplicas": 3}},
        {"metadata": {"namespace": "ns", "name": "degraded"},
         "spec": {"replicas": 3}, "status": {"readyReplicas": 1, "availableReplicas": 1}},
        {"metadata": {"namespace": "ns", "name": "down"},
         "spec": {"replicas": 2}, "status": {}},
    ]
    out = _parse_deployments(items)
    assert out["total"] == 3
    assert out["abnormal_count"] == 2
    names = {d["name"] for d in out["abnormal"]}
    assert names == {"degraded", "down"}


def test_manifest_shape():
    from skills.k8s_cluster_overview import MANIFEST
    assert MANIFEST["code"] == "k8s_cluster_overview"
    assert MANIFEST["required_connection_type"] == "k8s"
    assert MANIFEST["read_only"] is True

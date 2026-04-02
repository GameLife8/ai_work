from __future__ import annotations

from services.ai_client import AIClient


def test_plan_prompt_mentions_disk_specific_requirements():
    client = AIClient(provider="volcengine_coding", base_url="http://example.com", api_key="", model="", timeout_seconds=10, use_stub=True)

    prompt = client._build_plan_prompt(
        {
            "alert_type": "disk",
            "status": "problem",
            "resource_scope": {"mount_point": "/mnt/data01"},
            "signal": {"threshold_percent": 90},
        }
    )

    assert "disk_summary" in prompt
    assert "Disk alerts need disk_summary" in prompt


def test_judge_prompt_mentions_disk_and_cpu_guidelines():
    client = AIClient(provider="volcengine_coding", base_url="http://example.com", api_key="", model="", timeout_seconds=10, use_stub=True)

    prompt = client._build_judge_prompt(
        {
            "alert_type": "disk",
            "status": "problem",
            "tags": {"env": "prod"},
            "resource_scope": {"mount_point": "/mnt/data01"},
            "signal": {"threshold_percent": 90},
        },
        {"disk_summary": {"used_percent": 96.0}},
    )

    assert "Disk alerts must consider used_percent" in prompt
    assert "CPU alerts must consider cpu_avg" in prompt


def test_stub_judge_notifies_for_critical_disk_exhaustion():
    decision = AIClient._stub_judge(
        {
            "alert_type": "disk",
            "status": "problem",
            "tags": {"env": "prod"},
        },
        {"disk_summary": {"used_percent": 98.0, "free_gb": 5.0, "growth_gb_24h": 30.0}},
    )

    assert decision["decision"] == "notify"
    assert decision["priority"] == "P1"


def test_stub_plan_requests_memory_summary_for_memory_alert():
    plan = AIClient._stub_plan({"alert_type": "memory", "tags": {}})

    assert "memory_summary" in plan["needs"]


def test_stub_judge_notifies_for_host_down():
    decision = AIClient._stub_judge(
        {"alert_type": "host_down", "status": "problem", "tags": {"env": "prod"}},
        {"availability_summary": {"ping_status": "down", "agent_status": "down"}},
    )

    assert decision["decision"] == "notify"
    assert decision["priority"] == "P1"


def test_extract_json_object_supports_wrapped_text():
    parsed = AIClient._extract_json_object('result is {"needs":["disk_summary"]}')

    assert parsed["needs"] == ["disk_summary"]

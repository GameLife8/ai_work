"""意图 → tool 白名单的设计契约。

设计原则
========
1. **审批是平台正交关注点，不是意图过滤维度**——
   ``host_run_command`` / ``host_run_command_async`` 走 admin 审批的逃生口
   该在哪个意图就在哪个意图。意图层若为了"审批麻烦"剔除这些 skill，模型
   遇到 ``host_query`` binary 白名单覆盖不到的命令（``docker ps`` /
   ``virsh list`` 等）就只能写"请联系 admin"的死信。
2. 每个**取证类**意图（list_state / config_view / monitor / write_action /
   async_task）都应该有 ``host_run_command`` 或 ``host_run_command_async``
   至少其中一个——保证模型遇到白名单不覆盖的场景能转到"逃生口 + admin 审批"
   路径。
3. **每个意图**都应该带 ``platform_get_runbooks`` 入口（哪怕 chat_intro），
   让模型随时能引导到剧本。
4. **写操作 / 异步 / 监控**意图天然带 read query——写之前要先看状态。
5. ``diagnose`` / ``None``（unknown）走 full set，不在表里出现。
"""

from __future__ import annotations

import pytest

from ops_agent.agent import _INTENT_TOOLS, _filter_tools_by_intent


_ALL_READ_QUERIES = {"kube_query", "swarm_query", "host_query"}
_ESCAPE_HATCHES = {"host_run_command", "host_run_command_async"}

# 取证类意图——必须能调"逃生口"
_FORENSIC_INTENTS = {"config_view", "list_state", "monitor", "write_action", "async_task"}


def _whitelist(intent: str) -> set[str]:
    items = _INTENT_TOOLS.get(intent) or []
    return set(items)


# ---------- 核心契约 ---------- #


@pytest.mark.parametrize("intent", sorted(_FORENSIC_INTENTS))
def test_forensic_intent_has_escape_hatch(intent: str) -> None:
    """取证类意图必须保留至少一个"逃生口"——确保 host_query 白名单
    覆盖不到的命令（``docker ps`` 是头号案例）能走 admin 审批通道。
    """
    wl = _whitelist(intent)
    assert wl & _ESCAPE_HATCHES, (
        f"意图 {intent!r} 应包含 host_run_command 或 host_run_command_async"
        f"中的至少一个；当前白名单：{sorted(wl)}"
    )


def test_list_state_includes_docker_ps_path() -> None:
    """回归用例：用户问'192.168.2.60 上跑了什么容器'被分到 list_state，
    必须能调 host_run_command(docker ps)——否则模型只能放弃取证。
    """
    wl = _whitelist("list_state")
    assert "host_run_command" in wl, (
        "list_state 必须包含 host_run_command："
        "用户问'跑了哪些容器'时这是唯一能调 docker ps 的入口"
    )


def test_config_view_can_inspect_arbitrary_resource() -> None:
    """config_view 要能 ``docker inspect`` / ``cat`` 大配置 / ``virsh dumpxml``
    —— host_query 白名单不放 ``docker`` 等，必须有逃生口兜底。
    """
    wl = _whitelist("config_view")
    assert "host_run_command" in wl


def test_monitor_supports_long_sampling() -> None:
    """监控类意图要能跑 ``sar -A 1 60`` / ``iostat -x 1 30`` 这种长采样命令，
    必须保留 host_run_command_async。
    """
    wl = _whitelist("monitor")
    assert "host_run_command_async" in wl


@pytest.mark.parametrize("intent", sorted(_FORENSIC_INTENTS))
def test_forensic_intent_has_read_queries(intent: str) -> None:
    """取证类意图必须能读 —— 至少有一个通用查询 skill。"""
    wl = _whitelist(intent)
    assert wl & _ALL_READ_QUERIES, (
        f"意图 {intent!r} 白名单缺少 kube_query/swarm_query/host_query；"
        f"当前白名单：{sorted(wl)}"
    )


def test_write_action_has_full_write_set() -> None:
    """write_action 必须覆盖全部写 skill——避免模型不知道用 force_update / rollback。"""
    wl = _whitelist("write_action")
    required = {
        "k8s_scale_deployment", "k8s_restart_deployment", "k8s_rollout_undo",
        "swarm_scale_service", "swarm_update_service_image",
        "swarm_rollback_service", "swarm_remove_service", "swarm_force_update_service",
    }
    missing = required - wl
    assert not missing, f"write_action 缺少写 skill：{sorted(missing)}"


@pytest.mark.parametrize("intent", ["knowledge", "chat_intro"])
def test_routing_intents_have_runbook_entry(intent: str) -> None:
    """纯路由/闲聊意图至少要能引导用户到 runbook。"""
    wl = _whitelist(intent)
    assert "platform_get_runbooks" in wl


def test_diagnose_uses_full_set() -> None:
    """诊断 / unknown 必须走全集 —— 用空 list / None 标记。"""
    assert not _INTENT_TOOLS.get("diagnose"), "diagnose 应走 full set（空 list）"
    assert not _INTENT_TOOLS.get(None), "unknown intent 应走 full set"


# ---------- _filter_tools_by_intent 行为验证 ---------- #


def _fake_full_tools() -> list[dict]:
    """模拟 registry.openai_tools() 的返回——admin 用户全集。"""
    codes = [
        # 通用查询
        "kube_query", "swarm_query", "host_query",
        "swarm_cluster_overview",
        # 写 skill
        "k8s_scale_deployment", "k8s_restart_deployment", "k8s_rollout_undo",
        "swarm_scale_service", "swarm_update_service_image",
        "swarm_rollback_service", "swarm_remove_service", "swarm_force_update_service",
        # 逃生口（admin）
        "host_run_command", "host_run_command_async",
        # 异步
        "host_check_task", "host_list_tasks", "host_list_nodes",
        # 监控
        "zabbix_get_host_overview", "zabbix_get_host_storage_overview",
        "metric_query_peak", "metric_query_window_around",
        # 杂项
        "k8s_get_pod_logs", "host_capture_packets",
        "host_inspect_container_netns", "host_kernel_events",
        "alerts_analyze_payload",
        # 引导
        "platform_get_runbooks", "platform_run_runbook",
    ]
    return [{"type": "function", "function": {"name": c}} for c in codes]


def test_filter_list_state_keeps_escape_hatch_when_admin() -> None:
    """admin 用户走 list_state 时，host_run_command 必须仍在过滤后的工具集里。"""
    full = _fake_full_tools()
    filtered, was_filtered = _filter_tools_by_intent(full, "list_state")
    assert was_filtered, "list_state 应触发过滤"
    names = {t["function"]["name"] for t in filtered}
    assert "host_run_command" in names, (
        "list_state 过滤后必须保留 host_run_command —— "
        "这是回归测试覆盖的 docker ps 取证路径"
    )


def test_filter_drops_unrelated_write_for_list_state() -> None:
    """list_state 不应该看到 swarm_scale_service 这种纯写 skill。"""
    full = _fake_full_tools()
    filtered, _ = _filter_tools_by_intent(full, "list_state")
    names = {t["function"]["name"] for t in filtered}
    assert "swarm_scale_service" not in names
    assert "k8s_scale_deployment" not in names


def test_filter_diagnose_passes_through() -> None:
    """diagnose 走 full set——不过滤。"""
    full = _fake_full_tools()
    filtered, was_filtered = _filter_tools_by_intent(full, "diagnose")
    assert not was_filtered
    assert len(filtered) == len(full)


def test_filter_unknown_intent_passes_through() -> None:
    """未识别意图走 full set——不过滤。"""
    full = _fake_full_tools()
    filtered, was_filtered = _filter_tools_by_intent(full, None)
    assert not was_filtered
    assert len(filtered) == len(full)


def test_filter_fallback_to_full_when_intent_yields_too_few() -> None:
    """安全网：白名单命中 < 2 个 skill 时降级走 full set。"""
    # 构造一个只有 platform_get_runbooks 的 full tools，list_state 白名单命中只剩 1 个
    minimal = [{"type": "function", "function": {"name": "platform_get_runbooks"}}]
    filtered, was_filtered = _filter_tools_by_intent(minimal, "list_state")
    assert not was_filtered
    assert filtered is minimal

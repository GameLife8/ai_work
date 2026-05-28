"""Runbook 设计层面的契约测试。

不是单元行为测试,是**设计契约的钉子**——把"每个 runbook 应该长什么样"
钉死,避免未来重构时悄悄退化。

覆盖矩阵
========

A. 通用契约(所有 runbook 共有)
   A1. 所有 runbook 引用的 skill 都已注册
   A2. 所有 runbook 不引用 write skill(needs_confirmation 不能自动跑)
   A3. 所有 runbook 都能通过 validator(无环 / 引用合法 / on_error 合法)

B. swarm_service_not_starting 设计
   B1. OOM 分支必须经过 ``oom_node_lookup``(swarm hostname → IP)再到 zabbix
   B2. no_space 分支必须经过 ``disk_node_lookup``(同上)
   B3. host_overview / host_storage 的 ``host_query`` 必须引用 lookup 节点的 ``Status.Addr``
       —— 不能再像 legacy 那样直接传 ``$signals.X.context.node``(那是 hostname,zabbix 查不到)
   B4. **没有** ``service_detail`` 这个重复 inspect 节点

C. k8s_pod_crashloop 设计
   C1. ``logs_now`` 的 fallback 链优先取 scanner 识别的异常 pod
       (crash_loop_backoff / image_pull_fail / pod_evicted),最后才是 items[0]

D. 巡检 runbook
   D1. cluster_health_audit_swarm 存在且引用 swarm_cluster_overview
"""

from __future__ import annotations

import pytest


def _load_all_runbooks():
    """import 时即 load DEFAULTS。"""
    from ops_platform.runbook_engine import load_runbook_from_dict
    from ops_platform.runbook_seeds import DEFAULT_RUNBOOKS
    return [load_runbook_from_dict(r) for r in DEFAULT_RUNBOOKS]


# ============ A. 通用契约 ============


def test_A_all_runbooks_validate_clean() -> None:
    """所有 runbook 在当前 skill 注册表下,validator 必须零错误。

    用真实的 skill registry(import 平台 skills/ 目录扫一遍),不是 mock 的全集合。
    跑这个测试能在 CI 阶段挡下"runbook 引用了已删 skill"的事故。
    """
    from ops_platform.loader import load_skills_from_package
    from ops_platform.registry import SkillRegistry
    from ops_platform.runbook_engine import validate_runbook

    reg = SkillRegistry()
    load_skills_from_package("skills", reg)
    all_skills = reg.list(only_enabled=False)
    known = {s.code for s in all_skills}
    write = {s.code for s in all_skills if not s.read_only}

    fails: dict[str, list[str]] = {}
    for rb in _load_all_runbooks():
        errs = validate_runbook(rb, known_skills=known, write_skills=write)
        if errs:
            fails[rb.key] = errs

    assert not fails, f"以下 runbook 校验失败(请到 runbook_seeds.py 修):\n{fails}"


def test_A_no_runbook_references_write_skill() -> None:
    """**安全契约**:runbook 不能引用任何 write skill。

    写操作必须走 needs_confirmation 流程,不能在 runbook 里被平台自动执行——
    否则会绕过用户审批,出大事。validator 也有这层防御,这条测试是 redundant
    防御,挡 validator 哪天被人改坏。
    """
    from ops_platform.loader import load_skills_from_package
    from ops_platform.registry import SkillRegistry

    reg = SkillRegistry()
    load_skills_from_package("skills", reg)
    write_skills = {s.code for s in reg.list(only_enabled=False) if not s.read_only}

    violations: dict[str, list[str]] = {}
    for rb in _load_all_runbooks():
        for nid, n in rb.nodes.items():
            if n.skill in write_skills:
                violations.setdefault(rb.key, []).append(
                    f"node {nid} 引用了写 skill {n.skill}"
                )
    assert not violations, f"safety violation:{violations}"


# ============ B. swarm_service_not_starting 设计契约 ============


def _find_rb(key: str):
    for rb in _load_all_runbooks():
        if rb.key == key:
            return rb
    pytest.fail(f"runbook {key!r} 未在 DEFAULTS 中")


def test_B_swarm_oom_path_goes_through_node_lookup() -> None:
    """OOM pivot 必须先经过 swarm node inspect 拿 IP,而不是直接传 hostname。"""
    rb = _find_rb("swarm_service_not_starting")

    # failed_tasks 节点的 oom 边必须指向 oom_node_lookup
    failed_tasks = rb.nodes["failed_tasks"]
    oom_edges = [e for e in failed_tasks.edges
                 if e.when and e.when.signal_type == "oom_kill"]
    assert oom_edges, "failed_tasks 缺 oom_kill 边"
    assert oom_edges[0].target == "oom_node_lookup", (
        f"OOM 边应该先去 oom_node_lookup 拿 IP,实际去了 {oom_edges[0].target}"
    )

    # oom_node_lookup 节点必须用 swarm_query inspect + name=$signals.oom_kill.context.node
    lookup = rb.nodes["oom_node_lookup"]
    assert lookup.skill == "swarm_query"
    assert lookup.args.get("category") == "node"
    assert lookup.args.get("verb") == "inspect"
    assert lookup.args.get("name") == "$signals.oom_kill.context.node"

    # 下游 host_overview 必须从 lookup 取 Status.Addr,而不是再次用 signal.context.node
    host = rb.nodes["host_overview"]
    assert host.skill == "zabbix_get_host_overview"
    host_query = host.args.get("host_query") or ""
    assert "$nodes.oom_node_lookup.parsed[0].Status.Addr" in host_query, (
        f"host_overview.host_query 必须从 oom_node_lookup 取 IP,实际:{host_query!r}"
    )
    assert "$signals.oom_kill.context.node" not in host_query, (
        "host_overview.host_query 不应再直接用 signal.context.node(那是 hostname,"
        "Zabbix 通常按 IP 注册查不到)"
    )


def test_B_swarm_disk_path_goes_through_node_lookup() -> None:
    """no_space pivot 同上:必须先经过 disk_node_lookup 拿 IP。"""
    rb = _find_rb("swarm_service_not_starting")

    failed_tasks = rb.nodes["failed_tasks"]
    disk_edges = [e for e in failed_tasks.edges
                  if e.when and e.when.signal_type == "no_space_left"]
    assert disk_edges, "failed_tasks 缺 no_space_left 边"
    assert disk_edges[0].target == "disk_node_lookup"

    lookup = rb.nodes["disk_node_lookup"]
    assert lookup.skill == "swarm_query"
    assert lookup.args.get("name") == "$signals.no_space_left.context.node"

    host_storage = rb.nodes["host_storage"]
    assert host_storage.skill == "zabbix_get_host_storage_overview"
    assert "$nodes.disk_node_lookup.parsed[0].Status.Addr" in (host_storage.args.get("host_query") or "")


def test_B_swarm_no_duplicate_service_detail_node() -> None:
    """重构前的 service_detail 节点跟 status 节点完全等价(都是 inspect 同一个 service),
    重复浪费一轮 docker 调用。重构后必须去掉。"""
    rb = _find_rb("swarm_service_not_starting")
    assert "service_detail" not in rb.nodes, (
        "service_detail 节点跟 status 节点完全重复(都是 swarm_query inspect 同一个 service_name),"
        "应该去掉。image_pull_fail 信号让模型从 status 节点的 raw 数据里看完整 image 字段就够。"
    )


# ============ C. k8s_pod_crashloop 设计契约 ============


def test_C_k8s_logs_now_fallback_prefers_signal_pods_over_items0() -> None:
    """logs_now 的 name fallback 链必须优先取 scanner 识别的异常 pod,
    最后才退到 ``parsed.items[0]``——否则在 list 没异常时会拉到正常 pod 的日志。

    fallback 链至少要覆盖 crash_loop_backoff / image_pull_fail / pod_evicted 三种信号。
    """
    rb = _find_rb("k8s_pod_crashloop")
    logs_now = rb.nodes.get("logs_now")
    assert logs_now is not None
    name_expr = logs_now.args.get("name") or ""

    # 三种异常 pod signal 全部在 fallback 链里
    assert "$signals.crash_loop_backoff.next_args.name" in name_expr
    assert "$signals.image_pull_fail.next_args.name" in name_expr
    assert "$signals.pod_evicted.next_args.name" in name_expr

    # items[0] 必须在最后(链尾兜底,只在所有 signal 都没命中时才走)
    parts = [p.strip() for p in name_expr.split("||")]
    assert parts[-1].endswith("items[0].metadata.name"), (
        f"items[0] 应该在 fallback 链末尾,实际链:\n{parts}"
    )


# ============ D. 巡检 runbook 契约 ============


def test_D_swarm_cluster_audit_uses_aggregation_skill() -> None:
    rb = _find_rb("cluster_health_audit_swarm")
    assert "overview" in rb.nodes
    assert rb.nodes["overview"].skill == "swarm_cluster_overview"
    # 巡检场景只读、不需用户必填 inputs
    assert rb.inputs == []

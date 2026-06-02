"""runbook 预路由的集群类型一致性 guard 回归测试。

复现线上 bug:用户说"codewave(k8s) 集群巡检",关键词"集群巡检"命中 swarm 专用
runbook(cluster_health_audit_swarm),swarm_cluster_overview 需要 swarm 连接 →
codewave 没有 → fallback 到默认 swarm(192.168.2.122) → 巡检了**错误的集群**。

修复:_runbook_cluster_type_matches —— 用户点名了某 cluster type 但 runbook 需要
另一种且无交集时,跳过预路由,走正常 tool loop。
"""

from __future__ import annotations

from unittest.mock import MagicMock

from ops_agent.agent import UnifiedOpsAgent


def _agent_with_conns(conns: list[dict], skill_required: dict[str, str]) -> UnifiedOpsAgent:
    """造一个 agent:connection_manager.list 返回 conns;registry.get(skill).required_connection_type
    取自 skill_required。"""
    rt = MagicMock()
    rt.connection_manager.list.return_value = conns
    rt.model_manager.get_client.return_value = MagicMock()

    def fake_get(code):
        spec = MagicMock()
        spec.required_connection_type = skill_required.get(code)
        return spec
    rt.skill_registry.get.side_effect = fake_get

    agent = UnifiedOpsAgent.__new__(UnifiedOpsAgent)
    agent.runtime = rt
    agent.registry = rt.skill_registry
    agent.invoker = rt.skill_invoker
    agent.model = MagicMock()
    return agent


def _swarm_audit_rb():
    """模拟 cluster_health_audit_swarm:单节点 skill=swarm_cluster_overview。"""
    rb = MagicMock()
    node = MagicMock()
    node.skill = "swarm_cluster_overview"
    rb.nodes = {"overview": node}
    rb.key = "cluster_health_audit_swarm"
    return rb


_CONNS = [
    {"type_code": "swarm", "id": "sws-swarm-id", "name": "sws-swarm", "enabled": True,
     "tags": ["sws", "sws-swarm", "192.168.2"]},
    {"type_code": "k8s", "id": "codewave-id", "name": "codewave", "enabled": True,
     "tags": ["codewave", "lowcode", "192.168.9"]},
    {"type_code": "host_agent", "id": "codewave-ha", "name": "codewave-k8s", "enabled": True,
     "tags": ["codewave", "lowcode"]},
]
_SKILL_REQ = {"swarm_cluster_overview": "swarm"}


def _k8s_audit_rb():
    """模拟 cluster_health_audit_k8s:单节点 skill=k8s_cluster_overview。"""
    rb = MagicMock()
    node = MagicMock(); node.skill = "k8s_cluster_overview"
    rb.nodes = {"overview": node}
    rb.key = "cluster_health_audit_k8s"
    return rb


_SKILL_REQ_BOTH = {"swarm_cluster_overview": "swarm", "k8s_cluster_overview": "k8s"}


def test_required_types_extracted():
    agent = _agent_with_conns(_CONNS, _SKILL_REQ)
    assert agent._runbook_required_conn_types(_swarm_audit_rb()) == {"swarm"}


def test_codewave_picks_k8s_runbook_not_swarm():
    """**核心 bug 修复**:codewave(k8s) 集群巡检,候选 [swarm_audit, k8s_audit] →
    应挑 k8s_audit(swarm 的被跳过)。"""
    agent = _agent_with_conns(_CONNS, _SKILL_REQ_BOTH)
    # 候选顺序故意 swarm 在前,验证不是简单取第一个
    chosen = agent._pick_cluster_compatible_runbook(
        [_swarm_audit_rb(), _k8s_audit_rb()], "codewave 对这个集群进行集群巡检")
    assert chosen is not None and chosen.key == "cluster_health_audit_k8s"


def test_sws_swarm_picks_swarm_runbook():
    """sws-swarm 集群巡检,候选 [swarm, k8s] → 挑 swarm_audit。"""
    agent = _agent_with_conns(_CONNS, _SKILL_REQ_BOTH)
    chosen = agent._pick_cluster_compatible_runbook(
        [_k8s_audit_rb(), _swarm_audit_rb()], "sws-swarm 集群巡检")
    assert chosen is not None and chosen.key == "cluster_health_audit_swarm"


def test_codewave_only_swarm_candidate_returns_none():
    """只有 swarm 候选、用户却点名 k8s → 返回 None(跳过预路由,走 tool loop)。"""
    agent = _agent_with_conns(_CONNS, _SKILL_REQ)
    chosen = agent._pick_cluster_compatible_runbook(
        [_swarm_audit_rb()], "codewave 集群巡检")
    assert chosen is None


def test_generic_query_no_cluster_named_uses_top():
    """没点名集群 → 用排名最高的候选(行为不变)。"""
    agent = _agent_with_conns(_CONNS, _SKILL_REQ_BOTH)
    top = _swarm_audit_rb()
    chosen = agent._pick_cluster_compatible_runbook([top, _k8s_audit_rb()], "做个集群巡检")
    assert chosen is top


def test_runbook_without_cluster_type_always_eligible():
    """不绑定集群类型的 runbook(skill 无 required swarm/k8s)→ 永远可选。"""
    agent = _agent_with_conns(_CONNS, {})   # skill 无 required type
    chosen = agent._pick_cluster_compatible_runbook([_swarm_audit_rb()], "codewave 集群巡检")
    assert chosen is not None   # 无类型约束 → 兼容任何集群

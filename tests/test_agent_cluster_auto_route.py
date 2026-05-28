"""按 connection.tags 子串自动路由集群的测试。

设计契约
========

  1. **基于 tags,不写死任何集群名/IP**——新增集群只要在后台打 tag 就能识别
  2. **每个 type 各自挑 winner**——同一逻辑集群在 swarm/host_agent/zabbix 等
     多 type 上都有 connection 时,每种 type 独立选,共享 tag 让它们同时入选
  3. **命中 tag 数多者胜**——更精确的 tag(bigdata-swarm)优先于宽泛的(bigdata)
  4. **平票 deterministic**——按 connection.name 字典序兜底
  5. **用户显式选择 > 自动路由**——chainlit ChatSettings 选过的不被覆盖
  6. **无任何 tag 命中的 type 不写入 selected**——让平台 default 兜底,不强加
"""

from __future__ import annotations

import json
from typing import Any

from ops_agent.agent import AgentOutcome, UnifiedOpsAgent


# ---------- fakes ---------- #


class _ConnMgr:
    """connection_manager.list() 桩。"""

    def __init__(self, connections: list[dict]) -> None:
        self._conns = connections

    def list(self, type_code: str | None = None) -> list[dict]:
        if type_code:
            return [c for c in self._conns if c.get("type_code") == type_code]
        return list(self._conns)


class _Registry:
    def __init__(self, match=None) -> None:
        self._match = match

    def match_by_query(self, q):
        return self._match


class _Invoker:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, Any]] = []

    def invoke(self, name, args, ctx):
        # 记录调用时 ctx.selected_connections,后面断言
        self.calls.append((name, dict(args), dict(ctx.selected_connections)))
        return {
            "skill": name, "status": "ok", "latency_ms": 1,
            "result": {"final_report": "ok", "node_states": [], "_signals": []},
        }

    @staticmethod
    def serialize_for_model(env):
        return json.dumps(env, default=str)


class _SkillRegistry:
    def openai_tools(self, visibility=None):
        return []


class _Store:
    def list_prompt_segments(self): return []
    def save_chat_message(self, *a, **kw): pass
    def list_chat_messages(self, *a, **kw): return []
    def count_chat_messages(self, *a, **kw): return 0
    def get_latest_chat_summary(self, *a, **kw): return None


class _Model:
    def __init__(self) -> None:
        self.completions = 0
    def create_completion(self, *, messages, tools=None, tool_choice=None):
        self.completions += 1
        return {"content": "", "tool_calls": []}


class _Runtime:
    def __init__(self, connections: list[dict], *, runbook_match=None) -> None:
        self.connection_manager = _ConnMgr(connections)
        self.runbook_registry = _Registry(match=runbook_match)
        self.skill_invoker = _Invoker()
        self.skill_registry = _SkillRegistry()
        self.store = _Store()
        self._model = _Model()

    def get_client(self, _id=None):
        return self._model

    @property
    def model_manager(self):
        return self


# 真实集群数据(精简自生产 DB)—— 钉死真实场景
_CONNECTIONS = [
    {"id": "sws-swarm-id", "type_code": "swarm", "name": "sws-swarm",
     "alias": "SWS Swarm Cluster", "enabled": True, "is_default": True,
     "tags": ["sws", "sws-swarm", "主集群", "192.168.2", "image.chinasws"]},
    {"id": "bigdata-swarm-id", "type_code": "swarm", "name": "bigdata-swarm",
     "alias": "BigData Swarm Cluster", "enabled": True, "is_default": False,
     "tags": ["bigdata", "bigdata-swarm", "169.24.2.193", "bigdata6"]},
    {"id": "sws-agent-id", "type_code": "host_agent", "name": "sws-swarm-agent",
     "alias": "SWS Swarm (HTTP)", "enabled": True,
     "tags": ["sws", "sws-swarm", "主集群", "192.168.2", "image.chinasws"]},
    {"id": "bigdata-agent-id", "type_code": "host_agent", "name": "bigdata-swarm-agent",
     "alias": "BigData Swarm (HTTP)", "enabled": True,
     "tags": ["bigdata", "bigdata-swarm", "169.24.2.193", "bigdata6"]},
    {"id": "codewave-id", "type_code": "k8s", "name": "codewave",
     "alias": "CodeWave K8s", "enabled": True,
     "tags": ["codewave", "lowcode", "192.168.9"]},
    {"id": "zabbix-id", "type_code": "zabbix", "name": "default-zabbix",
     "alias": "默认 Zabbix", "enabled": True, "is_default": True,
     "tags": []},   # zabbix 无 tag —— 始终走 default
]


# ---------- 单测:resolve 纯函数行为 ---------- #


def test_resolve_user_says_bigdata_routes_to_bigdata_swarm() -> None:
    """用户说'bigdata-swarm 巡检' → swarm + host_agent 都要走 bigdata-* 那条。"""
    rt = _Runtime(_CONNECTIONS)
    agent = UnifiedOpsAgent(rt)
    out = agent._resolve_clusters_from_query("bigdata-swarm 巡检一下这个集群")
    assert out["swarm"] == "bigdata-swarm-id"
    assert out["host_agent"] == "bigdata-agent-id"
    # zabbix 无 tag → 不出现在 selected,走 default
    assert "zabbix" not in out
    # k8s 跟 bigdata 无关 → 不应被设
    assert "k8s" not in out


def test_resolve_user_says_sws_routes_to_sws_swarm() -> None:
    rt = _Runtime(_CONNECTIONS)
    agent = UnifiedOpsAgent(rt)
    out = agent._resolve_clusters_from_query("sws-swarm 巡检")
    assert out["swarm"] == "sws-swarm-id"
    assert out["host_agent"] == "sws-agent-id"


def test_resolve_more_specific_tag_beats_broader() -> None:
    """命中 tag 数多者胜:'bigdata-swarm' 文本同时含 'bigdata' + 'bigdata-swarm',
    bigdata-swarm connection 命中 2 个 tag,sws-swarm 命中 0 → bigdata-swarm 胜。
    """
    rt = _Runtime(_CONNECTIONS)
    agent = UnifiedOpsAgent(rt)
    out = agent._resolve_clusters_from_query("帮我看下 bigdata-swarm 状态")
    assert out["swarm"] == "bigdata-swarm-id"


def test_resolve_user_mentions_ip_routes_correctly() -> None:
    """IP 子串也算 tag,用户提到 IP 路由到对应集群。"""
    rt = _Runtime(_CONNECTIONS)
    agent = UnifiedOpsAgent(rt)
    # 192.168.2 段属于 sws-swarm
    assert agent._resolve_clusters_from_query("192.168.2.122 主机怎么样")["swarm"] == "sws-swarm-id"
    # 169.24.2.193 是 bigdata-swarm 的 manager
    assert agent._resolve_clusters_from_query("169.24.2.193 这台机器")["swarm"] == "bigdata-swarm-id"


def test_resolve_unknown_query_returns_empty() -> None:
    """user_message 不提任何已知 tag → 返回空 dict,让平台走 default。"""
    rt = _Runtime(_CONNECTIONS)
    agent = UnifiedOpsAgent(rt)
    assert agent._resolve_clusters_from_query("帮我看下今天天气") == {}
    assert agent._resolve_clusters_from_query("") == {}


def test_resolve_skips_disabled_connection() -> None:
    """disabled connection 即使 tag 命中也不入选。"""
    conns = list(_CONNECTIONS)
    # 把 bigdata-swarm 禁用
    conns = [
        dict(c, enabled=False) if c["id"] == "bigdata-swarm-id" else c
        for c in conns
    ]
    rt = _Runtime(conns)
    agent = UnifiedOpsAgent(rt)
    out = agent._resolve_clusters_from_query("bigdata-swarm 巡检")
    # swarm 候选全没了(只剩 sws-swarm,但它不含 bigdata tag),应该没 swarm key
    assert "swarm" not in out
    # host_agent 还在(bigdata-agent 还 enabled)
    assert out["host_agent"] == "bigdata-agent-id"


def test_resolve_no_code_writes_in_cluster_names() -> None:
    """关键设计契约:resolve 逻辑不能 hardcode 任何集群名/IP/tag。

    钉死实现是基于 connection.tags 的纯数据驱动——以后给一个**全新集群**打 tag,
    代码不改一行,平台立刻能识别。这条测试用一组完全编造的 connection 验证。
    """
    fictitious = [
        {"id": "fake-1", "type_code": "swarm", "name": "fake-cluster-1",
         "alias": "虚构集群一号", "enabled": True,
         "tags": ["abc-xyz", "fake-tag", "10.99.99"]},
        {"id": "fake-2", "type_code": "swarm", "name": "fake-cluster-2",
         "alias": "虚构集群二号", "enabled": True,
         "tags": ["pqr-uvw", "另一个虚构", "10.99.100"]},
    ]
    rt = _Runtime(fictitious)
    agent = UnifiedOpsAgent(rt)
    # 用户用 tag 关键字 → 路由对
    assert agent._resolve_clusters_from_query("abc-xyz 看下")["swarm"] == "fake-1"
    assert agent._resolve_clusters_from_query("查 pqr-uvw")["swarm"] == "fake-2"
    assert agent._resolve_clusters_from_query("另一个虚构 集群")["swarm"] == "fake-2"


# ---------- 集成:ask() 路径里 selected_connections 真的传给 invoker ---------- #


class _StubRB:
    key = "cluster_health_audit_swarm"


def test_ask_passes_resolved_cluster_into_runbook_invocation() -> None:
    """E2E 契约:用户说 bigdata-swarm 巡检 → runbook 跑的时候 ctx.selected_connections
    必须含 bigdata-swarm-id,这样 swarm_cluster_overview 拿到的是 bigdata 的 client。"""
    rt = _Runtime(_CONNECTIONS, runbook_match=_StubRB())
    agent = UnifiedOpsAgent(rt)
    out = agent.ask("bigdata-swarm 巡检一下", user={"role": "admin"}, session_id="s1")

    assert isinstance(out, AgentOutcome)
    assert len(rt.skill_invoker.calls) == 1
    name, args, sel = rt.skill_invoker.calls[0]
    assert name == "platform_run_runbook"
    # ⭐ 关键断言:ctx 里 selected_connections 把 bigdata-swarm 塞进去了
    assert sel.get("swarm") == "bigdata-swarm-id", (
        f"自动路由没把 bigdata-swarm 塞进 ctx.selected_connections,实际:{sel}"
    )
    assert sel.get("host_agent") == "bigdata-agent-id"


def test_ask_does_not_override_user_explicit_selection() -> None:
    """用户在 chainlit ChatSettings 里显式选了 sws-swarm,即使 message 提到 bigdata,
    也不能覆盖用户选择(setdefault 语义)。"""
    rt = _Runtime(_CONNECTIONS, runbook_match=_StubRB())
    agent = UnifiedOpsAgent(rt)
    out = agent.ask(
        "bigdata-swarm 巡检",
        user={"role": "admin"}, session_id="s1",
        selected_connections={"swarm": "sws-swarm-id"},   # 用户偏好
    )
    name, args, sel = rt.skill_invoker.calls[0]
    # 用户偏好被保留,没被自动路由覆盖
    assert sel["swarm"] == "sws-swarm-id"
    # 其它 type(host_agent)用户没选,自动路由仍能补上
    assert sel.get("host_agent") == "bigdata-agent-id"

"""按节点名路由 host_agent —— 根治「消息没集群关键词 → host 命令掉默认集群 → node not found」。

场景:用户在能源(k8s)会话里说「这个集群能访问 X 么」,消息里没有集群 tag。旧逻辑
host_agent 落到默认连接(sws-swarm),swarm 不认识能源节点 nyxt-k8sworker01 → docker
报 node not found。新逻辑:host skill 把 node 透传给连接解析,平台按「哪个 host_agent
连接有这个节点」路由到能源,确定性强、不靠 tag、不靠模型传 connection_id。
"""
from __future__ import annotations

import time

from ops_platform.connection_manager import ConnectionManager
from ops_platform.context import SkillContext


class _FakeStore:
    def __init__(self, conns):
        self._conns = conns

    def list_connections(self, type_code=None):
        return [c for c in self._conns if type_code is None or c["type_code"] == type_code]


class _NodesClient:
    def __init__(self, nodes, *, boom=False):
        self._nodes = nodes
        self._boom = boom
        self.calls = 0

    def list_nodes(self):
        self.calls += 1
        if self._boom:
            raise RuntimeError("cluster down")
        return list(self._nodes)


def _cm(conns, clients):
    cm = ConnectionManager(_FakeStore(conns))
    cm.get_client = lambda cid: clients[cid]   # 跳过真实 driver 构建
    return cm


def _ha(cid, name, enabled=True):
    return {"id": cid, "type_code": "host_agent", "name": name, "enabled": enabled}


# ---------- ConnectionManager.find_host_agent_for_node ----------

def test_routes_node_to_owning_connection():
    cm = _cm(
        [_ha("swarm", "sws-swarm"), _ha("energy", "energy-k8s")],
        {"swarm": _NodesClient(["swarm-1", "swarm-2"]),
         "energy": _NodesClient(["nyxt-k8sworker01", "nyxt-k8smaster01"])},
    )
    assert cm.find_host_agent_for_node("nyxt-k8sworker01") == "energy"
    assert cm.find_host_agent_for_node("swarm-2") == "swarm"
    assert cm.find_host_agent_for_node("does-not-exist") is None
    assert cm.find_host_agent_for_node("") is None


def test_index_is_cached_then_refresh_catches_new_node():
    energy = _NodesClient(["nyxt-k8sworker01"])
    cm = _cm([_ha("energy", "energy-k8s")], {"energy": energy})

    assert cm.find_host_agent_for_node("nyxt-k8sworker01") == "energy"
    after_first = energy.calls
    # 命中缓存:不再列举节点
    assert cm.find_host_agent_for_node("nyxt-k8sworker01") == "energy"
    assert energy.calls == after_first

    # 新节点上线 → miss;refresh 强刷(节流已过)能抓到
    energy._nodes.append("nyxt-k8sworker07")
    cm._node_index_ts = time.monotonic() - 100      # 让 MIN_REFRESH 节流放行
    assert cm.find_host_agent_for_node("nyxt-k8sworker07", refresh=True) == "energy"
    assert energy.calls > after_first


def test_one_cluster_unreachable_does_not_break_others():
    cm = _cm(
        [_ha("down", "broken"), _ha("energy", "energy-k8s")],
        {"down": _NodesClient([], boom=True),
         "energy": _NodesClient(["nyxt-k8sworker01"])},
    )
    # down 集群列举抛错被跳过,energy 仍能路由
    assert cm.find_host_agent_for_node("nyxt-k8sworker01") == "energy"


def test_disabled_connection_skipped():
    cm = _cm(
        [_ha("energy", "energy-k8s", enabled=False)],
        {"energy": _NodesClient(["nyxt-k8sworker01"])},
    )
    assert cm.find_host_agent_for_node("nyxt-k8sworker01") is None


# ---------- SkillContext 解析优先级:override > by-node > selected > default ----------

class _CM2:
    def __init__(self, by_node=None, default_id="default"):
        self._by_node = by_node or {}
        self._default_id = default_id

    def find_host_agent_for_node(self, node, *, refresh=False):
        return self._by_node.get(node)

    def get_client(self, cid):
        return f"client:{cid}"

    def get_default(self, type_code):
        return {"id": self._default_id}


class _RT:
    def __init__(self, cm):
        self.connection_manager = cm


def test_node_beats_selected_and_default():
    cm = _CM2(by_node={"nyxt-k8sworker01": "energy"})
    ctx = SkillContext(runtime=_RT(cm), selected_connections={"host_agent": "swarm"})
    # 这就是 bug 场景:selected/default 都是 swarm,但 node 在能源 → 必须走能源
    assert ctx.connection_for("host_agent", None, node="nyxt-k8sworker01") == "client:energy"


def test_unknown_node_falls_back_to_selected():
    cm = _CM2(by_node={})
    ctx = SkillContext(runtime=_RT(cm), selected_connections={"host_agent": "swarm"})
    assert ctx.connection_for("host_agent", None, node="ghost") == "client:swarm"


def test_explicit_connection_id_wins_over_node():
    cm = _CM2(by_node={"nyxt-k8sworker01": "energy"})
    ctx = SkillContext(runtime=_RT(cm))
    assert ctx.connection_for("host_agent", "explicit", node="nyxt-k8sworker01") == "client:explicit"


def test_no_node_uses_default():
    cm = _CM2(by_node={"x": "energy"})
    ctx = SkillContext(runtime=_RT(cm))
    assert ctx.connection_for("host_agent", None) == "client:default"


def test_resolve_connection_id_routes_by_node():
    cm = _CM2(by_node={"nyxt-k8sworker01": "energy"})
    ctx = SkillContext(runtime=_RT(cm), selected_connections={"host_agent": "swarm"})
    assert ctx.resolve_connection_id("host_agent", None, node="nyxt-k8sworker01") == "energy"


def test_non_host_agent_type_ignores_node():
    cm = _CM2(by_node={"nyxt-k8sworker01": "energy"}, default_id="zbx-default")
    ctx = SkillContext(runtime=_RT(cm))
    # zabbix 等其它 type 不参与按 node 路由
    assert ctx.connection_for("zabbix", None, node="nyxt-k8sworker01") == "client:zbx-default"

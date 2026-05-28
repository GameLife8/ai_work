"""``swarm_cluster_overview`` 聚合 skill + ``cluster_health_audit_swarm``
runbook 的单元测试。

覆盖矩阵
========
A. skill 行为
   A1. 节点列表 + 每节点 IP + Zabbix 反查 → 全 success 路径
   A2. 某节点 ``Status.Addr`` 缺失 → ``zabbix.found=False``
   A3. Zabbix 找不到 IP(ValueError) → ``zabbix.found=False`` + reason
   A4. ``skip_zabbix=True`` → 不调 Zabbix client,全部 found=False/skip
   A5. 服务 ``Replicas`` 解析 + 异常筛选(``"1/3"`` 进 abnormal,``"3/3"`` 不进)
   A6. ``include_service_list=False`` → 不输出 ``services.all``
   A7. ``include_service_list=True`` → 输出 ``services.all``
   A8. ``docker node ls`` 返回非零退出码 → 抛 RuntimeError

B. 默认 runbook
   B1. ``cluster_health_audit_swarm`` 在 DEFAULT_RUNBOOKS 里
   B2. triggers 含 swarm 限定 + 通用巡检词
   B3. 引用了 ``swarm_cluster_overview`` 这个 skill code
   B4. final_report_prompt 提到"表格"

C. Manifest 一致性
   C1. ``MANIFEST['code'] == 'swarm_cluster_overview'``
   C2. ``read_only=True`` + ``required_connection_type='swarm'``
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from skills import swarm_cluster_overview as skill


# ---------- 测试桩 ---------- #


@dataclass
class _Result:
    """模拟 ``DockerSwarmClient.run()`` 返回的 CommandResult。"""

    stdout: str = ""
    stderr: str = ""
    returncode: int = 0

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class _FakeSwarm:
    """按 (verb, name?) 路由的 docker 命令桩。"""

    def __init__(self, *,
                 nodes_ls: list[dict] | None = None,
                 inspect_map: dict[str, dict] | None = None,
                 services_ls: list[dict] | None = None,
                 node_ls_fail: bool = False) -> None:
        self.nodes_ls = nodes_ls or []
        self.inspect_map = inspect_map or {}
        self.services_ls = services_ls or []
        self.node_ls_fail = node_ls_fail
        self.calls: list[list[str]] = []

    def run(self, args: list[str]) -> _Result:
        self.calls.append(list(args))
        if args[:2] == ["node", "ls"]:
            if self.node_ls_fail:
                return _Result(stderr="permission denied", returncode=1)
            lines = "\n".join(json.dumps(n, ensure_ascii=False) for n in self.nodes_ls)
            return _Result(stdout=lines)
        if args[:2] == ["node", "inspect"]:
            ref = args[2]
            entry = self.inspect_map.get(ref)
            if entry is None:
                return _Result(stderr=f"no such node: {ref}", returncode=1)
            return _Result(stdout=json.dumps(entry, ensure_ascii=False))
        if args[:2] == ["service", "ls"]:
            lines = "\n".join(json.dumps(s, ensure_ascii=False) for s in self.services_ls)
            return _Result(stdout=lines)
        return _Result(stderr=f"unsupported: {args}", returncode=2)


class _FakeZabbix:
    """ZabbixClient 桩——按 host_query 路由到预设响应。

    覆盖两个独立方法:``get_host_overview`` 和 ``get_host_storage_overview``,
    因为它们是两个 Zabbix RPC,skill 需要并行调。
    """

    def __init__(self, by_ip: dict[str, dict] | None = None,
                 storage_by_ip: dict[str, dict] | None = None,
                 missing: set[str] | None = None,
                 errors: dict[str, Exception] | None = None,
                 storage_errors: dict[str, Exception] | None = None) -> None:
        self.by_ip = by_ip or {}
        self.storage_by_ip = storage_by_ip or {}
        self.missing = missing or set()
        self.errors = errors or {}
        self.storage_errors = storage_errors or {}
        self.queries: list[str] = []
        self.storage_queries: list[str] = []

    def get_host_overview(self, host_query: str, *, lookback_hours: float | int = 1) -> dict:
        self.queries.append(host_query)
        if host_query in self.errors:
            raise self.errors[host_query]
        if host_query in self.missing:
            raise ValueError(f"未找到主机: {host_query}")
        return self.by_ip.get(host_query) or {"host": {"host_name": host_query}, "_signals": []}

    def get_host_storage_overview(self, host_query: str, *,
                                   lookback_hours: float | int | None = None) -> dict:
        self.storage_queries.append(host_query)
        if host_query in self.storage_errors:
            raise self.storage_errors[host_query]
        return self.storage_by_ip.get(host_query) or {
            "host": {"host_name": host_query},
            "filesystems": [],
        }


class _FakeContext:
    """SkillContext 桩——只实现 connection_for。"""

    def __init__(self, *, swarm: _FakeSwarm, zabbix: _FakeZabbix | None = None,
                 zabbix_raise: bool = False) -> None:
        self.swarm = swarm
        self.zabbix = zabbix
        self.zabbix_raise = zabbix_raise

    def connection_for(self, type_code: str, override_id: str | None = None) -> Any:
        if type_code == "swarm":
            return self.swarm
        if type_code == "zabbix":
            if self.zabbix_raise:
                raise RuntimeError("no zabbix connection")
            return self.zabbix
        raise KeyError(type_code)


# ---------- A. skill 行为 ---------- #


def test_A1_happy_path_three_nodes_with_zabbix() -> None:
    """全 success 路径:3 节点都拿到 IP,都在 zabbix 能查到 overview + storage。"""
    swarm = _FakeSwarm(
        nodes_ls=[
            {"ID": "n1", "Hostname": "mgr1", "Availability": "Active",
             "Status": "Ready", "ManagerStatus": "Leader"},
            {"ID": "n2", "Hostname": "w1", "Availability": "Active",
             "Status": "Ready", "ManagerStatus": ""},
            {"ID": "n3", "Hostname": "w2", "Availability": "Active",
             "Status": "Ready", "ManagerStatus": ""},
        ],
        inspect_map={
            "n1": {"Status": {"Addr": "192.168.2.121"}},
            "n2": {"Status": {"Addr": "192.168.2.122"}},
            "n3": {"Status": {"Addr": "192.168.2.123"}},
        },
        services_ls=[
            {"Name": "svc-a", "Mode": "replicated", "Replicas": "3/3", "Image": "a:1.0"},
            {"Name": "svc-b", "Mode": "global", "Replicas": "3/3", "Image": "b:2.0"},
        ],
    )
    zabbix = _FakeZabbix(
        by_ip={
            "192.168.2.121": {"host": {"host_name": "192.168.2.121"},
                              "metric_summary": {"cpu_avg": 30}, "_signals": []},
            "192.168.2.122": {"host": {"host_name": "192.168.2.122"},
                              "metric_summary": {"cpu_avg": 85},
                              "_signals": [{"type": "high_cpu", "severity": "warning"}]},
            "192.168.2.123": {"host": {"host_name": "192.168.2.123"}, "_signals": []},
        },
        storage_by_ip={
            "192.168.2.121": {"filesystems": [
                {"mount_point": "/", "used_percent": 45.0, "total_gb": 100.0, "free_gb": 55.0},
            ]},
            "192.168.2.122": {"filesystems": [
                {"mount_point": "/", "used_percent": 70.0},
                {"mount_point": "/var", "used_percent": 93.5},  # 高水位
            ]},
            "192.168.2.123": {"filesystems": []},   # 没采到磁盘
        },
    )
    ctx = _FakeContext(swarm=swarm, zabbix=zabbix)

    out = skill.run(ctx)
    assert out["swarm"]["node_count"] == 3
    assert out["swarm"]["manager_count"] == 1
    assert out["swarm"]["worker_count"] == 2
    assert len(out["nodes"]) == 3
    # 3 个节点 IP 都从 inspect 里捞出来了
    assert [n["addr"] for n in out["nodes"]] == [
        "192.168.2.121", "192.168.2.122", "192.168.2.123",
    ]
    # 都能在 zabbix 里找到 + 都拿到 storage
    assert all(n["zabbix"]["found"] for n in out["nodes"])
    for n in out["nodes"]:
        assert "storage" in n["zabbix"], f"node {n['hostname']} 缺 storage 字段"
    # 字段路径 = zabbix.storage.filesystems(对齐 prompt 引用)
    n2 = next(n for n in out["nodes"] if n["addr"] == "192.168.2.122")
    fs = n2["zabbix"]["storage"]["filesystems"]
    assert any(m["mount_point"] == "/var" and m["used_percent"] == 93.5 for m in fs)
    # zabbix overview + storage 各调 3 次,**都用 IP**
    assert zabbix.queries == ["192.168.2.121", "192.168.2.122", "192.168.2.123"]
    assert zabbix.storage_queries == ["192.168.2.121", "192.168.2.122", "192.168.2.123"]
    # 服务原样塞 items,不判定
    assert out["services"]["total"] == 2
    assert {s["name"] for s in out["services"]["items"]} == {"svc-a", "svc-b"}
    # signals 已上抛到顶层 _signals
    assert any(s.get("type") == "high_cpu" for s in out.get("_signals", []))


def test_A2_node_without_addr_marked_unfound() -> None:
    """某节点 ``Status.Addr`` 是空 → zabbix 不查,标 found=False。"""
    swarm = _FakeSwarm(
        nodes_ls=[
            {"ID": "n1", "Hostname": "mgr1", "ManagerStatus": "Leader"},
        ],
        inspect_map={"n1": {"Status": {"Addr": ""}}},
        services_ls=[],
    )
    zabbix = _FakeZabbix()
    ctx = _FakeContext(swarm=swarm, zabbix=zabbix)

    out = skill.run(ctx)
    assert out["nodes"][0]["addr"] == ""
    assert out["nodes"][0]["zabbix"]["found"] is False
    assert "Status.Addr" in out["nodes"][0]["zabbix"]["reason"]
    # 没传 IP → 应该完全没调 zabbix
    assert zabbix.queries == []


def test_A3_zabbix_value_error_treated_as_not_found() -> None:
    """Zabbix client 抛 ValueError(未找到主机) → 归一化成 found=False + reason。"""
    swarm = _FakeSwarm(
        nodes_ls=[
            {"ID": "n1", "Hostname": "mgr1", "ManagerStatus": "Leader"},
        ],
        inspect_map={"n1": {"Status": {"Addr": "10.0.0.1"}}},
        services_ls=[],
    )
    zabbix = _FakeZabbix(missing={"10.0.0.1"})
    ctx = _FakeContext(swarm=swarm, zabbix=zabbix)

    out = skill.run(ctx)
    assert out["nodes"][0]["zabbix"]["found"] is False
    assert "10.0.0.1" in out["nodes"][0]["zabbix"]["reason"]
    assert zabbix.queries == ["10.0.0.1"]


def test_A4_skip_zabbix_does_not_call_client() -> None:
    """``skip_zabbix=True`` → 完全不调 zabbix。"""
    swarm = _FakeSwarm(
        nodes_ls=[{"ID": "n1", "Hostname": "mgr1", "ManagerStatus": "Leader"}],
        inspect_map={"n1": {"Status": {"Addr": "1.2.3.4"}}},
        services_ls=[],
    )
    zabbix = _FakeZabbix()
    ctx = _FakeContext(swarm=swarm, zabbix=zabbix)

    out = skill.run(ctx, skip_zabbix=True)
    assert zabbix.queries == []
    assert out["nodes"][0]["zabbix"]["found"] is False
    assert "skip_zabbix" in out["nodes"][0]["zabbix"]["reason"]


def test_A5_services_returned_raw_no_judgment() -> None:
    """关键设计契约:skill **不做异常判定**——所有服务原样塞 items,
    Replicas 字段保留原文,**不**自己筛 abnormal、**不**算 running/desired。

    异常判断是模型的事。skill 越界做了会剥夺模型的解析空间。
    """
    swarm = _FakeSwarm(
        nodes_ls=[],
        services_ls=[
            {"Name": "ok-svc",   "Mode": "replicated", "Replicas": "3/3"},
            {"Name": "bad-svc",  "Mode": "replicated", "Replicas": "1/3", "Image": "x:1"},
            {"Name": "dead-svc", "Mode": "replicated", "Replicas": "0/2"},
            {"Name": "ok-cap",   "Mode": "replicated", "Replicas": "2/2 (max 5 per node)"},
            {"Name": "weird",    "Mode": "replicated", "Replicas": "n/a"},
        ],
    )
    ctx = _FakeContext(swarm=swarm, zabbix=None, zabbix_raise=True)
    out = skill.run(ctx)

    # 没有 abnormal / reason / running / desired 字段——这些都是模型该自己解析的
    assert "abnormal" not in out["services"]
    assert "abnormal_count" not in out["services"]
    # items 是 raw list,每个服务的 Replicas 原文保留
    items = out["services"]["items"]
    assert out["services"]["total"] == 5
    assert len(items) == 5
    by_name = {s["name"]: s for s in items}
    assert by_name["dead-svc"]["replicas"] == "0/2"
    assert by_name["ok-cap"]["replicas"] == "2/2 (max 5 per node)"
    assert by_name["weird"]["replicas"] == "n/a"
    # 没有"reason"这种判定字段
    for s in items:
        assert "reason" not in s
        assert "replicas_running" not in s
        assert "replicas_desired" not in s


def test_A_storage_failure_does_not_break_overview() -> None:
    """单节点 storage 调用失败不能让 overview 也丢:仍返回 found=True + storage_error。"""
    swarm = _FakeSwarm(
        nodes_ls=[{"ID": "n1", "Hostname": "h1", "ManagerStatus": "Leader"}],
        inspect_map={"n1": {"Status": {"Addr": "10.0.0.1"}}},
        services_ls=[],
    )
    zabbix = _FakeZabbix(
        by_ip={"10.0.0.1": {"metric_summary": {"cpu_avg": 5}, "_signals": []}},
        storage_errors={"10.0.0.1": RuntimeError("zabbix item.get 超时")},
    )
    ctx = _FakeContext(swarm=swarm, zabbix=zabbix)

    out = skill.run(ctx)
    n = out["nodes"][0]
    # overview 仍然 found=True(主线没死)
    assert n["zabbix"]["found"] is True
    assert n["zabbix"]["overview"]["metric_summary"]["cpu_avg"] == 5
    # storage 不在,但 storage_error 把错误字段化保留(模型能识别"磁盘采集失败")
    assert "storage" not in n["zabbix"]
    assert "storage_error" in n["zabbix"]
    assert "超时" in n["zabbix"]["storage_error"]


def test_A_prompt_field_paths_aligned_with_skill_output() -> None:
    """关键契约:final_report_prompt 引用的字段路径必须真实存在于 skill 返回结构里。

    历史 bug:prompt 写 ``zabbix.overview.storage_summary.mounts``,实际 skill
    返回 ``zabbix.storage.filesystems``——字段对不上,模型只能写"无数据"。
    """
    from ops_platform.runbook_seeds import DEFAULT_RUNBOOKS

    swarm = _FakeSwarm(
        nodes_ls=[{"ID": "n1", "Hostname": "h1", "ManagerStatus": "Leader"}],
        inspect_map={"n1": {"Status": {"Addr": "10.0.0.1"}}},
        services_ls=[],
    )
    zabbix = _FakeZabbix(
        by_ip={"10.0.0.1": {
            "metric_summary": {"cpu_avg": 5, "cpu_p95": 6},
            "memory_summary": {"memory_used_percent": 50},
            "_signals": [],
        }},
        storage_by_ip={"10.0.0.1": {
            "filesystems": [{"mount_point": "/", "used_percent": 70}],
        }},
    )
    ctx = _FakeContext(swarm=swarm, zabbix=zabbix)
    out = skill.run(ctx)

    # 这些是 prompt 里强制引用的字段路径,如果改了 skill 还得改 prompt 配对
    n = out["nodes"][0]
    z = n["zabbix"]
    assert z["overview"]["metric_summary"]["cpu_avg"] == 5
    assert z["overview"]["metric_summary"]["cpu_p95"] == 6
    assert z["overview"]["memory_summary"]["memory_used_percent"] == 50
    assert z["storage"]["filesystems"][0]["mount_point"] == "/"
    assert z["storage"]["filesystems"][0]["used_percent"] == 70

    # prompt 里只能出现 skill 真实暴露的字段路径
    rb = next(r for r in DEFAULT_RUNBOOKS if r["key"] == "cluster_health_audit_swarm")
    prompt = rb["final_report_prompt"]
    assert "metric_summary.cpu_avg" in prompt
    assert "memory_summary.memory_used_percent" in prompt
    assert "zabbix.storage.filesystems" in prompt
    # ❌ 一旦有人重新引入 storage_summary 这种错路径,本测试会拦下
    assert "storage_summary" not in prompt, "prompt 仍引用了错误字段 storage_summary"


def test_A8_node_ls_failure_raises() -> None:
    swarm = _FakeSwarm(node_ls_fail=True)
    ctx = _FakeContext(swarm=swarm)
    with pytest.raises(RuntimeError, match="docker node ls 失败"):
        skill.run(ctx)


# ---------- B. 默认 runbook ---------- #


def test_B_final_report_prompt_disallows_fenced_table() -> None:
    """关键契约:prompt 必须**明确禁止**把表格放 ``` 块里。

    历史 bug:prompt 让模型 ``` 包裹示例表头,chainlit 渲染成 Raw code 块
    而不是 native markdown table。
    """
    from ops_platform.runbook_seeds import DEFAULT_RUNBOOKS

    rb = next(r for r in DEFAULT_RUNBOOKS if r["key"] == "cluster_health_audit_swarm")
    prompt = rb["final_report_prompt"]
    assert ("不要用 ``` 代码块包裹" in prompt) or ("不许加 ``` 包裹" in prompt), (
        "prompt 必须明确告诉模型不要把表格放 fenced code block 里"
    )


def test_B_runbook_seed_present_and_well_formed() -> None:
    from ops_platform.runbook_seeds import DEFAULT_RUNBOOKS

    rb = next((r for r in DEFAULT_RUNBOOKS
               if r.get("key") == "cluster_health_audit_swarm"), None)
    assert rb is not None, "cluster_health_audit_swarm runbook 未注册到 DEFAULT_RUNBOOKS"

    # B2 triggers 覆盖
    triggers = rb["triggers"]
    assert "swarm 巡检" in triggers
    assert "集群巡检" in triggers
    # B3 引用的 skill code 必须对得上
    overview = rb["nodes"]["overview"]
    assert overview["skill"] == "swarm_cluster_overview"
    # B4 final_report_prompt 要明确"模型自己判断" + 不假装 abnormal 已筛好
    prompt = rb["final_report_prompt"]
    assert "raw" in prompt or "原始" in prompt
    assert "node_states" in prompt
    # 关键契约:prompt 不能预设 "abnormal 已筛好"——否则模型不会自己看 raw
    assert "abnormal" not in prompt.lower()
    # prompt 要明确告诉模型异常判定由它自己做
    assert ("自己" in prompt or "你做" in prompt or "由你" in prompt)


def test_B_runbook_loads_via_engine_validator() -> None:
    """图引擎能成功加载并校验本 runbook(不能有环 / skill 引用错误等)。"""
    from ops_platform.runbook_engine import (
        load_runbook_from_dict, validate_runbook,
    )
    from ops_platform.runbook_seeds import DEFAULT_RUNBOOKS

    rb_dict = next(r for r in DEFAULT_RUNBOOKS
                   if r["key"] == "cluster_health_audit_swarm")
    rb = load_runbook_from_dict(rb_dict)
    # known_skills 提供本 skill,write_skills 给空集
    errs = validate_runbook(rb, known_skills={"swarm_cluster_overview"}, write_skills=set())
    assert errs == [], f"runbook 校验错误: {errs}"


# ---------- C. Manifest ---------- #


def test_B_pipe_fallback_literal_is_typed() -> None:
    """Regression(真实生产爆点):``$user.X||1`` 的字面值兜底必须返回 int/float,
    不能返回字符串 ``"1"``。否则下游 ``zabbix.get_host_overview(lookback_hours="1")``
    内部 ``compute_window`` 做 ``"1" <= 0`` 会爆
    ``TypeError: '<=' not supported between instances of 'str' and 'int'``。

    这个 bug 在生产环境真发生了——cluster_health_audit_swarm runbook 跑 19 节点
    全部失败,zabbix 这一步 100% 报这个 TypeError。
    """
    from ops_platform.runbook_engine import ExecutionContext, Runbook, resolve_ref

    # 构造一个没传 lookback_hours 的 context(走 fallback)
    rb = Runbook(key="_", title="_", nodes={})
    ctx = ExecutionContext(runbook=rb, user_inputs={})

    # 数值字面值必须返回数值
    assert resolve_ref("$user.lookback_hours||1", ctx) == 1
    assert isinstance(resolve_ref("$user.lookback_hours||1", ctx), int)
    assert resolve_ref("$user.lookback_hours||1.5", ctx) == 1.5
    assert isinstance(resolve_ref("$user.lookback_hours||1.5", ctx), float)

    # bool 字面值
    assert resolve_ref("$user.x||true", ctx) is True
    assert resolve_ref("$user.x||false", ctx) is False
    # null/none → None,且这会让链继续找下一段;只有一段且 None 时返回 None
    assert resolve_ref("$user.x||null", ctx) is None

    # 字符串字面值保持字符串
    assert resolve_ref("$user.x||some-name", ctx) == "some-name"

    # 用户实际传了值,fallback 不应触发
    ctx2 = ExecutionContext(runbook=rb, user_inputs={"lookback_hours": 24})
    assert resolve_ref("$user.lookback_hours||1", ctx2) == 24


def test_B_validator_accepts_pipe_fallback_in_ref() -> None:
    """Regression:validator 必须接受 resolver 已支持的 ``$x||literal`` fallback 语法。

    历史 bug:resolver 支持 ``$user.X||1`` 这种 fallback,但 validator 用单纯
    REF_PATTERN.match 直接拒了。本测试钉住:含 ``||`` 的引用必须能通过 validator。
    """
    from ops_platform.runbook_engine import (
        load_runbook_from_dict, validate_runbook,
    )

    rb = load_runbook_from_dict({
        "key": "_test_pipe", "title": "_",
        "nodes": {
            "a": {
                "skill": "swarm_cluster_overview",
                "args": {
                    "lookback_hours": "$user.lookback_hours||1",
                    "name": "$user.svc||$nodes.x.fallback||literal",
                },
            },
        },
    })
    errs = validate_runbook(rb, known_skills={"swarm_cluster_overview"}, write_skills=set())
    # 不能因为含 || 就被拒
    bad_pipe_errs = [e for e in errs if "||" in e or "格式不合法" in e]
    assert bad_pipe_errs == [], f"validator 误拒了 pipe fallback:{bad_pipe_errs}"


def test_B_seed_four_path_behavior() -> None:
    """Regression:seed_default_runbooks 必须按 4 种语义工作。

    1. DB 没有 key → insert
    2. DB bootstrap-owned + definition 跟 DEFAULTS 不一致 → 覆盖(让改 prompt 自动落库)
    3. DB updated_by != bootstrap(admin 改过)→ 绝不动
    4. DB bootstrap-owned + definition 一致 → 不动(幂等)

    这是支持"改 DEFAULTS 重启即生效"的关键路径,以前的"表非空整体跳过"语义
    会让所有 prompt 改动都得手写迁移脚本,这次彻底解决。
    """
    from ops_platform.runbook_seeds import DEFAULT_RUNBOOKS, seed_default_runbooks

    class _Store:
        def __init__(self, rows: list[dict]) -> None:
            self._rows = {r["key"]: r for r in rows}
            self.upserts: list[tuple[str, dict]] = []

        def list_runbooks(self) -> list[dict]:
            return list(self._rows.values())

        def upsert_runbook(self, *, key: str, definition: dict, **fields) -> None:
            self.upserts.append((key, dict(fields, definition=definition)))
            self._rows[key] = {"key": key, "definition": definition,
                               "updated_by": fields.get("updated_by"), **fields}

    assert len(DEFAULT_RUNBOOKS) >= 2, "测试假设 ≥ 2 条 default runbook"
    rb0 = DEFAULT_RUNBOOKS[0]
    rb1 = DEFAULT_RUNBOOKS[1]

    # 用例 A:DB 完全空 → 全部 insert
    s = _Store(rows=[])
    seed_default_runbooks(s)
    upserted = {k for k, _ in s.upserts}
    assert upserted == {r["key"] for r in DEFAULT_RUNBOOKS}, "空 DB 应全部插入"

    # 用例 B:bootstrap-owned 但 definition 跟 DEFAULTS 一致 → 不动
    s = _Store(rows=[
        {"key": rb0["key"], "definition": rb0, "updated_by": "bootstrap"},
    ])
    seed_default_runbooks(s)
    assert rb0["key"] not in {k for k, _ in s.upserts}, "bootstrap-owned 且一致不应 upsert"

    # 用例 C:bootstrap-owned 但 definition 是旧版本 → 强制更新
    s = _Store(rows=[
        {"key": rb0["key"], "definition": {"key": rb0["key"], "title": "OLD"},
         "updated_by": "bootstrap"},
    ])
    seed_default_runbooks(s)
    upserted_keys = [k for k, _ in s.upserts]
    assert rb0["key"] in upserted_keys, "bootstrap-owned 且 definition 变化应被覆盖"

    # 用例 D:admin 改过(updated_by != bootstrap)→ 绝不动,即使 DEFAULTS 变了
    s = _Store(rows=[
        {"key": rb0["key"], "definition": {"key": rb0["key"], "title": "ADMIN_OLD"},
         "updated_by": "admin"},
    ])
    seed_default_runbooks(s)
    assert rb0["key"] not in {k for k, _ in s.upserts}, (
        "admin 改过的 runbook 绝不能被 seed 覆盖"
    )

    # 用例 E(综合):一条 bootstrap 旧版 + 一条 admin 改过 + 缺失若干
    s = _Store(rows=[
        {"key": rb0["key"], "definition": {"key": rb0["key"], "title": "OLD"},
         "updated_by": "bootstrap"},                            # 该被更新
        {"key": rb1["key"], "definition": {"key": rb1["key"], "title": "ADMIN"},
         "updated_by": "admin"},                                # 该被跳过
    ])
    seed_default_runbooks(s)
    upserted_keys = {k for k, _ in s.upserts}
    assert rb0["key"] in upserted_keys, "用例 E:bootstrap 旧版要更新"
    assert rb1["key"] not in upserted_keys, "用例 E:admin 改过的不能动"
    # cluster_health_audit_swarm 在 DEFAULTS 里 → 本次启动应被 insert
    assert "cluster_health_audit_swarm" in upserted_keys


def test_B_seed_counters_and_bootstrap_sync_marker(caplog) -> None:
    """Regression:seed_default_runbooks 三个计数器 + ``bootstrap-sync`` 标记。

    "改 DEFAULTS 重启即生效"这条路径的关键不变量:

      1. 日志单行包含 ``inserted=N``、``synced=N``、``admin-skipped=N`` 三个数字
      2. sync 覆盖时 ``updated_by`` 必须改写为 ``bootstrap-sync``(区别于首次 seed
         的 ``bootstrap``),否则下次重启再 grep 日志就分不清是"刚刚同步"还是"自
         初次 seed 起一直没动过"
      3. ``updated_by`` ∈ {bootstrap, bootstrap-sync, None, ''} 都算平台所有,
         全可被同步覆盖;其它值(``alice`` / ``admin``)视作 admin 改过,绝不动
    """
    import logging
    from ops_platform.runbook_seeds import DEFAULT_RUNBOOKS, seed_default_runbooks

    class _Store:
        def __init__(self, rows: list[dict]) -> None:
            self._rows = {r["key"]: dict(r) for r in rows}
            self.upserts: list[dict] = []  # 完整 kwargs,便于断言 updated_by

        def list_runbooks(self) -> list[dict]:
            return [dict(r) for r in self._rows.values()]

        def upsert_runbook(self, **fields) -> None:
            self.upserts.append(dict(fields))
            self._rows[fields["key"]] = {
                "key": fields["key"],
                "definition": fields["definition"],
                "updated_by": fields["updated_by"],
            }

    n_defaults = len(DEFAULT_RUNBOOKS)
    assert n_defaults >= 2

    def _seed_and_get_log(store) -> str:
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="ops_platform.runbook_seeds"):
            seed_default_runbooks(store)
        for rec in reversed(caplog.records):
            if rec.getMessage().startswith("seed: inserted="):
                return rec.getMessage()
        raise AssertionError("没找到 'seed: inserted=...' 计数日志")

    # ---------- 用例 1:DB 空 → 全部插入,inserted=N / synced=0 / admin-skipped=0
    s = _Store(rows=[])
    line = _seed_and_get_log(s)
    assert f"inserted={n_defaults}" in line, line
    assert "synced=0" in line, line
    assert "admin-skipped=0" in line, line
    # 初插标记 == 'bootstrap'
    assert all(u["updated_by"] == "bootstrap" for u in s.upserts), (
        "初次 seed 应打 'bootstrap',实际:"
        f"{[u['updated_by'] for u in s.upserts]}"
    )

    rb0 = DEFAULT_RUNBOOKS[0]
    rb1 = DEFAULT_RUNBOOKS[1]
    others_count = n_defaults - 1  # 测试用例里 DB 只有 rb0,其它都得新插

    # ---------- 用例 2:bootstrap-owned 但 def 变了 → synced=1 + 标记改 bootstrap-sync
    s = _Store(rows=[
        {"key": rb0["key"],
         "definition": {"key": rb0["key"], "title": "OLD_VERSION"},
         "updated_by": "bootstrap"},
    ])
    line = _seed_and_get_log(s)
    assert "synced=1" in line, line
    assert f"inserted={others_count}" in line, line
    rb0_upsert = next(u for u in s.upserts if u["key"] == rb0["key"])
    assert rb0_upsert["updated_by"] == "bootstrap-sync", (
        "sync 覆盖必须把 updated_by 改写为 bootstrap-sync,实际:"
        f"{rb0_upsert['updated_by']}"
    )

    # ---------- 用例 3:admin-owned (updated_by='alice') → admin-skipped=1,不动
    s = _Store(rows=[
        {"key": rb0["key"],
         "definition": {"key": rb0["key"], "title": "ALICE_HAND_EDIT"},
         "updated_by": "alice"},
    ])
    line = _seed_and_get_log(s)
    assert "admin-skipped=1" in line, line
    assert rb0["key"] not in {u["key"] for u in s.upserts}, (
        "admin 改过的 runbook 不能被 upsert"
    )

    # ---------- 用例 4:'bootstrap-sync' 也算平台所有,再次重启还能同步
    s = _Store(rows=[
        {"key": rb0["key"],
         "definition": {"key": rb0["key"], "title": "PREV_SYNCED"},
         "updated_by": "bootstrap-sync"},
    ])
    line = _seed_and_get_log(s)
    assert "synced=1" in line, (
        f"updated_by=='bootstrap-sync' 应继续被视作平台所有,日志:{line}"
    )

    # ---------- 用例 5:updated_by=None / '' 也算平台所有(老库残留)
    for legacy_owner in (None, ""):
        s = _Store(rows=[
            {"key": rb0["key"],
             "definition": {"key": rb0["key"], "title": "LEGACY"},
             "updated_by": legacy_owner},
        ])
        line = _seed_and_get_log(s)
        assert "synced=1" in line, (
            f"updated_by={legacy_owner!r} 应被视作平台所有,日志:{line}"
        )

    # ---------- 用例 6:综合 — 1 条 bootstrap 旧版(sync) + 1 条 admin(skip)
    s = _Store(rows=[
        {"key": rb0["key"],
         "definition": {"key": rb0["key"], "title": "OLD"},
         "updated_by": "bootstrap"},
        {"key": rb1["key"],
         "definition": {"key": rb1["key"], "title": "ADMIN"},
         "updated_by": "alice"},
    ])
    line = _seed_and_get_log(s)
    assert "synced=1" in line and "admin-skipped=1" in line, line
    # rb0 被 sync,标记改 bootstrap-sync
    rb0_upsert = next(u for u in s.upserts if u["key"] == rb0["key"])
    assert rb0_upsert["updated_by"] == "bootstrap-sync"
    # rb1 不动
    assert rb1["key"] not in {u["key"] for u in s.upserts}


def test_C_manifest_shape() -> None:
    m = skill.MANIFEST
    assert m["code"] == "swarm_cluster_overview"
    assert m["read_only"] is True
    assert m["required_connection_type"] == "swarm"
    assert m["visibility"] == "all"
    # description 提到关键 know-how(防止改坏)
    desc = m["description"]
    assert "巡检" in desc
    assert "IP" in desc and "Zabbix" in desc

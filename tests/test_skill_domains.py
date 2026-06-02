"""渐进披露架构契约：核心常驻集 + 按域懒加载 + load_skills meta-tool。

设计契约
========
1. **完整性**：每个注册的 skill 要么在 ``CORE_SKILLS``，要么在某个 domain 里，
   不漏不重——否则模型永远加载不到它。
2. **核心集**：三把通用查询口 + zabbix 概览 + runbook 入口必须常驻。
3. **load_skills meta-tool**：schema 合法，domains enum 跟 SKILL_DOMAINS 一致。
4. **expand_tools 幂等 + 去重**：重复加载同域无副作用。
5. **signal 目标可达**：所有 scanner 发的 next_skill 要么在 core，要么在某域里
   （能被 signal 自动加载）——否则"挡 signal"硬伤复现。
"""

from __future__ import annotations

import importlib
import pkgutil

import pytest

import skills as _skills_pkg
from ops_agent.skill_domains import (
    CORE_SKILLS,
    SKILL_DOMAINS,
    build_core_tools,
    domain_for_skill,
    expand_tools,
    load_skills_tool_schema,
    skills_in_domains,
)


def _all_registered_skill_codes() -> set[str]:
    out: set[str] = set()
    for info in pkgutil.iter_modules(_skills_pkg.__path__, prefix="skills."):
        try:
            m = importlib.import_module(info.name)
        except Exception:
            continue
        man = getattr(m, "MANIFEST", None)
        if man and man.get("code"):
            out.add(man["code"])
    return out


def _all_domain_skills() -> list[str]:
    out: list[str] = []
    for codes in SKILL_DOMAINS.values():
        out.extend(codes)
    return out


# ---------- 完整性 ---------- #


def test_every_skill_is_core_or_in_exactly_one_domain():
    """每个注册 skill 必须 ∈ core 或 恰好一个 domain。漏了就永远加载不到。"""
    registered = _all_registered_skill_codes()
    domain_skills = _all_domain_skills()
    domain_set = set(domain_skills)

    # 1) 无重复（一个 skill 不能在两个域）
    assert len(domain_skills) == len(domain_set), (
        "有 skill 出现在多个 domain 里：" +
        str([c for c in domain_skills if domain_skills.count(c) > 1])
    )

    # 2) core 和 domain 不重叠
    overlap = CORE_SKILLS & domain_set
    assert not overlap, f"这些 skill 同时在 core 和 domain：{overlap}"

    # 3) 覆盖完整——每个注册 skill 都被分类
    classified = set(CORE_SKILLS) | domain_set
    missing = registered - classified
    assert not missing, (
        f"这些已注册 skill 既不在 core 也不在任何 domain，模型永远加载不到：{missing}"
    )

    # 4) 反向——分类表里不能有未注册的幽灵 skill
    ghost = classified - registered
    assert not ghost, f"分类表引用了未注册的 skill：{ghost}"


def test_core_has_the_three_query_mouths_and_runbook():
    """三把通用查询口 + runbook 入口 必须常驻。"""
    for must in ("kube_query", "swarm_query", "host_query",
                 "platform_run_runbook", "platform_get_runbooks"):
        assert must in CORE_SKILLS, f"{must} 应当在 Layer 0 常驻核心"


def test_core_includes_zabbix_overview():
    """用户说监控高频——zabbix 概览进常驻。"""
    assert "zabbix_get_host_overview" in CORE_SKILLS


def test_core_is_small():
    """常驻集要小（≤8）——大了就失去渐进披露的意义。"""
    assert len(CORE_SKILLS) <= 8, f"core 膨胀到 {len(CORE_SKILLS)}，渐进披露失效"


# ---------- load_skills meta-tool ---------- #


def test_load_skills_schema_shape():
    schema = load_skills_tool_schema()
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "load_skills"
    params = schema["function"]["parameters"]
    assert params["required"] == ["domains"]
    enum = params["properties"]["domains"]["items"]["enum"]
    assert set(enum) == set(SKILL_DOMAINS), "load_skills 的 domain enum 跟 SKILL_DOMAINS 不一致"


def test_load_skills_description_lists_all_domains():
    """描述要列全部域，否则模型不知道有哪些可加载。"""
    desc = load_skills_tool_schema()["function"]["description"]
    for d in SKILL_DOMAINS:
        assert d in desc, f"load_skills 描述漏了域 {d}"


# ---------- build_core_tools / expand_tools ---------- #


def _fake_full_tools(codes: list[str]) -> list[dict]:
    return [{"type": "function", "function": {"name": c}} for c in codes]


def test_build_core_tools_picks_core_plus_meta():
    full = _fake_full_tools(list(CORE_SKILLS) + ["swarm_scale_service", "jenkins_query"])
    core = build_core_tools(full)
    names = {t["function"]["name"] for t in core}
    assert "load_skills" in names, "build_core_tools 必须带 meta-tool"
    assert "swarm_scale_service" not in names, "写 skill 不该在核心集"
    assert CORE_SKILLS <= names


def test_expand_tools_adds_domain_skills():
    full = _fake_full_tools(list(CORE_SKILLS) + skills_in_domains(["swarm_write"]))
    core = build_core_tools(full)
    expanded, added = expand_tools(full, core, skills_in_domains(["swarm_write"]))
    names = {t["function"]["name"] for t in expanded}
    assert "swarm_scale_service" in names
    assert set(added) == set(skills_in_domains(["swarm_write"]))


def test_expand_tools_is_idempotent():
    """重复加载同域不应重复追加。"""
    codes = skills_in_domains(["swarm_write"])
    full = _fake_full_tools(list(CORE_SKILLS) + codes)
    core = build_core_tools(full)
    once, added1 = expand_tools(full, core, codes)
    twice, added2 = expand_tools(full, once, codes)
    assert added2 == [], "第二次加载同域应无新增"
    assert len(twice) == len(once)


def test_expand_tools_ignores_unregistered():
    """请求加载一个 full_tools 里没有的 skill（如 user 不可见的 admin skill）→ 跳过不崩。"""
    full = _fake_full_tools(list(CORE_SKILLS))
    core = build_core_tools(full)
    expanded, added = expand_tools(full, core, ["host_run_command"])  # 不在 full
    assert added == []


# ---------- signal 目标可达性（防"挡 signal"硬伤复现）---------- #


def test_all_scanner_next_skills_are_reachable():
    """所有 scanner 发的 next_skill 必须 ∈ core 或 某个 domain，
    否则 signal 自动加载够不到，模型遵循不了——这是重构前的硬伤。"""
    import ops_platform.scanners as scanners_pkg
    import re

    next_skills: set[str] = set()
    pat = re.compile(r'next_skill\s*=\s*["\']([a-z_]+)["\']')
    import pathlib
    scan_dir = pathlib.Path(scanners_pkg.__file__).parent
    for py in scan_dir.glob("*.py"):
        text = py.read_text(encoding="utf-8")
        next_skills.update(pat.findall(text))

    reachable = set(CORE_SKILLS) | {c for codes in SKILL_DOMAINS.values() for c in codes}
    unreachable = next_skills - reachable
    assert not unreachable, (
        f"这些 scanner next_skill 不在 core 也不在任何 domain，signal 自动加载够不到：{unreachable}"
    )


# ---------- domain_for_skill ---------- #


def test_domain_for_skill_lookup():
    assert domain_for_skill("swarm_scale_service") == "swarm_write"
    assert domain_for_skill("jenkins_query") == "cicd"
    assert domain_for_skill("metric_query") == "monitoring"
    # core skill 不属于任何懒加载域
    assert domain_for_skill("kube_query") is None
    assert domain_for_skill("不存在的skill") is None


# ---------- 端到端：agent loop 真的渐进披露 ---------- #


def _mock_runtime(full_codes: list[str]):
    from unittest.mock import MagicMock
    rt = MagicMock()
    rt.model_manager.get_client.return_value = MagicMock()
    rt.store.list_chat_messages.return_value = []
    rt.store.count_chat_messages.return_value = 0
    rt.store.list_prompt_segments.return_value = []
    rt.connection_manager.list.return_value = []
    rt.runbook_registry.match_by_query.return_value = None
    rt.skill_registry.openai_tools.return_value = [
        {"type": "function", "function": {"name": c}} for c in full_codes
    ]
    return rt


def test_agent_loop_progressive_disclosure_end_to_end():
    """第 1 轮模型只看到核心集（无写 skill）→ 调 load_skills → 第 2 轮写 skill 可见可调。"""
    from unittest.mock import MagicMock
    from ops_agent.agent import UnifiedOpsAgent

    rt = _mock_runtime(list(CORE_SKILLS) + ["swarm_scale_service", "swarm_remove_service"])
    agent = UnifiedOpsAgent(rt, max_steps=4)

    seen: list[list[str]] = []

    def fake_complete(messages=None, tools=None, tool_choice=None):
        if tools is None:   # 末轮 summary（无 tools）
            return {"role": "assistant", "content": "完成"}
        seen.append([t["function"]["name"] for t in tools])
        step = len(seen)
        if step == 1:
            return {"role": "assistant", "content": "",
                    "tool_calls": [{"id": "c1", "type": "function",
                                    "function": {"name": "load_skills",
                                                 "arguments": '{"domains":["swarm_write"]}'}}]}
        if step == 2:
            return {"role": "assistant", "content": "",
                    "tool_calls": [{"id": "c2", "type": "function",
                                    "function": {"name": "swarm_scale_service",
                                                 "arguments": '{"service":"web","replicas":3}'}}]}
        return {"role": "assistant", "content": "完成"}

    agent.model.create_completion = fake_complete
    agent.invoker.invoke = MagicMock(
        return_value={"status": "needs_confirmation", "pending_token": "t", "preview": {}})
    agent.invoker.serialize_for_model = lambda e: "{}"

    agent.ask("把 web 扩容到 3", user={"username": "admin", "role": "admin"}, session_id="s")

    # 第 1 轮：核心集 + meta，无写 skill
    assert "load_skills" in seen[0]
    assert "swarm_scale_service" not in seen[0], "首轮不该暴露写 skill"
    # 第 2 轮：load_skills 之后写 skill 可见
    assert "swarm_scale_service" in seen[1], "load_skills 后写 skill 应可调"


def test_agent_loop_signal_auto_loads_domain():
    """scanner 发 next_skill 指向懒加载域里的 skill 时，平台自动加载该域——
    模型下一轮立刻能调（复现并验证修复了'静态过滤挡 signal'硬伤）。"""
    from unittest.mock import MagicMock
    from ops_agent.agent import UnifiedOpsAgent

    # metric_query 在 monitoring 域（非核心）
    rt = _mock_runtime(list(CORE_SKILLS) + ["metric_query"])
    agent = UnifiedOpsAgent(rt, max_steps=4)

    seen: list[list[str]] = []

    def fake_complete(messages=None, tools=None, tool_choice=None):
        if tools is None:
            return {"role": "assistant", "content": "完成"}
        seen.append([t["function"]["name"] for t in tools])
        step = len(seen)
        if step == 1:
            # 调核心 skill host_query，其结果带一个指向 metric_query 的 signal
            return {"role": "assistant", "content": "",
                    "tool_calls": [{"id": "c1", "type": "function",
                                    "function": {"name": "host_query",
                                                 "arguments": '{"node":"n1","command":"df -h"}'}}]}
        return {"role": "assistant", "content": "完成"}

    agent.model.create_completion = fake_complete
    # host_query 返回一个 signal，next_skill 指向 monitoring 域的 metric_query
    agent.invoker.invoke = MagicMock(return_value={
        "status": "ok",
        "result": {"_signals": [{
            "type": "high_cpu", "severity": "critical", "evidence": "CPU 95%",
            "next_skill": "metric_query",
            "next_args": {"mode": "peak", "host_query": "n1", "metric": "cpu.utilization"},
        }]},
    })
    agent.invoker.serialize_for_model = lambda e: "{}"

    agent.ask("看下 n1 磁盘", user={"username": "admin", "role": "admin"}, session_id="s")

    # 第 2 轮：metric_query 应被 signal 自动加载进来（无需模型先调 load_skills）
    assert len(seen) >= 2, "应该至少跑了 2 轮"
    assert "metric_query" in seen[1], "signal 指向的 metric_query 应被自动加载"

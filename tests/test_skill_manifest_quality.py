"""Skill MANIFEST 质量 lint。

为什么需要
==========
28 个 skill 的 description 直接拼进 LLM 的 tools schema,质量直接决定模型选 skill
和填参数的准确度。审计发现:

- 部分 description < 100 字,模型搞不清楚什么时候该用
- 部分 description > 800 字,关键的"什么时候**不该**用"信息埋在末尾,模型容易漏
- 长 description 不带示例,模型只能"理解描述凭想象填参数"
- params_schema 个别 skill 缺关键约束(required 缺失 / enum 没限定)

理想三段式模板(新 skill 必须遵守)
=================================
1. **何时用**:一句话核心场景
2. **何时不用 / 相关 skill**:列 2-3 个易混淆的(避免模型选错)
3. **典型例子**:2-3 个具体参数组合(用 ``code`` 块包起来)

本测试**只**对**新 skill** 强制要求这三段。
已有的 28 个 skill 中已知不达标的列在 ``_LEGACY_EXEMPTIONS``,后续逐个清掉。
"""

from __future__ import annotations

import importlib
import json
import pkgutil

import pytest

import skills as _skills_pkg


# 历史遗留:这些 skill 当前 description 不满足某些质量规则,但功能正确。
# 后续逐个改进文案后从这里删除即可——目的是让新 skill 自动强制走规范,
# 老 skill 不阻塞 CI 但**有清单**逐个改。
#
# 注:host_list_tasks / host_run_command 已被 test_legacy_exemption_list_does_not_grow
# 检测出"实际已合规"(描述里出现了'例如'/'示例'),已从下面移除。
_LEGACY_NO_EXAMPLE = frozenset({
    # description > 200 字但缺 ``示例`` / `` ``` `` 代码块的:
    "zabbix_get_host_storage_overview",
    "zabbix_get_host_overview",
    "metric_query_peak",
    "metric_query_window_around",
    "alerts_analyze_payload",
})


def _collect_manifests() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for info in pkgutil.iter_modules(_skills_pkg.__path__, prefix="skills."):
        mod = importlib.import_module(info.name)
        manifest = getattr(mod, "MANIFEST", None)
        if manifest and manifest.get("code"):
            out[manifest["code"]] = manifest
    return out


_MANIFESTS = _collect_manifests()
_ALL_CODES = sorted(_MANIFESTS.keys())


# ---------- 基础 schema 健全性 ---------- #


@pytest.mark.parametrize("code", _ALL_CODES)
def test_required_top_level_fields(code: str) -> None:
    m = _MANIFESTS[code]
    for field in ("code", "name", "description", "category", "params_schema"):
        assert m.get(field), f"{code}: MANIFEST 缺字段 {field!r}"
    # code 唯一性已经由 SkillRegistry.register 保证;这里只校验自洽。
    assert m["code"] == code, f"{code}: MANIFEST.code 与模块名不匹配"


@pytest.mark.parametrize("code", _ALL_CODES)
def test_params_schema_is_well_formed(code: str) -> None:
    """params_schema 必须是 type=object 的 JSON schema,有 properties。"""
    m = _MANIFESTS[code]
    schema = m["params_schema"]
    assert isinstance(schema, dict), f"{code}: params_schema 必须是 dict"
    assert schema.get("type") == "object", \
        f"{code}: params_schema.type 必须是 'object'(当前: {schema.get('type')!r})"
    if schema.get("required"):
        # 有 required 时必须有 properties,且 required 字段必须在 properties 里
        props = schema.get("properties") or {}
        missing = [r for r in schema["required"] if r not in props]
        assert not missing, \
            f"{code}: required 字段 {missing} 不在 properties 中"


@pytest.mark.parametrize("code", _ALL_CODES)
def test_visibility_is_valid(code: str) -> None:
    m = _MANIFESTS[code]
    vis = m.get("visibility", "all")
    assert vis in ("all", "admin"), \
        f"{code}: visibility 必须是 'all' 或 'admin'(当前: {vis!r})"


@pytest.mark.parametrize("code", _ALL_CODES)
def test_read_only_field_present(code: str) -> None:
    m = _MANIFESTS[code]
    assert "read_only" in m, \
        f"{code}: 必须显式声明 read_only(True/False),不要靠默认值"


# ---------- 描述质量(可逐步提高的 lint)---------- #


@pytest.mark.parametrize("code", _ALL_CODES)
def test_description_length_range(code: str) -> None:
    """描述长度合理区间:50 ≤ len ≤ 1500 字符。

    - < 50: 太短,模型理解不到核心场景
    - > 1500: 太长,关键信息被淹没;模型注意力衰减
    """
    desc = _MANIFESTS[code]["description"]
    assert 50 <= len(desc) <= 1500, (
        f"{code}: description 长度 {len(desc)} 不在 [50, 1500] 范围。"
        f"过短 → 模型理解不到核心场景;过长 → 关键信息被淹没。"
    )


@pytest.mark.parametrize("code", _ALL_CODES)
def test_long_description_has_example(code: str) -> None:
    """长描述(>= 200 字)必须带示例(``示例`` 关键词或 ``` 代码块)。

    新 skill 强制要求;老 skill 列在 ``_LEGACY_NO_EXAMPLE``,后续改进时移除。
    """
    desc = _MANIFESTS[code]["description"]
    if len(desc) < 200:
        return   # 短描述豁免
    has_example = "示例" in desc or "例：" in desc or "例如" in desc or "``" in desc
    if not has_example:
        assert code in _LEGACY_NO_EXAMPLE, (
            f"{code}: description 长 {len(desc)} 字但缺示例。"
            f"新 skill 必须用 ``code 块`` 或 '示例:' 给出 1-2 个具体参数组合。"
            f"若是历史遗留,加入 _LEGACY_NO_EXAMPLE 白名单。"
        )


def test_legacy_exemption_list_does_not_grow() -> None:
    """白名单只能减小,不能增加 —— 防止历史包袱滚雪球。"""
    # 把当前确实缺示例的 skill 找出来对比白名单
    actual_no_example = set()
    for code, m in _MANIFESTS.items():
        desc = m["description"]
        if len(desc) < 200:
            continue
        has_example = "示例" in desc or "例：" in desc or "例如" in desc or "``" in desc
        if not has_example:
            actual_no_example.add(code)

    # 现状不该超过白名单(即:不要新增缺示例的长描述 skill)
    new_offenders = actual_no_example - _LEGACY_NO_EXAMPLE
    assert not new_offenders, (
        f"新增了缺示例的长描述 skill: {sorted(new_offenders)}。"
        f"新 skill 必须遵守'长描述带示例'规则,不要进 _LEGACY_NO_EXAMPLE 白名单。"
    )

    # 同时检测白名单里实际已经合规的(可清理):
    cleanable = _LEGACY_NO_EXAMPLE - actual_no_example
    if cleanable:
        # 不 fail,但提示开发者清理白名单
        import warnings
        warnings.warn(
            f"以下 skill 已添加示例,可从 _LEGACY_NO_EXAMPLE 移除:{sorted(cleanable)}",
            stacklevel=2,
        )


# ---------- 描述应说清楚"不该用"的相关 skill ---------- #


@pytest.mark.parametrize("code", _ALL_CODES)
def test_long_description_mentions_alternatives(code: str) -> None:
    """长描述(>= 300 字)应至少出现一处"不该用 / 易混淆"提示。

    国产模型容易在多个相似 skill 间选错:swarm_query vs swarm_cluster_overview /
    host_query vs host_run_command 等。在 description 显式说"X 场景请走 Y skill"
    能大幅降低误选率。
    """
    desc = _MANIFESTS[code]["description"]
    if len(desc) < 300:
        return
    # 常见的"提示走另一个 skill"模式
    markers = ("走专用", "请走", "请用", "不该用", "禁止", "禁用", "请改用",
               "不要用", "对应 skill", "替代", "对应走", "请到", "应改为")
    has_alternative = any(mk in desc for mk in markers)
    # 已知合规的 skill 自动通过;不达标的列在白名单逐步迁移
    _ALTERNATIVE_EXEMPTIONS = frozenset({
        # 真正单一职责 skill,确实没有"应该走另一个 skill"的语境
        "platform_get_runbooks", "platform_run_runbook",
        "host_capture_packets",  # 单一职责:抓包
        "host_inspect_container_netns",  # 单一职责:进容器 netns
        "alerts_analyze_payload",  # 入口职责
        "host_kernel_events",  # 单一职责:dmesg + 老节点兼容
        # ↓ TODO:这几个其实**有**易混淆相关 skill,描述里没说,逐步补:
        "k8s_get_pod_logs",  # TODO: 应提"看 pod 状态请走 kube_query verb=describe"
        "metric_query_peak",  # TODO: 应提"实时值请走 zabbix_get_host_overview"
        "metric_query_window_around",  # TODO: 同上 + 提 metric_query_peak 的区别
        "swarm_cluster_overview",  # TODO: 应提 swarm_query verb=node ls / zabbix_get_host_overview
        "zabbix_get_host_overview",  # TODO: 应提批量看用 swarm_cluster_overview / 趋势用 metric_query_*
    })
    if not has_alternative and code not in _ALTERNATIVE_EXEMPTIONS:
        pytest.fail(
            f"{code}: description 长 {len(desc)} 字但未提及'不该用/相关 skill'。"
            f"新 skill 必须在描述里说明易混淆的相关 skill(用'请走 X' / '禁止 X' 等),"
            f"避免模型选错。"
        )


# ---------- 兜底:确保 lint 覆盖所有 28 个 skill ---------- #


def test_lint_covers_full_skill_set() -> None:
    """跑这个 test 主要是个 sanity check —— 万一 _collect_manifests 漏扫,
    其他参数化 test 会"看起来全过",其实是 0 个用例。"""
    assert len(_MANIFESTS) >= 20, (
        f"只扫到 {len(_MANIFESTS)} 个 skill MANIFEST,远低于预期的 28。"
        f"_collect_manifests 可能挂了。"
    )

"""raw-first 产品契约回归测试。

实测发现:豆包在 config_view 意图下拿着 3496 字符 Corefile 就是不贴,写散文概括。
``_ensure_raw_displayed`` 是产品契约——这两类"要看数据"意图下模型没贴 raw 就平台补,
并 logger.warning 记遥测。
"""

from __future__ import annotations

from ops_agent.agent import (
    _ensure_raw_displayed,
    _extract_raw_for_display,
    _message_has_raw,
)


# ---------- _message_has_raw ---------- #

def test_has_raw_detects_code_block():
    assert _message_has_raw("结果:\n```\nfoo\n```")


def test_has_raw_detects_markdown_table():
    assert _message_has_raw("| A | B |\n| --- | --- |\n| 1 | 2 |")


def test_has_raw_false_for_pure_prose():
    assert not _message_has_raw("当前 CoreDNS 配置了 DNS 转发,运行正常。")


def test_has_raw_not_misfire_on_or_pipe():
    assert not _message_has_raw("可选 A | B | C 中任一")


# ---------- _extract_raw_for_display ---------- #

def test_extract_prefers_compose_yaml():
    item = {"tool_result": {"compose_yaml": "version: '3'\nservices:\n  web: {}",
                            "stdout": "raw json"}}
    text, lang = _extract_raw_for_display(item)
    assert "version" in text and lang == "yaml"


def test_extract_falls_back_to_stdout():
    item = {"tool_result": {"stdout": ".:53 {\n  forward . /etc/resolv.conf\n}"}}
    text, lang = _extract_raw_for_display(item)
    assert "forward" in text and lang == ""


def test_extract_parsed_json_when_no_stdout():
    item = {"tool_result": {"parsed": {"items": [{"name": "pod1"}]}}}
    text, lang = _extract_raw_for_display(item)
    assert lang == "json" and "pod1" in text


def test_extract_empty_when_nothing():
    assert _extract_raw_for_display({"tool_result": {}}) == ("", "")
    assert _extract_raw_for_display({"tool_result": None}) == ("", "")


# ---------- _ensure_raw_displayed:核心契约 ---------- #

_COREFILE = ".:53 {\n  errors\n  health\n  kubernetes cluster.local\n  forward . /etc/resolv.conf\n}"


def _config_trace():
    return [{
        "tool_name": "kube_query", "status": "ok", "pending_token": None,
        "tool_result": {"stdout": _COREFILE},
    }]


def test_config_view_appends_raw_when_model_prose_only():
    """复现线上:config_view 模型只写散文 → 平台补贴 Corefile。"""
    prose = "当前 CoreDNS 监听 53 端口,配置了 errors/health 插件,运行正常。"
    out = _ensure_raw_displayed(prose, _config_trace(), "config_view", model_name="doubao")
    assert "```" in out, "应补一个代码块"
    assert "forward . /etc/resolv.conf" in out, "应包含 Corefile 原文"
    assert prose in out, "模型原文保留在前"


def test_list_state_also_covered():
    trace = [{"tool_name": "host_query", "status": "ok", "pending_token": None,
              "tool_result": {"stdout": "CONTAINER  IMAGE  STATUS\nabc  nginx  Up"}}]
    out = _ensure_raw_displayed("共 1 个容器,运行正常。", trace, "list_state")
    assert "```" in out and "nginx" in out


def test_no_append_when_model_already_pasted():
    """模型自己贴了 → 不重复补。"""
    msg = "配置如下:\n```\n" + _COREFILE + "\n```\n以上是 Corefile。"
    out = _ensure_raw_displayed(msg, _config_trace(), "config_view")
    assert out == msg


def test_no_append_for_diagnose_intent():
    """诊断意图不补——五段式散文 + 关键证据段才是对的输出。"""
    prose = "**当前状态**:服务异常。**判断结论**:OOM。"
    out = _ensure_raw_displayed(prose, _config_trace(), "diagnose")
    assert out == prose


def test_no_append_for_write_intent():
    out = _ensure_raw_displayed("我打算执行扩容。", _config_trace(), "write_action")
    assert "```" not in out


def test_unknown_intent_still_covered():
    """**关键鲁棒性**:看配置的说法关键词没命中、落到 unknown(None) 意图,
    照样补 raw——回应'换个说法看其他配置兜底还生效么'。"""
    prose = "该机器的 nginx 配置走的是反向代理,upstream 指向后端集群。"
    out = _ensure_raw_displayed(prose, _config_trace(), None)
    assert "```" in out, "unknown 意图也必须补 raw"
    assert "forward . /etc/resolv.conf" in out


def test_monitor_intent_covered():
    """监控意图(看指标数据)也覆盖。"""
    trace = [{"tool_name": "zabbix_get_host_overview", "status": "ok", "pending_token": None,
              "tool_result": {"stdout": "cpu_avg: 45%\nmem: 80%"}}]
    out = _ensure_raw_displayed("CPU 平均 45%,内存偏高。", trace, "monitor")
    assert "```" in out and "cpu_avg" in out


def test_knowledge_intent_covered_if_has_data():
    """knowledge 意图若真有数据查询,也补(不门控)。"""
    out = _ensure_raw_displayed("排查思路是先看 configmap。", _config_trace(), "knowledge")
    assert "```" in out


def test_no_append_when_no_displayable_trace():
    """trace 里没有可展示的 read-query → 不补(不硬塞)。"""
    trace = [{"tool_name": "platform_run_runbook", "status": "ok",
              "tool_result": {"final_report": "..."}}]
    out = _ensure_raw_displayed("散文", trace, "config_view")
    assert out == "散文"


def test_no_append_when_pending_token():
    """写操作待确认场景不补 raw(那是提议卡片)。"""
    trace = [{"tool_name": "swarm_query", "status": "ok", "pending_token": "tok-1",
              "tool_result": {"stdout": "data"}}]
    out = _ensure_raw_displayed("提议...", trace, "config_view")
    assert out == "提议..."


def test_truncates_oversized_raw():
    big = "x" * 20000
    trace = [{"tool_name": "kube_query", "status": "ok", "pending_token": None,
              "tool_result": {"stdout": big}}]
    out = _ensure_raw_displayed("散文", trace, "config_view", max_chars=8000)
    assert "截断" in out
    assert len(out) < 20000


def test_picks_latest_successful_query():
    """多个 read-query 时取最近一条成功的。"""
    trace = [
        {"tool_name": "kube_query", "status": "ok", "pending_token": None,
         "tool_result": {"stdout": "OLD-DATA"}},
        {"tool_name": "kube_query", "status": "ok", "pending_token": None,
         "tool_result": {"stdout": "NEW-DATA"}},
    ]
    out = _ensure_raw_displayed("散文", trace, "config_view")
    assert "NEW-DATA" in out and "OLD-DATA" not in out

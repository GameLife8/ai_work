"""平台层兜底机制契约:"能展示 raw 数据就先贴,不能展示的才出报告"。

设计契约
========

1. 任何 host/swarm/kube/zabbix/swarm_cluster_overview 的成功只读调用,
   如果模型回复里**没贴** raw 数据(没 ``` code block 也没 markdown 表格),
   平台**自动**从 trace 抽 stdout/parsed 拼到末尾。

2. 触发条件**不依赖**意图分类关键词命中——只看 trace 里有没有"拉数据"调用。
   关键词分类太脆弱,用户表达千变万化(扩关键词只是在污染 prompt)。

3. **报告类意图**(diagnose / write_action / async_task / knowledge / chat_intro)
   **不触发**——模型该写文字的场景不能被 raw 冲走。

4. 模型自己已经贴了(``` 或 markdown 表格分隔行)→ 不重复贴。
"""

from __future__ import annotations

import re

from ops_agent.agent import (
    _augment_message_for_intent,
    _extract_raw_for_display,
    _is_data_query_trace_item,
    _message_already_has_raw,
)


# ---------- 单元工具 ---------- #


def test_is_data_query_recognizes_all_read_skills() -> None:
    """识别成功的"拉数据"型 trace item。"""
    pos = [
        {"tool_name": "host_query", "status": "ok"},
        {"tool_name": "swarm_query", "status": "ok"},
        {"tool_name": "kube_query", "status": "ok"},
        {"tool_name": "swarm_cluster_overview", "status": "ok"},
        {"tool_name": "zabbix_get_host_overview", "status": "ok"},
        {"tool_name": "zabbix_get_host_storage_overview", "status": "ok"},
    ]
    for item in pos:
        assert _is_data_query_trace_item(item), f"应该识别为数据查询:{item}"

    neg = [
        {"tool_name": "host_query", "status": "error"},        # 失败
        {"tool_name": "swarm_scale_service", "status": "ok"},  # 写操作
        {"tool_name": "host_run_command", "status": "ok"},     # 写,审批
        {"tool_name": "platform_run_runbook", "status": "ok"}, # 高层 runbook
    ]
    for item in neg:
        assert not _is_data_query_trace_item(item), f"不该识别为数据查询:{item}"


def test_message_already_has_raw_detects_fenced_block() -> None:
    assert _message_already_has_raw("结果如下:\n```\nfoo\n```")
    assert _message_already_has_raw("```yaml\nfoo: bar\n```")


def test_message_already_has_raw_detects_markdown_table() -> None:
    msg = """
| Name | Status |
| --- | --- |
| a | running |
"""
    assert _message_already_has_raw(msg)


def test_message_already_has_raw_does_not_misfire_on_or_pipes() -> None:
    """单独的 `|` 字符不该被当 markdown 表格(很多文档用 `A | B` 表示 OR)。"""
    msg = "你可以选择 A | B | C 中任意一个"
    assert not _message_already_has_raw(msg)


def test_extract_raw_prefers_compose_yaml() -> None:
    """有 compose_yaml(swarm reverse-engineer) 时优先用它,因为对用户最熟悉。"""
    item = {
        "tool_name": "swarm_query",
        "status": "ok",
        "tool_result": {
            "compose_yaml": "version: '3'\nservices:\n  web:\n    image: nginx\n",
            "stdout": "...raw json...",
            "parsed": [{"foo": "bar"}],
        },
    }
    text, lang = _extract_raw_for_display(item)
    assert "version" in text and "nginx" in text
    assert lang == "yaml"


def test_extract_raw_falls_back_to_stdout() -> None:
    item = {
        "tool_name": "host_query",
        "status": "ok",
        "tool_result": {"stdout": "container1  nginx:1.25  Up 2 hours\n"},
    }
    text, _ = _extract_raw_for_display(item)
    assert "container1" in text


def test_extract_raw_uses_parsed_json_when_no_stdout() -> None:
    item = {
        "tool_name": "kube_query",
        "status": "ok",
        "tool_result": {"parsed": {"items": [{"name": "pod1"}, {"name": "pod2"}]}},
    }
    text, lang = _extract_raw_for_display(item)
    assert lang == "json"
    assert "pod1" in text


# ---------- 集成:_augment_message_for_intent 行为 ---------- #


def _trace(stdout="container1 nginx:1.25 Up 2 hours\ncontainer2 redis Up 3 days\n"):
    return [{
        "tool_name": "host_query",
        "status": "ok",
        "tool_args": {"node": "n1", "command": "docker ps"},
        "tool_result": {"stdout": stdout},
    }]


def test_augment_appends_raw_when_model_did_not() -> None:
    """模型没贴 raw → 平台兜底拼 raw 到末尾。"""
    user_msg = "192.168.2.58 上跑了哪些容器"   # 不命中 list_state 关键词,intent=None
    model_msg = "192.168.2.58 上共有 28 个容器,1 个 ai-ops-agent..."
    out = _augment_message_for_intent(model_msg, _trace(), user_msg)
    assert "container1 nginx" in out, "平台没贴 raw"
    assert "原始数据" in out
    # 模型原文保留
    assert "共有 28 个容器" in out


def test_augment_no_op_when_model_already_pasted_code() -> None:
    """模型自己贴 raw 了 → 平台不重复。"""
    user_msg = "192.168.2.58 上跑了哪些容器"
    model_msg = "结果如下:\n```\ncontainer1 nginx\n```"
    out = _augment_message_for_intent(model_msg, _trace(), user_msg)
    assert out == model_msg, "模型已贴 raw 平台不该重复"


def test_augment_no_op_when_model_already_used_markdown_table() -> None:
    user_msg = "看下集群节点"
    model_msg = """
| Name | Status |
| --- | --- |
| n1 | Ready |
"""
    out = _augment_message_for_intent(model_msg, _trace(), user_msg)
    assert out == model_msg


def test_augment_no_op_when_write_action_needs_confirmation() -> None:
    """trace 里有 ``needs_confirmation`` 状态的写操作 → 模型写的是审批说明,
    raw 不该插。这是按 trace 事实判定,不靠 intent 关键词。"""
    user_msg = "重启 my-app 这个 deployment"
    model_msg = "我打算执行 swarm_force_update_service,请下方点击确认。"
    trace = [
        # 模型先调了一个查询确认目标存在
        {"tool_name": "swarm_query", "status": "ok",
         "tool_args": {"category": "service", "verb": "inspect", "name": "my-app"},
         "tool_result": {"stdout": "my-app service inspect output..."}},
        # 然后调写操作,返回 needs_confirmation
        {"tool_name": "swarm_force_update_service", "status": "needs_confirmation",
         "pending_token": "abc123",
         "tool_result": {"_pending": True}},
    ]
    out = _augment_message_for_intent(model_msg, trace, user_msg)
    assert out == model_msg, "写操作 needs_confirmation 时不该被 raw 末尾插一刀"


def test_augment_no_op_when_pending_token_present() -> None:
    """即使没 needs_confirmation 状态字段,只要 pending_token 在 trace,
    也说明在审批流程里。"""
    user_msg = "看下"
    model_msg = "操作待用户确认"
    trace = [
        {"tool_name": "host_query", "status": "ok",
         "tool_result": {"stdout": "data..."}},
        {"tool_name": "host_run_command", "status": "ok",
         "pending_token": "xyz",
         "tool_result": {"_pending": True}},
    ]
    out = _augment_message_for_intent(model_msg, trace, user_msg)
    assert out == model_msg


def test_augment_works_regardless_of_keyword_match() -> None:
    """关键设计契约:augment **不依赖**关键词匹配。"""
    # 这 3 句话关键词命中各不相同(monitor / None / list_state),
    # 但 trace 都是同样的 host_query stdout —— 都应该被贴 raw
    msgs = [
        "iiot 服务为什么起不来",   # _classify_intent 误判 monitor
        "看看那台机器",            # _classify_intent 返回 None
        "都有哪些容器",            # _classify_intent 返回 list_state
    ]
    model_msg = "结果如下"
    for user_msg in msgs:
        out = _augment_message_for_intent(model_msg, _trace(), user_msg)
        assert "container1 nginx" in out, (
            f"augment 不该依赖关键词;user_msg={user_msg!r} 没贴 raw"
        )


def test_augment_no_op_without_data_trace() -> None:
    """trace 里没有"拉数据"调用 → 不触发。"""
    user_msg = "重启 my-app"
    model_msg = "已提交"
    trace = [{"tool_name": "swarm_force_update_service", "status": "ok",
              "tool_result": {"ok": True}}]
    out = _augment_message_for_intent(model_msg, trace, user_msg)
    assert out == model_msg


def test_augment_no_op_when_only_failed_data_query() -> None:
    """成功 trace 才算 —— 失败的不能被当成可贴的 raw。"""
    user_msg = "看下"
    model_msg = "出错了"
    trace = [{"tool_name": "host_query", "status": "error",
              "tool_result": {"stderr": "permission denied"}}]
    out = _augment_message_for_intent(model_msg, trace, user_msg)
    assert out == model_msg


def test_augment_truncates_huge_raw() -> None:
    """raw 超 12K 字符截断,但保留尾部 hint。"""
    huge = "x" * 20_000
    user_msg = "看下"
    model_msg = "结果如下"
    trace = [{"tool_name": "host_query", "status": "ok",
              "tool_result": {"stdout": huge}}]
    out = _augment_message_for_intent(model_msg, trace, user_msg)
    assert "已截断" in out
    assert len(out) < 15_000   # 12K raw + 头尾文字

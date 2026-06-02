"""回归测试:"docker container prune -f 请执行一下" 类用户消息必须命中
write_action 意图,并启用 ``tool_choice="required"`` 强制模型调用真实 tool 而不是
只输出文字描述。

背景
====
线上踩坑:用户提了 ``docker container prune -f`` + "请使用这条命令清理一下",
豆包模型输出了一大段"本次将在 X 节点执行 docker container prune -f..."的纯文
本描述,**但 tool_calls 为空**——平台没有 pending action,UI 下方没有 ✅/❌
确认按钮,用户卡死。

三层修复:
1. ``_INTENT_KEYWORDS["write_action"]`` 加 "请使用 / 请执行 / 清理一下 / docker prune"
   等高频"执行某条具体命令"变体
2. ``write_action`` intent 命中时,首轮 ``tool_choice="required"`` 强制模型必须
   调一个 tool(配合白名单只含写 skill + 必要 read,自然走到写 skill)
3. ``_INTENT_HINTS["write_action"]`` 重写,顶部加大字号"第一步是调用,不是描述",
   反例/正例对照说清楚
"""

from __future__ import annotations

from ops_agent.agent import _INTENT_HINTS, _INTENT_KEYWORDS, _classify_intent


# ============================================================
# Fix #1: 关键词扩展 —— 实际线上 case 必须命中
# ============================================================


def test_explicit_run_command_classified_as_write_action():
    """复现线上 case:用户给一条具体 shell 命令 + '请执行/清理' 动词"""
    cases = [
        "docker container prune -f 请使用这条命令清理一下",
        "请执行 docker system prune -af",
        "请帮我运行 systemctl restart docker",
        "请使用这条命令把僵尸容器清理一下",
        "把这些容器清理一下",
        "现在执行 rm -rf /tmp/foo",
        "马上执行下这条命令",
        "用这条命令清一下",
    ]
    for msg in cases:
        assert _classify_intent(msg) == "write_action", \
            f"消息 {msg!r} 未识别为 write_action,意图分类: {_classify_intent(msg)}"


def test_traditional_write_verbs_still_work():
    """老的写操作关键词不能因为新加的关键词被影响。"""
    assert _classify_intent("把 web 服务扩容到 5 个副本") == "write_action"
    assert _classify_intent("回滚 audit 服务到上个版本") == "write_action"
    assert _classify_intent("重新部署 codewave 服务") == "write_action"


def test_read_intent_not_misclassified_as_write():
    """`查/看` 类问句不该被误识别为 write_action。"""
    # 这些是 list_state / monitor / config_view 场景
    for msg in [
        "看下 audit-management 这个服务的配置",
        "192.168.2.60 上跑了哪些容器",
        "近一小时 CPU 趋势怎样",
        "列一下所有的 deployment",
    ]:
        intent = _classify_intent(msg)
        assert intent != "write_action", \
            f"读意图 {msg!r} 被误识别为 write_action(intent={intent})"


# ============================================================
# Fix #2: tool_choice="required" 在 ask() 首轮生效
# ============================================================


def test_ask_first_round_uses_required_when_write_intent(monkeypatch):
    """write_action 意图首轮必须传 tool_choice='required',强制模型调 tool。"""
    from unittest.mock import MagicMock
    from ops_agent.agent import UnifiedOpsAgent

    rt = MagicMock()
    rt.model_manager.get_client.return_value = MagicMock()
    rt.skill_registry = MagicMock()
    rt.skill_invoker = MagicMock()
    rt.store = MagicMock()
    rt.store.list_chat_messages.return_value = []
    rt.store.count_chat_messages.return_value = 0
    rt.store.list_prompt_segments.return_value = []
    rt.connection_manager.list.return_value = []
    rt.runbook_registry.match_by_query.return_value = None
    rt.runbook_registry.match_all_by_query.return_value = []
    # 让 registry.openai_tools 返回不为空,避免空集合干扰
    rt.skill_registry.openai_tools.return_value = [
        {"type": "function", "function": {"name": "host_run_command"}},
        {"type": "function", "function": {"name": "host_query"}},
    ]

    agent = UnifiedOpsAgent(rt, max_steps=1)
    # 模型直接返回成功调 tool,避免走到第二轮
    agent.model.create_completion = MagicMock(side_effect=[
        # 第一轮:模型调了 host_run_command
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "host_run_command",
                                      "arguments": '{"node":"x","command":"docker prune -f"}'}}]},
        # 第二轮:模型基于 envelope 写最终描述
        {"role": "assistant", "content": "已挂起待确认,请下方点击 ✅ 确认 或 ❌ 取消"},
    ])
    agent.invoker.invoke = MagicMock(return_value={
        "status": "needs_confirmation", "pending_token": "tok-1", "preview": {},
    })
    agent.invoker.serialize_for_model = lambda e: '{"status":"needs_confirmation"}'

    agent.ask(
        user_message="docker container prune -f 请执行一下",
        user={"username": "admin", "role": "admin"},
        session_id="sess-1",
    )

    # 第一次 create_completion 调用必须传 tool_choice="required"
    first_call = agent.model.create_completion.call_args_list[0]
    assert first_call.kwargs.get("tool_choice") == "required", \
        f"write_action 首轮 tool_choice 应是 'required',实际 {first_call.kwargs.get('tool_choice')}"


def test_ask_first_round_uses_auto_when_not_write_intent(monkeypatch):
    """非 write_action 意图(diagnose / unknown)首轮仍是 'auto',不强制。"""
    from unittest.mock import MagicMock
    from ops_agent.agent import UnifiedOpsAgent

    rt = MagicMock()
    rt.model_manager.get_client.return_value = MagicMock()
    rt.skill_registry = MagicMock()
    rt.skill_invoker = MagicMock()
    rt.store = MagicMock()
    rt.store.list_chat_messages.return_value = []
    rt.store.count_chat_messages.return_value = 0
    rt.store.list_prompt_segments.return_value = []
    rt.connection_manager.list.return_value = []
    rt.runbook_registry.match_by_query.return_value = None
    rt.runbook_registry.match_all_by_query.return_value = []
    rt.skill_registry.openai_tools.return_value = [
        {"type": "function", "function": {"name": "host_query"}},
        {"type": "function", "function": {"name": "kube_query"}},
    ]

    agent = UnifiedOpsAgent(rt, max_steps=1)
    agent.model.create_completion = MagicMock(return_value={
        "role": "assistant", "content": "OK",
    })

    # diagnose 意图(关键词命中"为什么")
    agent.ask(user_message="为什么这个服务起不来",
              user={"username": "u", "role": "user"}, session_id="s")
    first_call = agent.model.create_completion.call_args_list[0]
    assert first_call.kwargs.get("tool_choice") == "auto"


# ============================================================
# Fix #3: hint 文案明确"先调用,后描述"
# ============================================================


def test_write_action_hint_is_now_short_and_points_to_system_prompt():
    """write_action hint **故意**精简:完整路由表 + "首轮调 tool" 等规则统一搬到
    ``_LEGACY_SYSTEM_PROMPT`` 的 「## 写操作」段(常驻 + 受 prompt cache 保护),
    hint 只剩"提醒第二轮怎么写"的短文本。

    回归契约:
    - hint < 400 字符(防再次膨胀到 1000+ 的反例段堆栈)
    - hint **不该**有路由表示例(那已在 system prompt)
    - hint **不该**有"反例 ❌"(LLM pattern-match 反例会被污染——这是上次踩坑)
    """
    hint = _INTENT_HINTS["write_action"]
    assert len(hint) < 400, (
        f"write_action hint 膨胀到 {len(hint)} 字符——"
        f"完整路由规则应该在 system prompt,hint 只留'第二轮格式提醒'。"
    )
    # 反例段的特征:"❌ 用户:" / "❌ 你:" / "反例" header / 多个 ❌ 集中出现
    # (注意:单独的 ❌ 是按钮 emoji,不算反例段)
    assert "❌ 用户" not in hint, "hint 不该再贴反例对话——LLM 会 pattern-match 错误样式"
    assert "❌ 你" not in hint
    assert "反例" not in hint, "hint 不该再含反例段标题"
    # ❌ 出现次数应当 ≤ 1 (按钮 emoji 一次)。多个 ❌ 暗示又夹反例了。
    assert hint.count("❌") <= 1, "hint 含多个 ❌ 暗示反例段又长出来了"


def test_write_action_hint_still_anchors_round2_format():
    """虽然瘦身了,hint 仍要锁住关键第二轮契约——确认按钮提示语必须保留。"""
    hint = _INTENT_HINTS["write_action"]
    # 第二轮的核心契约:必须以这句结尾让用户看到下方按钮
    assert "确认" in hint and "取消" in hint, \
        f"hint 缺第二轮的'请下方点击 ✅ 确认 或 ❌ 取消'契约,当前:\n{hint}"
    # 必须明确点出"第二轮"或"needs_confirmation",让模型知道这条规则适用什么场景
    assert "第二轮" in hint or "needs_confirmation" in hint


def test_system_prompt_owns_routing_table():
    """完整路由规则统一在**真 system prompt**(``ops_platform.prompts`` 装配的),
    不是死代码 _LEGACY_SYSTEM_PROMPT(已删)。这是核心契约的 source of truth。"""
    from ops_platform.prompts import assemble_default
    sp = assemble_default()
    # 路由表必须在 system prompt 里
    assert "host_run_command" in sp, "system prompt 应当列写操作路由表(shell→host_run_command)"
    assert "swarm_" in sp or "swarm" in sp.lower(), "system prompt 应当提 swarm 类写 skill"
    assert "k8s_" in sp or "k8s" in sp.lower(), "system prompt 应当提 k8s 类写 skill"
    # "第一步调 tool 不要写描述" 的核心契约也应当在 system prompt
    assert "第一轮" in sp or "第一步" in sp or "首轮" in sp, \
        "system prompt 应明确'写操作首轮必须调 tool'"


def test_legacy_system_prompt_is_deleted():
    """``_LEGACY_SYSTEM_PROMPT`` 是死代码,已删除——防止有人再往里加规则(没人读)。"""
    import ops_agent.agent as agent_mod
    assert not hasattr(agent_mod, "_LEGACY_SYSTEM_PROMPT"), (
        "_LEGACY_SYSTEM_PROMPT 又出现了——它从不被 _load_system_prompt 引用,"
        "真 prompt 在 ops_platform/prompts.py。别往死代码里加规则。"
    )


def test_write_action_hint_does_not_imply_describe_first():
    """删掉了老 hint 里『必须按这个格式输出』的硬性指令——那是触发模型先描述
    后调用的根因。"""
    hint = _INTENT_HINTS["write_action"]
    # 老的『我打算执行...』模板模式不该再作为第一步指令出现
    assert "必须按这个格式输出" not in hint or "第二轮" in hint, \
        "hint 仍把『描述格式』放在第一步,可能再次触发模型只输出文字"


# ============================================================
# 综合:线上原始 case 端到端覆盖
# ============================================================


def test_real_world_case_intent_chain():
    """完整复现线上踩坑的链路:
    用户说话 → 意图识别 → 该走 write_action → 首轮 tool_choice 必为 required
    """
    real_msg = "docker container prune -f 请使用这条命令清理一下"
    assert _classify_intent(real_msg) == "write_action", \
        f"线上消息 {real_msg!r} 必须命中 write_action;否则修复无效"

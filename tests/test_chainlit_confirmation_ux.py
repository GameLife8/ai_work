"""chainlit 确认卡片 UX 契约。

历史踩坑
========
模型在 needs_confirmation 路径下生成的"待确认操作说明"很长(操作内容 + 影响范围
+ 回滚方式...),会把下方的 ``cl.AskActionMessage`` 卡片**挤到屏幕外**。用户没
看到按钮就被 5 分钟 timeout 默认放弃。

现在的设计
==========
1. chainlit ``_ask_confirmation`` 卡片顶部加"👇 点击下方按钮"强调指引
2. AskActionMessage timeout 从 300s → 1800s(30 分钟)
3. agent 在 needs_confirmation 路径下让模型只写 2-3 句**精炼提议**,
   不重写 skill/参数(那些 chainlit 卡片自己呈现),结尾强制提示按钮位置

本测试钉死这三个契约。
"""

from __future__ import annotations

import re

import chainlit_app


def test_ask_confirmation_card_emphasizes_button_at_top() -> None:
    """卡片 body 顶部必须有"点击下方按钮"指引——按钮被挤走时用户看到顶部还能找到。"""
    src = open(chainlit_app.__file__, encoding="utf-8").read()
    # 提取 _ask_confirmation 函数体
    m = re.search(r"async def _ask_confirmation\(.*?(?=\nasync def |\Z)", src, re.S)
    assert m, "找不到 _ask_confirmation 函数"
    body = m.group(0)
    # 必须含强调按钮位置的指引(emoji 或文字)
    assert ("点击下方" in body and ("✅" in body or "按钮" in body)), (
        "_ask_confirmation 卡片顶部必须有「点击下方 ✅/❌ 按钮」指引,"
        "让用户在模型说明拖长时仍能立刻找到按钮"
    )


def test_ask_confirmation_timeout_is_at_least_15_minutes() -> None:
    """timeout 至少 15 分钟——5 分钟实测太短。"""
    src = open(chainlit_app.__file__, encoding="utf-8").read()
    # AskActionMessage(..., timeout=NNNN)
    m = re.search(r"AskActionMessage\([^)]*timeout\s*=\s*(\d+)", src)
    assert m, "找不到 AskActionMessage 调用的 timeout 参数"
    timeout = int(m.group(1))
    assert timeout >= 900, f"写操作确认 timeout={timeout}s 太短,至少 900s(15 分钟)"


def test_agent_pending_path_avoids_redundant_card_content() -> None:
    """agent needs_confirmation 路径下的 prompt **不能**再让模型重写
    skill / 参数 / 影响范围 / 回滚方式(那些 chainlit 卡片本身会显示)。
    """
    # 通过 import 拿模块文件位置,跨平台兼容
    import ops_agent.agent as _agent_mod
    src = open(_agent_mod.__file__, encoding="utf-8").read()

    # 找 had_pending 之后到 return 的 prompt 段落
    m = re.search(r"if had_pending:.*?return AgentOutcome", src, re.S)
    assert m, "找不到 had_pending 分支"
    pending_block = m.group(0)

    # 旧的 prompt 让模型"说明:你打算执行什么、为什么、影响范围、回滚方式"——
    # 这是把 chainlit 卡片要展示的所有事情让模型重写一遍,正是被废弃的反模式
    bad_phrase = "影响范围、回滚方式"
    assert bad_phrase not in pending_block, (
        f"agent needs_confirmation prompt 还在让模型写「{bad_phrase}」"
        f"——这些是 chainlit 卡片该呈现的,模型重写就把按钮挤走了"
    )
    # 必须提示模型让用户去点按钮
    assert ("请在下方点击" in pending_block
            or "✅" in pending_block), (
        "prompt 应强制模型结尾提示「请在下方点击 ✅/❌」"
    )

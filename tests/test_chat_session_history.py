"""会话历史 store 方法 + chainlit 端 UX 契约。

设计契约
========

1. ``list_chat_sessions_by_user(username)`` —— 只返回 ``metadata_json.user`` 等于
   该 username 的会话(隔离不同用户的历史),按 last_message_at desc
2. ``delete_chat_session(sid)`` —— 级联删 chat_message + chat_session 行
3. ``get_chat_session(sid)`` —— 返回单条;不存在返回 None
4. InMemory 跟 SQL 两个 store 行为一致(关键 — 测试中两边都验证)
5. chainlit_app 入口对历史会话有"列 / 选 / 删 / 新"4 个 action
"""

from __future__ import annotations

import time

import pytest

from models.db import InMemoryStore


# ---------- InMemory store ---------- #


def _make_store_with_sessions() -> InMemoryStore:
    """造两个用户、各 3 个会话的 fixture。"""
    s = InMemoryStore()
    # alice 的 3 个会话,故意打散时间
    for i, sid in enumerate(["a-1", "a-2", "a-3"]):
        s.save_chat_session(sid, metadata={"user": "alice", "channel": "chainlit"})
        # 改时间制造顺序
        s.chat_sessions[sid]["last_message_at"] = f"2026-05-2{i}T10:00:00+00:00"
        if i == 0:
            s.save_chat_message(sid, "user", "alice 的第一句:看下 nginx pod")
            s.save_chat_message(sid, "assistant", "好的,我去看")
        elif i == 1:
            s.save_chat_message(sid, "user", "重启 my-deploy")
        # a-3 故意留空,验证 _short_session_title 对空会话不崩
        s.chat_sessions[sid]["last_message_at"] = f"2026-05-2{i}T10:00:00+00:00"

    # bob 的 2 个会话(用来验证隔离)
    for sid in ["b-1", "b-2"]:
        s.save_chat_session(sid, metadata={"user": "bob"})
        s.save_chat_message(sid, "user", "bob 的对话")
    return s


def test_list_sessions_by_user_filters_correctly() -> None:
    store = _make_store_with_sessions()
    alice_sessions = store.list_chat_sessions_by_user("alice")
    bob_sessions = store.list_chat_sessions_by_user("bob")
    assert {s["session_id"] for s in alice_sessions} == {"a-1", "a-2", "a-3"}
    assert {s["session_id"] for s in bob_sessions} == {"b-1", "b-2"}
    # 不能串号
    assert not any(s["session_id"].startswith("b") for s in alice_sessions)


def test_list_sessions_ordered_by_last_message_desc() -> None:
    store = _make_store_with_sessions()
    sessions = store.list_chat_sessions_by_user("alice")
    # a-3 最新(time=2026-05-22),a-1 最旧
    assert [s["session_id"] for s in sessions] == ["a-3", "a-2", "a-1"]


def test_list_sessions_unknown_user_returns_empty() -> None:
    store = _make_store_with_sessions()
    assert store.list_chat_sessions_by_user("ghost") == []


def test_delete_session_cascades_messages() -> None:
    store = _make_store_with_sessions()
    # a-1 有 2 条 message
    before = sum(1 for m in store.chat_messages if m["session_id"] == "a-1")
    assert before == 2

    removed = store.delete_chat_session("a-1")
    assert removed == 2
    # session 没了
    assert store.get_chat_session("a-1") is None
    # message 也没了
    assert not any(m["session_id"] == "a-1" for m in store.chat_messages)
    # 其他会话不受影响
    assert store.get_chat_session("a-2") is not None
    assert any(m["session_id"] == "a-2" for m in store.chat_messages)


def test_delete_unknown_session_does_not_crash() -> None:
    store = _make_store_with_sessions()
    # 不存在的 session_id —— 删 0 条 message,不抛
    removed = store.delete_chat_session("not-real")
    assert removed == 0


def test_get_chat_session_returns_metadata_dict() -> None:
    store = _make_store_with_sessions()
    s = store.get_chat_session("a-1")
    assert s["session_id"] == "a-1"
    md = s.get("metadata_json") or {}
    assert md.get("user") == "alice"
    assert md.get("channel") == "chainlit"


# ---------- chainlit_app 入口契约 ---------- #


def test_chainlit_on_chat_start_handles_history_picker() -> None:
    """关键契约:chainlit_app 必须把历史会话暴露在 ChatSettings(顶栏齿轮)里,
    而不是无脑每次 uuid4。"""
    import chainlit_app
    src = open(chainlit_app.__file__, encoding="utf-8").read()

    # 必须调 list_chat_sessions_by_user(列用户的历史会话)
    assert "list_chat_sessions_by_user" in src, "chainlit on_chat_start 没列历史会话"
    # 必须有 resume_session / delete_session 两个 ChatSettings Select
    assert "resume_session" in src, "缺少切换会话的 Select id"
    assert "delete_session" in src, "缺少删除会话的 Select id"
    # 必须有重放历史消息的逻辑
    assert "list_chat_messages" in src
    # 必须用 delete_chat_session 而不是直接动 store 内部 dict
    assert "delete_chat_session" in src


def test_chainlit_no_inline_action_button_spam() -> None:
    """Regression:不能再用 AskActionMessage + 一堆 cl.Action 在消息流里堆按钮——
    上次用户反馈说"丑"。改成 ChatSettings 顶栏齿轮里的 Select。"""
    import chainlit_app
    src = open(chainlit_app.__file__, encoding="utf-8").read()
    # 不能含 _render_history_picker(那是用按钮列表堆的旧设计)
    assert "_render_history_picker" not in src, (
        "旧的消息流按钮列表设计不该保留——改用顶栏齿轮 ChatSettings"
    )
    # 不能在 on_chat_start 里用 AskActionMessage 列会话(那是按钮堆)
    # 注意:写操作确认仍然用 AskActionMessage,所以不能完全禁,只检查 on_chat_start 段
    import re
    m = re.search(r"async def on_chat_start.*?(?=\n@|\nasync def |\Z)", src, re.S)
    assert m, "找不到 on_chat_start"
    on_chat_start_body = m.group(0)
    assert "AskActionMessage" not in on_chat_start_body, (
        "on_chat_start 不能再用 AskActionMessage 堆按钮列表"
    )


def test_chainlit_resume_session_replays_history_messages() -> None:
    """resume 函数应该真的重放历史消息到 UI(不仅仅切 session_id)。"""
    import chainlit_app
    src = open(chainlit_app.__file__, encoding="utf-8").read()
    # 找 _resume_session
    import re
    m = re.search(r"async def _resume_session\(.*?(?=\nasync def |\ndef |\Z)", src, re.S)
    assert m, "找不到 _resume_session 函数"
    body = m.group(0)
    assert "list_chat_messages" in body, "_resume_session 必须读历史 message"
    # 必须 send 至少一条消息把历史显示出来
    assert "cl.Message" in body or "Message(" in body


# ---------- SQL store:消息保存不能抹掉会话 owner(回归) ---------- #


def test_sql_save_message_does_not_wipe_session_owner(tmp_path) -> None:
    """回归(真实线上 bug):SQLStore.save_chat_message 内部会调
    ``save_chat_session(sid)`` 续命,但**绝不能**把会话 metadata(尤其
    ``metadata.user`` = owner)覆盖成 ``{}``。

    旧 bug:首条消息把 metadata_json 抹成 ``{}`` → ``list_chat_sessions_by_user``
    靠 ``metadata.$.user`` 过滤就再也查不到 → 用户**有内容**的历史会话"凭空消失",
    历史下拉里只剩没发过消息的空会话。InMemoryStore 一直是对的,只有 SQLStore
    会抹。这条测试钉死 SQL 行为跟 InMemory 对齐。
    """
    from models.db import SQLStore

    store = SQLStore(f"sqlite:///{tmp_path / 'chat.db'}")
    store.initialize()

    store.save_chat_session("s1", metadata={"user": "alice", "channel": "chainlit"})
    # 首条用户消息——内部 save_chat_session("s1") 的 metadata=None
    store.save_chat_message("s1", "user", "看下 nginx pod")
    store.save_chat_message("s1", "assistant", "好的")

    # owner 必须还在——这正是 list_chat_sessions_by_user(靠 metadata.$.user 过滤)
    # 能不能查到这条会话的关键。(注:list 查询用 MySQL 的 JSON_UNQUOTE,sqlite 跑不了,
    # 所以这里直接断言底层 metadata 没被抹,等价于"MySQL 下查得到"。)
    s = store.get_chat_session("s1")
    md = s.get("metadata_json") or {}
    assert md.get("user") == "alice", f"会话 owner 被消息保存抹掉了:{md!r}"
    assert md.get("channel") == "chainlit", "其它 metadata 字段也不该丢"


def test_sql_ensure_exists_call_preserves_metadata(tmp_path) -> None:
    """``save_chat_session(sid)`` 不带 metadata = 仅确保存在 / 续命,
    既有 metadata 必须原样保留(不被空 dict 覆盖)。"""
    from models.db import SQLStore

    store = SQLStore(f"sqlite:///{tmp_path / 'chat2.db'}")
    store.initialize()

    store.save_chat_session("s1", metadata={"user": "bob", "k": "v"})
    store.save_chat_session("s1")          # 续命:metadata=None,不能动 metadata
    md = (store.get_chat_session("s1") or {}).get("metadata_json") or {}
    assert md.get("user") == "bob"
    assert md.get("k") == "v"

    # 显式传新 metadata 时才更新
    store.save_chat_session("s1", metadata={"user": "bob", "k": "v2"})
    md2 = (store.get_chat_session("s1") or {}).get("metadata_json") or {}
    assert md2.get("k") == "v2"

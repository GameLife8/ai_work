"""host_run_command_async 的 hybrid 同步/异步行为契约。

设计契约
========

  1. ``wait_seconds=0`` → 立即返回 task_id,不轮询(纯异步路径)
  2. ``wait_seconds>0`` 且任务在该时长内完成 → 返回完整结果(stdout/stderr/exit_code),
     wait_mode='completed_inline'
  3. ``wait_seconds>0`` 但任务超时仍 running → 返回 task_id + status='running',
     wait_mode='deferred_to_async'
  4. service.submit 自身报错 → ok=False + error 字段
  5. wait_seconds 默认是 30(用户没指定时,平台主动等一会儿,符合"短任务无感"哲学)
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from skills import host_run_command_async as skill


# ---------- 桩 ---------- #


class _Service:
    """AsyncTaskService 桩,行为受 _Plan 控制。"""

    def __init__(self, plan: "_Plan") -> None:
        self._plan = plan
        self.get_calls = 0

    def submit(self, **fields) -> dict:
        if self._plan.submit_error:
            return {
                "task_id": None, "status": "error",
                "last_poll_error": self._plan.submit_error,
            }
        return {
            "task_id": "ptk_test",
            "status": "running",
            "submitted_at": "2026-05-20T00:00:00Z",
            "started_at": "2026-05-20T00:00:01Z",
            "max_runtime_sec": fields.get("max_runtime_sec", 300),
        }

    def get(self, task_id: str, *, refresh: bool = True) -> dict | None:
        self.get_calls += 1
        return self._plan.next_state()


class _Plan:
    """模拟任务跑完所需的时间(以"轮询第几次完成"表示)。"""

    def __init__(self, *,
                 finish_at_poll: int | None = None,
                 final_status: str = "done",
                 stdout: str = "/data/logs   12G\n/data/cache  4.5G\n",
                 exit_code: int = 0,
                 submit_error: str | None = None) -> None:
        self.finish_at_poll = finish_at_poll
        self.final_status = final_status
        self.stdout = stdout
        self.exit_code = exit_code
        self.submit_error = submit_error
        self._poll = 0

    def next_state(self) -> dict:
        self._poll += 1
        if self.finish_at_poll is not None and self._poll >= self.finish_at_poll:
            return {
                "task_id": "ptk_test",
                "status": self.final_status,
                "stdout": self.stdout, "stderr": "",
                "exit_code": self.exit_code,
                "duration_ms": 1234,
                "submitted_at": "2026-05-20T00:00:00Z",
                "started_at": "2026-05-20T00:00:01Z",
                "ended_at": "2026-05-20T00:00:02Z",
            }
        return {"task_id": "ptk_test", "status": "running"}


class _Runtime:
    def __init__(self, plan: _Plan) -> None:
        self.async_task_service = _Service(plan)


class _Ctx:
    def __init__(self, plan: _Plan) -> None:
        self.runtime = _Runtime(plan)
        self.user = {"username": "alice"}
        self.session_id = "sess-1"

    def resolve_connection_id(self, type_code: str, override: str | None) -> str:
        return override or "ha-default"


# ---------- 测试 ---------- #


def test_wait_zero_returns_task_id_without_polling(monkeypatch) -> None:
    """wait_seconds=0 → 纯异步,绝对不能有 sleep(否则就违背"立即返回"的契约)。"""
    # 任何 sleep 都该被避免;若不小心调了 sleep,raise 让测试明显失败
    monkeypatch.setattr(time, "sleep", lambda s: (_ for _ in ()).throw(
        AssertionError(f"wait_seconds=0 时不应有 sleep,实际 sleep({s})")
    ))

    plan = _Plan(finish_at_poll=None)   # 永不完成(模拟长任务)
    ctx = _Ctx(plan)
    out = skill.run(ctx, node="bd6", command="tcpdump -G 600 -W 1",
                    wait_seconds=0)

    assert out["ok"] is True
    assert out["task_id"] == "ptk_test"
    assert out["status"] == "running"
    assert out["wait_mode"] == "deferred_to_async"
    # 允许有一次 service.get(refresh=False) 读 DB 拿最新元信息(不调 agent,纯本地)
    # —— 但 polling loop 一次都不许进
    assert ctx.runtime.async_task_service.get_calls <= 1, (
        f"wait_seconds=0 时 service.get 应只在降级路径读一次 DB,"
        f"实际调了 {ctx.runtime.async_task_service.get_calls} 次"
    )


def test_wait_30_task_completes_quickly_returns_full_result(monkeypatch) -> None:
    """快任务:wait_seconds=30,第 2 轮 polling 就完成 → 返回完整 stdout。"""
    # 跳过真 sleep
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))

    plan = _Plan(finish_at_poll=2, stdout="/data/logs 12G\n", exit_code=0)
    ctx = _Ctx(plan)
    out = skill.run(ctx, node="bd6", command="du -xh --max-depth=1 /data",
                    wait_seconds=30)

    assert out["ok"] is True
    assert out["status"] == "done"
    assert out["exit_code"] == 0
    assert out["stdout"] == "/data/logs 12G\n"
    assert out["wait_mode"] == "completed_inline"
    assert out["duration_ms"] == 1234
    # 至少调了 1 次 sleep(第 1 轮 polling 拿到 running 后等了一下),不会无意义太长
    assert all(s <= 30 for s in sleeps), f"单次 sleep 不应超过 wait_seconds:{sleeps}"


def test_wait_30_task_too_slow_defers_to_async(monkeypatch) -> None:
    """慢任务:wait_seconds=5,任务一直 running → 超时后返回 task_id 走异步路径。"""
    # 用 fake time 加速;每次 sleep 推进虚拟时间
    base = [0.0]
    monkeypatch.setattr(time, "time", lambda: base[0])
    monkeypatch.setattr(time, "sleep", lambda s: base.__setitem__(0, base[0] + s))

    plan = _Plan(finish_at_poll=None)   # 永不完成
    ctx = _Ctx(plan)
    out = skill.run(ctx, node="bd6", command="du -sh /*", wait_seconds=5)

    assert out["ok"] is True   # 提交成功(没失败)
    assert out["task_id"] == "ptk_test"
    assert out["status"] == "running"
    assert out["wait_mode"] == "deferred_to_async"
    assert "_hint" in out and "host_check_task" in out["_hint"]


def test_submit_error_returns_failure(monkeypatch) -> None:
    monkeypatch.setattr(time, "sleep", lambda s: None)
    plan = _Plan(submit_error="agent 不通")
    ctx = _Ctx(plan)
    out = skill.run(ctx, node="bd6", command="ls /", wait_seconds=10)
    assert out["ok"] is False
    assert out["status"] == "error"
    assert "agent 不通" in (out.get("error") or "")


def test_wait_seconds_default_is_30_aligned_with_doc() -> None:
    """params_schema 默认是 30 —— 跟 description 描述的「短任务无感」哲学一致。"""
    schema = skill.MANIFEST["params_schema"]["properties"]
    assert schema["wait_seconds"]["default"] == 30


def test_task_completes_with_error_status_reported() -> None:
    """任务跑完但 exit_code != 0 → ok=False,但仍返回完整 stdout/stderr。"""
    import time as _t
    orig = _t.sleep
    try:
        _t.sleep = lambda s: None
        plan = _Plan(finish_at_poll=1, final_status="done",
                     stdout="", exit_code=1)
        ctx = _Ctx(plan)
        out = skill.run(ctx, node="bd6", command="false", wait_seconds=10)
        assert out["status"] == "done"
        assert out["exit_code"] == 1
        assert out["ok"] is False   # exit_code != 0
        assert out["wait_mode"] == "completed_inline"
    finally:
        _t.sleep = orig


def test_manifest_description_includes_wait_seconds_guidance() -> None:
    """description 必须明确告诉模型怎么选 wait_seconds(短/中/长任务)。"""
    desc = skill.MANIFEST["description"]
    assert "wait_seconds" in desc
    # 默认值 + 极端值都给指引
    assert "30" in desc
    assert "wait_seconds=0" in desc or "0(立即" in desc


def test_manifest_does_not_falsely_claim_short_commands_are_fast() -> None:
    """Regression:之前的 description 武断说『du -xh --max-depth=1 短命令 → 优先 host_query』,
    这是错的——/data 100G vs 100T 速度差千倍,模型猜不准。

    钉死:description 必须明示"耗时不可预测的命令走本 skill"。
    """
    desc = skill.MANIFEST["description"]
    # 反向断言:必须含"不可预测 / 猜不准 / 估保守 / 跟数据规模相关"中的某个,
    # 让模型理解"按命令名分类不靠谱"
    assert (
        "不可预测" in desc or "猜不准" in desc or "估保守" in desc
        or "跟数据规模相关" in desc
    ), "description 应明示「耗时不可预测的命令走本 skill」,而不是按命令名简单分类"
    # 不能直接把 du / find / journalctl 推给 host_query —— 这些命令耗时跟数据规模相关
    for cmd in ("du", "find", "journalctl"):
        # 简单 heuristic:同段(同行 / 距离 < 30 字符)出现 cmd 跟 host_query 推荐
        # 我们允许的是 cmd 后面带"用本 skill"的句子,禁止"cmd 用 host_query"
        bad_phrases = [
            f"{cmd} 用 host_query", f"{cmd} 优先 host_query",
            f"{cmd} 直接 host_query",
        ]
        for p in bad_phrases:
            assert p not in desc, (
                f"description 武断推荐 {cmd} 走 host_query,但耗时不可预测;"
                f"应该走本 skill 的 hybrid"
            )

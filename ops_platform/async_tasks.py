"""AsyncTaskService —— 异步宿主机任务的平台层服务。

为啥不直接让 skill 调 ``HostAgentClient.exec_async_on_node``
==========================================================
直接调有以下硬伤：

1. **任务只存 agent 内存**：agent 重启 / OOM / 30min GC 之后，``task_id`` 在平台
   完全没有痕迹——LLM 拿到的 ``task_id`` 变成幽灵。
2. **会话不能恢复**：用户切会话回来想看上次跑的 ``du -sh /*``，没法找回，因为只有
   当时上下文里有那个 ``task_id``。
3. **后台运维盲**：管理员在 admin UI 看不到任何异步任务，无法审计。
4. **没有跨节点状态同步**：每次查询都要现连 agent，网络抖动一次就 5xx。

解法：**DB 是 source of truth**，agent 只是无状态执行单元
------------------------------------------------------
::

    skill ──> AsyncTaskService.submit()
                ├─> 1. 先在 DB 插入 row（status=submitting，PK 由平台生成）
                ├─> 2. 调 agent /v1/exec_async（带平台 PK 作 task_id）
                ├─> 3a 成功 → 更新 DB status=running
                └─> 3b 失败 → 更新 DB status=error + last_poll_error

    AsyncTaskService.get(task_id) ─> 永远先读 DB；status=running 且 last_poll 太老就
                                     inline 调 agent 同步一次，更新 DB，返回

    后台 poller ─> 每 10s 扫 DB 里 status=running 的，按 connection_id+node 分组，
                  调对应 agent；agent 返回终态则更新 DB；agent 返 404 且任务已 running
                  > GRACE_SEC 就标 status=lost

字段对照
--------
::

    DB.platform_async_task               <->   agent 内存
    task_id                              <->   task_id（agent 1.3+ 接受调用方传入）
    status: submitting/running/done/error/timeout/cancelled/lost
    stdout / stderr / exit_code          <->   stdout / stderr / exit_code
    started_at / ended_at / duration_ms  <->   started_at / ended_at / duration_ms
    last_poll_at / last_poll_error       <->   仅 DB 端
    poll_count                           <->   仅 DB 端
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)


# ---- 常量 ------------------------------------------------------------------ #

# poller 默认轮询间隔——10s 是平衡：太短给 agent 加压力，太长用户看着不动
POLLER_INTERVAL_SEC = 10

# inline refresh：调用方 get() 时如果距上次 poll 超过此时长，就同步拉一次 agent
INLINE_REFRESH_STALE_SEC = 5

# 任务标记 LOST 的宽限期——agent 404 不能立刻判 lost，可能是 agent 刚重启还没接管
# 完毕；任务从 started_at 起经过 GRACE_LOST_SEC 还查不到才认定丢失。
GRACE_LOST_SEC = 60

# 终态——一旦进入这些 status 就不再 poll
TERMINAL_STATUSES = frozenset({"done", "error", "timeout", "cancelled", "lost"})


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _new_task_id() -> str:
    """平台侧生成 task_id；用 ``ptk_`` 前缀跟 agent 自己生成的 ``tk_`` 区分。"""
    return "ptk_" + secrets.token_urlsafe(12)


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


class AsyncTaskService:
    """异步宿主机任务的平台门面。

    skills / admin / chat 全部走这个 service，不要绕过它直接调 host_agent_client。

    构造参数
    --------
    runtime
        平台 runtime；用来拿 ``connection_manager`` 解析 host_agent client + store。
    poller_interval_sec
        后台 poll 间隔，默认 10s；测试 / 低延迟场景可调小。
    """

    def __init__(self, runtime: Any, *, poller_interval_sec: int = POLLER_INTERVAL_SEC) -> None:
        self.runtime = runtime
        self.store = runtime.store
        self.poller_interval_sec = poller_interval_sec
        self._poller_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    # ---- 公开 API：给 skill / admin 用 ----

    def submit(
        self,
        *,
        connection_id: str,
        node: str,
        command: str,
        argv: list[str],
        nsenter: str = "muinp",
        max_runtime_sec: int = 300,
        max_output_bytes: int | None = None,
        submitted_by: str | None = None,
        session_id: str | None = None,
        skill_call_id: int | None = None,
        pending_action_token: str | None = None,
    ) -> dict:
        """提交一个新任务。流程：先写 DB → 调 agent → 更新 DB。"""
        task_id = _new_task_id()
        cm = self.runtime.connection_manager
        conn_record = None
        try:
            conn_record = cm.get(connection_id) if hasattr(cm, "get") else None
        except Exception:
            conn_record = None
        conn_name = (conn_record or {}).get("name") or (conn_record or {}).get("alias")

        # 1. 先写库（status=submitting）
        rec = self.store.create_async_task(
            task_id=task_id,
            connection_id=connection_id,
            connection_name=conn_name,
            node=node,
            command=command,
            argv=argv,
            nsenter=nsenter,
            max_runtime_sec=max_runtime_sec,
            status="submitting",
            submitted_by=submitted_by,
            session_id=session_id,
            skill_call_id=skill_call_id,
            pending_action_token=pending_action_token,
            submitted_at=_now_iso(),
        )

        # 2. 调 agent；失败就把 row 标 error
        try:
            client = cm.get_client(connection_id)
        except Exception as exc:
            logger.exception("submit: 无法获取 connection %s 的 client", connection_id)
            self._mark_failure(task_id, f"connection_unavailable: {exc}")
            return self.store.get_async_task(task_id)

        if not hasattr(client, "http") or client.http is None:
            self._mark_failure(
                task_id,
                "connection transport 不是 http；异步任务仅支持 transport=http 的 host_agent",
            )
            return self.store.get_async_task(task_id)

        try:
            ip = client._node_ip(node)
        except Exception as exc:
            logger.exception("submit: node→ip 解析失败 node=%s", node)
            self._mark_failure(task_id, f"node_ip_resolve_failed: {exc}")
            return self.store.get_async_task(task_id)

        body: dict = {
            "cmd": list(argv),
            "nsenter": nsenter or "",
            "task_id": task_id,                  # 让 agent 用我们生成的 PK
            "max_runtime_sec": int(max_runtime_sec),
        }
        if max_output_bytes:
            body["max_output_bytes"] = int(max_output_bytes)

        try:
            # 直接走 _HttpExec._request，body 里塞 task_id 让 agent 用平台生成的 PK
            # （不走 exec_async 包装是因为那个接口不暴露 task_id 字段）
            status_code, data = client.http._request(
                "POST", ip, "/v1/exec_async",
                json_body=body, timeout_seconds=10,
            )
            if status_code != 201:
                err = data.get("error") if isinstance(data, dict) else None
                raise RuntimeError(f"agent submit failed: status={status_code} err={err}")
        except Exception as exc:
            logger.exception("submit: agent /v1/exec_async 失败 task=%s", task_id)
            self._mark_failure(task_id, f"agent_submit_failed: {exc}")
            return self.store.get_async_task(task_id)

        # 3. 成功 → 标 running
        self.store.update_async_task(
            task_id,
            status="running",
            started_at=data.get("started_at") or _now_iso(),
            last_poll_at=_now_iso(),
            last_poll_error=None,
        )
        logger.info(
            "async task submitted task_id=%s node=%s conn=%s by=%s",
            task_id, node, connection_id, submitted_by or "system",
        )
        return self.store.get_async_task(task_id)

    def get(self, task_id: str, *, refresh: bool = True) -> dict | None:
        """读取任务最新状态。

        ``refresh=True`` （默认）且任务还在 running、距上次 poll 超过 5s 时，
        会同步调 agent 一次更新 DB；这样模型轮询不会落后 poller 周期太多。
        """
        rec = self.store.get_async_task(task_id)
        if not rec:
            return None
        if not refresh or rec["status"] in TERMINAL_STATUSES:
            return rec
        last_poll = _parse_iso(rec.get("last_poll_at"))
        now = datetime.now(UTC)
        if last_poll and (now - last_poll).total_seconds() < INLINE_REFRESH_STALE_SEC:
            return rec
        # 落后了：同步 poll 一次
        self._poll_one(rec)
        return self.store.get_async_task(task_id)

    def cancel(self, task_id: str, *, by: str | None = None) -> dict | None:
        rec = self.store.get_async_task(task_id)
        if not rec:
            return None
        if rec["status"] in TERMINAL_STATUSES:
            return rec
        # 调 agent DELETE；agent 端会处理幂等
        try:
            client = self.runtime.connection_manager.get_client(rec["connection_id"])
            client.task_cancel_on_node(rec["node"], task_id)
        except Exception as exc:
            logger.warning("cancel: agent 端 DELETE 失败（继续标本地 cancelled）：%s", exc)
        self.store.update_async_task(
            task_id,
            status="cancelled",
            ended_at=_now_iso(),
            last_poll_at=_now_iso(),
            last_poll_error=f"cancelled by {by or 'system'}",
        )
        return self.store.get_async_task(task_id)

    def list(
        self,
        *,
        connection_id: str | None = None,
        node: str | None = None,
        status: str | None = None,
        session_id: str | None = None,
        submitted_by: str | None = None,
        limit: int = 200,
    ) -> list[dict]:
        return self.store.list_async_tasks(
            connection_id=connection_id, node=node, status=status,
            session_id=session_id, submitted_by=submitted_by, limit=limit,
        )

    def _mark_failure(self, task_id: str, error: str) -> None:
        """submit 阶段出错时调用，标 error 状态并写错误原因。"""
        self.store.update_async_task(
            task_id,
            status="error",
            ended_at=_now_iso(),
            last_poll_at=_now_iso(),
            last_poll_error=error,
        )

    # ---- 后台 poller ----

    def start_poller(self) -> None:
        """启动后台 poll 线程；幂等。"""
        if self._poller_thread and self._poller_thread.is_alive():
            return
        self._stop_event.clear()
        self._poller_thread = threading.Thread(
            target=self._poller_loop, name="async-task-poller", daemon=True,
        )
        self._poller_thread.start()
        logger.info("AsyncTaskService poller 启动；间隔 %ds", self.poller_interval_sec)

    def stop_poller(self) -> None:
        self._stop_event.set()

    def _poller_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._poll_running_once()
            except Exception:
                logger.exception("async-task-poller iteration 失败（继续）")
            self._stop_event.wait(self.poller_interval_sec)

    def _poll_running_once(self) -> int:
        """跑一轮：扫 DB 里所有 running 任务，每个调一次 agent。返回处理数。"""
        running = self.store.list_running_async_tasks(limit=500)
        if not running:
            return 0
        for rec in running:
            try:
                self._poll_one(rec)
            except Exception:
                logger.exception("poll task %s 失败", rec.get("task_id"))
        return len(running)

    def _poll_one(self, rec: dict) -> None:
        """拉一个任务的最新状态，更新 DB。"""
        task_id = rec["task_id"]
        connection_id = rec["connection_id"]
        node = rec["node"]
        try:
            client = self.runtime.connection_manager.get_client(connection_id)
            payload = client.task_get_on_node(node, task_id)
        except Exception as exc:
            self.store.update_async_task(
                task_id,
                last_poll_at=_now_iso(),
                last_poll_error=f"connection_or_agent_unreachable: {exc}",
                poll_count=(rec.get("poll_count") or 0) + 1,
            )
            return

        # agent 找不到 task（404 / task_not_found）
        if isinstance(payload, dict) and payload.get("error") == "task_not_found":
            # 是否超过宽限期？
            started = _parse_iso(rec.get("started_at"))
            if started and (datetime.now(UTC) - started).total_seconds() > GRACE_LOST_SEC:
                self.store.update_async_task(
                    task_id,
                    status="lost",
                    ended_at=_now_iso(),
                    last_poll_at=_now_iso(),
                    last_poll_error="agent 端 task_not_found 超过宽限期，标记 lost",
                    poll_count=(rec.get("poll_count") or 0) + 1,
                )
            else:
                self.store.update_async_task(
                    task_id,
                    last_poll_at=_now_iso(),
                    last_poll_error="agent 暂查不到（宽限中）",
                    poll_count=(rec.get("poll_count") or 0) + 1,
                )
            return

        # 正常返回——更新所有字段
        if not isinstance(payload, dict):
            self.store.update_async_task(
                task_id,
                last_poll_at=_now_iso(),
                last_poll_error=f"agent 返回非 dict: {type(payload).__name__}",
                poll_count=(rec.get("poll_count") or 0) + 1,
            )
            return

        agent_status = payload.get("status") or rec["status"]
        # 标准化状态：agent 端是 running/done/timeout/error/cancelled；平台多一个 lost
        # 都接受
        update = {
            "status":          agent_status,
            "exit_code":       payload.get("exit_code"),
            "stdout":          payload.get("stdout") or "",
            "stderr":          payload.get("stderr") or "",
            "truncated":       bool(payload.get("truncated")),
            "started_at":      payload.get("started_at") or rec.get("started_at"),
            "ended_at":        payload.get("ended_at"),
            "duration_ms":     payload.get("duration_ms"),
            "last_poll_at":    _now_iso(),
            "last_poll_error": None,
            "poll_count":      (rec.get("poll_count") or 0) + 1,
        }
        self.store.update_async_task(task_id, **update)

    # ---- 启动期清理：把可能存在的 orphan submitting 状态修复 ----

    def reconcile_on_startup(self) -> int:
        """启动时把上次进程崩溃留下的 submitting/running 任务做一次同步。

        - status=submitting 卡了 > 60s 的 → 标 error（说明上次没成功调到 agent）
        - status=running 的 → 走一次 poll；agent 找不到就按 lost 处理
        """
        fixed = 0
        # submitting 卡死的
        candidates = self.store.list_async_tasks(status="submitting", limit=500)
        for rec in candidates:
            submitted = _parse_iso(rec.get("submitted_at"))
            if submitted and (datetime.now(UTC) - submitted).total_seconds() > 60:
                self.store.update_async_task(
                    rec["task_id"],
                    status="error",
                    ended_at=_now_iso(),
                    last_poll_error="进程重启时 status 还在 submitting，标记 error",
                )
                fixed += 1
        # running 的同步一次
        try:
            running_count = self._poll_running_once()
        except Exception:
            logger.exception("reconcile 阶段轮询 running 任务失败")
            running_count = 0
        logger.info(
            "AsyncTaskService 启动期 reconcile：submitting 异常 %d 条，running 已同步 %d 条",
            fixed, running_count,
        )
        return fixed + running_count


def attach_async_task_service(runtime: Any, *, start_poller: bool = True) -> AsyncTaskService:
    """工厂 + 注入。``runtime.async_task_service`` 之后就有了。"""
    service = AsyncTaskService(runtime)
    runtime.async_task_service = service
    try:
        service.reconcile_on_startup()
    except Exception:
        logger.exception("AsyncTaskService reconcile 阶段失败（不阻塞启动）")
    if start_poller:
        service.start_poller()
    return service

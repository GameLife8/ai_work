"""host_run_command 三模式:同步 / 异步提交 / 轮询,以及「轮询免确认」。

留长命令能力(大道至简版):不再单独造 host_run_command_async / host_check_task —— 同一个
host_run_command:传 max_runtime_sec 走异步返回 task_id,只传 task_id 查结果(只读免确认)。
"""
from __future__ import annotations

from ops_platform.registry import SkillSpec
from ops_platform.invoker import SkillInvoker
from skills.host_run_command import run


class _Result:
    def __init__(self, *, stdout="", stderr="", returncode=0, ok=True):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.ok = ok


class _Client:
    def __init__(self):
        self.nsenter_calls = []

    def nsenter_on_node(self, node, cmd, *, namespaces=None, timeout=None):
        self.nsenter_calls.append({"node": node, "cmd": list(cmd), "ns": tuple(namespaces or ())})
        return _Result(stdout="sync-out", ok=True, returncode=0)


class _Svc:
    def __init__(self):
        self.submitted = None
        self.polled = None

    def submit(self, **kw):
        self.submitted = kw
        return {"task_id": "ptk_1", "status": "running"}

    def get(self, task_id, *, refresh=True):
        self.polled = task_id
        return {"status": "done", "stdout": "async-out", "stderr": "",
                "exit_code": 0, "node": "n1", "command": "du -sh /", "duration_ms": 1234}


class _RT:
    def __init__(self, client, svc):
        self.async_task_service = svc
        self.connection_manager = object()


class _Ctx:
    def __init__(self, client, svc):
        self.runtime = _RT(client, svc)
        self.user = {"username": "alice"}
        self.session_id = "s1"
        self._client = client

    def connection_for(self, _t, _c=None, *, node=None):
        return self._client

    def resolve_connection_id(self, _t, _c=None, *, node=None):
        return _c or "ha-1"


def test_sync_mode_runs_on_host_namespaces():
    c, s = _Client(), _Svc()
    out = run(_Ctx(c, s), node="n1", command="echo hi")
    assert out["ok"] and out["stdout"] == "sync-out"
    assert c.nsenter_calls[0]["cmd"] == ["sh", "-c", "echo hi"]
    assert c.nsenter_calls[0]["ns"] == ("m", "u", "i", "n", "p")   # 宿主机全 namespace
    assert s.submitted is None                                      # 没走异步


def test_async_mode_submits_and_returns_task_id():
    c, s = _Client(), _Svc()
    out = run(_Ctx(c, s), node="n1", command="du -sh /", max_runtime_sec=600)
    assert out["async"] is True and out["task_id"] == "ptk_1"
    assert not c.nsenter_calls                                      # 没同步跑
    assert s.submitted["node"] == "n1"
    assert s.submitted["argv"] == ["sh", "-c", "du -sh /"]
    assert s.submitted["max_runtime_sec"] == 600
    assert s.submitted["connection_id"] == "ha-1"


def test_poll_mode_reads_task_result():
    c, s = _Client(), _Svc()
    out = run(_Ctx(c, s), task_id="ptk_1")
    assert s.polled == "ptk_1"
    assert out["status"] == "done" and out["done"] is True
    assert out["stdout"] == "async-out" and out["exit_code"] == 0
    assert not c.nsenter_calls and s.submitted is None              # 纯查询,没跑命令


def test_run_without_node_or_command_errors():
    c, s = _Client(), _Svc()
    out = run(_Ctx(c, s), node="n1")        # 缺 command
    assert out["ok"] is False and "command" in out["error"]


# ---- 轮询免确认:read_only_params 机制 ----

def _spec(**over):
    base = dict(code="host_run_command", name="x", description="x", category="host",
                required_connection_type="host_agent", read_only=False, visibility="admin",
                params_schema={}, handler=lambda ctx, **k: {}, read_only_params=("task_id",))
    base.update(over)
    return SkillSpec(**base)


def test_poll_invocation_is_read_only_no_confirm():
    spec = _spec()
    # 只传 task_id = 查异步结果 → 视为只读(免确认)
    assert SkillInvoker._is_read_only_invocation(spec, {"task_id": "ptk_1"}) is True
    # 传 command = 真跑命令 → 不是只读,要确认
    assert SkillInvoker._is_read_only_invocation(spec, {"node": "n1", "command": "rm -rf /tmp/x"}) is False
    # 没声明 read_only_params 的写 skill → 永远要确认
    assert SkillInvoker._is_read_only_invocation(_spec(read_only_params=()), {"task_id": "x"}) is False

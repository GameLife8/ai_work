"""admin maintenance API:扫 + 清理残留 sibling 容器的契约测试。

设计契约
========
  1. scan 端点:遍历所有 enabled host_agent,逐节点跑 `docker ps -a`;
     每节点单独捕获错误,不阻塞其它节点
  2. image_filter 默认 'ai-ops-agent',传空 ('') 表示不过滤(看全部 stopped)
  3. cleanup 端点:按 ``(connection_id, node, container_ids)`` 批量 ``docker rm``
  4. container_ids 必须 quote 防注入(不能让用户写 ``"abc; rm -rf /"`` 拼进 sh -c)
  5. 写操作必须落审计(skill_call 表里能找到这条 admin maintenance 操作)
  6. role 校验:非 admin 不能调
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from admin_app.blueprint import _parse_ps_output, _ps_stopped_containers_cmd
from app import create_app


# ---------- helpers ---------- #


@pytest.fixture()
def client(monkeypatch):
    """Flask test client with stubbed host_agent connections + clients."""
    monkeypatch.setenv("STORE_BACKEND", "memory")
    monkeypatch.setenv("USE_STUB_AI", "true")
    monkeypatch.setenv("USE_STUB_ZABBIX", "true")
    app = create_app()
    app.config["TESTING"] = True

    # 注入 fake host_agent connection + client
    runtime = app.extensions["runtime"]
    cm = runtime.connection_manager
    cm.create(
        type_code="host_agent",
        name="fake-ha", alias="Fake HA",
        config={"kind": "swarm", "transport": "http", "docker_host": "tcp://1.2.3.4:2375"},
        is_default=False, created_by="test", tags=["fake"],
    )

    with app.test_client() as c:
        yield c, runtime


def _login(c, runtime, role: str = "admin") -> str:
    """登录拿 admin token(平台启动会自建默认 admin)。"""
    rv = c.post("/admin/api/v1/auth/login",
                json={"username": "admin", "password": "admin123"})
    assert rv.status_code == 200, rv.get_json()
    token = rv.get_json()["token"]
    return token


class _FakeResult:
    def __init__(self, stdout="", stderr="", returncode=0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class _FakeHostAgent:
    """HostAgentClient 桩。"""

    def __init__(self, *, nodes=None, exec_responses=None) -> None:
        self._nodes = nodes or []
        # exec_responses[(node, cmd_signature)] = _FakeResult or callable
        self._exec_responses = exec_responses or {}
        self.exec_calls: list[tuple[str, list[str]]] = []

    def list_nodes(self) -> list[str]:
        return list(self._nodes)

    def exec_on_node(self, node, cmd, *, timeout=None):
        self.exec_calls.append((node, cmd))
        # 取响应:按 node 匹配,然后看 cmd 是 ps 还是 rm
        joined = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
        is_ps = "docker ps" in joined
        is_rm = "docker rm" in joined
        key = (node, "ps" if is_ps else "rm" if is_rm else "other")
        resp = self._exec_responses.get(key)
        if callable(resp):
            return resp()
        if isinstance(resp, _FakeResult):
            return resp
        return _FakeResult(returncode=0)


def _inject_fake_client(runtime, fake: _FakeHostAgent) -> None:
    """把 connection_manager 缓存里 host_agent client 替换成 fake。"""
    cm = runtime.connection_manager
    ha_conns = [c for c in cm.list(type_code="host_agent")]
    assert ha_conns, "测试 fixture 必须先注入一条 host_agent connection"
    for c in ha_conns:
        cm._client_cache[c["id"]] = fake


# ---------- parse helpers ---------- #


def test_parse_ps_output_filters_by_image_substring() -> None:
    """tab 分隔输出 → list[dict];image_filter 按子串过滤。"""
    sample = "\n".join([
        "abc123\tai-ops-agent:1.3\tboring_einstein\tExited (0) 2 hours ago\t2026-05-19 11:00",
        "def456\tnginx:1.25\tweb-1\tExited (137) 1 hour ago\t2026-05-19 12:00",
        "ghi789\tregistry/ai-ops-agent@sha256:abc\thappy_fermi\tExited (0) 30 min ago\t2026-05-19 13:00",
    ])
    rows = _parse_ps_output(sample, image_filter="ai-ops-agent")
    ids = [r["container_id"] for r in rows]
    assert ids == ["abc123", "ghi789"], f"image_filter 过滤错:{ids}"
    assert rows[0]["image"] == "ai-ops-agent:1.3"
    assert rows[0]["name"] == "boring_einstein"
    assert rows[0]["status"].startswith("Exited (0)")


def test_parse_ps_output_no_filter_returns_all() -> None:
    sample = "x1\timg1\tn1\tExited\t2026-05-19\nx2\timg2\tn2\tExited\t2026-05-19"
    rows = _parse_ps_output(sample, image_filter=None)
    assert len(rows) == 2


def test_ps_cmd_uses_sh_c_with_format_string() -> None:
    cmd = _ps_stopped_containers_cmd()
    assert cmd[0] == "sh"
    assert cmd[1] == "-c"
    assert "docker ps -a" in cmd[2]
    assert "--filter status=exited" in cmd[2]
    # 必须用 tab 分隔,parser 才能正确切分
    assert "\\t" in cmd[2]


# ---------- scan endpoint ---------- #


def test_scan_requires_admin(client) -> None:
    c, runtime = client
    # 无 token
    rv = c.get("/admin/api/v1/maintenance/sibling-containers")
    assert rv.status_code == 401


def test_scan_returns_per_node_leftovers(client) -> None:
    c, runtime = client
    token = _login(c, runtime)

    fake = _FakeHostAgent(
        nodes=["node-a", "node-b"],
        exec_responses={
            ("node-a", "ps"): _FakeResult(stdout=(
                "abc123\tai-ops-agent:1.3\tn1\tExited (0) 1h ago\t2026-05-19 10:00\n"
                "def456\tnginx:1.25\twebnode\tExited (137) 2h ago\t2026-05-19 11:00\n"
            )),
            ("node-b", "ps"): _FakeResult(stdout="zzz999\tai-ops-agent:1.3\tn2\tExited (0) 5m ago\t2026-05-19 13:00\n"),
        },
    )
    _inject_fake_client(runtime, fake)

    rv = c.get("/admin/api/v1/maintenance/sibling-containers",
               headers={"Authorization": f"Bearer {token}"})
    assert rv.status_code == 200, rv.get_json()
    body = rv.get_json()

    assert body["image_filter"] == "ai-ops-agent"
    assert len(body["connections"]) == 1
    conn = body["connections"][0]
    assert conn["alias"] == "Fake HA"
    assert conn["kind"] == "swarm"
    nodes_map = {n["node"]: n for n in conn["nodes"]}

    # node-a:2 个 stopped 中只有 1 个 ai-ops-agent(image_filter 默认过滤)
    a_leftovers = nodes_map["node-a"]["leftovers"]
    assert len(a_leftovers) == 1
    assert a_leftovers[0]["container_id"] == "abc123"
    # node-b:1 个 ai-ops-agent
    b_leftovers = nodes_map["node-b"]["leftovers"]
    assert len(b_leftovers) == 1
    assert b_leftovers[0]["container_id"] == "zzz999"


def test_scan_per_node_error_isolated(client) -> None:
    """某节点 exec 抛异常,不能让整个 scan fail —— 错误塞到 nodes[].error 字段。"""
    c, runtime = client
    token = _login(c, runtime)
    fake = _FakeHostAgent(
        nodes=["healthy", "broken"],
        exec_responses={
            ("healthy", "ps"): _FakeResult(stdout="abc\tai-ops-agent:1.3\tn\tExited\t-\n"),
            ("broken", "ps"): lambda: (_ for _ in ()).throw(RuntimeError("agent 不通")),
        },
    )
    _inject_fake_client(runtime, fake)

    rv = c.get("/admin/api/v1/maintenance/sibling-containers",
               headers={"Authorization": f"Bearer {token}"})
    assert rv.status_code == 200
    nodes_map = {n["node"]: n for n in rv.get_json()["connections"][0]["nodes"]}
    assert nodes_map["healthy"]["leftovers"][0]["container_id"] == "abc"
    assert "agent 不通" in nodes_map["broken"]["error"]


def test_scan_image_filter_empty_returns_all(client) -> None:
    """image_filter='' 时不过滤,返回全部 stopped 容器。"""
    c, runtime = client
    token = _login(c, runtime)
    fake = _FakeHostAgent(
        nodes=["n1"],
        exec_responses={
            ("n1", "ps"): _FakeResult(stdout=(
                "id1\tai-ops-agent:1.3\tn1\tExited\t-\n"
                "id2\tnginx:1.25\tn2\tExited\t-\n"
            )),
        },
    )
    _inject_fake_client(runtime, fake)

    rv = c.get("/admin/api/v1/maintenance/sibling-containers?image_filter=",
               headers={"Authorization": f"Bearer {token}"})
    assert rv.status_code == 200
    leftovers = rv.get_json()["connections"][0]["nodes"][0]["leftovers"]
    assert {l["container_id"] for l in leftovers} == {"id1", "id2"}


# ---------- cleanup endpoint ---------- #


def test_cleanup_rejects_empty_items(client) -> None:
    c, runtime = client
    token = _login(c, runtime)
    rv = c.post("/admin/api/v1/maintenance/sibling-containers/cleanup",
                json={"items": []},
                headers={"Authorization": f"Bearer {token}"})
    assert rv.status_code == 400


def test_cleanup_quotes_container_ids_against_shell_injection(client) -> None:
    """关键安全契约:container_ids 必须 shlex.quote,防 ``"abc;rm -rf /"`` 注入。"""
    c, runtime = client
    token = _login(c, runtime)
    fake = _FakeHostAgent(
        nodes=["n1"],
        exec_responses={("n1", "rm"): _FakeResult(stdout="abc\nevil; rm -rf /\n")},
    )
    _inject_fake_client(runtime, fake)
    conn_id = runtime.connection_manager.list(type_code="host_agent")[0]["id"]

    evil_id = "abc; rm -rf /"
    rv = c.post("/admin/api/v1/maintenance/sibling-containers/cleanup",
                json={"items": [{"connection_id": conn_id, "node": "n1",
                                 "container_ids": ["good_id", evil_id]}]},
                headers={"Authorization": f"Bearer {token}"})
    assert rv.status_code == 200

    # 检查实际下发到 exec_on_node 的 cmd 字符串:evil_id 必须被 quote 包了引号,
    # 不能让 ``;`` 当作命令分隔符执行
    assert len(fake.exec_calls) == 1
    _, cmd = fake.exec_calls[0]
    cmd_str = cmd[2]   # sh -c <这个字符串>
    # quote 后 evil_id 应该被单引号包(或别的 quote 形式),`;` 不在 quote 外
    # 简单 check:cmd_str 里 evil_id 整体应该被 quote 包,不能是 "docker rm good_id abc; rm -rf /"
    assert "'abc; rm -rf /'" in cmd_str or '"abc; rm -rf /"' in cmd_str, (
        f"container_id 没被 quote,有 shell 注入风险:{cmd_str!r}"
    )


def test_cleanup_calls_docker_rm_and_returns_per_item_result(client) -> None:
    c, runtime = client
    token = _login(c, runtime)
    fake = _FakeHostAgent(
        nodes=["n1", "n2"],
        exec_responses={
            ("n1", "rm"): _FakeResult(stdout="abc\ndef\n", returncode=0),
            ("n2", "rm"): _FakeResult(stdout="", stderr="No such container: xyz", returncode=1),
        },
    )
    _inject_fake_client(runtime, fake)
    conn_id = runtime.connection_manager.list(type_code="host_agent")[0]["id"]

    rv = c.post("/admin/api/v1/maintenance/sibling-containers/cleanup",
                json={"items": [
                    {"connection_id": conn_id, "node": "n1", "container_ids": ["abc", "def"]},
                    {"connection_id": conn_id, "node": "n2", "container_ids": ["xyz"]},
                ]},
                headers={"Authorization": f"Bearer {token}"})
    assert rv.status_code == 200
    results = rv.get_json()["results"]
    assert len(results) == 2
    r1 = next(r for r in results if r["node"] == "n1")
    assert r1["ok"] is True
    assert r1["requested"] == ["abc", "def"]
    r2 = next(r for r in results if r["node"] == "n2")
    assert r2["ok"] is False
    assert "No such container" in r2["stderr"]


def test_cleanup_writes_audit_skill_call(client) -> None:
    c, runtime = client
    token = _login(c, runtime)
    fake = _FakeHostAgent(
        nodes=["n1"],
        exec_responses={("n1", "rm"): _FakeResult(stdout="abc")},
    )
    _inject_fake_client(runtime, fake)
    conn_id = runtime.connection_manager.list(type_code="host_agent")[0]["id"]

    rv = c.post("/admin/api/v1/maintenance/sibling-containers/cleanup",
                json={"items": [{"connection_id": conn_id, "node": "n1",
                                 "container_ids": ["abc"]}]},
                headers={"Authorization": f"Bearer {token}"})
    assert rv.status_code == 200

    # 审计表里必须有一条 admin.maintenance.cleanup_sibling_containers
    calls = runtime.store.list_skill_calls(limit=200)
    audit_rows = [r for r in calls
                  if r.get("skill_code") == "admin.maintenance.cleanup_sibling_containers"]
    assert audit_rows, "admin maintenance 操作没落审计"
    last = audit_rows[-1]
    assert last["user"] == "admin"
    # args 里有 items 详细数据
    args_raw = last.get("args_json") or last.get("args")
    args = args_raw if isinstance(args_raw, dict) else json.loads(args_raw or "{}")
    assert args["items"][0]["container_ids"] == ["abc"]


def test_cleanup_requires_admin_role(client) -> None:
    """non-admin token 不能调 cleanup(写操作)。"""
    c, runtime = client
    # 建一个普通 user
    from ops_platform.auth import hash_password
    runtime.store.create_user(
        username="bob", password_hash=hash_password("pwd"),
        role="user", display_name="Bob",
    )
    rv = c.post("/admin/api/v1/auth/login", json={"username": "bob", "password": "pwd"})
    token = rv.get_json()["token"]

    rv = c.post("/admin/api/v1/maintenance/sibling-containers/cleanup",
                json={"items": [{"connection_id": "x", "node": "y", "container_ids": ["z"]}]},
                headers={"Authorization": f"Bearer {token}"})
    assert rv.status_code == 403

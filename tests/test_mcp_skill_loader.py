"""MCPSkillLoader + ExternalMCPClient.tool_filter 行为回归测试。

不打真实 MCP server——用 FakeStore + FakeClient 模拟边界，覆盖：
  1. 工具过滤（include / exclude / mix）
  2. Skill code 安全化（含非标字符的 tool_name → 合法 [A-Za-z0-9_-]）
  3. 注册成功 / 重名兜底 / 远端失败时单 connection 隔离
  4. reload 替换：connection.skill_prefix 改变后旧 skill 被清掉
"""

from __future__ import annotations

from typing import Any

from ops_platform.mcp_skill_loader import MCPSkillLoader, _safe_skill_code
from ops_platform.registry import SkillRegistry
from services.mcp_client import ExternalMCPClient, serialize_call_result_for_model


# ---------- _safe_skill_code 单元 ---------- #

def test_safe_skill_code_replaces_unsafe_chars():
    # OpenAI function-calling 名字只允许 [A-Za-z0-9_-]
    assert _safe_skill_code("gh_", "list::repos") == "gh_list_repos"
    assert _safe_skill_code("gh_", "search.code/files") == "gh_search_code_files"


def test_safe_skill_code_strips_leading_trailing():
    # 全是非法字符 → fallback 数字串
    code = _safe_skill_code("", "@@@@")
    assert code.startswith("mcp_tool_")


# ---------- ExternalMCPClient.tool_filter ---------- #

def _client(tool_filter: str) -> ExternalMCPClient:
    return ExternalMCPClient(
        endpoint="http://x/mcp", auth_header="", timeout_seconds=10,
        tool_filter=tool_filter,
    )


def test_tool_filter_empty_includes_all():
    c = _client("")
    assert c._should_include("anything")
    assert c._should_include("debug_x")


def test_tool_filter_include_substring_match():
    c = _client("github_,gitlab_")
    assert c._should_include("github_list_repos")
    assert c._should_include("gitlab_clone")
    assert not c._should_include("debug_x")


def test_tool_filter_exclude_with_bang():
    c = _client("!debug_,!internal_")
    assert c._should_include("github_list")
    assert not c._should_include("debug_dump")
    assert not c._should_include("internal_state")


def test_tool_filter_mix_include_and_exclude():
    """同时给 include + exclude：先 include 筛，再 exclude 拒。"""
    c = _client("github_,!github_debug")
    assert c._should_include("github_list_repos")
    assert not c._should_include("github_debug_dump")
    assert not c._should_include("gitlab_anything")    # 没命中 include


# ---------- MCPSkillLoader 行为 ---------- #


class _FakeMCPClient:
    """模拟 ExternalMCPClient.list_tools。"""

    def __init__(self, tools: list[dict[str, Any]], *, raise_on_list: bool = False) -> None:
        self._tools = tools
        self._raise = raise_on_list
        self.called: list[tuple[str, dict]] = []

    def list_tools(self):
        if self._raise:
            raise RuntimeError("simulated upstream down")
        return self._tools

    def call_tool(self, name, args):
        self.called.append((name, args))
        return {"content": [{"type": "text", "text": "ok"}], "isError": False}


class _FakeStore:
    def __init__(self, connections: list[dict]) -> None:
        self._connections = connections

    def list_connections(self, *, type_code: str | None = None):
        if type_code:
            return [c for c in self._connections if c.get("type_code") == type_code]
        return list(self._connections)

    def get_connection(self, cid: str):
        return next((c for c in self._connections if c.get("id") == cid), None)


class _FakeConnMgr:
    """connection_manager 替身——把 connection.id → _FakeMCPClient 映射写死。"""

    def __init__(self, clients_by_id: dict[str, _FakeMCPClient]) -> None:
        self._map = clients_by_id

    def get_client(self, cid: str):
        if cid not in self._map:
            raise KeyError(cid)
        return self._map[cid]


class _FakeRuntime:
    def __init__(self, connections, clients) -> None:
        self.store = _FakeStore(connections)
        self.connection_manager = _FakeConnMgr(clients)
        self.skill_registry = SkillRegistry()


def test_loader_registers_tools_with_prefix():
    rt = _FakeRuntime(
        connections=[{
            "id": "c1", "type_code": "mcp_client", "name": "github", "alias": "GitHub-prod",
            "enabled": True,
            "config": {"skill_prefix": "gh_", "category": "mcp", "visibility": "admin"},
        }],
        clients={"c1": _FakeMCPClient([
            {"name": "list_repos", "description": "List repos", "inputSchema": {}},
            {"name": "search.code", "description": "Search code", "inputSchema": {}},
        ])},
    )

    loader = MCPSkillLoader(rt)
    errs = loader.reload()
    assert errs == {}

    codes = {s.code for s in rt.skill_registry.list(only_enabled=False)}
    assert "gh_list_repos" in codes
    # 含 "." 的 tool name 被安全化
    assert "gh_search_code" in codes
    # source 标记是 "mcp"
    spec = rt.skill_registry.get("gh_list_repos")
    assert spec.source == "mcp"
    assert spec.source_id == "c1"
    assert spec.category == "mcp"


def test_loader_fallback_prefix_uses_connection_alias():
    """没显式 skill_prefix 时用 alias 的 lowercase 化。"""
    rt = _FakeRuntime(
        connections=[{
            "id": "c1", "type_code": "mcp_client", "name": "x", "alias": "FileSystem-MCP",
            "enabled": True, "config": {},
        }],
        clients={"c1": _FakeMCPClient([
            {"name": "read_file", "description": "Read", "inputSchema": {}},
        ])},
    )
    MCPSkillLoader(rt).reload()
    codes = {s.code for s in rt.skill_registry.list(only_enabled=False)}
    # alias "FileSystem-MCP" → "filesystem_mcp_" + tool → filesystem_mcp_read_file
    assert "filesystem_mcp_read_file" in codes


def test_loader_handles_remote_list_failure_per_connection():
    """一个 connection list_tools 抛错时，其它仍能注册。"""
    rt = _FakeRuntime(
        connections=[
            {"id": "good", "type_code": "mcp_client", "name": "g", "alias": "good",
             "enabled": True, "config": {"skill_prefix": "g_"}},
            {"id": "bad", "type_code": "mcp_client", "name": "b", "alias": "bad",
             "enabled": True, "config": {"skill_prefix": "b_"}},
        ],
        clients={
            "good": _FakeMCPClient([{"name": "t1", "description": "", "inputSchema": {}}]),
            "bad": _FakeMCPClient([], raise_on_list=True),
        },
    )
    errs = MCPSkillLoader(rt).reload()
    assert "bad" in errs
    assert "good" not in errs
    # 好的注册成功
    codes = {s.code for s in rt.skill_registry.list(only_enabled=False)}
    assert "g_t1" in codes


def test_loader_skips_disabled_connections():
    rt = _FakeRuntime(
        connections=[{
            "id": "c1", "type_code": "mcp_client", "name": "x", "alias": "X",
            "enabled": False, "config": {"skill_prefix": "x_"},
        }],
        clients={"c1": _FakeMCPClient([{"name": "t", "description": "", "inputSchema": {}}])},
    )
    MCPSkillLoader(rt).reload()
    assert rt.skill_registry.list(only_enabled=False) == []


def test_loader_reload_replaces_old_registrations():
    """改完 connection 再 reload，旧 prefix 的 skill 应该被清掉，新 prefix 的注册上。"""
    fake_client = _FakeMCPClient([{"name": "t1", "description": "", "inputSchema": {}}])
    connection = {
        "id": "c1", "type_code": "mcp_client", "name": "x", "alias": "X",
        "enabled": True, "config": {"skill_prefix": "old_"},
    }
    rt = _FakeRuntime(connections=[connection], clients={"c1": fake_client})
    loader = MCPSkillLoader(rt)
    loader.reload()
    assert "old_t1" in {s.code for s in rt.skill_registry.list(only_enabled=False)}

    # 改 prefix → reload → 旧的应该没了，新的就位
    connection["config"]["skill_prefix"] = "new_"
    loader.reload()
    codes = {s.code for s in rt.skill_registry.list(only_enabled=False)}
    assert "new_t1" in codes
    assert "old_t1" not in codes


def test_loader_handler_invokes_remote_call_tool():
    """注册出来的 skill handler 执行时应该真的调到远端 client.call_tool。"""
    fake_client = _FakeMCPClient([{
        "name": "list_repos", "description": "", "inputSchema": {},
    }])
    rt = _FakeRuntime(
        connections=[{
            "id": "c1", "type_code": "mcp_client", "name": "x", "alias": "gh",
            "enabled": True, "config": {"skill_prefix": "gh_"},
        }],
        clients={"c1": fake_client},
    )
    MCPSkillLoader(rt).reload()
    spec = rt.skill_registry.get("gh_list_repos")

    # 构造一个最小的 ctx——只有 runtime.connection_manager 被访问
    class _Ctx:
        def __init__(self, _runtime):
            self.runtime = _runtime

    out = spec.handler(_Ctx(rt), owner="anthropics", limit=10)
    assert out["mcp_tool"] == "list_repos"
    assert out["mcp_connection_id"] == "c1"
    assert out["isError"] is False
    assert out["text"] == "ok"
    # 远端真的被调到了
    assert fake_client.called == [("list_repos", {"owner": "anthropics", "limit": 10})]


# ---------- serialize_call_result_for_model ---------- #


def test_serialize_concatenates_text_segments_and_marks_others():
    out = serialize_call_result_for_model({
        "content": [
            {"type": "text", "text": "first chunk"},
            {"type": "image", "data": "..."},
            {"type": "text", "text": "second chunk"},
            {"type": "resource", "uri": "..."},
        ],
        "isError": False,
    })
    assert "first chunk" in out
    assert "second chunk" in out
    assert "2 non-text item(s) omitted" in out


def test_serialize_truncates_oversized_output():
    huge_text = "x" * 20_000
    out = serialize_call_result_for_model(
        {"content": [{"type": "text", "text": huge_text}], "isError": False},
        max_chars=1000,
    )
    assert len(out) <= 1000 + 100
    assert "truncated to 1000" in out


def test_serialize_marks_isError():
    out = serialize_call_result_for_model({
        "content": [{"type": "text", "text": "stack trace..."}],
        "isError": True,
    })
    assert out.startswith("[MCP isError=true]")

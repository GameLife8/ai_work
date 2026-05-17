"""Swarm 通用只读查询 skill —— 一把口替代所有 docker service/node/task ls/inspect/ps/logs。

跟 kube_query 同思路：执行通用化、信号扫描独立。

支持 category × verb 组合（**只读**）
- ``service``:  ls / inspect / ps / logs
- ``node``:     ls / inspect
- ``task``:     inspect
- ``network``:  ls / inspect
- ``volume``:   ls / inspect
- ``secret``:   ls / inspect              # secret value 不会回显，只看 metadata
- ``config``:   ls / inspect
- ``stack``:    ls / services / ps

**禁止**：service create/update/scale/rm/rollback（这些是写操作，走专用 skill）。
"""

from __future__ import annotations

import shlex

from ops_platform.scanners import swarm_failed_tasks as scan_failed
from ops_platform.scanners import swarm_logs as scan_logs
from ops_platform.signals import attach


MANIFEST = {
    "code": "swarm_query",
    "name": "Swarm 通用查询",
    "description": (
        "**Swarm 只读查询的唯一入口**——任意 ``docker <category> <verb>``。"
        "示例："
        "  - 列所有服务：``category=service, verb=ls``"
        "  - 服务详情：``category=service, verb=inspect, name=mysvc``"
        "  - 失败任务：``category=service, verb=ps, name=mysvc, filters={'desired-state':'failed'}``"
        "  - 服务日志（带关键字过滤）：``category=service, verb=logs, name=mysvc, filters={'grep':'error','tail':500}``"
        "  - 列节点：``category=node, verb=ls``  /  节点详情：``category=node, verb=inspect, name=hostA``"
        "  - 看 stack：``category=stack, verb=ls`` / ``category=stack, verb=services, name=ai-ops``"
        "**写操作请走专用 skill**（swarm_scale_service / swarm_update_service_image / swarm_rollback_service / swarm_remove_service）。"
    ),
    "category": "swarm",
    "required_connection_type": "swarm",
    "read_only": True,
    "visibility": "all",
    "params_schema": {
        "type": "object",
        "properties": {
            "category": {"type": "string",
                         "enum": ["service", "node", "task", "network", "volume", "secret", "config", "stack"]},
            "verb": {"type": "string",
                     "enum": ["ls", "inspect", "ps", "logs", "services"]},
            "name": {"type": "string"},
            "output": {"type": "string",
                       "description": "json/table；默认 json（结构化好解析）。inspect 永远走 json。"},
            "filters": {"type": "object",
                        "description": "ls/ps: 透传 ``--filter k=v``；logs: 支持 ``grep`` 关键字, ``tail``, ``since``"},
            "extra_args": {"type": "array", "items": {"type": "string"}},
            "connection_id": {"type": "string"},
        },
        "required": ["category", "verb"],
    },
}


_VALID = {
    "service": {"ls", "inspect", "ps", "logs"},
    "node":    {"ls", "inspect"},
    "task":    {"inspect"},
    "network": {"ls", "inspect"},
    "volume":  {"ls", "inspect"},
    "secret":  {"ls", "inspect"},
    "config":  {"ls", "inspect"},
    "stack":   {"ls", "services", "ps"},
}

_BAD_CHARS = set(";|&`$()<>\n\r\t\"'")
_BLOCKED_FLAGS = {"-f", "--follow"}      # follow 模式会阻塞


def _safe(s: str, field: str) -> None:
    if not isinstance(s, str) or any(c in _BAD_CHARS for c in s):
        raise ValueError(f"{field} 含非法字符：{s!r}")


def _build_cmd(*, category: str, verb: str, name: str | None, output: str | None,
               filters: dict | None, extra_args: list[str] | None) -> list[str]:
    if category not in _VALID:
        raise ValueError(f"unknown category={category}")
    if verb not in _VALID[category]:
        raise ValueError(f"category={category} 不支持 verb={verb}（允许：{sorted(_VALID[category])}）")

    cmd: list[str] = [category, verb]

    # ---- stack 子命令布局 (docker stack ls/services/ps) ----
    if category == "stack":
        if verb in ("services", "ps"):
            if not name:
                raise ValueError(f"stack {verb} 需要 name (stack 名)")
            _safe(name, "name")
            cmd.append(name)
        if output == "json" or output is None:
            cmd.extend(["--format", "{{json .}}"])
        return cmd

    # ---- inspect 永远 json ----
    if verb == "inspect":
        if not name:
            raise ValueError(f"{category} inspect 需要 name")
        _safe(name, "name")
        cmd.extend([name, "--format", "{{json .}}"])
    elif verb in ("ls", "ps", "services"):
        # ps 需要 name (service 或 stack)
        if verb == "ps":
            if not name:
                raise ValueError(f"{category} ps 需要 name")
            _safe(name, "name")
            cmd.append(name)
            cmd.append("--no-trunc")
        # filters 透传
        for k, v in (filters or {}).items():
            if k in ("grep", "tail", "since"):
                continue   # 仅 logs 用
            _safe(str(k), "filter-key")
            _safe(str(v), "filter-value")
            cmd.extend(["--filter", f"{k}={v}"])
        if output == "json" or output is None:
            cmd.extend(["--format", "{{json .}}"])
    elif verb == "logs":
        # logs 用 service logs（不接管 stack/task logs）
        if category != "service":
            raise ValueError("logs 仅支持 category=service")
        if not name:
            raise ValueError("service logs 需要 name")
        _safe(name, "name")
        tail = int((filters or {}).get("tail") or 100)
        since = (filters or {}).get("since")
        cmd = ["service", "logs", name, "--tail", str(tail), "--no-trunc"]
        if since:
            _safe(str(since), "since")
            cmd.extend(["--since", str(since)])

    # extra_args 透传（禁阻塞 flag）
    for arg in (extra_args or []):
        if arg in _BLOCKED_FLAGS:
            raise ValueError(f"禁用参数：{arg}")
        if any(c in _BAD_CHARS for c in arg):
            raise ValueError(f"extra_args 含非法字符：{arg!r}")
        cmd.append(arg)
    return cmd


def _route_scanner(*, category: str, verb: str, name: str | None,
                   stdout: str, parsed) -> list[dict]:
    if not stdout:
        return []
    # service ps + filters.desired-state=failed 时跑 failed_tasks scanner
    if category == "service" and verb == "ps":
        rows = parsed if isinstance(parsed, list) else (
            [parsed] if isinstance(parsed, dict) else []
        )
        failed = []
        for r in rows:
            cs = (r.get("CurrentState") or "").lower()
            err = (r.get("Error") or "")
            if "failed" in cs or err:
                r.setdefault("ServiceName", name)
                failed.append(r)
        return scan_failed.scan(failed)
    if category == "service" and verb == "logs":
        return scan_logs.scan(name or "", stdout)
    return []


def _parse_lines(stdout: str):
    """Swarm 输出每行一个 JSON 对象（``--format {{json .}}``），合并成 list。"""
    if not stdout:
        return None
    rows = []
    try:
        import json as _json
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            rows.append(_json.loads(line))
    except Exception:
        return None
    if not rows:
        return None
    return rows[0] if len(rows) == 1 else rows


def run(
    ctx,
    *,
    category: str,
    verb: str,
    name: str | None = None,
    output: str | None = None,
    filters: dict | None = None,
    extra_args: list[str] | None = None,
    connection_id: str | None = None,
) -> dict:
    client = ctx.connection_for("swarm", connection_id)
    args = _build_cmd(category=category, verb=verb, name=name, output=output,
                      filters=filters, extra_args=extra_args)
    result = client.run(args)
    parsed = _parse_lines(result.stdout) if result.ok else None

    # logs 命令需要 grep 过滤（filters.grep）
    if category == "service" and verb == "logs" and (filters or {}).get("grep"):
        keyword = (filters or {})["grep"].lower()
        matched = [ln for ln in (result.stdout or "").splitlines() if keyword in ln.lower()]
        matched_text = "\n".join(matched[-int((filters or {}).get("tail") or 100):])
    else:
        matched_text = result.stdout

    payload = {
        "category": category, "verb": verb, "name": name,
        "command": " ".join(shlex.quote(c) for c in result.command),
        "ok": result.ok,
        "stdout": matched_text,
        "stderr": result.stderr,
        "returncode": result.returncode,
    }
    if parsed is not None:
        payload["parsed"] = parsed

    sigs = _route_scanner(category=category, verb=verb, name=name,
                          stdout=matched_text, parsed=parsed)
    return attach(payload, sigs)

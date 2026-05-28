"""Jenkins 通用只读查询 skill —— 一把口替代 list jobs / job detail / build / console / queue / nodes。

跟 ``swarm_query`` / ``kube_query`` 同思路:一个 skill 通过 ``category × verb``
组合覆盖大多数只读场景,避免给模型暴露十来个 jenkins-* 工具增加选择负担。

支持 category × verb (read-only)
================================
- ``job``:    ls / inspect              —— 列 job / 单 job 详情
- ``build``:  inspect / console         —— build 详情 / 控制台输出
- ``queue``:  ls                        —— 排队中任务
- ``node``:   ls                        —— Jenkins 节点(master + agents)

**写操作 (触发 build / 取消 / 启停 node)** 必须走专用 skill,不在本入口。

示例
====
- 列所有 job(含 lastBuild 状态)::

    category=job, verb=ls

- 单 job 详情(看 builds 列表 / health score / nextBuildNumber)::

    category=job, verb=inspect, name=audit-management

- 拉 lastBuild 详情(看 result / duration / 触发原因 / 参数)::

    category=build, verb=inspect, name=audit-management

- 指定 build 号::

    category=build, verb=inspect, name=audit-management, build_number=19

- build 控制台输出(默认拉尾部 200 行,长 build 不撑爆 context)::

    category=build, verb=console, name=audit-management, build_number=lastBuild

- 排队任务 + 节点状态::

    category=queue, verb=ls
    category=node, verb=ls

何时不该用本 skill
==================
- 想触发 build / 取消 build / 看 user 信息 → 暂未实现,走 ``host_run_command`` +
  ``curl`` 临时绕过(admin 审批),或者后续扩展专用 ``jenkins_trigger_build`` skill
- 想查 plugin 列表 / system info → 同上
"""

from __future__ import annotations

import shlex
from typing import Any


MANIFEST = {
    "code": "jenkins_query",
    "name": "Jenkins 通用查询",
    "description": (
        "**Jenkins 只读查询的唯一入口**——任意 ``category × verb`` 组合覆盖 job/build/queue/node。"
        "示例:"
        "  - 列所有 job(含 lastBuild)::``category=job, verb=ls``"
        "  - 单 job 详情:``category=job, verb=inspect, name=audit-management``"
        "  - 最新 build 详情:``category=build, verb=inspect, name=audit-management``"
        "  - 指定 build:``category=build, verb=inspect, name=audit-management, build_number=19``"
        "  - 控制台输出(尾部 200 行):``category=build, verb=console, name=audit-management``"
        "  - 排队任务:``category=queue, verb=ls``"
        "  - 节点状态:``category=node, verb=ls``"
        "**何时不用**:触发 build / 改配置等写操作目前请走 ``host_run_command`` + curl(admin 审批);"
        "巡检流程(每日 build 失败统计)请直接调多次 ls + filter,不要在 prompt 里硬编码。"
        "**folder 嵌套 job 名**用 ``/`` 分隔,如 ``app/sub-job``。"
    ),
    "category": "automation",
    "required_connection_type": "jenkins",
    "read_only": True,
    "visibility": "all",
    "params_schema": {
        "type": "object",
        "properties": {
            "category": {"type": "string",
                         "enum": ["job", "build", "queue", "node"]},
            "verb": {"type": "string",
                     "enum": ["ls", "inspect", "console"]},
            "name": {"type": "string",
                     "description": "job 名;folder 嵌套用 ``/`` 分隔,如 ``app/sub-job``"},
            "build_number": {"type": ["integer", "string"],
                             "description": "build 编号 int,或 'lastBuild' / "
                                            "'lastSuccessfulBuild' / 'lastFailedBuild'"},
            "tree": {"type": "string",
                     "description": "可选;Jenkins ?tree= 字段裁剪(省 token),"
                                    "如 ``builds[number,result,timestamp]``"},
            "depth": {"type": "integer",
                      "description": "可选;Jenkins ?depth=,展开嵌套层数(folder 递归)"},
            "tail_lines": {"type": "integer", "default": 200,
                           "description": "verb=console 时返回尾部 N 行,防 context 溢出;"
                                          "默认 200,要全文设 0"},
            "connection_id": {"type": "string"},
        },
        "required": ["category", "verb"],
    },
}


_VALID = {
    "job":   {"ls", "inspect"},
    "build": {"inspect", "console"},
    "queue": {"ls"},
    "node":  {"ls"},
}


def _validate(category: str, verb: str, name: str | None,
              build_number: int | str | None) -> str | None:
    """校验参数组合合法。返回 None 表示 OK,否则返回错误描述。"""
    if category not in _VALID:
        return f"unknown category={category}(允许:{sorted(_VALID)})"
    if verb not in _VALID[category]:
        return (f"category={category} 不支持 verb={verb}"
                f"(允许:{sorted(_VALID[category])})")
    if category in ("job", "build"):
        if verb in ("inspect", "console") and not name:
            return f"{category} {verb} 需要 name"
    return None


def _format_job_summary(job: dict) -> dict:
    """从 list_jobs 的单 job 提关键字段——给模型一个清爽的 raw。"""
    last = job.get("lastBuild") or {}
    return {
        "name": job.get("name"),
        "url": job.get("url"),
        "type": (job.get("_class") or "").rsplit(".", 1)[-1],   # WorkflowJob / Folder / etc
        "color": job.get("color"),    # blue / red / yellow / disabled / aborted / notbuilt
        "last_build_number": last.get("number"),
        "last_build_result": last.get("result"),
        "last_build_timestamp": last.get("timestamp"),
    }


def _summarize_console_failure(text: str, *, max_evidence: int = 30) -> dict:
    """扫 console 找失败证据——给模型快速定位用。"""
    if not text:
        return {}
    lines = text.splitlines()
    # 经典错误关键字(stacktrace / shell error / Jenkins build phase fail)
    # 同时覆盖 Maven 风格的 ``[ERROR]`` / ``[FATAL]`` 括号标记
    markers = (
        "ERROR:", "ERROR ", "[ERROR]",
        "FATAL:", "FATAL ", "[FATAL]",
        "FAILURE:", "FAILURE ", "FAILED",
        "Error:", "error:",
        "Exception", "Traceback",
        "Build step", "exit code",
        "Permission denied", "No such file",
    )
    hits = []
    for i, line in enumerate(lines):
        if any(m in line for m in markers):
            hits.append({"line": i + 1, "text": line[:200]})
            if len(hits) >= max_evidence:
                break
    return {"error_hits": hits, "total_lines": len(lines)}


def run(
    ctx,
    *,
    category: str,
    verb: str,
    name: str | None = None,
    build_number: int | str | None = None,
    tree: str | None = None,
    depth: int | None = None,
    tail_lines: int | None = 200,
    connection_id: str | None = None,
) -> dict:
    err = _validate(category, verb, name, build_number)
    if err:
        return {
            "ok": False,
            "category": category, "verb": verb,
            "error": err,
        }

    client = ctx.connection_for("jenkins", connection_id)
    payload: dict[str, Any] = {
        "category": category, "verb": verb, "name": name,
    }

    if category == "job" and verb == "ls":
        r = client.list_jobs(depth=depth or 0, tree=tree)
        if r.ok and isinstance(r.data, dict):
            jobs = r.data.get("jobs", [])
            payload["jobs"] = [_format_job_summary(j) for j in jobs]
            payload["total"] = len(jobs)
            # 异常 job 数量统计(color != blue / disabled / notbuilt 视为可能要看)
            abnormal = [j for j in payload["jobs"]
                        if j.get("color") in ("red", "red_anime", "yellow",
                                              "yellow_anime", "aborted")]
            payload["abnormal_count"] = len(abnormal)
            payload["abnormal_names"] = [j["name"] for j in abnormal[:20]]

    elif category == "job" and verb == "inspect":
        r = client.get_job(name, tree=tree)
        if r.ok:
            payload["job"] = r.data

    elif category == "build" and verb == "inspect":
        r = client.get_build(name, build_number or "lastBuild", tree=tree)
        if r.ok:
            payload["build"] = r.data

    elif category == "build" and verb == "console":
        tail = None if (tail_lines is not None and tail_lines <= 0) else (tail_lines or 200)
        r = client.get_console(name, build_number or "lastBuild", tail=tail)
        if r.ok:
            console = r.data if isinstance(r.data, str) else ""
            payload["console"] = console
            payload["scanner"] = _summarize_console_failure(console)

    elif category == "queue" and verb == "ls":
        r = client.list_queue()
        if r.ok and isinstance(r.data, dict):
            items = r.data.get("items", [])
            payload["queue_items"] = items
            payload["queue_size"] = len(items)
            payload["stuck_count"] = sum(1 for x in items if x.get("stuck"))

    elif category == "node" and verb == "ls":
        r = client.list_nodes()
        if r.ok and isinstance(r.data, dict):
            nodes = r.data.get("computer", [])
            payload["nodes"] = [{
                "name": n.get("displayName"),
                "offline": n.get("offline"),
                "temporarily_offline": n.get("temporarilyOffline"),
                "num_executors": n.get("numExecutors"),
                "offline_cause": (n.get("offlineCause") or {}).get("description")
                                 if n.get("offline") else None,
            } for n in nodes]
            payload["node_count"] = len(nodes)
            payload["offline_count"] = sum(1 for n in nodes if n.get("offline"))

    else:   # pragma: no cover —— _validate 已经拦掉
        return {"ok": False, "error": f"unhandled ({category},{verb})"}

    payload["ok"] = r.ok
    payload["status_code"] = r.status_code
    payload["url"] = r.url
    if not r.ok:
        payload["error"] = r.error
    return payload

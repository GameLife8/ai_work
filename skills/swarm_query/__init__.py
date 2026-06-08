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
        "**Swarm 只读查询的唯一入口**——任意 ``docker <category> <verb>``。示例：\n"
        "  - 列服务 ``category=service, verb=ls``；详情 ``verb=inspect, name=mysvc``\n"
        "  - 失败任务 ``verb=ps, name=mysvc, filters={'desired-state':'failed'}``\n"
        "  - 服务日志 ``verb=logs, name=mysvc, filters={'grep':'error','tail':500}``\n"
        "  - 节点 ``category=node, verb=ls/inspect``；stack ``category=stack, verb=ls/services``\n"
        "注：Swarm 不存原始 compose.yml，问『stack 配置 / compose』时返回里有从 Spec 反推的 "
        "``compose_yaml`` 字段（优先贴，并说明是反推的等价 YAML）。"
        "**写操作请走专用 skill**（swarm_scale_service / swarm_update_service_image / "
        "swarm_rollback_service / swarm_remove_service）。"
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


def _spec_to_compose_yaml(spec_entry) -> str | None:
    """把 ``docker service inspect`` 的输出反推成 compose.yml 风格 YAML。

    背景
    ----
    Docker Swarm **不保存**用户部署用的原始 compose.yml，只保留翻译过的
    Service Spec。用户问"stack 配置 / compose.yml"时，原始文件已经丢了，能拿
    到的最接近的就是 Spec。

    本函数把 Spec 反推回 compose v3.x 风格的 YAML——
    用户阅读习惯最熟悉，比直接看 raw JSON 友好得多。

    覆盖的字段：image / replicas / labels / placement / networks / ports /
    env / mounts / healthcheck / resources（limits + reservations）/
    restart_policy / update_config / rollback_config / configs / secrets。

    Args:
        spec_entry: 单个 service inspect 的 dict（``docker service inspect``
            通常返回 list[dict]，调用方传第一项）。

    Returns:
        compose 风格 YAML 字符串；解析失败返回 None。
    """
    if not isinstance(spec_entry, dict):
        return None
    spec = (spec_entry.get("Spec") or {})
    if not spec:
        return None
    task = spec.get("TaskTemplate", {}) or {}
    cspec = task.get("ContainerSpec", {}) or {}
    endpoint = spec.get("EndpointSpec", {}) or {}

    svc: dict = {}
    # 镜像
    if cspec.get("Image"):
        svc["image"] = cspec["Image"]
    # 命令 / 入口
    if cspec.get("Command"):
        svc["entrypoint"] = cspec["Command"]
    if cspec.get("Args"):
        svc["command"] = cspec["Args"]
    # 工作目录、用户
    if cspec.get("Dir"):
        svc["working_dir"] = cspec["Dir"]
    if cspec.get("User"):
        svc["user"] = cspec["User"]
    if cspec.get("Hostname"):
        svc["hostname"] = cspec["Hostname"]
    # 环境变量
    if cspec.get("Env"):
        svc["environment"] = list(cspec["Env"])
    # 挂载
    mounts = cspec.get("Mounts") or []
    if mounts:
        compose_mounts = []
        for m in mounts:
            t = m.get("Type", "volume")
            src = m.get("Source", "")
            tgt = m.get("Target", "")
            ro = m.get("ReadOnly", False)
            if t == "bind":
                compose_mounts.append(f"{src}:{tgt}" + (":ro" if ro else ""))
            else:
                compose_mounts.append({
                    "type": t, "source": src, "target": tgt,
                    **({"read_only": True} if ro else {}),
                })
        svc["volumes"] = compose_mounts
    # 端口
    if endpoint.get("Ports"):
        compose_ports = []
        for p in endpoint["Ports"]:
            mode = p.get("PublishMode", "ingress")
            proto = p.get("Protocol", "tcp")
            published = p.get("PublishedPort")
            target = p.get("TargetPort")
            if published and target:
                if mode == "host" or proto != "tcp":
                    compose_ports.append({
                        "target": target, "published": published,
                        "protocol": proto, "mode": mode,
                    })
                else:
                    compose_ports.append(f"{published}:{target}")
            elif target:
                compose_ports.append(f"{target}")
        if compose_ports:
            svc["ports"] = compose_ports
    # 网络
    nets = task.get("Networks") or []
    if nets:
        compose_nets = []
        for n in nets:
            # 取 NetworkID 不友好，但有 Target/Aliases 字段时给到
            entry = {}
            if n.get("Aliases"):
                entry["aliases"] = list(n["Aliases"])
            target = n.get("Target") or n.get("NetworkID", "")
            if entry:
                compose_nets.append({target: entry})
            else:
                compose_nets.append(target)
        if compose_nets:
            svc["networks"] = compose_nets
    # 健康检查
    hc = cspec.get("Healthcheck") or {}
    if hc:
        compose_hc = {}
        if hc.get("Test"):
            compose_hc["test"] = list(hc["Test"])
        for k_src, k_dst in (("Interval", "interval"), ("Timeout", "timeout"),
                              ("Retries", "retries"), ("StartPeriod", "start_period")):
            v = hc.get(k_src)
            if v:
                # docker 用纳秒，compose 用 1m30s 这种字符串；简化成 ns 显示
                compose_hc[k_dst] = v
        if compose_hc:
            svc["healthcheck"] = compose_hc
    # labels（容器和服务两份都合并）
    cls = cspec.get("Labels") or {}
    sls = spec.get("Labels") or {}
    merged_labels = {**cls, **sls}
    if merged_labels:
        svc["labels"] = dict(merged_labels)
    # 资源限制
    resources = task.get("Resources", {}) or {}
    limits = resources.get("Limits", {}) or {}
    reservations = resources.get("Reservations", {}) or {}
    deploy: dict = {}
    if limits or reservations:
        compose_res = {}
        if limits:
            lc = {}
            if limits.get("NanoCPUs"):
                lc["cpus"] = f"{limits['NanoCPUs'] / 1e9:.3f}"
            if limits.get("MemoryBytes"):
                lc["memory"] = f"{limits['MemoryBytes']}"
            if lc:
                compose_res["limits"] = lc
        if reservations:
            rc = {}
            if reservations.get("NanoCPUs"):
                rc["cpus"] = f"{reservations['NanoCPUs'] / 1e9:.3f}"
            if reservations.get("MemoryBytes"):
                rc["memory"] = f"{reservations['MemoryBytes']}"
            if rc:
                compose_res["reservations"] = rc
        if compose_res:
            deploy["resources"] = compose_res
    # 副本数 / 模式
    mode = spec.get("Mode", {}) or {}
    if mode.get("Replicated", {}).get("Replicas") is not None:
        deploy["replicas"] = mode["Replicated"]["Replicas"]
        deploy["mode"] = "replicated"
    elif "Global" in mode:
        deploy["mode"] = "global"
    # 调度约束 / placement
    placement = task.get("Placement", {}) or {}
    if placement:
        cp: dict = {}
        if placement.get("Constraints"):
            cp["constraints"] = list(placement["Constraints"])
        if placement.get("Preferences"):
            cp["preferences"] = list(placement["Preferences"])
        if placement.get("Platforms"):
            cp["platforms"] = list(placement["Platforms"])
        if cp:
            deploy["placement"] = cp
    # 重启策略
    rp = task.get("RestartPolicy", {}) or {}
    if rp:
        compose_rp = {}
        for k_src, k_dst in (("Condition", "condition"), ("Delay", "delay"),
                              ("MaxAttempts", "max_attempts"), ("Window", "window")):
            v = rp.get(k_src)
            if v is not None:
                compose_rp[k_dst] = v
        if compose_rp:
            deploy["restart_policy"] = compose_rp
    # 更新 / 回滚策略
    for sk, dk in (("UpdateConfig", "update_config"),
                    ("RollbackConfig", "rollback_config")):
        cfg = spec.get(sk, {}) or {}
        if cfg:
            cc = {}
            for k_src, k_dst in (("Parallelism", "parallelism"), ("Delay", "delay"),
                                  ("FailureAction", "failure_action"),
                                  ("Monitor", "monitor"),
                                  ("MaxFailureRatio", "max_failure_ratio"),
                                  ("Order", "order")):
                v = cfg.get(k_src)
                if v is not None:
                    cc[k_dst] = v
            if cc:
                deploy[dk] = cc
    if deploy:
        svc["deploy"] = deploy

    # 拼成 compose v3.x 顶层结构
    svc_name = spec.get("Name", "service") or "service"
    # 拿掉 stack 前缀，更接近用户写的 compose（``haitu_web`` → ``web``）
    parts = svc_name.split("_", 1)
    base_name = parts[1] if len(parts) == 2 else svc_name
    compose = {
        "version": "3.8",
        "services": {base_name: svc},
    }
    try:
        import yaml as _yaml
        return _yaml.safe_dump(compose, allow_unicode=True, sort_keys=False, default_flow_style=False)
    except Exception:
        # yaml 缺失或序列化失败时降级到 JSON-as-YAML
        import json as _json
        return _json.dumps(compose, ensure_ascii=False, indent=2)


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

    # service inspect 时把 Spec 反推成 compose.yml 风格 YAML，方便用户读
    # 这是因为 Docker Swarm 不存原始 compose.yml，用户问 "stack 配置" 时
    # 最接近的是 service inspect，但 raw JSON 太难读——反推出来的 compose
    # 风格 YAML 是大家最熟悉的格式。
    if category == "service" and verb == "inspect" and parsed is not None:
        try:
            spec_entry = parsed[0] if isinstance(parsed, list) and parsed else parsed
            compose_yaml = _spec_to_compose_yaml(spec_entry)
            if compose_yaml:
                payload["compose_yaml"] = compose_yaml
                payload["compose_note"] = (
                    "Docker Swarm 不保存 `docker stack deploy` 用的原始 compose.yml。"
                    "上面的 compose_yaml 是平台从运行时 Service Spec **反推**出来的等价 YAML，"
                    "字段语义跟原 compose 一致，但格式可能跟用户原始文件有微小差异。"
                )
        except Exception:
            pass  # 反推失败不阻塞主流程，留 stdout/parsed 给模型

    sigs = _route_scanner(category=category, verb=verb, name=name,
                          stdout=matched_text, parsed=parsed)
    return attach(payload, sigs)

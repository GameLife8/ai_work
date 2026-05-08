"""默认 HTTP Skill 种子。

平台首启时如果 ``platform_http_skill`` 表为空，把这些 seed 进去（默认 disabled）。
admin 在后台可以按需启用、改写。

每条 seed 配套要有一份对应类型的 ``http_api`` connection：
  - zabbix_jsonrpc → 一个 base_url 指向 ``/zabbix/api_jsonrpc.php``、
    auth_kind=bearer 的 http_api connection（Zabbix 6.0+ 在 admin UI 里
    生成 API token 直接用）
"""

from __future__ import annotations

import logging
from typing import Any


logger = logging.getLogger(__name__)


# 通用 Zabbix JSON-RPC 调用——架构统一示例。
# 你想看 host.get / item.get / history.get / trigger.get / event.get 任何 Zabbix
# 标准 method 都用这一个 skill：模型只需提供 method 和 params 两个字段。
ZABBIX_JSONRPC: dict[str, Any] = {
    "code": "zabbix_jsonrpc",
    "name": "Zabbix JSON-RPC（任意 method）",
    "description": (
        "通用 Zabbix JSON-RPC 调用。**单次调用对应 Zabbix 任意 method**——"
        "host.get / item.get / history.get / trigger.get / event.get / problem.get 等。"
        "适合 admin 已经熟悉 Zabbix API 的场景。"
        "需要一个 type=http_api 的 connection（base_url 指向 /zabbix/api_jsonrpc.php，"
        "auth_kind=bearer，token 在 Zabbix UI 里 Administration → User → API tokens 生成）。"
        "返回里 ``$.result`` 是真实数据；``$.error`` 出现说明 method/params 错。"
        "**适合**：临时取一个原始 Zabbix 数据、admin 自定义查询。"
        "**不适合**：复合诊断（多步 RPC + 聚合）——那种用 zabbix_get_host_overview 这种 Python skill 或 runbook。"
    ),
    "category": "zabbix",
    "connection_id": None,                 # admin 后台保存时填实际 http_api connection
    "read_only": True,
    "visibility": "all",

    "method": "POST",
    "path": "",                            # connection.base_url 已经是完整 RPC endpoint
    "headers_template": {
        "Content-Type": "application/json-rpc",
    },
    "body_template": (
        '{"jsonrpc": "2.0", '
        '"method": "{{ method }}", '
        '"params": {{ params | tojson if params else "{}" }}, '
        '"id": 1}'
    ),
    "params_schema": {
        "type": "object",
        "required": ["method"],
        "properties": {
            "method": {
                "type": "string",
                "description": (
                    "Zabbix API method 名，如 host.get / item.get / history.get / "
                    "problem.get。完整列表见 Zabbix 官方文档。"
                ),
            },
            "params": {
                "type": "object",
                "description": (
                    "method 对应的 params 对象。例：host.get → "
                    "{output: ['hostid','host','name'], filter: {host: ['169.24.2.82']}}"
                ),
                "default": {},
            },
        },
    },
    "response_extract": {
        "result": "$.result",
        "error_code": "$.error.code",
        "error_data": "$.error.data",
        "error_message": "$.error.message",
    },
    "signal_rules": [
        {
            "when": {"type": "response_field", "path": "$.error.code", "exists": True},
            "emit": {
                "type": "zabbix_rpc_error",
                "severity": "warning",
                "evidence": "Zabbix RPC 返回 error.code={{ response.error.code }}：{{ response.error.message }}",
            },
        },
        {
            "when": {"type": "http_status", "eq": 401},
            "emit": {
                "type": "auth_failure",
                "severity": "critical",
                "evidence": "Zabbix 返回 401，API token 失效；到 Zabbix UI 重新生成。",
            },
        },
    ],
    "timeout_seconds": 30,
    "retry": 0,
    "enabled": False,                      # 默认关闭——admin 需先建好 http_api connection 再启用
}


SEEDS: list[dict[str, Any]] = [ZABBIX_JSONRPC]


def seed_default_http_skills(store) -> int:
    """如果 ``platform_http_skill`` 表为空，把 seeds 写进去（默认 disabled）。

    幂等：表非空时直接跳过。Admin 改过的不会被覆盖。
    """
    try:
        existing = store.list_http_skills()
    except Exception as exc:
        logger.warning("seed http skills: list_http_skills 失败，跳过：%s", exc)
        return 0
    if existing:
        return 0
    n = 0
    for seed in SEEDS:
        try:
            store.upsert_http_skill(
                code=seed["code"],
                title=seed.get("name") or seed["code"],
                description=seed.get("description", ""),
                category=seed.get("category", "integration"),
                connection_id=seed.get("connection_id"),
                definition=seed,
                enabled=bool(seed.get("enabled", False)),  # 默认 disabled
                updated_by="bootstrap",
            )
            n += 1
        except Exception:  # pragma: no cover
            logger.exception("seed http skill %s 失败", seed.get("code"))
    if n:
        logger.info("seed http skills: 写入 %d 条默认（默认 disabled，admin 启用前先建对应 http_api connection）", n)
    return n

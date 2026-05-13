from __future__ import annotations

import json

MANIFEST = {
    "code": "alerts_analyze_payload",
    "name": "告警 payload 预分析",
    "description": (
        "对一段原始告警 payload（Zabbix webhook / Prometheus AlertManager / 自定义 JSON 格式）做"
        "**结构化解析**：识别告警类型、severity、affected_host、affected_service，生成「需要补哪些"
        "上下文数据」的 plan，并给出推荐下一步 skill。\n"
        "**典型场景**：\n"
        "(1) 用户把一段 webhook / 告警 JSON 文本贴进来问「这个告警是啥意思 / 该咋办」——直接调本 skill 解析；\n"
        "(2) 收到第三方告警系统推送，想自动决定先调 zabbix_get_host_overview / k8s_describe_pod / "
        "swarm_check_service_health 中哪个——本 skill 返回 plan 字段直接给 next_skill 建议；\n"
        "(3) 新接入告警源时，验证 payload 字段是否能被平台正确解析（debugging 用）。\n"
        "**不适合**：(1) 用户用自然语言描述问题（「web 服务挂了」「这台机磁盘满了」）——直接走对应"
        "域 skill 即可，别用这个；(2) 已经有具体 host / pod 名时，跳过本 skill 直接查相关 skill 更直接。"
    ),
    "category": "alerts",
    "required_connection_type": "alert_analysis",
    "read_only": True,
    "visibility": "all",
    "params_schema": {
        "type": "object",
        "properties": {
            "raw_payload_json": {
                "type": "string",
                "description": (
                    "原始告警 payload 的 JSON 字符串。常见格式："
                    "Zabbix webhook（含 trigger / host / severity）、"
                    "AlertManager（含 alerts[].labels / annotations）、"
                    "自定义 schema 也行——平台会尽力识别。"
                ),
            },
            "connection_id": {"type": "string"},
        },
        "required": ["raw_payload_json"],
    },
}


def run(ctx, *, raw_payload_json: str, connection_id: str | None = None) -> dict:
    service = ctx.connection_for("alert_analysis", connection_id)
    payload = json.loads(raw_payload_json)
    return service.analyze(payload)

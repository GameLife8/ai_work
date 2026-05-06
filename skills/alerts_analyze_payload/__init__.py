from __future__ import annotations

import json

MANIFEST = {
    "code": "alerts_analyze_payload",
    "name": "告警 payload 预分析",
    "description": "对一段原始告警 payload 进行预分析，返回告警解析、上下文规划、补数结果和模型决策，但不落库。",
    "category": "alerts",
    "required_connection_type": "alert_analysis",
    "read_only": True,
    "visibility": "all",
    "params_schema": {
        "type": "object",
        "properties": {
            "raw_payload_json": {"type": "string"},
            "connection_id": {"type": "string"},
        },
        "required": ["raw_payload_json"],
    },
}


def run(ctx, *, raw_payload_json: str, connection_id: str | None = None) -> dict:
    service = ctx.connection_for("alert_analysis", connection_id)
    payload = json.loads(raw_payload_json)
    return service.analyze(payload)

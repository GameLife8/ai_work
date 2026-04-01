from __future__ import annotations

from flask import Blueprint, current_app, jsonify, request

alert_bp = Blueprint("alert", __name__, url_prefix="/api/v1/alerts")


def _handle_incoming_payload(payload: dict):
    alert_service = current_app.extensions["alert_service"]
    result = alert_service.handle_alert(payload)
    return jsonify(
        {
            "code": 0,
            "message": "accepted",
            "alert_id": result["alert_event_id"],
            "decision": result["decision"],
        }
    )


@alert_bp.post("/zabbix")
def receive_zabbix_alert():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"code": 400, "message": "invalid JSON payload"}), 400
    return _handle_incoming_payload(payload)


@alert_bp.post("/notification")
@alert_bp.post("/wechat")
def receive_notification_alert():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        payload = request.form.to_dict()
    if not isinstance(payload, dict) or not payload:
        return jsonify({"code": 400, "message": "invalid notification payload"}), 400
    return _handle_incoming_payload(payload)

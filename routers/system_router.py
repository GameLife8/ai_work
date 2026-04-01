from __future__ import annotations

from flask import Blueprint, current_app, jsonify

system_bp = Blueprint("system", __name__)


@system_bp.get("/health")
def healthcheck():
    store = current_app.extensions["store"]
    try:
        storage = store.healthcheck()
        status_code = 200 if storage["healthy"] else 503
        return (
            jsonify(
                {
                    "status": "ok" if storage["healthy"] else "degraded",
                    "storage": storage,
                    "ai_stub_enabled": current_app.config["USE_STUB_AI"],
                }
            ),
            status_code,
        )
    except Exception as exc:  # pragma: no cover
        return (
            jsonify(
                {
                    "status": "error",
                    "storage": {
                        "backend": getattr(store, "backend_name", "unknown"),
                        "healthy": False,
                        "error": str(exc),
                    },
                    "ai_stub_enabled": current_app.config["USE_STUB_AI"],
                }
            ),
            503,
        )


@system_bp.get("/api/v1/system/storage")
def storage_status():
    store = current_app.extensions["store"]
    try:
        return jsonify(store.healthcheck())
    except Exception as exc:  # pragma: no cover
        return (
            jsonify(
                {
                    "backend": getattr(store, "backend_name", "unknown"),
                    "healthy": False,
                    "error": str(exc),
                }
            ),
            503,
        )

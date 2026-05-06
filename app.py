from __future__ import annotations

from flask import Flask

from admin_app import init_admin
from config import Config
from routers.alert_router import alert_bp
from routers.system_router import system_bp
from runtime import create_runtime
from utils.logger import configure_logging


def create_app() -> Flask:
    configure_logging()

    app = Flask(__name__)
    app.config.from_object(Config)

    runtime = create_runtime(Config)

    app.extensions["runtime"] = runtime
    app.extensions["alert_service"] = runtime.alert_service
    app.extensions["store"] = runtime.store
    app.register_blueprint(alert_bp)
    app.register_blueprint(system_bp)
    init_admin(app, runtime, Config)
    return app


app = create_app()


if __name__ == "__main__":
    app.run(host=Config.APP_HOST, port=Config.APP_PORT, debug=True)

from __future__ import annotations

from flask import Flask

from config import Config
from models.db import create_store
from routers.alert_router import alert_bp
from routers.system_router import system_bp
from services.ai_client import AIClient
from services.alert_service import AlertService
from services.context_fetcher import ContextFetcher
from services.decision_engine import DecisionEngine
from services.graph_client import GraphClient
from services.incident_service import IncidentService
from services.zabbix_client import ZabbixClient
from utils.logger import configure_logging


def create_app() -> Flask:
    configure_logging()

    app = Flask(__name__)
    app.config.from_object(Config)

    store = create_store(Config)
    zabbix_client = ZabbixClient(
        base_url=app.config["ZABBIX_BASE_URL"],
        username=app.config["ZABBIX_USERNAME"],
        password=app.config["ZABBIX_PASSWORD"],
        timeout_seconds=app.config["ZABBIX_TIMEOUT_SECONDS"],
        use_stub=app.config["USE_STUB_ZABBIX"],
    )
    graph_client = GraphClient()
    incident_service = IncidentService(store)
    context_fetcher = ContextFetcher(zabbix_client, graph_client, incident_service)
    ai_client = AIClient(
        base_url=app.config["AI_BASE_URL"],
        timeout_seconds=app.config["AI_TIMEOUT_SECONDS"],
        use_stub=app.config["USE_STUB_AI"],
    )
    decision_engine = DecisionEngine()

    alert_service = AlertService(
        store=store,
        ai_client=ai_client,
        context_fetcher=context_fetcher,
        incident_service=incident_service,
        decision_engine=decision_engine,
        default_needs=app.config["DEFAULT_CONTEXT_NEEDS"],
    )

    app.extensions["alert_service"] = alert_service
    app.extensions["store"] = store
    app.register_blueprint(alert_bp)
    app.register_blueprint(system_bp)
    return app


app = create_app()


if __name__ == "__main__":
    app.run(host=Config.APP_HOST, port=Config.APP_PORT, debug=True)

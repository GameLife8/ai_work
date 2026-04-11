from __future__ import annotations

from dataclasses import dataclass

from config import Config
from models.db import create_store
from services.ai_client import AIClient
from services.alert_analysis_service import AlertAnalysisService
from services.alert_service import AlertService
from services.context_fetcher import ContextFetcher
from services.decision_engine import DecisionEngine
from services.docker_swarm_client import DockerSwarmClient
from services.graph_client import GraphClient
from services.incident_service import IncidentService
from services.zabbix_client import ZabbixClient


@dataclass
class AppRuntime:
    store: object
    zabbix_client: ZabbixClient
    graph_client: GraphClient
    incident_service: IncidentService
    context_fetcher: ContextFetcher
    ai_client: AIClient
    decision_engine: DecisionEngine
    alert_service: AlertService
    alert_analysis_service: AlertAnalysisService
    docker_swarm_client: DockerSwarmClient


def create_runtime(config_cls=Config) -> AppRuntime:
    store = create_store(config_cls)
    zabbix_client = ZabbixClient(
        base_url=config_cls.ZABBIX_BASE_URL,
        username=config_cls.ZABBIX_USERNAME,
        password=config_cls.ZABBIX_PASSWORD,
        timeout_seconds=config_cls.ZABBIX_TIMEOUT_SECONDS,
        use_stub=config_cls.USE_STUB_ZABBIX,
    )
    graph_client = GraphClient()
    incident_service = IncidentService(store)
    context_fetcher = ContextFetcher(zabbix_client, graph_client, incident_service)
    ai_client = AIClient(
        provider=config_cls.AI_PROVIDER,
        base_url=config_cls.AI_BASE_URL,
        api_key=config_cls.AI_API_KEY,
        model=config_cls.AI_MODEL,
        timeout_seconds=config_cls.AI_TIMEOUT_SECONDS,
        use_stub=config_cls.USE_STUB_AI,
    )
    decision_engine = DecisionEngine()
    alert_service = AlertService(
        store=store,
        ai_client=ai_client,
        context_fetcher=context_fetcher,
        incident_service=incident_service,
        decision_engine=decision_engine,
        default_needs=config_cls.DEFAULT_CONTEXT_NEEDS,
    )
    alert_analysis_service = AlertAnalysisService(
        ai_client=ai_client,
        context_fetcher=context_fetcher,
        decision_engine=decision_engine,
        default_needs=config_cls.DEFAULT_CONTEXT_NEEDS,
    )
    docker_swarm_client = DockerSwarmClient(
        docker_bin=config_cls.DOCKER_BIN,
        docker_runner=config_cls.DOCKER_RUNNER,
        docker_host=config_cls.DOCKER_HOST,
        docker_tls_verify=config_cls.DOCKER_TLS_VERIFY,
        docker_cert_path=config_cls.DOCKER_CERT_PATH,
        wsl_distro=config_cls.WSL_DISTRO,
        log_default_tail=config_cls.DOCKER_LOG_DEFAULT_TAIL,
        log_max_tail=config_cls.DOCKER_LOG_MAX_TAIL,
    )
    return AppRuntime(
        store=store,
        zabbix_client=zabbix_client,
        graph_client=graph_client,
        incident_service=incident_service,
        context_fetcher=context_fetcher,
        ai_client=ai_client,
        decision_engine=decision_engine,
        alert_service=alert_service,
        alert_analysis_service=alert_analysis_service,
        docker_swarm_client=docker_swarm_client,
    )

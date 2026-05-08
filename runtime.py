from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from config import Config
from models.db import create_store
from ops_platform import (
    ConnectionManager,
    ModelManager,
    SkillInvoker,
    SkillRegistry,
)
from ops_platform.http_skill_loader import HttpSkillLoader
from ops_platform.http_skill_seeds import seed_default_http_skills
from ops_platform.loader import load_skills_from_package
from ops_platform.runbook_engine import RunbookRegistry
from ops_platform.runbook_seeds import seed_default_runbooks
from ops_platform.store import attach_platform_store
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

    # 兼容 alert pipeline 的旧 client
    zabbix_client: ZabbixClient
    graph_client: GraphClient
    incident_service: IncidentService
    context_fetcher: ContextFetcher
    ai_client: AIClient
    decision_engine: DecisionEngine
    alert_service: AlertService
    alert_analysis_service: AlertAnalysisService
    docker_swarm_client: DockerSwarmClient

    # 新 platform kernel
    connection_manager: ConnectionManager
    model_manager: ModelManager
    skill_registry: SkillRegistry
    skill_invoker: SkillInvoker
    runbook_registry: RunbookRegistry
    http_skill_loader: HttpSkillLoader


def create_runtime(config_cls=Config) -> AppRuntime:
    store = create_store(config_cls)
    attach_platform_store(store)

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
        docker_host=config_cls.DOCKER_HOST,
        docker_tls_verify=config_cls.DOCKER_TLS_VERIFY,
        docker_cert_path=config_cls.DOCKER_CERT_PATH,
        log_default_tail=config_cls.DOCKER_LOG_DEFAULT_TAIL,
        log_max_tail=config_cls.DOCKER_LOG_MAX_TAIL,
    )

    # ---- platform kernel ----
    skill_registry = SkillRegistry()
    load_skills_from_package("skills", skill_registry)

    connection_manager = ConnectionManager(store)
    model_manager = ModelManager(store)

    runbook_registry = RunbookRegistry(None)  # 先占位，下面把 runtime 灌进去
    http_skill_loader = HttpSkillLoader.__new__(HttpSkillLoader)  # 同样先占位

    runtime = AppRuntime(
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
        connection_manager=connection_manager,
        model_manager=model_manager,
        skill_registry=skill_registry,
        skill_invoker=SkillInvoker(skill_registry, store),
        runbook_registry=runbook_registry,
        http_skill_loader=http_skill_loader,
    )
    connection_manager.attach_runtime(runtime)
    connection_manager.ensure_bootstrap(config_cls)
    model_manager.ensure_bootstrap(config_cls)

    # 装配 runbook_registry：先 seed 默认（如果空表），再 reload 进内存
    runbook_registry.runtime = runtime
    seed_default_runbooks(store)
    runbook_registry.reload()

    # 装配 http_skill_loader：先 seed 默认（如 zabbix_jsonrpc，默认 disabled），再 reload
    HttpSkillLoader.__init__(http_skill_loader, runtime)
    seed_default_http_skills(store)
    http_skill_loader.reload()

    _log_data_source_state(runtime, config_cls)
    return runtime


def _log_data_source_state(runtime, config_cls) -> None:
    """启动时把"数据源真不真"明确打印出来，避免悄悄走 stub / memory 的尴尬。"""
    import logging as _logging
    log = _logging.getLogger(__name__)

    store_kind = type(runtime.store).__name__
    if "InMemory" in store_kind:
        log.error(
            "⚠️  数据库未连通，store 已降级到 InMemoryStore——所有用户/接入/skill_call/runbook "
            "执行历史**重启即丢**。检查 DATABASE_URL 是否可达：%s",
            getattr(config_cls, "DATABASE_URL", "<unset>"),
        )
    else:
        log.info("✅ 持久化层：%s（DATABASE_URL=%s）",
                  store_kind, getattr(config_cls, "DATABASE_URL", "<unset>"))

    if getattr(config_cls, "USE_STUB_ZABBIX", False):
        log.error(
            "⚠️  USE_STUB_ZABBIX=true，Zabbix 返回的全是 mock 数据。"
            "生产/演示前请关掉这个开关并填 ZABBIX_USERNAME/ZABBIX_PASSWORD。"
        )
    elif not (getattr(config_cls, "ZABBIX_USERNAME", "") and getattr(config_cls, "ZABBIX_PASSWORD", "")):
        log.warning(
            "⚠️  Zabbix 用户名/密码为空，下次调用 Zabbix API 时会 login 失败。"
            "在 .env 或 admin 后台 connection 里补全 ZABBIX_USERNAME/ZABBIX_PASSWORD。"
        )
    else:
        log.info("✅ Zabbix 接入：%s（账号 %s）",
                  getattr(config_cls, "ZABBIX_BASE_URL", ""),
                  getattr(config_cls, "ZABBIX_USERNAME", ""))

    if getattr(config_cls, "USE_STUB_AI", False):
        log.warning("⚠️  USE_STUB_AI=true，模型走 stub 不真实调用。")
    elif not getattr(config_cls, "AI_API_KEY", ""):
        log.error(
            "⚠️  AI_API_KEY 未配置，模型调用必失败。生产前一定填火山方舟 / 通义 / 智谱 等真实 key。"
        )
    else:
        log.info("✅ 默认模型：%s（base_url=%s）",
                  getattr(config_cls, "AI_MODEL", ""),
                  getattr(config_cls, "AI_BASE_URL", ""))

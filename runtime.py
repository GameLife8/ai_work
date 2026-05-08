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
    _kick_off_async_health_check(runtime)
    return runtime


def _kick_off_async_health_check(runtime) -> None:
    """启动后异步把所有 connection 跑一次 healthcheck，把 status 字段从 unknown 更新成 ok/fail。

    异步线程跑，**不阻塞启动**——单条 zabbix login 失败可能要 10s，串行跑 5 条会让平台
    启动等几十秒。后台慢慢跑，admin 列表页刷新就能看到。
    """
    import logging as _logging
    import threading as _threading

    log = _logging.getLogger(__name__)

    def _run():
        try:
            conns = runtime.connection_manager.list()
        except Exception:
            return
        for c in conns:
            cid = c["id"]
            type_code = c.get("type_code")
            if type_code == "alert_analysis":
                # 内部 driver，没真实健康检查
                runtime.connection_manager.update(cid, status="ok")
                continue
            try:
                result = runtime.connection_manager.validate(cid)
                new_status = "ok" if result.get("ok") else "fail"
                runtime.connection_manager.update(cid, status=new_status)
                log.info("[health] %s (%s) → %s", c["name"], type_code, new_status)
            except Exception as exc:
                runtime.connection_manager.update(cid, status="fail")
                log.warning("[health] %s (%s) → fail: %s", c["name"], type_code, exc)

    t = _threading.Thread(target=_run, name="conn-health", daemon=True)
    t.start()


def _log_data_source_state(runtime, config_cls) -> None:
    """启动时把"数据源真不真"明确打印出来。

    **关键**：检查的是 **DB 中 connection / model_config 的实际记录**，不是 Config env。
    env 仅是 ensure_bootstrap 的种子值；admin 在后台编辑过任何东西后，DB 才是 source of truth。
    之前错把 env 当真相 → 用户后台改了真凭证启动还是报"未配置"——是 bug，本次修。
    """
    import logging as _logging
    log = _logging.getLogger(__name__)

    # ---------- 数据库 ----------
    store_kind = type(runtime.store).__name__
    db_url_masked = _mask_db_url(getattr(config_cls, "DATABASE_URL", "<unset>"))
    if "InMemory" in store_kind:
        log.error(
            "⚠️  数据库未连通，store 已降级到 InMemoryStore——所有用户/接入/skill_call/runbook "
            "执行历史**重启即丢**。检查 DATABASE_URL 是否可达：%s", db_url_masked,
        )
    else:
        log.info("✅ 持久化层：%s（%s）", store_kind, db_url_masked)

    # ---------- Zabbix（看 DB connection 真实状态，不看 env）----------
    try:
        z_conns = runtime.connection_manager.list(type_code="zabbix")
    except Exception:
        z_conns = []
    if not z_conns:
        log.warning("⚠️  没有 zabbix connection（如不用 zabbix 可忽略）")
    else:
        stubs, no_creds, real = [], [], []
        for c in z_conns:
            if not c.get("enabled", True):
                continue
            cfg = c.get("config") or {}
            if cfg.get("use_stub"):
                stubs.append(c)
            elif not (cfg.get("username") and cfg.get("password")):
                no_creds.append(c)
            else:
                real.append(c)
        if stubs:
            log.error(
                "⚠️  %d 条 zabbix connection 还在 stub 模式（返回的全是 mock 数据）：%s。"
                "到管理后台编辑这些 connection 关掉 stub。",
                len(stubs), ", ".join(c["name"] for c in stubs),
            )
        if no_creds:
            log.warning(
                "⚠️  %d 条 zabbix connection 未填账号密码（调用时 login 必失败）：%s。",
                len(no_creds), ", ".join(c["name"] for c in no_creds),
            )
        for c in real:
            log.info("✅ Zabbix 接入「%s」：%s（账号 %s）",
                      c.get("alias") or c["name"],
                      c["config"].get("base_url", ""),
                      c["config"].get("username", ""))

    # ---------- 模型（看 DB model_config 真实状态，不看 env）----------
    try:
        models = runtime.store.list_model_configs()
    except Exception:
        models = []
    if not models:
        log.error("⚠️  数据库里没有任何模型配置——agent 调用必失败。到 admin 后台 → 模型管理 → 新增。")
    else:
        usable = [m for m in models if m.get("enabled", True) and m.get("api_key")]
        no_key = [m for m in models if m.get("enabled", True) and not m.get("api_key")]
        if no_key:
            log.error(
                "⚠️  %d 个 enabled 模型 api_key 为空：%s。模型调用必失败。",
                len(no_key), ", ".join(m["name"] for m in no_key),
            )
        if usable:
            default = next((m for m in usable if m.get("is_default")), usable[0])
            log.info("✅ 默认模型：%s（model=%s, base_url=%s）",
                      default["name"], default.get("model", ""), default.get("base_url", ""))
            if len(usable) > 1:
                log.info("   另有 %d 个备用模型可切换。", len(usable) - 1)


def _mask_db_url(url: str) -> str:
    """mysql+pymysql://user:pass@host:port/db → mysql+pymysql://user:***@host:port/db"""
    if not url:
        return "<unset>"
    import re
    return re.sub(r"(://[^:/?#]+):[^@]+@", r"\1:***@", url)

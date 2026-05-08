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
    """构造平台 runtime。

    启动顺序很重要——必须先把 connection_manager / model_manager 装好并 ensure_bootstrap
    （这一步把 env 里的种子值写进 DB），**之后** 再从 DB 派生 alert pipeline 老链路用的
    zabbix_client / ai_client / docker_swarm_client。这样 env 真的只是"种子"，
    DB 才是 single source of truth。

    admin 在后台修改默认 connection / model 之后调用 ``runtime.refresh_legacy_clients()``
    可让 alert pipeline 立即拿到新凭证，无需重启进程。
    """
    store = create_store(config_cls)
    attach_platform_store(store)

    # ---- platform kernel：先装 manager，再让它从 env 种子写一份 DB connection ----
    skill_registry = SkillRegistry()
    load_skills_from_package("skills", skill_registry)

    connection_manager = ConnectionManager(store)
    model_manager = ModelManager(store)

    runbook_registry = RunbookRegistry(None)  # 先占位
    http_skill_loader = HttpSkillLoader.__new__(HttpSkillLoader)

    # 先建 runtime 骨架，下面再把 alert pipeline 的 legacy clients 灌进来
    runtime = AppRuntime(
        store=store,
        zabbix_client=None,                     # 先占位，下面 refresh_legacy_clients 填
        graph_client=GraphClient(),
        incident_service=IncidentService(store),
        context_fetcher=None,                   # 占位
        ai_client=None,                         # 占位
        decision_engine=DecisionEngine(),
        alert_service=None,                     # 占位
        alert_analysis_service=None,            # 占位
        docker_swarm_client=None,               # 占位
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

    # ---- alert pipeline 老链路：从 DB 默认 connection / model 派生（不再读 env） ----
    _build_legacy_clients(runtime, config_cls)

    # 装配 runbook_registry：先 seed 默认（如果空表），再 reload 进内存
    runbook_registry.runtime = runtime
    seed_default_runbooks(store)
    runbook_registry.reload()

    # 装配 http_skill_loader：先 seed 默认（如 zabbix_jsonrpc，默认 disabled），再 reload
    HttpSkillLoader.__init__(http_skill_loader, runtime)
    seed_default_http_skills(store)
    http_skill_loader.reload()

    _attach_refresh_method(runtime, config_cls)
    _log_data_source_state(runtime, config_cls)
    _kick_off_async_health_check(runtime)
    return runtime


def _build_legacy_clients(runtime, config_cls) -> None:
    """从 DB 的默认 connection / model 派生 alert pipeline 老链路用的 client。

    这样：
      - alert pipeline (POST /api/v1/alerts/*) 看到的凭证 = admin 在后台改的凭证（DB）
      - env 真的只是 ensure_bootstrap 的种子值
      - admin 改完连接后调 ``runtime.refresh_legacy_clients()`` 立即生效
    """
    store = runtime.store
    cm = runtime.connection_manager
    mm = runtime.model_manager

    # ---- ZabbixClient：从默认 zabbix connection 派生 ----
    z_default = cm.get_default("zabbix")
    if z_default:
        # connection_manager.get_client 已经按 driver 建好了 ZabbixClient，直接复用
        runtime.zabbix_client = cm.get_client(z_default["id"])
    else:
        # DB 没 zabbix connection（极少见）→ 用 env 种子兜底
        runtime.zabbix_client = ZabbixClient(
            base_url=config_cls.ZABBIX_BASE_URL,
            username=config_cls.ZABBIX_USERNAME,
            password=config_cls.ZABBIX_PASSWORD,
            timeout_seconds=config_cls.ZABBIX_TIMEOUT_SECONDS,
            use_stub=config_cls.USE_STUB_ZABBIX,
        )

    # ---- DockerSwarmClient ----
    s_default = cm.get_default("swarm")
    if s_default:
        runtime.docker_swarm_client = cm.get_client(s_default["id"])
    else:
        runtime.docker_swarm_client = DockerSwarmClient(
            docker_bin=config_cls.DOCKER_BIN,
            docker_host=config_cls.DOCKER_HOST,
            docker_tls_verify=config_cls.DOCKER_TLS_VERIFY,
            docker_cert_path=config_cls.DOCKER_CERT_PATH,
            log_default_tail=config_cls.DOCKER_LOG_DEFAULT_TAIL,
            log_max_tail=config_cls.DOCKER_LOG_MAX_TAIL,
        )

    # ---- AIClient（alert pipeline 用的，跟 OpsModelClient 不是同一个但参数一致）----
    m_default = mm.get_default()
    if m_default:
        runtime.ai_client = AIClient(
            provider=m_default.get("provider", "volcengine_ark"),
            base_url=m_default["base_url"],
            api_key=m_default["api_key"],
            model=m_default.get("model", ""),
            timeout_seconds=int(m_default.get("timeout_seconds") or 120),
            use_stub=False,
        )
    else:
        runtime.ai_client = AIClient(
            provider=config_cls.AI_PROVIDER,
            base_url=config_cls.AI_BASE_URL,
            api_key=config_cls.AI_API_KEY,
            model=config_cls.AI_MODEL,
            timeout_seconds=config_cls.AI_TIMEOUT_SECONDS,
            use_stub=config_cls.USE_STUB_AI,
        )

    # ---- 依赖这些 client 的服务 ----
    runtime.context_fetcher = ContextFetcher(
        runtime.zabbix_client, runtime.graph_client, runtime.incident_service,
    )
    runtime.alert_service = AlertService(
        store=store,
        ai_client=runtime.ai_client,
        context_fetcher=runtime.context_fetcher,
        incident_service=runtime.incident_service,
        decision_engine=runtime.decision_engine,
        default_needs=config_cls.DEFAULT_CONTEXT_NEEDS,
    )
    runtime.alert_analysis_service = AlertAnalysisService(
        ai_client=runtime.ai_client,
        context_fetcher=runtime.context_fetcher,
        decision_engine=runtime.decision_engine,
        default_needs=config_cls.DEFAULT_CONTEXT_NEEDS,
    )


def _attach_refresh_method(runtime, config_cls) -> None:
    """挂一个 refresh_legacy_clients 方法到 runtime，admin 改连接后调用即可。"""
    def _refresh():
        import logging as _logging
        log = _logging.getLogger(__name__)
        try:
            _build_legacy_clients(runtime, config_cls)
            # 顺便刷一下 app.extensions，让 Flask 路由也看见新实例
            try:
                from flask import current_app
                current_app.extensions["alert_service"] = runtime.alert_service
            except Exception:
                pass  # 不在请求上下文里，跳过
            log.info("alert pipeline 老链路 client 已刷新（取自 DB 当前默认 connection / model）")
            return True
        except Exception as exc:
            log.exception("refresh_legacy_clients 失败：%s", exc)
            return False

    runtime.refresh_legacy_clients = _refresh


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

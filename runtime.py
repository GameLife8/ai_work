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
from ops_platform.async_tasks import AsyncTaskService, attach_async_task_service
from ops_platform.http_skill_loader import HttpSkillLoader
from ops_platform.http_skill_seeds import seed_default_http_skills
from ops_platform.loader import load_skills_from_package
from ops_platform.mcp_skill_loader import MCPSkillLoader
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
    mcp_skill_loader: MCPSkillLoader
    async_task_service: AsyncTaskService | None = None


def create_runtime(config_cls=Config) -> AppRuntime:
    """构造平台 runtime。

    启动顺序很重要——必须先把 connection_manager / model_manager 装好并 ensure_bootstrap
    （这一步把 env 里的种子值写进 DB），**之后** 再从 DB 派生 alert pipeline 老链路用的
    zabbix_client / ai_client / docker_swarm_client。这样 env 真的只是"种子"，
    DB 才是 single source of truth。

    admin 在后台修改默认 connection / model 之后调用 ``runtime.refresh_legacy_clients()``
    可让 alert pipeline 立即拿到新凭证，无需重启进程。
    """
    # 启动前 fail-fast 校验加密配置：生产环境（STRICT_ENCRYPTION=true）若
    # PLATFORM_ENCRYPTION_KEY 未配置直接 raise，阻止明文敏感数据落库。
    # 开发环境（默认 false）继续走 warning + 明文降级路径。
    from ops_platform.crypto import ensure_strict_encryption
    ensure_strict_encryption(getattr(config_cls, "STRICT_ENCRYPTION", False))

    store = create_store(config_cls)
    attach_platform_store(store)

    # ---- platform kernel：先装 manager，再让它从 env 种子写一份 DB connection ----
    skill_registry = SkillRegistry()
    load_skills_from_package("skills", skill_registry)

    connection_manager = ConnectionManager(store)
    model_manager = ModelManager(store)

    runbook_registry = RunbookRegistry(None)  # 先占位
    http_skill_loader = HttpSkillLoader.__new__(HttpSkillLoader)
    mcp_skill_loader = MCPSkillLoader.__new__(MCPSkillLoader)

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
        mcp_skill_loader=mcp_skill_loader,
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

    # 装配 mcp_skill_loader：扫所有 enabled 的 mcp_client connection 拉远端 tools 注册成 skill。
    # 失败不阻塞启动——远端 MCP 服务可能暂时挂、网络抖动等。后续 admin 改完触发手动 reload。
    MCPSkillLoader.__init__(mcp_skill_loader, runtime)
    try:
        mcp_skill_loader.reload()
    except Exception as exc:    # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning("MCP skill 初次加载失败（不阻塞启动）：%s", exc)

    # ---- AsyncTaskService：异步宿主机任务的平台门面 ----
    # 必须在 connection_manager 之后构造（依赖它解析 host_agent client）。
    # 内部启动后台 poller 线程跑（daemon），进程退出自动收尾。
    try:
        attach_async_task_service(runtime, start_poller=True)
    except Exception:
        import logging
        logging.getLogger(__name__).exception("AsyncTaskService 初始化失败（不阻塞启动）")

    _attach_refresh_method(runtime, config_cls)
    _attach_hot_read_caches(runtime, config_cls)
    _attach_shutdown_hook(runtime)
    _log_data_source_state(runtime, config_cls)
    _kick_off_async_health_check(runtime)
    return runtime


def _attach_shutdown_hook(runtime) -> None:
    """挂 ``runtime.shutdown(timeout=30)`` —— SIGTERM / atexit / 显式调用都能用。

    解决的问题
    ----------
    AsyncTaskService.poller 是 daemon thread,进程 SIGTERM 会**直接砍掉**正在
    跑的 ``_poll_running_once()`` —— agent 端任务继续跑（agent 进程没死）,但
    平台侧 DB 记录卡在 running,下次启动靠 reconcile 阶段才能修复,中间窗口
    用户看不到结果。

    本函数提供一个明确的 shutdown 入口:
    1. 停 AsyncTaskService.poller（停 dispatch,等当前 iteration 结束）
    2. 关 host_agent_client 的 requests.Session 连接池
    3. 关 store engine（如果用的是 SQL）—— SQLAlchemy 连接池 dispose

    托管层（app.py / chainlit_app.py）负责:
    - 注册 ``atexit.register(runtime.shutdown)`` —— 正常退出
    - 注册 ``signal.signal(SIGTERM, ...)`` —— k8s / docker stop
    """
    import logging as _logging
    log = _logging.getLogger(__name__)
    _shutdown_done = {"flag": False}

    def _shutdown(timeout: float = 30.0) -> None:
        # 幂等:多次调（atexit + signal handler 可能都触发）只跑一次
        if _shutdown_done["flag"]:
            return
        _shutdown_done["flag"] = True

        # atexit 触发时 pytest / stdlib 可能已经关掉了 stdout/stderr——
        # 这种情况下 logger.info 会抛 "I/O operation on closed file"。
        # 注意:logging 库默认**不会**把 handler 异常抛回给调用方,而是 dump 到
        # stderr;所以这里既要 catch 调用本身的异常,还要临时关 ``raiseExceptions``
        # 防止 handler 内部把错打到已关闭的 stderr。
        import logging as _logmod
        def _safe_log(level: str, fmt: str, *args) -> None:
            saved = _logmod.raiseExceptions
            _logmod.raiseExceptions = False
            try:
                getattr(log, level)(fmt, *args)
            except Exception:    # noqa: BLE001
                pass
            finally:
                _logmod.raiseExceptions = saved

        _safe_log("info", "runtime.shutdown 开始(timeout=%.1fs)...", timeout)

        # 1. 停 AsyncTaskService poller —— 给当前 iteration 一个有限时间收尾
        ats = getattr(runtime, "async_task_service", None)
        if ats is not None:
            try:
                ats.stop_poller()
                t = getattr(ats, "_poller_thread", None)
                if t is not None and t.is_alive():
                    t.join(timeout=timeout)
                    if t.is_alive():
                        _safe_log("warning",
                            "async-task-poller 在 %.1fs 内未停止;在跑的任务靠下次启动 reconcile 修复",
                            timeout,
                        )
                    else:
                        _safe_log("info","async-task-poller 已停")
            except Exception:
                _safe_log("exception","停 AsyncTaskService.poller 时出错")

        # 2. 关 host_agent_client 的 requests.Session —— 释放 TCP 连接
        # host_agent_client 是 per-connection 的(在 ConnectionManager 里缓存),
        # 这里通过 manager 拿所有客户端逐个关。
        try:
            cm = getattr(runtime, "connection_manager", None)
            if cm is not None and hasattr(cm, "iter_clients_for_shutdown"):
                for client in cm.iter_clients_for_shutdown():
                    close_fn = getattr(client, "close", None)
                    if callable(close_fn):
                        try:
                            close_fn()
                        except Exception:    # pragma: no cover
                            pass
        except Exception:
            _safe_log("exception","关 host_agent_client sessions 时出错")

        # 3. 关 store engine —— SQLAlchemy 连接池 dispose
        try:
            engine = getattr(getattr(runtime, "store", None), "engine", None)
            if engine is not None:
                engine.dispose()
                _safe_log("info","store engine 已 dispose")
        except Exception:
            _safe_log("exception","关 store engine 时出错")

        _safe_log("info","runtime.shutdown 完成")

    runtime.shutdown = _shutdown


def _attach_hot_read_caches(runtime, config_cls) -> None:
    """挂 TTL 缓存给 hot read 路径（每次 ask() 都打 DB 的查询）。

    覆盖的查询：
    - ``store.list_prompt_segments()`` — agent.py:_load_system_prompt
    - ``connection_manager.list()``     — agent.py:_build_cluster_registry_prompt

    TTL 取默认 5s（``PROMPT_CACHE_TTL`` env 可调）——admin 改完最多等 5s 生效,
    高并发期 hit 率 >90%,DB 压力线性下降。
    """
    from ops_platform.ttl_cache import TTLCache
    import os as _os

    ttl = float(_os.getenv("PROMPT_CACHE_TTL", "5.0"))
    runtime.prompt_segments_cache = TTLCache(ttl_seconds=ttl)
    runtime.connections_list_cache = TTLCache(ttl_seconds=ttl)
    # 当前靠 TTL 自然过期（≤5s）兜底；admin 修改后立即生效需求
    # 可以未来扩展 connection_manager 的 on_change hook 来主动 invalidate。


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
        # 让 alert pipeline 能 best-effort 触发 runbook 自动诊断（_maybe_run_runbook）
        runtime_ref=runtime,
        auto_runbook_enabled=getattr(config_cls, "ALERT_AUTO_RUNBOOK", False),
        auto_runbook_timeout_seconds=getattr(config_cls, "ALERT_AUTO_RUNBOOK_TIMEOUT_SECONDS", 120),
    )
    runtime.alert_analysis_service = AlertAnalysisService(
        ai_client=runtime.ai_client,
        context_fetcher=runtime.context_fetcher,
        decision_engine=runtime.decision_engine,
        default_needs=config_cls.DEFAULT_CONTEXT_NEEDS,
    )


def _attach_refresh_method(runtime, config_cls) -> None:
    """挂一个 refresh_legacy_clients 方法到 runtime，admin 改连接后调用即可。

    同时把它注册成 ``model_manager`` 的 on_change 回调——这样**任何**经过
    manager 的模型 mutation（admin UI / 脚本 / 内部调用）都会自动触发 legacy
    pipeline 刷新，调用方不用再记得"改完默认要手动刷"。
    """
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

    # 把 legacy refresh 绑成 model_manager 的 on_change 回调。
    # 只关心 "默认模型" 是否变化——非默认行的增删改不影响 alert pipeline。
    def _on_model_change(action: str, model_id: str | None, record: dict | None) -> None:
        # create + is_default=True / update + is_default=True / 任何 delete 都可能换默认
        is_default_change = (
            action == "delete"
            or (record is not None and record.get("is_default"))
        )
        if is_default_change:
            _refresh()
    runtime.model_manager.register_on_change(_on_model_change)


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

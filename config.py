from __future__ import annotations

import os

from dotenv import load_dotenv


load_dotenv()


class Config:
    APP_HOST = os.getenv("APP_HOST", "127.0.0.1")
    APP_PORT = int(os.getenv("APP_PORT", "5000"))
    TESTING = os.getenv("TESTING", "false").lower() == "true"

    ZABBIX_BASE_URL = os.getenv("ZABBIX_BASE_URL", "http://169.24.2.90:80/zabbix/api_jsonrpc.php")
    ZABBIX_USERNAME = os.getenv("ZABBIX_USERNAME", "")
    ZABBIX_PASSWORD = os.getenv("ZABBIX_PASSWORD", "")
    ZABBIX_TIMEOUT_SECONDS = int(os.getenv("ZABBIX_TIMEOUT_SECONDS", "10"))
    # ⚠️ 默认 false——stub 数据是开发兜底，不该作为默认；如果想跑无 zabbix 的纯演示，
    # 显式设 USE_STUB_ZABBIX=true
    USE_STUB_ZABBIX = os.getenv("USE_STUB_ZABBIX", "false").lower() == "true"
    DOCKER_BIN = os.getenv("DOCKER_BIN", "docker")
    DOCKER_HOST = os.getenv("DOCKER_HOST", "tcp://169.24.216.227:3389")
    DOCKER_TLS_VERIFY = os.getenv("DOCKER_TLS_VERIFY", "")
    DOCKER_CERT_PATH = os.getenv("DOCKER_CERT_PATH", "")
    DOCKER_LOG_DEFAULT_TAIL = int(os.getenv("DOCKER_LOG_DEFAULT_TAIL", "100"))
    DOCKER_LOG_MAX_TAIL = int(os.getenv("DOCKER_LOG_MAX_TAIL", "1000"))

    TIDB_HOST = os.getenv("TIDB_HOST", "169.24.1.87")
    TIDB_PORT = int(os.getenv("TIDB_PORT", "4000"))
    TIDB_DATABASE = os.getenv("TIDB_DATABASE", "ai_alert")
    TIDB_USERNAME = os.getenv("TIDB_USERNAME", "ai_alert")
    TIDB_PASSWORD = os.getenv("TIDB_PASSWORD", "ai_alert")
    DATABASE_URL = os.getenv(
        "DATABASE_URL",
        f"mysql+pymysql://{TIDB_USERNAME}:{TIDB_PASSWORD}@{TIDB_HOST}:{TIDB_PORT}/{TIDB_DATABASE}?charset=utf8mb4",
    )
    STORE_BACKEND = os.getenv("STORE_BACKEND", "sql")
    # STORE_STRICT=true 时，DB 连接失败直接 raise（生产建议打开），
    # 不会悄悄走内存 store 让用户以为数据落库了
    STORE_STRICT = os.getenv("STORE_STRICT", "false").lower() == "true"
    # STRICT_ENCRYPTION=true 时，PLATFORM_ENCRYPTION_KEY 未配置直接 raise
    # 启动失败（生产强制要求）。否则只 warning + 降级明文（开发友好）。
    # 推荐：生产 Docker compose / k8s 部署强制设 true，本地开发不设。
    STRICT_ENCRYPTION = os.getenv("STRICT_ENCRYPTION", "false").lower() == "true"

    AI_PROVIDER = os.getenv("AI_PROVIDER", "volcengine_coding")
    AI_BASE_URL = os.getenv("AI_BASE_URL", "https://ark.cn-beijing.volces.com/api/coding/v3")
    AI_API_KEY = os.getenv("AI_API_KEY", "")
    AI_MODEL = os.getenv("AI_MODEL", "ark-code-latest")
    AI_TIMEOUT_SECONDS = int(os.getenv("AI_TIMEOUT_SECONDS", "10"))

    DEFAULT_CONTEXT_NEEDS = ["metric_summary", "topology", "related_incidents"]
    # ⚠️ 默认 false：必须配真实 AI key 才工作。CI/单元测试再显式开 stub。
    USE_STUB_AI = os.getenv("USE_STUB_AI", "false").lower() == "true"
    AGENT_MAX_REASONING_STEPS = int(os.getenv("AGENT_MAX_REASONING_STEPS", "8"))

    # ---- 告警自动诊断 ----
    # alert webhook 进来 → 旧 pipeline 出决策后，再 best-effort 跑一次 runbook
    # （按 alert.summary + host + service 匹配 trigger）。失败不影响告警入库与决策。
    # 默认开；ENV ``ALERT_AUTO_RUNBOOK=false`` 可关掉（如平台还在调试期、runbook 不稳）。
    ALERT_AUTO_RUNBOOK = os.getenv("ALERT_AUTO_RUNBOOK", "true").lower() == "true"
    # auto-diagnose 单次执行上限：超时直接放弃（不阻塞告警 API 响应）。
    ALERT_AUTO_RUNBOOK_TIMEOUT_SECONDS = int(os.getenv("ALERT_AUTO_RUNBOOK_TIMEOUT", "120"))

    # ---- 平台后台 ----
    ADMIN_JWT_SECRET = os.getenv("ADMIN_JWT_SECRET", "change-me-in-prod")
    ADMIN_BOOTSTRAP_USERNAME = os.getenv("ADMIN_BOOTSTRAP_USERNAME", "admin")
    ADMIN_BOOTSTRAP_PASSWORD = os.getenv("ADMIN_BOOTSTRAP_PASSWORD", "admin123")
    PLATFORM_ENCRYPTION_KEY = os.getenv("PLATFORM_ENCRYPTION_KEY", "")

    # ---- MCP server ----
    MCP_HOST = os.getenv("MCP_HOST", "0.0.0.0")
    MCP_PORT = int(os.getenv("MCP_PORT", "8765"))
    MCP_API_KEYS = os.getenv("MCP_API_KEYS", "")
    MCP_ALLOW_ANON = os.getenv("MCP_ALLOW_ANON", "false").lower() == "true"

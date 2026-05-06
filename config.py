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
    USE_STUB_ZABBIX = os.getenv("USE_STUB_ZABBIX", "true").lower() == "true"
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

    AI_PROVIDER = os.getenv("AI_PROVIDER", "volcengine_coding")
    AI_BASE_URL = os.getenv("AI_BASE_URL", "https://ark.cn-beijing.volces.com/api/coding/v3")
    AI_API_KEY = os.getenv("AI_API_KEY", "")
    AI_MODEL = os.getenv("AI_MODEL", "ark-code-latest")
    AI_TIMEOUT_SECONDS = int(os.getenv("AI_TIMEOUT_SECONDS", "10"))

    DEFAULT_CONTEXT_NEEDS = ["metric_summary", "topology", "related_incidents"]
    USE_STUB_AI = os.getenv("USE_STUB_AI", "true").lower() == "true"
    AGENT_MAX_REASONING_STEPS = int(os.getenv("AGENT_MAX_REASONING_STEPS", "8"))

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

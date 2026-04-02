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

    TIDB_HOST = os.getenv("TIDB_HOST", "169.24.1.87")
    TIDB_PORT = int(os.getenv("TIDB_PORT", "4000"))
    TIDB_DATABASE = os.getenv("TIDB_DATABASE", "ai_alert")
    TIDB_USERNAME = os.getenv("TIDB_USERNAME", "ai_alert")
    TIDB_PASSWORD = os.getenv("TIDB_PASSWORD", "ai_alert")
    DATABASE_URL = os.getenv(
        "DATABASE_URL",
        f"mysql+pymysql://{TIDB_USERNAME}:{TIDB_PASSWORD}@{TIDB_HOST}:{TIDB_PORT}/{TIDB_DATABASE}?charset=utf8mb4",
    )
    STORE_BACKEND = os.getenv("STORE_BACKEND", "memory")

    AI_PROVIDER = os.getenv("AI_PROVIDER", "volcengine_coding")
    AI_BASE_URL = os.getenv("AI_BASE_URL", "https://ark.cn-beijing.volces.com/api/coding/v3")
    AI_API_KEY = os.getenv("AI_API_KEY", "")
    AI_MODEL = os.getenv("AI_MODEL", "ark-code-latest")
    AI_TIMEOUT_SECONDS = int(os.getenv("AI_TIMEOUT_SECONDS", "10"))

    DEFAULT_CONTEXT_NEEDS = ["metric_summary", "topology", "related_incidents"]
    USE_STUB_AI = os.getenv("USE_STUB_AI", "true").lower() == "true"

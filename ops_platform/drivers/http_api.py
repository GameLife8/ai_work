from __future__ import annotations

from typing import Any

from ops_platform.drivers.base import ConnectionDriver, DriverField
from services.http_api_client import HttpApiClient


class HttpApiDriver(ConnectionDriver):
    """通用 HTTP API 接入。

    每个 ``http_api`` connection 代表一个外部系统（Jira / GitLab / 内部 CMDB / ...）。
    一份 connection 配一次，下面挂任意多个 HTTP skill（数据驱动的 YAML 定义，
    [`platform_http_skill`](../store.py) 表存储）。
    """

    type_code = "http_api"
    display_name = "HTTP API（外部系统）"
    category = "integration"

    def __init__(self) -> None:
        self.fields = [
            DriverField("base_url", "Base URL", required=True,
                        placeholder="https://jira.intra.corp",
                        help="外部系统根 URL；HTTP skill 的 path 会拼到这后面"),

            DriverField("auth_kind", "鉴权方式", required=True, default="none",
                        help="none / bearer / basic / api_key_header / oauth2_client_credentials"),

            # bearer
            DriverField("bearer_token", "Bearer Token", type="password",
                        help="auth_kind=bearer 时填"),

            # basic
            DriverField("basic_user", "Basic 用户名", help="auth_kind=basic 时填"),
            DriverField("basic_pass", "Basic 密码", type="password"),

            # api key in custom header
            DriverField("api_key", "API Key", type="password",
                        help="auth_kind=api_key_header 时填"),
            DriverField("api_key_header_name", "API Key Header 名", default="X-Api-Key"),

            # oauth2 client credentials
            DriverField("oauth2_token_url", "OAuth2 Token URL",
                        placeholder="https://auth.example.com/oauth/token",
                        help="auth_kind=oauth2_client_credentials 时填，平台自动获取并缓存 token"),
            DriverField("oauth2_client_id", "OAuth2 Client ID"),
            DriverField("oauth2_client_secret", "OAuth2 Client Secret", type="password"),
            DriverField("oauth2_scope", "OAuth2 Scope", help="可选，多个空格分隔"),

            # 通用
            DriverField("custom_headers", "自定义 Header（多行）", type="textarea", default="",
                        placeholder="Cookie: ...\nX-Tenant: prod",
                        help="每行一个 ``Key: Value``；会和 skill 自身定义的 headers 合并"),
            DriverField("verify_ssl", "校验 SSL", type="boolean", default=True),
            DriverField("timeout_seconds", "默认超时(秒)", type="integer", default=30),
            DriverField("proxy", "HTTP Proxy", help="可选，企业内网常用"),
        ]

    def build_client(self, config: dict[str, Any]) -> HttpApiClient:
        return HttpApiClient(
            base_url=config["base_url"],
            auth_kind=config.get("auth_kind", "none"),
            bearer_token=config.get("bearer_token", ""),
            basic_user=config.get("basic_user", ""),
            basic_pass=config.get("basic_pass", ""),
            api_key=config.get("api_key", ""),
            api_key_header_name=config.get("api_key_header_name", "X-Api-Key"),
            oauth2_token_url=config.get("oauth2_token_url", ""),
            oauth2_client_id=config.get("oauth2_client_id", ""),
            oauth2_client_secret=config.get("oauth2_client_secret", ""),
            oauth2_scope=config.get("oauth2_scope", ""),
            custom_headers=config.get("custom_headers", ""),
            verify_ssl=bool(config.get("verify_ssl", True)),
            timeout_seconds=int(config.get("timeout_seconds") or 30),
            proxy=config.get("proxy", ""),
        )

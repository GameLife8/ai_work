"""Jenkins 接入驱动。"""

from __future__ import annotations

from typing import Any

from ops_platform.drivers.base import ConnectionDriver, DriverField
from services.jenkins_client import JenkinsClient


class JenkinsDriver(ConnectionDriver):
    """Jenkins CI/CD 接入。

    支持 Basic Auth(老 Jenkins 用户名 + 密码)和 API Token(推荐,新版用户在
    People → admin → Configure → API Token 生成);填了 api_token 时优先用 token。

    实测兼容 Jenkins 2.190+。`useCrumbs` 自动探测,实例没开就不带 crumb 头。
    """

    type_code = "jenkins"
    display_name = "Jenkins CI/CD"
    category = "automation"

    def __init__(self) -> None:
        self.fields = [
            DriverField("base_url", "Jenkins URL", required=True,
                        placeholder="http://jenkins.intra.corp:8080",
                        help="不带尾斜杠;skill 自动拼 /api/json 等路径"),
            DriverField("username", "用户名", required=True,
                        placeholder="admin"),
            DriverField("api_token", "API Token", type="password",
                        help="推荐:在 Jenkins 用户配置页生成。比明文密码安全。"),
            DriverField("password", "密码", type="password",
                        help="老 Jenkins 兼容选项;有 api_token 时本字段被忽略。"),
            DriverField("timeout_seconds", "超时(秒)", type="integer", default=30,
                        help="单次 HTTP 调用上限"),
            DriverField("verify_ssl", "校验 SSL", type="boolean", default=True,
                        help="内网自签证书可关。生产 https 必须开。"),
        ]

    def build_client(self, config: dict[str, Any]) -> JenkinsClient:
        return JenkinsClient(
            base_url=config["base_url"],
            username=config.get("username", ""),
            api_token=config.get("api_token", ""),
            password=config.get("password", ""),
            timeout_seconds=int(config.get("timeout_seconds") or 30),
            verify_ssl=bool(config.get("verify_ssl", True)),
        )

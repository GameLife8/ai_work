from __future__ import annotations

from typing import Any

from ops_platform.drivers.base import ConnectionDriver


class AlertAnalysisDriver(ConnectionDriver):
    """告警分析 skill 不需要外部接入；这里提供一个 'virtual' driver
    保证 skill 取 ctx.connection 时拿得到 runtime.alert_analysis_service。"""

    type_code = "alert_analysis"
    display_name = "告警分析（内置）"
    category = "internal"

    def __init__(self) -> None:
        self.fields = []

    def build_client(self, config: dict[str, Any]) -> Any:
        # 真正的 client 由 ConnectionManager 在 build 时直接从 runtime 注入，
        # 这里给个占位实现。
        return None

from __future__ import annotations

import logging
import re
from typing import Any

import requests


logger = logging.getLogger(__name__)


# 知名国产模型 + OpenAI 协议路径修正：base_url 漏了 v3/v1 时自动补
_VOLC_HOSTS = ("ark.cn-beijing.volces.com", "ark.volces.com")


class OpsModelClient:
    def __init__(self, base_url: str, api_key: str, model: str, timeout_seconds: int = 120) -> None:
        self.base_url = self._normalize_base_url(base_url)
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds

    @staticmethod
    def _normalize_base_url(raw: str) -> str:
        """容错处理 base_url：

        - 去尾部斜杠
        - 用户填了完整 ``/chat/completions`` 后缀 → 去掉（拼回时统一加）
        - 火山方舟 Code Plan 漏了 ``/v3`` → 自动补，并 warning 提示
        """
        url = (raw or "").rstrip("/")
        # 用户把 endpoint 完整路径都填了
        if url.endswith("/chat/completions"):
            url = url[: -len("/chat/completions")].rstrip("/")
        # 火山 coding 端点缺 /v3 → 补
        if any(h in url for h in _VOLC_HOSTS):
            if url.endswith("/api/coding") or url.endswith("/api"):
                logger.warning(
                    "AI base_url=%r 缺少 /v3 版本号；自动按 OpenAI 兼容协议补成 .../v3。"
                    "建议进管理后台把模型配置改对。", raw,
                )
                url = url + "/v3"
        return url

    def create_completion(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "temperature": 0.1,
            "messages": messages,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice or "auto"

        response = requests.post(
            f"{self.base_url}/chat/completions",
            json=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            timeout=self.timeout_seconds,
        )
        if response.status_code == 404:
            raise requests.HTTPError(
                f"404 Not Found：调用 {response.url} 失败。"
                f"很可能是模型 base_url 配置不对——OpenAI 兼容协议必须以 ``/v3`` 结尾，"
                f"如 https://ark.cn-beijing.volces.com/api/coding/v3。",
                response=response,
            )
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]

from __future__ import annotations

import logging
import re
from typing import Any

import requests


logger = logging.getLogger(__name__)


# 知名国产模型 + OpenAI 协议路径修正：base_url 漏了 v3/v1 时自动补
_VOLC_HOSTS = ("ark.cn-beijing.volces.com", "ark.volces.com")


# OpenAI 协议 assistant message 的"标准字段"——其余字段一律视为厂商噪音剥掉。
#
# 为什么要剥
# ----------
# 国产推理模型（doubao seed-2 / qwen3-thinking / deepseek-r1 等）在响应里塞额外字段：
#   - ``reasoning_content`` / ``thinking`` / ``thinking_content`` —— 思考链原文
#   - ``chain_of_thought`` / ``cot`` —— 部分模型的别名
#   - ``audio`` —— 多模态返回
# 这些字段单条可能上千 token。如果调用方未来不小心写 ``messages.append(message)``
# 把整个 dict 塞回历史，下一轮 API 调用会**整体回传**给模型，token 几轮就翻几倍。
#
# 在 model_client 这一层一次性剥掉,是"边界处消除风险"的最干净做法——
# 调用方拿到的 message dict **永远不含**这些噪音，无论后续如何使用都安全。
_ASSISTANT_MESSAGE_ALLOWED_FIELDS = frozenset({
    "role", "content", "tool_calls", "tool_call_id", "refusal", "name",
    "function_call",   # 旧版 openai 工具调用兼容（不再常用，但留着无害）
})

# 这些字段被剥时如果有内容，就记录字节数到 debug 日志——便于观测"思考成本"
_NOISE_FIELDS_TO_LOG = ("reasoning_content", "thinking", "thinking_content",
                        "chain_of_thought", "cot")


def _strip_noise(message: dict[str, Any]) -> dict[str, Any]:
    """剥掉非 OpenAI 标准字段，返回干净的 assistant message。

    保留 OpenAI 协议规定的字段（role / content / tool_calls 等），把厂商特有
    的思考链等字段全部剔除。原 dict 不修改，返回新 dict。
    """
    noise_sizes = {
        f: len(str(message.get(f) or ""))
        for f in _NOISE_FIELDS_TO_LOG
        if message.get(f)
    }
    if noise_sizes:
        logger.debug(
            "model_client: 剥离厂商噪音字段 %s（不进 conversation history）",
            noise_sizes,
        )
    return {k: v for k, v in message.items() if k in _ASSISTANT_MESSAGE_ALLOWED_FIELDS}


class OpsModelClient:
    # OpenAI 协议合法的 tool_choice 取值（除了具体的 ``{type:function,...}`` 形式）
    _VALID_TOOL_CHOICE = frozenset({"auto", "required", "none"})

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: int = 120,
        tool_choice_default: str | None = None,
    ) -> None:
        self.base_url = self._normalize_base_url(base_url)
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        # tool_choice_default 来自 ``platform_model_config.tool_choice_preference``;
        # 由 admin 按模型实测行为配置（不同国产模型对 ``auto`` 的实现差异较大）。
        # None / 不合法值 → 走 OpenAI 协议默认 ``auto``。
        if tool_choice_default and tool_choice_default not in self._VALID_TOOL_CHOICE:
            logger.warning(
                "model %s 的 tool_choice_default=%r 不合法（应为 %s 之一），降级为 'auto'",
                model, tool_choice_default, sorted(self._VALID_TOOL_CHOICE),
            )
            tool_choice_default = None
        self.tool_choice_default = tool_choice_default

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
            # 优先级:调用方显式传入 > 模型配置默认 > 协议默认 "auto"。
            # 这样 agent 主循环可以对特定阶段（如最终报告 step）强制覆盖,
            # 平常走 admin 在管理后台为该模型调好的偏好。
            payload["tool_choice"] = tool_choice or self.tool_choice_default or "auto"

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
        # 4xx/5xx：在抛之前把 ark 返回的错误正文 + 请求 messages 摘要打日志
        # （400 这种状态码 ark 都会带 ``error.message`` 字段告诉你具体哪不对——
        # 上下文太长 / 字段非法 / tool_calls 不配对……）
        if response.status_code >= 400:
            body_preview = response.text[:1500]
            n_msgs = len(messages)
            roles = [m.get("role") for m in messages]
            char_total = sum(len(str(m.get("content") or "")) for m in messages)
            tool_msg_with_no_id = sum(
                1 for m in messages
                if m.get("role") == "tool" and not m.get("tool_call_id")
            )
            empty_content = sum(
                1 for m in messages
                if m.get("role") in ("user", "system") and not (m.get("content") or "").strip()
            )
            logger.error(
                "model %s returned %d: %s\n"
                "  request: msgs=%d roles=%s content_chars=%d empty=%d tool_no_id=%d tools=%s",
                response.url, response.status_code, body_preview,
                n_msgs, roles, char_total, empty_content,
                tool_msg_with_no_id, bool(tools),
            )
        response.raise_for_status()
        data = response.json()
        message = _strip_noise(data["choices"][0]["message"])
        # 把 OpenAI 兼容协议的 ``usage`` 字段挂到 message 上,让上层（agent.py）能累计
        # 每会话/每用户的 token 消耗,做监控 + 异常告警 + 成本分账。
        # 用下划线开头的 ``_usage`` 表明是平台内部字段,不会反送回模型——agent.py 的
        # ``messages.append({"role": "assistant", "content": ..., "tool_calls": ...})``
        # 已经显式只取 OpenAI 标准字段,_usage 自然落地不进 history。
        usage = data.get("usage") or {}
        if usage:
            message["_usage"] = {
                "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                "completion_tokens": int(usage.get("completion_tokens") or 0),
                "total_tokens": int(usage.get("total_tokens") or 0),
                "model": self.model,
            }
        return message

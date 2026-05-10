"""HTTP API 通用客户端。

平台所有"对接外部系统 HTTP API"的 skill 都共用这一个 client。
admin 在后台注册一个 ``http_api`` connection，driver 用本类构造实例。

支持的鉴权方式
--------------
- ``none``                     —— 不附鉴权 header
- ``bearer``                   —— ``Authorization: Bearer <token>``
- ``basic``                    —— ``Authorization: Basic <base64(user:pass)>``
- ``api_key_header``           —— 自定义 header 名 + 值，如 ``X-Api-Key: ...``
- ``oauth2_client_credentials`` —— Client Credentials Flow，token 自动缓存 + 401 时重新申请

所有凭证字段（``bearer_token``、``basic_pass``、``api_key``、``oauth2_client_secret``）
落到 ``platform_connection.config_json`` 时由 [crypto.py](../ops_platform/crypto.py) 透明加密。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin

import requests


logger = logging.getLogger(__name__)


@dataclass
class HttpResult:
    """统一返回结构，对齐 swarm/k8s client 风格便于审计。"""

    method: str
    url: str
    status: int
    headers: dict[str, str]
    json_body: Any | None
    text_body: str
    elapsed_ms: int
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and 200 <= self.status < 300

    def to_dict(self) -> dict[str, Any]:
        # 仅返回模型/审计真正要看的字段，省掉一些 boilerplate
        return {
            "method": self.method,
            "url": self.url,
            "status": self.status,
            "ok": self.ok,
            "elapsed_ms": self.elapsed_ms,
            "json_body": self.json_body,
            "text_body": self.text_body if self.json_body is None else None,
            "error": self.error,
        }


class HttpApiClient:
    """每个 ``http_api`` connection 对应一个实例（ConnectionManager 缓存）。

    线程安全：requests.Session 本身线程安全；OAuth2 token 缓存用 lock 保护。
    """

    AUTH_KINDS = {"none", "bearer", "basic", "api_key_header", "oauth2_client_credentials"}

    def __init__(
        self,
        *,
        base_url: str,
        auth_kind: str = "none",
        bearer_token: str = "",
        basic_user: str = "",
        basic_pass: str = "",
        api_key: str = "",
        api_key_header_name: str = "X-Api-Key",
        oauth2_token_url: str = "",
        oauth2_client_id: str = "",
        oauth2_client_secret: str = "",
        oauth2_scope: str = "",
        custom_headers: str = "",
        verify_ssl: bool = True,
        timeout_seconds: int = 30,
        proxy: str = "",
    ) -> None:
        if auth_kind not in self.AUTH_KINDS:
            raise ValueError(f"未知 auth_kind={auth_kind!r}，可选：{sorted(self.AUTH_KINDS)}")
        if not base_url:
            raise ValueError("base_url 不能为空")
        self.base_url = base_url.rstrip("/") + "/"
        self.auth_kind = auth_kind
        self.bearer_token = bearer_token
        self.basic_user = basic_user
        self.basic_pass = basic_pass
        self.api_key = api_key
        self.api_key_header_name = api_key_header_name or "X-Api-Key"
        self.oauth2_token_url = oauth2_token_url
        self.oauth2_client_id = oauth2_client_id
        self.oauth2_client_secret = oauth2_client_secret
        self.oauth2_scope = oauth2_scope
        self.verify_ssl = bool(verify_ssl)
        self.timeout_seconds = int(timeout_seconds or 30)

        self._session = requests.Session()
        if proxy:
            self._session.proxies = {"http": proxy, "https": proxy}

        # custom_headers 是多行 "Key: Value" 文本
        self._extra_headers = self._parse_custom_headers(custom_headers)

        # OAuth2 token 缓存
        self._oauth2_lock = threading.Lock()
        self._oauth2_token: str = ""
        self._oauth2_expires_at: float = 0.0

    # ---------- 公开调用 ----------

    def request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        json_body: Any = None,
        timeout: int | None = None,
    ) -> HttpResult:
        """发一次 HTTP 请求。path 可以是完整 URL（http(s)://...）或相对 base_url 的路径。

        失败时 **不抛异常**——把错误塞进 HttpResult.error，让 skill / 审计统一处理。
        """
        method = method.upper()
        if path.lower().startswith(("http://", "https://")):
            url = path
        else:
            url = urljoin(self.base_url, path.lstrip("/"))

        merged_headers = dict(self._extra_headers)
        if headers:
            merged_headers.update(headers)
        try:
            self._apply_auth(merged_headers)
        except Exception as exc:
            return HttpResult(
                method=method, url=url, status=0, headers={},
                json_body=None, text_body="", elapsed_ms=0,
                error=f"auth 失败：{exc}",
            )

        t0 = time.time()
        try:
            resp = self._session.request(
                method, url,
                params=query or None,
                headers=merged_headers,
                json=json_body if json_body is not None else None,
                verify=self.verify_ssl,
                timeout=timeout or self.timeout_seconds,
            )
        except requests.RequestException as exc:
            return HttpResult(
                method=method, url=url, status=0, headers={},
                json_body=None, text_body="", elapsed_ms=int((time.time() - t0) * 1000),
                error=str(exc),
            )
        elapsed_ms = int((time.time() - t0) * 1000)

        # OAuth2: 401 时尝试一次刷新 token 再重试（最多一次）
        if (resp.status_code == 401 and self.auth_kind == "oauth2_client_credentials"):
            self._invalidate_oauth2_token()
            try:
                self._apply_auth(merged_headers)
                resp = self._session.request(
                    method, url,
                    params=query or None,
                    headers=merged_headers,
                    json=json_body if json_body is not None else None,
                    verify=self.verify_ssl,
                    timeout=timeout or self.timeout_seconds,
                )
                elapsed_ms = int((time.time() - t0) * 1000)
            except requests.RequestException as exc:
                # 只接住网络层错误。其它（比如 _apply_auth 里的 KeyError /
                # AttributeError）属于配置/编程错误，应该直接冒出来——之前
                # broad except 把它们全裹成"OAuth2 重试失败"，让排障变难。
                return HttpResult(
                    method=method, url=url, status=0, headers={},
                    json_body=None, text_body="", elapsed_ms=elapsed_ms,
                    error=f"OAuth2 重试网络异常：{exc}",
                )

        # 解析 body
        json_b: Any | None = None
        text_b = resp.text or ""
        ctype = (resp.headers.get("content-type") or "").lower()
        if "json" in ctype or text_b.startswith(("{", "[")):
            try:
                json_b = resp.json()
            except (ValueError, requests.JSONDecodeError):
                json_b = None

        return HttpResult(
            method=method, url=url,
            status=resp.status_code,
            headers={k: v for k, v in resp.headers.items()},
            json_body=json_b,
            text_body=text_b if json_b is None else "",
            elapsed_ms=elapsed_ms,
        )

    def healthcheck(self) -> dict[str, Any]:
        """driver.validate() 调用：尝试一次 GET base_url/。"""
        result = self.request("GET", "/")
        return {
            "healthy": result.ok or 200 <= result.status < 500,  # 4xx 也认为通了（鉴权可能拦了 / 但网络通）
            "details": {
                "status": result.status,
                "elapsed_ms": result.elapsed_ms,
                "auth_kind": self.auth_kind,
                "error": result.error,
            },
        }

    # ---------- auth 内部 ----------

    def _apply_auth(self, headers: dict[str, str]) -> None:
        if self.auth_kind == "none":
            return
        if self.auth_kind == "bearer":
            if self.bearer_token:
                headers["Authorization"] = f"Bearer {self.bearer_token}"
            return
        if self.auth_kind == "basic":
            import base64
            raw = f"{self.basic_user}:{self.basic_pass}".encode("utf-8")
            headers["Authorization"] = f"Basic {base64.b64encode(raw).decode()}"
            return
        if self.auth_kind == "api_key_header":
            if self.api_key:
                headers[self.api_key_header_name] = self.api_key
            return
        if self.auth_kind == "oauth2_client_credentials":
            token = self._ensure_oauth2_token()
            if token:
                headers["Authorization"] = f"Bearer {token}"
            return

    def _ensure_oauth2_token(self) -> str:
        with self._oauth2_lock:
            if self._oauth2_token and time.time() < self._oauth2_expires_at - 30:
                return self._oauth2_token
            if not self.oauth2_token_url:
                raise RuntimeError("oauth2_client_credentials 模式必须配置 oauth2_token_url")
            data = {
                "grant_type": "client_credentials",
                "client_id": self.oauth2_client_id,
                "client_secret": self.oauth2_client_secret,
            }
            if self.oauth2_scope:
                data["scope"] = self.oauth2_scope
            resp = self._session.post(
                self.oauth2_token_url,
                data=data,
                verify=self.verify_ssl,
                timeout=self.timeout_seconds,
            )
            resp.raise_for_status()
            payload = resp.json()
            tok = payload.get("access_token")
            if not tok:
                raise RuntimeError(f"OAuth2 响应没有 access_token：{payload}")
            expires_in = int(payload.get("expires_in") or 3600)
            self._oauth2_token = tok
            self._oauth2_expires_at = time.time() + expires_in
            logger.info("OAuth2 token 已刷新，有效期 %ds", expires_in)
            return tok

    def _invalidate_oauth2_token(self) -> None:
        with self._oauth2_lock:
            self._oauth2_token = ""
            self._oauth2_expires_at = 0.0

    @staticmethod
    def _parse_custom_headers(text: str) -> dict[str, str]:
        out: dict[str, str] = {}
        if not text:
            return out
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
        return out

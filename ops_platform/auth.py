from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any


def hash_password(password: str, salt: str | None = None) -> str:
    """简单实现：PBKDF2-HMAC-SHA256。生产可换 bcrypt。"""
    if salt is None:
        salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000)
    return f"pbkdf2_sha256$120000${salt}${dk.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, iters, salt, dk_hex = encoded.split("$")
        if scheme != "pbkdf2_sha256":
            return False
        expected = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt.encode("utf-8"), int(iters)
        )
        return hmac.compare_digest(expected.hex(), dk_hex)
    except Exception:
        return False


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def issue_token(payload: dict[str, Any], secret: str, ttl_seconds: int = 24 * 3600) -> str:
    """极简 JWT (HS256)。"""
    header = {"alg": "HS256", "typ": "JWT"}
    now = int(time.time())
    body = {**payload, "iat": now, "exp": now + ttl_seconds}
    h = _b64url(json.dumps(header, separators=(",", ":")).encode())
    p = _b64url(json.dumps(body, separators=(",", ":")).encode())
    signing_input = f"{h}.{p}".encode()
    sig = hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()
    return f"{h}.{p}.{_b64url(sig)}"


def verify_token(token: str, secret: str) -> dict[str, Any] | None:
    try:
        h, p, s = token.split(".")
        signing_input = f"{h}.{p}".encode()
        expected = hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()
        if not hmac.compare_digest(_b64url(expected), s):
            return None
        body = json.loads(_b64url_decode(p))
        if int(body.get("exp", 0)) < int(time.time()):
            return None
        return body
    except Exception:
        return None

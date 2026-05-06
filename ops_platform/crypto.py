"""敏感字段对称加密（Fernet / AES-128-CBC + HMAC-SHA256）。

设计目标
--------
1. **DBA 看库不到明文**：``platform_connection.config_json`` 和
   ``platform_model_config.api_key`` 落库前加密。
2. **向后兼容**：旧版明文行不破坏；通过 ``enc:v1:`` 前缀区分。读时无前缀 → 当作明文。
3. **零依赖**：``cryptography`` 已经被 chainlit 间接拉进 requirements，无需额外装。
4. **运维友好**：未配置密钥时降级为明文（warning 日志），不阻塞启动；
   适合本地开发 / CI 临时跑。

启用方式
--------
1. 生成密钥：``python scripts/generate_encryption_key.py``
2. ``.env`` 设 ``PLATFORM_ENCRYPTION_KEY=<生成的 44 字符串>``
3. 重启 backend；旧明文 row 在第一次 update 时会自动加密；可手动调
   ``SQLPlatformStore.migrate_encrypt_existing()`` 一次性迁移。

⚠️ **绝对不要** 把这把 key 跟 DB 备份放一起；丢 key 等于丢数据。
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import threading
from typing import Any


logger = logging.getLogger(__name__)

PREFIX = "enc:v1:"
_KEY_ENV = "PLATFORM_ENCRYPTION_KEY"

_lock = threading.Lock()
_fernet = None
_warned_missing = False


def _build_fernet() -> Any | None:
    """读取环境变量，懒构造 Fernet。Key 支持两种形态：

    - **44 字符 Fernet key**（base64 32B）—— 直接使用
    - **任意长度 passphrase** —— sha256 派生 32 字节再 base64 编码

    后者是给运维方便用的（人类可读 key），不强迫他们用 ``Fernet.generate_key``。
    """
    raw = os.getenv(_KEY_ENV, "").strip()
    if not raw:
        return None
    try:
        from cryptography.fernet import Fernet  # 延迟 import
    except ImportError:
        logger.warning("cryptography 未安装，跳过加密")
        return None
    try:
        if len(raw) == 44 and raw.endswith("="):
            key_bytes = raw.encode("ascii")
        else:
            digest = hashlib.sha256(raw.encode("utf-8")).digest()
            key_bytes = base64.urlsafe_b64encode(digest)
        return Fernet(key_bytes)
    except Exception as exc:  # pragma: no cover
        logger.error("PLATFORM_ENCRYPTION_KEY 解析失败 (%s)，加密将禁用", exc)
        return None


def _get_fernet():
    global _fernet, _warned_missing
    with _lock:
        if _fernet is not None:
            return _fernet
        f = _build_fernet()
        if f is None:
            if not _warned_missing:
                logger.warning(
                    "%s 未配置；敏感字段将以明文落库。生产部署务必生成并设置一把 key。",
                    _KEY_ENV,
                )
                _warned_missing = True
            return None
        _fernet = f
        return _fernet


def is_active() -> bool:
    return _get_fernet() is not None


def is_encrypted(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def encrypt(plaintext: str | None) -> str | None:
    """无密钥则原样返回；已加密则不重复加密。None / 空串原样。"""
    if plaintext is None or plaintext == "":
        return plaintext
    if is_encrypted(plaintext):
        return plaintext
    f = _get_fernet()
    if f is None:
        return plaintext
    token = f.encrypt(plaintext.encode("utf-8")).decode("ascii")
    return PREFIX + token


def decrypt(value: str | None) -> str | None:
    """无前缀则当作明文返回（兼容旧数据）；密钥未配置仍返回原值并警告。"""
    if value is None or not isinstance(value, str):
        return value
    if not is_encrypted(value):
        return value
    f = _get_fernet()
    if f is None:
        logger.warning("收到 enc:v1: 数据但 PLATFORM_ENCRYPTION_KEY 未配置；返回原文")
        return value
    try:
        from cryptography.fernet import InvalidToken
    except ImportError:
        return value
    try:
        return f.decrypt(value[len(PREFIX):].encode("ascii")).decode("utf-8")
    except InvalidToken:
        logger.error("decrypt 失败：密钥不匹配或数据损坏")
        return value


def reset_for_test() -> None:
    """单元测试用：清掉缓存的 fernet 让下次重新读 env。"""
    global _fernet, _warned_missing
    with _lock:
        _fernet = None
        _warned_missing = False

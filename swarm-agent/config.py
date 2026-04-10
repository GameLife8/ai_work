from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _first_env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return default


def _as_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    model_api_key: str
    model_base_url: str
    model_name: str
    docker_bin: str
    docker_host: str
    docker_tls_verify: str
    docker_cert_path: str
    chainlit_auth: bool
    chainlit_port: int
    log_default_tail: int
    log_max_tail: int


def get_settings() -> Settings:
    return Settings(
        model_api_key=_first_env("MODEL_API_KEY", "QWEN_API_KEY"),
        model_base_url=_first_env("MODEL_BASE_URL", "QWEN_BASE_URL"),
        model_name=_first_env("MODEL_NAME", "QWEN_MODEL", default="qwen-plus"),
        docker_bin=os.getenv("DOCKER_BIN", "docker"),
        docker_host=os.getenv("DOCKER_HOST", "tcp://169.24.216.227:3389"),
        docker_tls_verify=os.getenv("DOCKER_TLS_VERIFY", ""),
        docker_cert_path=os.getenv("DOCKER_CERT_PATH", ""),
        chainlit_auth=os.getenv("CHAINLIT_AUTH", "false").lower() == "true",
        chainlit_port=_as_int("CHAINLIT_PORT", 8000),
        log_default_tail=_as_int("LOG_DEFAULT_TAIL", 100),
        log_max_tail=_as_int("LOG_MAX_TAIL", 1000),
    )

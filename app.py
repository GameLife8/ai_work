from __future__ import annotations

import atexit
import logging
import os
import signal

from flask import Flask

from admin_app import init_admin
from config import Config
from routers.alert_router import alert_bp
from routers.system_router import system_bp
from runtime import create_runtime
from utils.logger import configure_logging


logger = logging.getLogger(__name__)


def _wire_graceful_shutdown(runtime) -> None:
    """注册 atexit + SIGTERM/SIGINT handler 让 runtime.shutdown 被调到。

    场景:
    - **正常 exit** (代码跑完 / Ctrl-C in REPL): atexit 触发
    - **SIGTERM** (k8s rolling update, docker stop): signal handler 触发
    - **SIGINT** (Ctrl-C 终端): signal handler 触发

    幂等:runtime.shutdown 内部有 done flag,多次触发只执行一次。

    在 pytest 环境下完全跳过——单测会反复调 create_app() 累积 atexit handler,
    pytest 进程退出时一并触发,会在已关闭的 stdout 上 dump"I/O operation on
    closed file"噪音。测试不需要 graceful shutdown,跳过最干净。
    """
    shutdown = getattr(runtime, "shutdown", None)
    if not callable(shutdown):
        return
    # 探测是否在 pytest 内运行（pytest 自动设置该 env）
    if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("PYTEST_VERSION"):
        return
    atexit.register(shutdown)

    def _signal_handler(signum, _frame):
        logger.info("收到 signal %d,触发 runtime.shutdown", signum)
        shutdown()
        # SIGTERM 收到后我们应该退出;让默认 handler 跑(不阻塞 supervisor)
        raise SystemExit(0)

    # Windows 不支持 SIGTERM;只注册存在的信号
    for sig_name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _signal_handler)
        except (ValueError, OSError):    # pragma: no cover
            # 非主线程注册 signal 会抛 ValueError;开发模式 reloader 也可能踩到
            pass


def create_app() -> Flask:
    configure_logging()

    app = Flask(__name__)
    app.config.from_object(Config)

    runtime = create_runtime(Config)
    _wire_graceful_shutdown(runtime)

    app.extensions["runtime"] = runtime
    app.extensions["alert_service"] = runtime.alert_service
    app.extensions["store"] = runtime.store
    app.register_blueprint(alert_bp)
    app.register_blueprint(system_bp)
    init_admin(app, runtime, Config)
    return app


app = create_app()


if __name__ == "__main__":
    app.run(host=Config.APP_HOST, port=Config.APP_PORT, debug=True)

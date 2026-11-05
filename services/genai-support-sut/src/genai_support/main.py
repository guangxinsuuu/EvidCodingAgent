"""python -m genai_support.main → 配置 → 只读fixture → FastAPI → Uvicorn。

启动无数据库、Redis或模型调用；不会自动读取.env或输出token。
SIGINT/SIGTERM由Uvicorn收尾；关闭本进程监听不触碰其他实验资源。
"""

import json
import os
import socket
import sys

import uvicorn

from .config import DEFAULT_FIXTURE_PATH, ConfigurationError, load_config
from .fixtures import load_fixture_bundle
from .server import create_app


def main() -> int:
    """命令入口：启动前失败返回2；只使用固定本地fixture，不加载验证期望。"""
    try:
        settings = load_config(os.environ)
        fixtures = load_fixture_bundle(DEFAULT_FIXTURE_PATH)
        app = create_app(settings, fixtures)
    except (ConfigurationError, ValueError, OSError):
        print("SUPPORT_STARTUP_REJECTED: check configuration and pinned synthetic fixtures", file=sys.stderr)
        return 2
    # 先绑定本机socket再交给ASGI server；端口0时报告的是内核分配值而非规划值。
    # socket_bound事件只声明资源身份，客户端仍须实际GET /health确认HTTP已启动。
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind((settings.host, settings.port))
            listener.listen(128)
        except OSError:
            print("SUPPORT_BIND_REJECTED", file=sys.stderr)
            return 2
        print(json.dumps({"event": "support_socket_bound", "host": settings.host,
                          "port": listener.getsockname()[1], "pid": os.getpid(),
                          "scope": "support-service-skeleton"}), flush=True)
        config = uvicorn.Config(app, host=settings.host, port=settings.port, access_log=False,
                                proxy_headers=False, server_header=False, log_level="warning")
        uvicorn.Server(config).run(sockets=[listener])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

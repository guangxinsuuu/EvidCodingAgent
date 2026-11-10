"""宿主 subprocess 入口：0600 私有配置 → 迁移 → loopback 动态端口 → Uvicorn。

复用现有 SUT Python 依赖；无需再安装另一套框架。stdout 仅产生脱敏 READY
回执供宿主登记资源。过期同时由每请求门禁和进程定时退出执行，不触碰系统时钟。
"""

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
import signal
import socket
import sys
from threading import Timer

import uvicorn

from .protocol import Settings, timestamp
from .server import create_app
from .store import CommercialStore


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    try:
        settings = Settings.from_private_file(args.config)
        remaining = (timestamp(settings.expires_at) - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0:
            raise ValueError("EXPIRED")
        store = CommercialStore(settings.database_url, settings.read_database_url)
        store.migrate()
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", settings.port))
        listener.listen(128)
        port = listener.getsockname()[1]
        app = create_app(settings, store)

        class SourceServer(uvicorn.Server):
            async def startup(self, sockets=None):
                await super().startup(sockets)
                if self.started:
                    print(json.dumps({"event": "COMMERCIAL_SOURCE_READY", "instance_id": settings.instance_id,
                                      "host": "127.0.0.1", "port": port, "pid": os.getpid()}), flush=True)

        server = SourceServer(uvicorn.Config(app, host="127.0.0.1", port=port, access_log=False, log_level="critical"))
        deadline = Timer(remaining, lambda: os.kill(os.getpid(), signal.SIGTERM))
        deadline.daemon = True
        deadline.start()
        try:
            asyncio.run(server.serve(sockets=[listener]))
        finally:
            deadline.cancel()
            listener.close()
        return 0
    except Exception:
        print(json.dumps({"error_code": "COMMERCIAL_SOURCE_START_FAILED"}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

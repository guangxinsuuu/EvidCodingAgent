"""受信本机宿主 → 本进程角色 → API / relay / Worker / gateway / 本地模型。

每个进程只读取自己的0600配置。候选Worker没有网关数据库/模型供应商凭据。
这是本机校准拓扑，不具备不同OS用户/容器文件隔离；正式隔离资格另行验证。
"""

import argparse
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from urllib.request import ProxyHandler, build_opener

import redis
from redis.backoff import NoBackoff
from redis.retry import Retry
import uvicorn

from .config import DEFAULT_FIXTURE_PATH, Settings
from .fixtures import load_fixture_bundle
from .gateway_store import GatewayStore
from .model_gateway import GatewaySettings, create_gateway_app
from .relay import Relay
from .server import create_app
from .store import Store
from .worker import GatewayClient, Worker

_LAUNCH: dict = {}


def redis_client(url: str) -> redis.Redis:
    """只使用宿主传入的实验连接；关闭客户端隐式重试，模糊回执交上层对账。"""
    return redis.Redis.from_url(url, decode_responses=True, socket_timeout=3, socket_connect_timeout=3,
                                retry=Retry(NoBackoff(), 0), retry_on_timeout=False)


def _receipt(directory: Path, role: str, data: dict) -> None:
    path = directory / f"{role}.receipt.json"
    temp = path.with_suffix(".tmp")
    with temp.open("w") as output:
        os.chmod(temp, 0o600)
        signature = subprocess.run(["ps", "-p", str(os.getpid()), "-o", "lstart=", "-o", "command="], capture_output=True, text=True, timeout=3).stdout.strip()
        json.dump({"pid": os.getpid(), "role": role, "signature": signature, **_LAUNCH, **data}, output)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temp, path)


def run_role(role: str, config: dict, directory: Path) -> None:
    """单角色生命周期；expires_at是本地环境上限，SIGTERM停止新增执行并收尾。"""
    stop = threading.Event()
    lifetime = max(0.0, datetime.fromisoformat(config["expires_at"].replace("Z", "+00:00")).timestamp() - time.time())
    timer = threading.Timer(lifetime, stop.set)
    timer.daemon = True
    timer.start()
    for event in (signal.SIGTERM, signal.SIGINT):
        signal.signal(event, lambda *_: stop.set())
    if role == "model":
        model_home = directory / "model-home"
        model_home.mkdir(mode=0o700, exist_ok=True)
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(model_home),
               "OLLAMA_MODELS": config["model_directory"], "OLLAMA_HOST": f"127.0.0.1:{config['port']}",
               "OLLAMA_NO_CLOUD": "1", "OLLAMA_NUM_PARALLEL": "1", "OLLAMA_CONTEXT_LENGTH": "4096",
               "OLLAMA_KEEP_ALIVE": "5m"}
        child = subprocess.Popen([config["ollama_binary"], "serve"], env=env)
        child_signature = subprocess.run(["ps", "-p", str(child.pid), "-o", "lstart=", "-o", "command="], capture_output=True, text=True, timeout=3).stdout.strip()
        _receipt(directory, role, {"child_pid": child.pid, "child_signature": child_signature, "port": config["port"], "status": "STARTING"})
        try:
            while not stop.wait(0.2):
                if child.poll() is not None:
                    raise RuntimeError("MODEL_PROCESS_EXITED")
        finally:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=3)
        return
    if role in {"relay", "worker"}:
        store = Store(config["database_url"])
        queue = redis_client(config["redis_url"])
        _receipt(directory, role, {"status": "STARTING"})
        component = Relay(store, queue, owner=config["instance_id"] + "-relay") if role == "relay" else Worker(
            store, queue, GatewayClient(config["gateway_url"], config["gateway_token"]),
            owner=config["instance_id"] + "-worker-" + str(os.getpid()))
        try:
            component.run_forever(stop)
        finally:
            queue.close()
        return
    fixtures = load_fixture_bundle(DEFAULT_FIXTURE_PATH)
    if role == "gateway":
        settings = GatewaySettings(token=config["gateway_token"], execution_mode=config["execution_mode"],
            model_endpoint=config.get("model_endpoint"), model_name=config.get("model_name"),
            job_reader_dsn=config["reader_database_url"], max_calls=config["max_calls"],
            max_output_tokens=1024, timeout_seconds=120, budget_id=config["instance_id"])
        app = create_gateway_app(settings, fixtures, GatewayStore(config["database_url"]))
    elif role == "api":
        store = Store(config["database_url"])
        queue = redis_client(config["redis_url"])
        opener = build_opener(ProxyHandler({}))

        def readiness() -> dict:
            checks = {"database": store.health(), "redis": bool(queue.ping()), **store.service_health()}
            try:
                with opener.open(config["gateway_url"] + "/health", timeout=2) as response:
                    checks["gateway"] = response.status == 200
            except (OSError, ValueError):
                checks["gateway"] = False
            checks["execution_mode"] = config["execution_mode"]
            return checks

        app = create_app(Settings(api_token=config["api_token_a"], tenant_id="tenant-a"), fixtures, store,
                         sessions={config["api_token_a"]: "tenant-a", config["api_token_b"]: "tenant-b"}, runtime_health=readiness)
    else:
        raise ValueError("INVALID_ROLE")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", config.get("port", 0)))
        listener.listen(128)
        server = uvicorn.Server(uvicorn.Config(app, access_log=False, proxy_headers=False, server_header=False,
                                               log_level="warning", timeout_graceful_shutdown=140))
        _receipt(directory, role, {"port": listener.getsockname()[1], "status": "SOCKET_BOUND"})
        def stop_server() -> None:
            stop.wait()
            server.should_exit = True
        threading.Thread(target=stop_server, daemon=True).start()
        server.run(sockets=[listener])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--role", choices=("api", "worker", "relay", "gateway", "model"), required=True)
    parser.add_argument("--instance", required=True)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    try:
        path = args.config
        if path.is_symlink() or path.stat().st_mode & 0o077:
            raise ValueError("PRIVATE_CONFIG_REQUIRED")
        config = json.loads(path.read_text())
        if config["instance_id"] != args.instance:
            raise ValueError("INSTANCE_MISMATCH")
        _LAUNCH.update(instance_id=args.instance, launch_id=config["launch_id"])
        _receipt(path.parent, args.role, {"status": "BOOTING"})
        run_role(args.role, config, path.parent)
        return 0
    except Exception:
        print("SUPPORT_ROLE_FAILED", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

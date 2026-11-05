#!/usr/bin/env python3
"""03 环境只读预检：main → collect_preflight → 有界命令/loopback 探针。

仅报告可观察到的本机条件，不启动服务、不安装依赖、不加载 .env、不调用模型。
命令原始 stderr/环境值不进入报告；工具存在与业务服务可用分别记录。
运行：python3 tools/genai-support-preflight.py > artifacts/.../preflight.json
"""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import errno
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from typing import Any, Callable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
VERSION = re.compile(r"(?<![\w.])v?(\d+\.\d+(?:\.\d+)?(?:[-+][\w.]+)?)(?![\w.])")
TOOLS = {
    "node": ("node", "--version"),
    "pnpm": ("pnpm", "--version"),
    "uv": ("uv", "--version"),
    "docker": ("docker", "--version"),
    "compose": ("docker", "compose", "version", "--short"),
    "postgres_client": ("psql", "--version"),
    "redis_client": ("redis-cli", "--version"),
}
DEPENDENCIES = ("pip", "uv", "fastapi", "pydantic", "uvicorn", "jsonschema", "httpx2", "psycopg", "redis")
MODEL_ENV_NAMES = (
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "EGAP_MODEL_GATEWAY_URL",
    "EGAP_MODEL_ID", "EGAP_MODEL_MAX_REQUESTS", "EGAP_MODEL_MAX_TOKENS",
    "EGAP_MODEL_MAX_COST",
)


def run_command(argv: tuple[str, ...], timeout: float = 4.0) -> dict[str, Any]:
    """只执行固定探测命令；失败只返回类别，避免 CLI 将 URL 或凭据写进报告。

    stdout 仅供下游按白名单提取版本/镜像字段，不得把它整个输出到最终 JSON。
    timeout 是一次探测的上限，失败不能被解释成组件不存在或已停止。
    """
    if shutil.which(argv[0]) is None:
        return {"status": "NOT_INSTALLED", "exit_code": None, "stdout": ""}
    try:
        # daemon 查询固定本机 socket 和临时空配置；不继承远端 context/TLS/凭据。
        # compose version 仅查询 CLI 插件，不连接 daemon，保留默认插件发现路径。
        daemon_command = argv[0] == "docker" and argv[1] in ("info", "image")
        with tempfile.TemporaryDirectory(prefix="egap-preflight-docker-") as config:
            environment = {key: value for key, value in os.environ.items() if not key.startswith("DOCKER_")}
            command = argv
            if daemon_command:
                socket_path = Path.home() / ".docker" / "run" / "docker.sock"
                command = ("docker", "--config", config, "--host", f"unix://{socket_path}", *argv[1:])
            result = subprocess.run(
                command, cwd=PROJECT_ROOT, capture_output=True, text=True,
                timeout=timeout, check=False, env=environment,
            )
    except subprocess.TimeoutExpired:
        return {"status": "TIMEOUT", "exit_code": None, "stdout": ""}
    except (OSError, UnicodeError):
        return {"status": "UNAVAILABLE", "exit_code": None, "stdout": ""}
    return {
        "status": "OBSERVED" if result.returncode == 0 else "UNAVAILABLE",
        "exit_code": result.returncode,
        "stdout": result.stdout[:65_536] if result.returncode == 0 else "",
    }


def tool_version(argv: tuple[str, ...], runner: Callable = run_command) -> dict[str, Any]:
    """提取版本号而非原始命令输出；CLI 可执行不表示对应服务已经 ready。"""
    observation = runner(argv)
    match = VERSION.search(observation["stdout"])
    return {
        "command": list(argv), "status": observation["status"],
        "exit_code": observation["exit_code"], "version": match.group(1) if match else None,
    }


def probe_port(port: int, connector: Callable = socket.create_connection) -> dict[str, Any]:
    """仅探测三个预定 loopback 端口；接受连接不证明服务种类或资源归属。

    EPERM/超时不能当成端口空闲，因此保留 UNKNOWN。探针不发送应用数据。
    """
    try:
        with connector(("127.0.0.1", port), timeout=0.3):
            return {"port": port, "status": "ACCEPTING_CONNECTIONS", "service_identity": "UNKNOWN"}
    except OSError as exc:
        status = "CONNECTION_REFUSED" if exc.errno == errno.ECONNREFUSED else "UNKNOWN"
        return {"port": port, "status": status, "service_identity": "UNKNOWN"}


def docker_state(runner: Callable = run_command) -> dict[str, Any]:
    """读取 daemon 版本和目标 postgres/redis 本地镜像，不拉取/构建/启动容器。

    Docker CLI 缺失、权限不足、daemon 未启动等分别保留探测状态；仅凭本地
    镜像存在不能宣称已冻结 digest 或具备实际业务拓扑。
    """
    daemon = runner(("docker", "info", "--format", "{{.ServerVersion}}"))
    match = VERSION.fullmatch(daemon["stdout"].strip())
    images: dict[str, Any] = {}
    if daemon["status"] == "OBSERVED" and match:
        for repository in ("postgres", "redis"):
            result = runner(("docker", "image", "ls", "--no-trunc", "--format", "{{.ID}} {{.Tag}}", repository))
            records = []
            for line in result["stdout"].splitlines():
                fields = line.split()
                if len(fields) == 2 and re.fullmatch(r"sha256:[a-f0-9]{64}", fields[0]) and re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", fields[1]):
                    records.append({"local_image_id": fields[0], "tag": fields[1]})
            images[repository] = {"status": result["status"], "images": records[:64]}
    return {
        "daemon": {"status": daemon["status"], "exit_code": daemon["exit_code"],
                   "server_version": match.group(1) if match else None},
        "daemon_transport": "FIXED_LOCAL_DESKTOP_UNIX_SOCKET",
        "local_images": images,
        "runtime_images_frozen": False, "service_topology": "NOT_STARTED_BY_PREFLIGHT",
    }


def model_access(environ: dict[str, str] | os._Environ[str] = os.environ) -> dict[str, Any]:
    """只看约定变量是否非空；不会输出其值、加载 .env 或发认证请求。

    环境中存在 key 不代表受控网关、预算、数据通道获准。即使所有字段存在，
    此只读预检仍保持 NOT_RUN，实际模型准入由后续独立步骤承担。
    """
    return {
        "status": "NOT_RUN", "provider_request_count": 0,
        "environment_presence_only": {name: bool(environ.get(name)) for name in MODEL_ENV_NAMES},
        "controlled_gateway": "NOT_VALIDATED", "approved_model_and_budget": "NOT_VALIDATED",
        "credential_values_reported": False,
    }


def collect_preflight() -> dict[str, Any]:
    """CLI 编排入口；并行只读版本探测后汇总环境证据，绝不生成业务 PASS。"""
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        pending = {name: executor.submit(tool_version, argv) for name, argv in TOOLS.items()}
        versions = {name: future.result() for name, future in pending.items()}
    installed = {}
    for name in DEPENDENCIES:
        try:
            installed[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            installed[name] = None
    disk = shutil.disk_usage(PROJECT_ROOT)
    memory = None
    if platform.system() == "Darwin":
        observation = run_command(("sysctl", "-n", "hw.memsize"))
        value = observation["stdout"].strip()
        if observation["status"] == "OBSERVED" and value.isdecimal():
            memory = int(value)
    return {
        "schema_version": 1, "kind": "genai-support-host-preflight",
        "observed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "scope": "READ_ONLY_HOST_OBSERVATION", "business_verification": "NOT_RUN",
        "host": {"os": platform.system(), "architecture": platform.machine(),
                 "cpu_count": os.cpu_count(), "physical_memory_bytes": memory,
                 "workspace_disk_free_bytes": disk.free},
        "python": {"version": platform.python_version(), "implementation": platform.python_implementation(),
                   "executable": sys.executable, "installed_dependencies": installed,
                   "sut_dependency_lock_verified": False},
        "tools": versions, "docker": docker_state(),
        "loopback_ports": [probe_port(port) for port in (5432, 6379, 4321)],
        "model_access": model_access(),
        "service_checks": {"postgres": "NOT_AUTHENTICATED", "redis": "NOT_AUTHENTICATED",
                           "support_api": "NOT_RUN", "worker": "NOT_RUN", "tool_gateway": "NOT_RUN"},
    }


if __name__ == "__main__":
    print(json.dumps(collect_preflight(), indent=2, ensure_ascii=False))

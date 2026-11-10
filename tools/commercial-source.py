#!/usr/bin/env python3
"""商业来源实验宿主：独立 PostgreSQL → 来源进程 → 只读 SUT 配置投影。

本工具只管理本次 directory 中登记、并由 Docker 标签确认的资源。stop 保留卷，
down 删除本实验卷；不会连接或清理 Chatwoot/Lago/Keycloak 的 Compose 项目。
源码摘要、租期、PID/启动签名与动态端口均持久化，恢复不得悄悄换来源或身份。
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import time
from urllib.request import ProxyHandler, Request, build_opener
from uuid import uuid4

import psycopg
from psycopg import sql


ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "artifacts/business-source"
SERVICE = ROOT / "services/commercial-source"
PYTHON = ROOT / "services/genai-support-sut/.venv/bin/python"
PROJECT = "egap-commercial-source"
IMAGE = "postgres@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea"
API_VERSION = "commercial-source-v1"


class HostError(RuntimeError):
    """只向终端传递稳定错误码，不回显 Docker/数据库原文或任何凭据。"""


def demand(condition: bool, code: str) -> None:
    if not condition:
        raise HostError(code)


def save(path: Path, value: object) -> None:
    """私有原子落盘；临时文件不跟随同名链接，写完 fsync 后替换。"""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def private_read(path: Path) -> dict:
    demand(path.is_file() and not path.is_symlink() and not path.stat().st_mode & 0o077,
           "PRIVATE_REGULAR_FILE_REQUIRED")
    value = json.loads(path.read_text())
    demand(isinstance(value, dict), "INVALID_STATE")
    return value


def directory_path(value: str | Path) -> Path:
    """只接受 artifacts/business-source 下的一个实验目录，防误指向既有产品。"""
    path = Path(value).absolute()
    demand(path.parent.resolve() == BASE.resolve() and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", path.name) is not None,
           "EXACT_BUSINESS_SOURCE_DIRECTORY_REQUIRED")
    for ancestor in (path, *path.parents):
        demand(not ancestor.is_symlink(), "DIRECTORY_SYMLINK_FORBIDDEN")
    return path


def source_manifest() -> dict:
    """来源实现单独绑定；业务对象 revision 由来源数据库管理，不作为候选源码。"""
    files = []
    for base in (SERVICE / "src", SERVICE / "migrations"):
        if base.exists():
            files.extend(p for p in base.rglob("*") if p.suffix in {".py", ".sql", ".json"} and "__pycache__" not in p.parts)
    demand(bool(files), "COMMERCIAL_SOURCE_IMPLEMENTATION_MISSING")
    manifest = {}
    for path in sorted(files):
        demand(path.is_file() and not path.is_symlink(), "SOURCE_SYMLINK_FORBIDDEN")
        for parent in path.parents:
            if parent == SERVICE.parent:
                break
            demand(not parent.is_symlink(), "SOURCE_SYMLINK_FORBIDDEN")
        manifest[str(path.relative_to(ROOT))] = hashlib.sha256(path.read_bytes()).hexdigest()
    digest = hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"sha256": digest, "files": manifest}


def validate_state(directory: Path, state: dict) -> None:
    identity = state.get("instance_id", "")
    demand(re.fullmatch(r"commercial_[a-f0-9]{32}", identity) is not None, "INVALID_INSTANCE_ID")
    suffix = identity.removeprefix("commercial_")
    demand(state.get("directory") == str(directory) and state.get("project") == PROJECT,
           "STATE_DIRECTORY_MISMATCH")
    demand(state.get("container_name") == "egap-commercial-pg-" + suffix
           and state.get("volume_name") == "egap-commercial-pg-" + suffix, "RESOURCE_NAME_MISMATCH")


def labels(state: dict) -> dict[str, str]:
    return {"io.egap.project": PROJECT, "io.egap.instance": state["instance_id"], "io.egap.run": state["run_id"]}


def require_owner(state: dict, observed: dict | None) -> None:
    demand(isinstance(observed, dict) and all(observed.get(k) == v for k, v in labels(state).items()),
           "RESOURCE_OWNERSHIP_MISMATCH")


def docker(directory: Path, arguments: list[str], *, missing: str | None = None, timeout: int = 40) -> str | None:
    """固定本机 socket，不继承 DOCKER_HOST/context，不打印原始错误或容器环境。"""
    command = ["docker", "--host", "unix://" + str(Path.home() / ".docker/run/docker.sock"), *arguments]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout,
                                env={k: v for k, v in os.environ.items() if k in {"PATH", "HOME", "LANG"}})
    except (OSError, subprocess.TimeoutExpired):
        raise HostError("DOCKER_RECEIPT_UNCONFIRMED") from None
    if result.returncode == 0:
        return result.stdout.strip()
    # 只有指定对象的明确不存在回执才允许重新创建；连接失败不能冒充缺失。
    message = re.sub(r"^Error(?: response from daemon)?:\s*", "", result.stderr.strip(), flags=re.IGNORECASE)
    if missing and message in {f"No such object: {missing}", f"No such container: {missing}",
                               f"No such volume: {missing}", f"get {missing}: no such volume"}:
        return None
    raise HostError("DOCKER_OPERATION_FAILED")


def inspect_container(directory: Path, state: dict) -> dict | None:
    # 仅投影需要的字段，绝不读取/输出 Config.Env（其中包含 PostgreSQL 口令）。
    fmt = '{"id":{{json .Id}},"labels":{{json .Config.Labels}},"state":{{json .State.Status}},"ports":{{json .NetworkSettings.Ports}},"mounts":{{json .Mounts}},"image":{{json .Image}}}'
    raw = docker(directory, ["container", "inspect", "--format", fmt, state["container_name"]], missing=state["container_name"])
    if raw is None:
        return None
    value = json.loads(raw)
    require_owner(state, value["labels"])
    demand(not state.get("container_id") or state["container_id"] == value["id"], "CONTAINER_ID_CHANGED")
    mounts = value["mounts"]
    demand(len(mounts) == 1 and mounts[0].get("Type") == "volume" and mounts[0].get("Name") == state["volume_name"]
           and mounts[0].get("Destination") == "/var/lib/postgresql/data", "DATABASE_MOUNT_MISMATCH")
    demand(not state.get("image_id") or state["image_id"] == value["image"], "DATABASE_IMAGE_MISMATCH")
    return value


def inspect_volume(directory: Path, state: dict) -> dict | None:
    raw = docker(directory, ["volume", "inspect", "--format", '{"name":{{json .Name}},"labels":{{json .Labels}},"driver":{{json .Driver}},"options":{{json .Options}}}', state["volume_name"]], missing=state["volume_name"])
    if raw is None:
        return None
    value = json.loads(raw)
    require_owner(state, value["labels"])
    demand(value["driver"] == "local" and not value["options"], "DATABASE_VOLUME_OPTIONS_FORBIDDEN")
    return value


def canonical_signature(value: str) -> str:
    """macOS venv 会 re-exec Python.app；只归一化已知解释器别名。"""
    marker = " -m commercial_source "
    if marker not in value:
        return value
    executable = value.split(marker, 1)[0][24:].strip()
    aliases = {str(PYTHON), str(PYTHON.resolve()), str(Path(sys.base_prefix) / "Resources/Python.app/Contents/MacOS/Python")}
    return value[:24] + marker + value.split(marker, 1)[1] if executable in aliases else value


def process_signature(pid: int) -> str | None:
    demand(type(pid) is int and pid > 1, "INVALID_PID")
    result = subprocess.run(["ps", "-p", str(pid), "-o", "lstart=", "-o", "command="], capture_output=True, text=True, timeout=3)
    return canonical_signature(result.stdout.strip()) if result.returncode == 0 and result.stdout.strip() else None


def http_health(url: str) -> dict:
    with build_opener(ProxyHandler({})).open(Request(url), timeout=3) as response:
        demand(response.status == 200, "SOURCE_NOT_READY")
        raw = response.read(65537)
        demand(len(raw) <= 65536, "HEALTH_RESPONSE_TOO_LARGE")
        return json.loads(raw)


def ensure_database(directory: Path, state: dict, secret: dict) -> dict:
    """可恢复准备器：固定命名先查所属资源；业务库、测试库及角色完全分开。"""
    label_args = [part for key, value in labels(state).items() for part in ("--label", f"{key}={value}")]
    image = docker(directory, ["image", "inspect", "--format", "{{.Id}}", IMAGE])
    demand(isinstance(image, str) and re.fullmatch(r"sha256:[a-f0-9]{64}", image) is not None, "CACHED_POSTGRES_IMAGE_REQUIRED")
    demand(not state.get("image_id") or state["image_id"] == image, "DATABASE_IMAGE_CHANGED")
    state["image_id"] = image
    save(directory / "state.json", state)
    if inspect_volume(directory, state) is None:
        docker(directory, ["volume", "create", "--driver", "local", *label_args, state["volume_name"]])
        demand(inspect_volume(directory, state) is not None, "VOLUME_RECEIPT_UNCONFIRMED")
    container = inspect_container(directory, state)
    if container is None:
        demand(not state.get("container_id"), "REGISTERED_CONTAINER_MISSING")
        # 口令仅从 0600 文件传给 Docker，既不进入命令参数，也不回显 inspect Env。
        envfile = directory / "postgres.private.env"
        descriptor = os.open(envfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600) if not envfile.exists() else None
        if descriptor is not None:
            with os.fdopen(descriptor, "w") as stream:
                stream.write("POSTGRES_PASSWORD=" + secret["postgres_password"] + "\nPOSTGRES_DB=postgres\n")
        demand(not envfile.is_symlink() and not envfile.stat().st_mode & 0o077, "PRIVATE_REGULAR_FILE_REQUIRED")
        docker(directory, ["run", "-d", "--pull=never", "--name", state["container_name"], *label_args,
                           "--publish", "127.0.0.1::5432", "--env-file", str(envfile),
                           "--mount", f"type=volume,source={state['volume_name']},target=/var/lib/postgresql/data", IMAGE])
        container = inspect_container(directory, state)
    demand(container is not None, "CONTAINER_RECEIPT_UNCONFIRMED")
    state["container_id"] = container["id"]
    save(directory / "state.json", state)
    if container["state"] != "running":
        docker(directory, ["container", "start", container["id"]])
        container = inspect_container(directory, state)
    mapping = container["ports"].get("5432/tcp")
    demand(isinstance(mapping, list) and len(mapping) == 1 and mapping[0]["HostIp"] == "127.0.0.1", "DATABASE_LOOPBACK_PORT_REQUIRED")
    port = int(mapping[0]["HostPort"])
    state["postgres_port"] = port
    save(directory / "state.json", state)

    def dsn(user: str, password: str, database: str) -> str:
        return f"postgresql://{user}:{password}@127.0.0.1:{port}/{database}"

    deadline = time.monotonic() + 30
    while True:
        try:
            with psycopg.connect(dsn("postgres", secret["postgres_password"], "postgres"), connect_timeout=2, autocommit=True) as conn:
                for role, key in (("commercial_source", "writer_password"), ("commercial_reader", "reader_password"), ("commercial_test", "test_password")):
                    if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone():
                        conn.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(sql.Identifier(role), sql.Literal(secret[key])))
                for database, owner in (("commercial_source", "commercial_source"), ("commercial_source_test", "commercial_test")):
                    if not conn.execute("SELECT 1 FROM pg_database WHERE datname=%s", (database,)).fetchone():
                        conn.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(sql.Identifier(database), sql.Identifier(owner)))
                    conn.execute(sql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC").format(sql.Identifier(database)))
                    conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(database), sql.Identifier(owner)))
                conn.execute("GRANT CONNECT ON DATABASE commercial_source TO commercial_reader")
            break
        except psycopg.OperationalError:
            if time.monotonic() >= deadline:
                raise HostError("DATABASE_START_TIMEOUT") from None
            time.sleep(0.2)
    connections = {"database_url": dsn("commercial_source", secret["writer_password"], "commercial_source"),
                   "read_database_url": dsn("commercial_reader", secret["reader_password"], "commercial_source"),
                   "test_database_url": dsn("commercial_test", secret["test_password"], "commercial_source_test")}
    with psycopg.connect(connections["database_url"], connect_timeout=3) as conn:
        conn.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        conn.execute("CREATE SCHEMA IF NOT EXISTS egap_commercial")
        conn.execute("GRANT USAGE ON SCHEMA egap_commercial TO commercial_reader")
        conn.execute("GRANT SELECT ON ALL TABLES IN SCHEMA egap_commercial TO commercial_reader")
        conn.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA egap_commercial GRANT SELECT ON TABLES TO commercial_reader")
    with psycopg.connect(connections["test_database_url"], connect_timeout=3) as conn:
        conn.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
    save(directory / "connections.private.json", connections)
    return connections


def start_service(directory: Path, state: dict, secret: dict, connections: dict) -> None:
    """先记启动意图，再启动；socket 端口只信任匹配本 PID 的 READY 回执。"""
    config_path = directory / "runtime.private.json"
    config = {"database_url": connections["database_url"], "read_database_url": connections["read_database_url"],
              "admin_token": secret["admin_token"], "read_token": secret["read_token"],
              "tenant_scopes": state["tenant_scopes"], "instance_id": state["instance_id"], "expires_at": state["expires_at"],
              "port": state.get("http_port", 0)}
    save(config_path, config)
    launch = uuid4().hex
    log_path = directory / ("source-" + launch + ".log")
    if state.get("process"):
        state.setdefault("prior_launches", []).append(dict(state["process"]))
    state["process"] = {"status": "STARTING", "launch_id": launch, "log_ref": log_path.name, "config_ref": config_path.name}
    save(directory / "state.json", state)
    env = {k: v for k, v in os.environ.items() if k in {"PATH", "HOME", "LANG", "TMPDIR"}}
    env.update(PYTHONPATH=str(SERVICE / "src"), PYTHONDONTWRITEBYTECODE="1")
    with log_path.open("ab") as log:
        os.chmod(log_path, 0o600)
        child = subprocess.Popen([str(PYTHON), "-m", "commercial_source", "--config", str(config_path)], cwd=ROOT,
                                 env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    state["process"].update(pid=child.pid, signature=process_signature(child.pid))
    save(directory / "state.json", state)
    deadline = time.monotonic() + 25
    while True:
        for line in log_path.read_text(errors="replace").splitlines():
            try:
                receipt = json.loads(line)
            except ValueError:
                continue
            if isinstance(receipt, dict) and receipt.get("event") == "COMMERCIAL_SOURCE_READY":
                demand(receipt.get("pid") == child.pid and receipt.get("instance_id") == state["instance_id"]
                       and receipt.get("host") == "127.0.0.1" and type(receipt.get("port")) is int
                       and 1 <= receipt["port"] <= 65535, "SOURCE_RECEIPT_MISMATCH")
                state["http_port"] = receipt["port"]
                state["process"].update(status="RUNNING", signature=process_signature(child.pid))
                demand(bool(state["process"]["signature"]), "PROCESS_IDENTITY_UNCONFIRMED")
                save(directory / "state.json", state)
                return
        if child.poll() is not None or time.monotonic() >= deadline:
            raise HostError("SOURCE_START_UNCONFIRMED")
        time.sleep(0.05)


def stop_process(directory: Path, state: dict) -> None:
    """仅向 PID+启动签名均匹配的来源进程发信号；身份不明时保留资源。"""
    process = state.get("process", {})
    if not process.get("pid"):
        demand(not process or process.get("status") == "STOPPED", "PROCESS_START_REQUIRES_RECONCILIATION")
        return
    observed = process_signature(process["pid"])
    if observed is not None:
        demand(observed == process.get("signature"), "PROCESS_IDENTITY_MISMATCH")
        os.kill(process["pid"], signal.SIGTERM)
        deadline = time.monotonic() + 20
        while process_signature(process["pid"]) is not None:
            if time.monotonic() >= deadline:
                raise HostError("PROCESS_STOP_UNCONFIRMED")
            time.sleep(0.1)
    process["status"] = "STOPPED"
    save(directory / "state.json", state)


def reconcile_start(directory: Path, state: dict) -> None:
    """恢复启动回执窗口：READY日志+实际命令共同确认，不能只相信旧PID。

    父进程可能在Popen之后、保存PID/最终签名之前中断。仅重认同一私有目录的
    commercial_source命令；没有可核对回执时保持未知，绝不另启一个来源进程。
    """
    process = state.get("process", {})
    if process.get("status") != "STARTING":
        return
    name = process.get("log_ref", "")
    demand(re.fullmatch(r"source-[a-f0-9]{32}\.log", name) is not None, "SOURCE_LOG_IDENTITY_INVALID")
    log_path = directory / name
    demand(not log_path.is_symlink(), "PRIVATE_REGULAR_FILE_REQUIRED")
    if not log_path.is_file():
        return
    for line in log_path.read_text(errors="replace").splitlines():
        try:
            receipt = json.loads(line)
        except ValueError:
            continue
        if not isinstance(receipt, dict) or receipt.get("event") != "COMMERCIAL_SOURCE_READY":
            continue
        demand(receipt.get("instance_id") == state["instance_id"] and receipt.get("host") == "127.0.0.1"
               and type(receipt.get("pid")) is int and receipt["pid"] > 1 and type(receipt.get("port")) is int
               and 1 <= receipt["port"] <= 65535, "SOURCE_RECEIPT_MISMATCH")
        demand(not process.get("pid") or process["pid"] == receipt["pid"], "SOURCE_RECEIPT_MISMATCH")
        observed = process_signature(receipt["pid"])
        if observed is None:
            process.update(pid=receipt["pid"], status="STOPPED")
        else:
            demand(observed.endswith(" -m commercial_source --config " + str(directory / "runtime.private.json")),
                   "PROCESS_IDENTITY_MISMATCH")
            process.update(pid=receipt["pid"], signature=observed, status="RUNNING")
            state["http_port"] = receipt["port"]
        save(directory / "state.json", state)
        return


def publish_ready(directory: Path, state: dict, secret: dict) -> dict:
    base_url = f"http://127.0.0.1:{state['http_port']}"
    demand(source_manifest()["sha256"] == state["source_digest"], "SOURCE_CHANGED_REQUIRES_NEW_EXPERIMENT")
    health = http_health(base_url + "/health/ready")
    demand(health.get("instance_id") == state["instance_id"] and health.get("api_version") == API_VERSION,
           "SOURCE_HEALTH_IDENTITY_MISMATCH")
    public = {"status": "READY", "instance_id": state["instance_id"], "run_id": state["run_id"], "base_url": base_url,
              "source_digest": state["source_digest"], "api_version": API_VERSION, "authority_scope": "LOCAL_LAB",
              "expires_at": state["expires_at"], "tenant_scopes": state["tenant_scopes"],
              "qualification": "SYNTHETIC_BUSINESS_SOURCE_LOCAL_LAB", "directory": str(directory)}
    # 唯一供 SUT 消费的投影；写入 token、数据库 DSN 从不进入此文件。
    save(directory / "commercial-reader.private.json", {k: v for k, v in public.items() if k not in {"status", "directory"}} | {"read_token": secret["read_token"]})
    save(directory / "ready.json", public)
    state["status"] = "RUNNING"
    save(directory / "state.json", state)
    return public


def status(directory: Path, state: dict) -> dict:
    """只读检查；不初始化数据库、不续租、不生成配置或凭据。"""
    container = inspect_container(directory, state)
    process = state.get("process", {})
    running = bool(process.get("pid") and process.get("signature") == process_signature(process["pid"]))
    health = None
    if running and state.get("http_port"):
        try:
            observed = http_health(f"http://127.0.0.1:{state['http_port']}/health/ready")
            health = observed.get("instance_id") == state["instance_id"]
        except (OSError, ValueError, HostError):
            health = False
    return {"instance_id": state["instance_id"], "recorded_status": state["status"], "source_process_running": running,
            "database_state": container["state"] if container else "ABSENT", "ready": health,
            "source_unchanged": source_manifest()["sha256"] == state["source_digest"], "expires_at": state["expires_at"]}


def execute(action: str, directory: Path, *, tenants: list[str] | None = None, lease_hours: int = 24) -> dict:
    if action == "up":
        demand(not (directory / "state.json").exists(), "EXPERIMENT_EXISTS_USE_START_OR_STATUS")
        demand(type(lease_hours) is int and 1 <= lease_hours <= 24, "LEASE_LIMIT_INVALID")
        scopes = sorted(set(tenants or ["tenant-a", "tenant-b"]))
        demand(bool(scopes) and all(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", x) for x in scopes), "TENANT_SCOPES_INVALID")
        manifest = source_manifest()
        identity = "commercial_" + uuid4().hex
        state = {"schema_version": 1, "project": PROJECT, "instance_id": identity, "run_id": directory.name,
                 "directory": str(directory), "status": "PROVISIONING", "source_digest": manifest["sha256"],
                 "api_version": API_VERSION, "tenant_scopes": scopes,
                 "expires_at": (datetime.now(timezone.utc) + timedelta(hours=lease_hours)).isoformat(),
                 "container_name": "egap-commercial-pg-" + identity.removeprefix("commercial_"),
                 "volume_name": "egap-commercial-pg-" + identity.removeprefix("commercial_")}
        save(directory / "source-manifest.json", manifest)
        save(directory / "state.json", state)
        secret = {key: secrets.token_hex(32) for key in ("postgres_password", "writer_password", "reader_password", "test_password", "admin_token", "read_token")}
        save(directory / "secrets.private.json", secret)
    else:
        state = private_read(directory / "state.json")
    validate_state(directory, state)
    if action == "status":
        return status(directory, state)
    reconcile_start(directory, state)
    if action in {"up", "start"}:
        demand(state["status"] != "DESTROYED", "EXPERIMENT_ALREADY_DESTROYED")
        demand(source_manifest()["sha256"] == state["source_digest"], "SOURCE_CHANGED_REQUIRES_NEW_EXPERIMENT")
        demand(datetime.fromisoformat(state["expires_at"]) > datetime.now(timezone.utc), "EXPERIMENT_EXPIRED")
        process = state.get("process", {})
        if process.get("pid") and process_signature(process["pid"]) is not None:
            demand(process.get("signature") == process_signature(process["pid"]), "PROCESS_IDENTITY_MISMATCH")
            demand(process.get("status") == "RUNNING", "PROCESS_START_REQUIRES_RECONCILIATION")
            return publish_ready(directory, state, private_read(directory / "secrets.private.json"))
        demand(not process or process.get("pid") is not None or process.get("status") == "STOPPED", "PROCESS_START_REQUIRES_RECONCILIATION")
        secret = private_read(directory / "secrets.private.json")
        connections = ensure_database(directory, state, secret)
        start_service(directory, state, secret, connections)
        return publish_ready(directory, state, secret)
    stop_process(directory, state)
    container = inspect_container(directory, state)
    if container and container["state"] in {"running", "restarting", "paused"}:
        demand(container["state"] != "paused", "PAUSED_CONTAINER_REQUIRES_RECONCILIATION")
        docker(directory, ["container", "stop", "--time", "15", container["id"]])
        container = inspect_container(directory, state)
        demand(container is not None and container["state"] not in {"running", "restarting", "paused"}, "DATABASE_STOP_UNCONFIRMED")
    if action == "down":
        if container:
            docker(directory, ["container", "rm", container["id"]])
        if inspect_volume(directory, state):
            docker(directory, ["volume", "rm", state["volume_name"]])
        state["status"] = "DESTROYED"
    else:
        state["status"] = "STOPPED"
    save(directory / "state.json", state)
    # ready.json 是观察投影，不保留过时的 READY 声明。
    save(directory / "ready.json", {"status": state["status"], "instance_id": state["instance_id"], "run_id": state["run_id"]})
    return {"status": state["status"], "instance_id": state["instance_id"], "volumes_retained": action != "down"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("up", "status", "stop", "start", "down"))
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--tenant", action="append")
    parser.add_argument("--lease-hours", type=int, default=24)
    args = parser.parse_args()
    try:
        directory = directory_path(args.directory)
        if args.action == "status":
            result = execute(args.action, directory)
        else:
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(directory / ".host.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "a+") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                result = execute(args.action, directory, tenants=args.tenant, lease_hours=args.lease_hours)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (HostError, OSError, ValueError, TypeError, KeyError, psycopg.Error, subprocess.SubprocessError) as error:
        code = str(error) if isinstance(error, HostError) else "COMMERCIAL_SOURCE_HOST_FAILED"
        print(json.dumps({"status": "FAILED", "error_code": code}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

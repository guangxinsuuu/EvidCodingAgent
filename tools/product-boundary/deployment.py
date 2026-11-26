#!/usr/bin/env python3
"""EGAP 受信部署宿主；只编排登记镜像，不执行候选仓库中的宿主脚本。

prepare 固定镜像身份和随机凭据 → up 启动独立 Compose 工程 → inspect 记录实际部署。
凭据目录仅操作员可遍历，各容器只挂载对应角色文件。receipt 可交给独立观察者，
observer.private.json 仅在宿主侧生成；本机 Docker 管理员仍具有最高权限。
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
DEPLOYMENTS = ROOT / "artifacts/product-deployments"
PG_IMAGE = "postgres:16-alpine@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea"
# 实际部署前由 prepare 检查本地镜像摘要；数据库基础镜像不冒充产品候选身份。
REDIS_IMAGE = "redis:7-alpine@sha256:858f009f9709ce576febc734aa78b8f6d624b82571f9ddb6bda4377c833b3499"


def command(args: list[str], *, timeout: int = 120) -> str:
    """不回显 stderr；Compose/驱动异常可能包含宿主路径或连接信息。"""
    result = subprocess.run(args, text=True, capture_output=True, timeout=timeout, cwd=ROOT)
    if result.returncode:
        raise RuntimeError("DEPLOYMENT_COMMAND_FAILED:" + args[0])
    return result.stdout.strip()


def save(path: Path, value: object, *, mode: int = 0o600) -> None:
    """同目录原子替换；私有目录控制文件可见性，容器配置另外仅以只读方式挂载。"""
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w") as stream:
        os.chmod(temporary, mode)
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def prepare(image: str) -> Path:
    """新实验专有工程；镜像必须是完整 ID，禁止把 latest 当作固定候选。"""
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", image):
        raise ValueError("IMMUTABLE_IMAGE_ID_REQUIRED")
    image_record = json.loads(command(["docker", "image", "inspect", image]))[0]
    revision = image_record["Config"].get("Labels", {}).get("org.opencontainers.image.revision", "")
    if not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise ValueError("PRODUCT_REVISION_LABEL_REQUIRED")
    # 不凭 tag 猜基础设施版本；读取当前已安装镜像并固定 ID。
    pg = command(["docker", "image", "inspect", PG_IMAGE, "--format", "{{.Id}}"])
    redis = command(["docker", "image", "inspect", REDIS_IMAGE, "--format", "{{.Id}}"])
    instance = "evid-sut-" + uuid4().hex[:12]
    directory = DEPLOYMENTS / instance
    directory.mkdir(mode=0o700, parents=True)
    os.chmod(directory, 0o700)
    passwords = {name: secrets.token_urlsafe(32) for name in (
        "postgres", "support_app", "support_gateway", "support_reader", "support_observer", "redis")}
    tokens = {name: secrets.token_urlsafe(32) for name in ("api_token_a", "api_token_b", "gateway_token")}
    def dsn(role: str, database: str) -> str:
        return f"postgresql://{role}:{passwords[role]}@database:5432/{database}"
    base = {"instance_id": instance}
    queue = "redis://:" + passwords["redis"] + "@queue:6379/0"
    configs = {
        "init": {**base, "admin_database_url": dsn("postgres", "postgres"),
                 "support_admin_url": dsn("postgres", "support"), "gateway_admin_url": dsn("postgres", "gateway"),
                 "roles": {k:v for k,v in passwords.items() if k not in ("postgres", "redis")}},
        "api": {**base, "database_url": dsn("support_app", "support"), "redis_url": queue,
                "gateway_url": "http://gateway:8080", "api_token_a": tokens["api_token_a"],
                "api_token_b": tokens["api_token_b"], "execution_mode": "SCRIPTED_CALIBRATION"},
        "relay": {**base, "database_url": dsn("support_app", "support"), "redis_url": queue},
        "worker": {**base, "database_url": dsn("support_app", "support"), "redis_url": queue,
                   "gateway_url": "http://gateway:8080", "gateway_token": tokens["gateway_token"],
                   "gateway_allowed_origins": ["http://gateway:8080"]},
        "gateway": {**base, "database_url": dsn("support_gateway", "gateway"),
                    "reader_database_url": dsn("support_reader", "support"), "gateway_token": tokens["gateway_token"],
                    "execution_mode": "SCRIPTED_CALIBRATION"},
    }
    for role, config in configs.items():
        save(directory / (role + ".private.json"), {"role": role, **config}, mode=0o444)
    # Linux 非 root UID 可以读挂入容器的单个文件，无法遍历宿主 0700 目录。
    (directory / "postgres.password").write_text(passwords["postgres"])
    os.chmod(directory / "postgres.password", 0o444)
    (directory / "redis.conf").write_text("appendonly yes\nprotected-mode yes\nrequirepass " + passwords["redis"] + "\n")
    os.chmod(directory / "redis.conf", 0o444)
    services = {
        "database": {"image": pg, "environment": {"POSTGRES_PASSWORD_FILE": "/run/secrets/password"},
            "volumes": ["product-db:/var/lib/postgresql/data", f"{directory}/postgres.password:/run/secrets/password:ro"],
            "networks": ["database", "access"], "ports": ["127.0.0.1::5432"],
            "healthcheck": {"test": ["CMD", "pg_isready", "-U", "postgres"], "interval": "2s", "timeout": "2s", "retries": 30}},
        "queue": {"image": redis, "command": ["redis-server", "/run/secrets/redis.conf"],
            "volumes": ["product-queue:/data", f"{directory}/redis.conf:/run/secrets/redis.conf:ro"], "networks": ["queue"]},
    }
    for role in configs:
        networks = {"init": ["database"], "api": ["database", "queue", "product", "access"],
                    "worker": ["database", "queue", "product"], "relay": ["database", "queue"],
                    "gateway": ["database", "product"]}[role]
        services[role] = {"image": image, "command": ["--role", role], "read_only": True,
            "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"], "tmpfs": ["/tmp"],
            "volumes": [f"{directory}/{role}.private.json:/run/secrets/role.json:ro"], "networks": networks,
            "labels": {"evid.deployment": instance, "evid.product_revision": revision},
            "depends_on": ({"database": {"condition": "service_healthy"}} if role == "init" else
                           {"init": {"condition": "service_completed_successfully"}, "queue": {"condition": "service_started"}})}
    services["api"]["ports"] = ["127.0.0.1::8080"]
    compose = {"name": instance, "services": services,
        "volumes": {"product-db": {}, "product-queue": {}},
        # Docker 的纯 internal 网络不建立宿主端口转发。仅 API/观察数据库接入 access，
        # 对宿主仍只发布 127.0.0.1，其他产品角色没有额外出口。
        "networks": {**{name: {"internal": True} for name in ("database", "queue", "product")}, "access": {}}}
    save(directory / "compose.json", compose)
    save(directory / "manifest.json", {"schema_version": 1, "deployment_id": instance, "product_revision": revision,
        "product_image_id": image, "created_at": datetime.now(timezone.utc).isoformat(),
        "mode": "SCRIPTED_CALIBRATION", "infrastructure": {"postgres_image_id": pg, "redis_image_id": redis}})
    save(directory / "observer.private.json", {"deployment_id": instance, "username": "support_observer",
        "password": passwords["support_observer"], "databases": ["support", "gateway"], "scope": "THIS_SYNTHETIC_DEPLOYMENT_ONLY"})
    save(directory / "caller.private.json", {"deployment_id": instance, "api_token_a": tokens["api_token_a"], "api_token_b": tokens["api_token_b"]})
    return directory


def inspect(directory: Path) -> dict:
    """从 Docker 取得容器身份、镜像与实际端口；产品自报信息不能替代此回执。"""
    manifest = json.loads((directory / "manifest.json").read_text())
    ids = command(["docker", "compose", "-f", str(directory / "compose.json"), "ps", "-a", "-q"]).splitlines()
    if not ids:
        raise ValueError("DEPLOYMENT_NOT_FOUND")
    containers = json.loads(command(["docker", "inspect", *ids]))
    records = []
    endpoints = {}
    for item in containers:
        role = item["Config"]["Labels"]["com.docker.compose.service"]
        if item["Config"]["Labels"]["com.docker.compose.project"] != manifest["deployment_id"]:
            raise ValueError("DEPLOYMENT_IDENTITY_MISMATCH")
        if role in ("init", "api", "relay", "worker", "gateway") and item["Image"] != manifest["product_image_id"]:
            raise ValueError("CANDIDATE_IMAGE_MISMATCH")
        records.append({"role": role, "container_id": item["Id"], "image_id": item["Image"],
            "state": item["State"]["Status"], "exit_code": item["State"]["ExitCode"],
            "mounts": [{"destination": m["Destination"], "read_only": not m["RW"], "type": m["Type"]} for m in item["Mounts"]]})
        if role in ("api", "database", "model-access"):
            ports = item["NetworkSettings"]["Ports"].get("5432/tcp" if role == "database" else "8080/tcp")
            if ports:
                if ports[0]["HostIp"] != "127.0.0.1":
                    raise ValueError("LOCAL_ENDPOINT_REQUIRED")
                endpoints[role] = {"host": "127.0.0.1", "port": int(ports[0]["HostPort"])}
    # 已停止容器没有发布端口，仍应返回真实 STOPPED 状态；启动准入另行要求完整端点。
    receipt = {**manifest, "observed_at": datetime.now(timezone.utc).isoformat(), "containers": records, "endpoints": endpoints,
               "qualification": "LOCAL_CONTAINER_DEPLOYMENT_NOT_ENTERPRISE_ISOLATION"}
    save(directory / "deployment-receipt.json", receipt)
    return receipt



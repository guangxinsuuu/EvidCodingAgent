"""产品部署的可选 LIVE_MODEL 装配：固定本地权重 → 受信出口 → Gateway 专有网络。

product.up 在冻结部署前调用 attach；恢复与观察走 validate。产品镜像不包含出口
实现和观察凭据；API/Worker 也不接模型网络。这里绑定基础设施，不判断草稿正确性。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import secrets
import subprocess
import tempfile
from urllib.request import build_opener, ProxyHandler
from uuid import uuid4

import deployment
from platform_attachment import binding as platform_boundary

ROOT = deployment.ROOT
SOURCE = ROOT / "tools/model-boundary"
BUILDS = ROOT / "artifacts/model-host/builds"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def local_manifest(name):
    """只读已安装模型，无下载或自动选择；精确名称和权重摘要进入部署身份。"""
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", name):
        raise ValueError("INSTALLED_LOCAL_MODEL_REQUIRED")
    with build_opener(ProxyHandler({})).open("http://127.0.0.1:11434/api/tags", timeout=3) as response:
        raw = response.read(65537)
        if response.status != 200 or len(raw) > 65536:
            raise ValueError("LOCAL_MODEL_MANIFEST_UNAVAILABLE")
    rows = [r for r in json.loads(raw).get("models", []) if r.get("name") == name]
    if len(rows) != 1 or not re.fullmatch(r"[a-f0-9]{64}", rows[0].get("digest", "")):
        raise ValueError("MODEL_NOT_INSTALLED_NO_DOWNLOAD")
    return {"name": name, "digest": rows[0]["digest"], "authority": "LOCAL_OLLAMA_TAGS_NOT_PROVIDER_ATTESTATION"}


def build_outlet():
    """仅两个受信文件进入镜像；构建失败/未知都保留唯一 tag，不执行产品 checkout。"""
    files = {name:sha(SOURCE / name) for name in ("Dockerfile", "proxy.py")}
    source_digest = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    BUILDS.mkdir(parents=True, exist_ok=True)
    indexed = BUILDS / (source_digest + ".json")
    if indexed.exists():
        record = json.loads(indexed.read_text())
        if record["status"] != "BUILT":
            raise ValueError("MODEL_BUILD_REQUIRES_RECONCILIATION")
        image = json.loads(deployment.command(["docker", "image", "inspect", record["image_id"]]))[0]
        if image["Config"]["Labels"].get("evid.model_outlet_source") != source_digest:
            raise ValueError("MODEL_OUTLET_IMAGE_CHANGED")
        return record
    record = {"operation_id": uuid4().hex, "status": "BUILDING", "source_sha256": source_digest, "files": files,
              "egap_commit": deployment.command(["git", "rev-parse", "HEAD"])}
    record["tag"] = "evid-model-outlet:" + record["operation_id"]
    deployment.save(indexed, record)
    with tempfile.TemporaryDirectory(prefix="evid-model-outlet-") as temporary:
        for name, expected in files.items():
            raw = (SOURCE / name).read_bytes()
            if hashlib.sha256(raw).hexdigest() != expected:
                raise ValueError("MODEL_OUTLET_SOURCE_CHANGED")
            (Path(temporary) / name).write_bytes(raw)
        try:
            result = subprocess.run(["docker", "build", "--build-arg", "OUTLET_SOURCE_SHA256=" + source_digest,
                "--tag", record["tag"], temporary], capture_output=True, text=True, timeout=180)
        except subprocess.TimeoutExpired:
            record["status"] = "UNKNOWN"
            deployment.save(indexed, record)
            raise ValueError("MODEL_OUTLET_BUILD_UNKNOWN") from None
        log = BUILDS / (record["operation_id"] + ".private.log")
        with log.open("w") as stream:
            log.chmod(0o600)
            stream.write(result.stdout + result.stderr)
        record["status"] = "FAILED" if result.returncode else "BUILT"
        if not result.returncode:
            record["image_id"] = deployment.command(["docker", "image", "inspect", record["tag"], "--format", "{{.Id}}"])
        deployment.save(indexed, record)
    if record["status"] != "BUILT":
        raise ValueError("MODEL_OUTLET_BUILD_FAILED")
    return record


def attach(directory: Path, name: str, max_calls: int):
    manifest = json.loads((directory / "manifest.json").read_text())
    if "compose_sha256" in manifest or "model_binding" in manifest:
        raise ValueError("MODEL_ATTACHMENT_ALREADY_FROZEN")
    if type(max_calls) is not int or not 1 <= max_calls <= 12:
        raise ValueError("MODEL_CALL_BUDGET_INVALID")
    model = local_manifest(name)
    build = build_outlet()
    instance = manifest["deployment_id"]
    config = {"deployment_id": instance, "model_name": name, "model_digest": model["digest"],
              "max_calls": max_calls, "max_output_tokens": 1024,
              "expires_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
              "product_token": secrets.token_urlsafe(32), "observer_token": secrets.token_urlsafe(32)}
    deployment.save(directory / "model-access.private.json", config, mode=0o444)
    deployment.save(directory / "model-observer.private.json", {"deployment_id": instance, "role": "OBSERVER", "token": config["observer_token"]})
    gateway = json.loads((directory / "gateway.private.json").read_text())
    gateway.update(execution_mode="LIVE_MODEL", model_endpoint="http://model-access:8080/v1/chat/completions",
        model_name=name, model_access_token=config["product_token"], model_allowed_origins=["http://model-access:8080"], max_calls=max_calls)
    api = json.loads((directory / "api.private.json").read_text())
    api["execution_mode"] = "LIVE_MODEL"
    deployment.save(directory / "gateway.private.json", gateway, mode=0o444)
    deployment.save(directory / "api.private.json", api, mode=0o444)
    compose = json.loads((directory / "compose.json").read_text())
    compose["networks"]["model"] = {"internal": True}
    compose["volumes"]["model-evidence"] = {}
    compose["services"]["gateway"]["networks"].append("model")
    compose["services"]["model-access"] = {"image": build["image_id"], "read_only": True, "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"], "tmpfs": ["/tmp"], "ports": ["127.0.0.1::8080"],
        "networks": ["model", "access"],
        "volumes": [f"{directory}/model-access.private.json:/run/secrets/role.json:ro", "model-evidence:/data"],
        "labels": {"evid.deployment": instance, "evid.model_outlet_source": build["source_sha256"]}}
    compose["services"]["gateway"]["depends_on"]["model-access"] = {"condition": "service_started"}
    deployment.save(directory / "compose.json", compose)
    manifest.update(mode="LIVE_MODEL", model_binding={"model": model, "outlet_image_id": build["image_id"],
        "outlet_source_sha256": build["source_sha256"], "build_operation_id": build["operation_id"],
        "max_calls": max_calls, "max_output_tokens": config["max_output_tokens"], "expires_at": config["expires_at"],
        "outlet_config_sha256": sha(directory / "model-access.private.json"),
        "scope": "FIXED_LOCAL_MODEL_SYNTHETIC_BUSINESS_ONLY"})
    deployment.save(directory / "manifest.json", manifest)


def validate(directory: Path, *, running=True):
    """拒绝文件、实际镜像、挂载、模型网络或权重变化；不能只信 readiness 的 ready。"""
    manifest = json.loads((directory / "manifest.json").read_text())
    binding = manifest.get("model_binding")
    if binding is None:
        return
    if sha(directory / "model-access.private.json") != binding["outlet_config_sha256"]:
        raise ValueError("MODEL_OUTLET_CONFIG_CHANGED")
    if datetime.now(timezone.utc) >= datetime.fromisoformat(binding["expires_at"]):
        raise ValueError("MODEL_LEASE_EXPIRED")
    if local_manifest(binding["model"]["name"]) != binding["model"]:
        raise ValueError("MODEL_MANIFEST_CHANGED")
    if not running:
        return
    ids = deployment.command(["docker", "compose", "-f", str(directory / "compose.json"), "ps", "-a", "-q", "model-access", "gateway"]).splitlines()
    rows = json.loads(deployment.command(["docker", "inspect", *ids]))
    outlet = next((r for r in rows if r["Config"]["Labels"]["com.docker.compose.service"] == "model-access"), None)
    gateway = next((r for r in rows if r["Config"]["Labels"]["com.docker.compose.service"] == "gateway"), None)
    if outlet is None or gateway is None or outlet["Image"] != binding["outlet_image_id"] \
            or outlet["State"]["Status"] != "running" or outlet["Config"]["User"] != "10001:10001" \
            or not outlet["HostConfig"]["ReadonlyRootfs"]:
        raise ValueError("MODEL_OUTLET_DEPLOYMENT_CHANGED")
    mounts = [m for m in outlet["Mounts"] if m["Destination"] == "/run/secrets/role.json"]
    if len(mounts) != 1 or mounts[0]["RW"] or platform_boundary.host_mount_file(mounts[0]["Source"]) != directory / "model-access.private.json":
        raise ValueError("MODEL_OUTLET_MOUNT_CHANGED")
    network_name = manifest["deployment_id"] + "_model"
    network = json.loads(deployment.command(["docker", "network", "inspect", network_name]))[0]
    if network["Internal"] is not True or set(network["Containers"]) != {outlet["Id"], gateway["Id"]}:
        raise ValueError("MODEL_NETWORK_MEMBERSHIP_CHANGED")
    ports = outlet["NetworkSettings"]["Ports"]["8080/tcp"]
    if not ports or len(ports) != 1 or ports[0]["HostIp"] != "127.0.0.1":
        raise ValueError("MODEL_OBSERVER_ENDPOINT_CHANGED")

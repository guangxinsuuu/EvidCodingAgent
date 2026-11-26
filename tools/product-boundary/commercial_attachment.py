"""商业来源的受信接入：平台固定镜像/角色/范围 → 产品只读入口。

调用链 product.up → attach；每次 start → validate。这里只登记来源，不把来源
条款解释成验收答案。不同来源实例或范围必须创建新部署，旧证据不能换标签复用。
"""
from datetime import datetime, timezone
import json
from pathlib import Path
import re

import deployment
from platform_attachment import binding


def validate(identity):
    """从 Docker 核对原容器、镜像、网络；过期与来源停机均拒绝新的产品启动。"""
    if datetime.fromisoformat(identity["expires_at"]) <= datetime.now(timezone.utc):
        raise binding.BoundaryError("COMMERCIAL_SOURCE_LEASE_EXPIRED")
    expected = {r["container_id"]: r for r in identity["containers"]}
    live = json.loads(deployment.command(["docker", "inspect", *expected]))
    if {r["Id"] for r in live} != set(expected):
        raise binding.BoundaryError("COMMERCIAL_CONTAINER_SET_CHANGED")
    for row in live:
        record = expected[row["Id"]]; labels = row["Config"]["Labels"]; state = row["State"]
        if row["Image"] != record["image_id"] or labels.get("evid.commercial_instance") != identity["project"] \
                or labels.get("com.docker.compose.project") != identity["project"] \
                or (state["Status"] != "running" and not (record["role"] == "init" and state["Status"] == "exited" and state["ExitCode"] == 0)):
            raise binding.BoundaryError("COMMERCIAL_DEPLOYMENT_DRIFT")
        if record["role"] != "database":
            mounts = [m for m in row["Mounts"] if m["Type"] != "tmpfs"]
            if len(mounts) != 1 or mounts[0]["Destination"] != "/run/secrets/role.json" or mounts[0]["RW"] \
                    or Path(mounts[0]["Source"]).name != record["role"] + ".private.json" \
                    or row["Config"]["User"] != "10001:10001" or not row["HostConfig"]["ReadonlyRootfs"]:
                raise binding.BoundaryError("COMMERCIAL_ROLE_BOUNDARY_CHANGED")
            mounted = Path(mounts[0]["Source"])
            if mounted.is_symlink() or mounted.stat().st_size > 131072 \
                    or binding.digest(mounted.read_bytes()) != identity["role_config_sha256"][record["role"]]:
                raise binding.BoundaryError("COMMERCIAL_MOUNTED_CONFIG_CHANGED")
        if record["role"] in identity["endpoints"]:
            port = "5432/tcp" if record["role"] == "database" else "8080/tcp"
            published = row["NetworkSettings"]["Ports"].get(port) or []
            expected_endpoint = identity["endpoints"][record["role"]]
            if len(published) != 1 or published[0]["HostIp"] != "127.0.0.1" or int(published[0]["HostPort"]) != expected_endpoint["port"]:
                raise binding.BoundaryError("COMMERCIAL_ENDPOINT_CHANGED")
    network = json.loads(deployment.command(["docker", "network", "inspect", identity["product_network"]]))[0]
    if network["Id"] != identity["product_network_id"] or network["Internal"] is not True \
            or network["Labels"].get("com.docker.compose.project") != identity["project"]:
        raise binding.BoundaryError("COMMERCIAL_NETWORK_CHANGED")


def attach(directory, repo, project):
    """来源范围必须与当前候选已登记的平台范围相同；产品只获得读取 token。"""
    if not re.fullmatch(r"evid-commercial-[a-f0-9]{12}", project):
        raise binding.BoundaryError("REGISTERED_COMMERCIAL_PROJECT_REQUIRED")
    manifest = binding.private(directory / "manifest.json")
    if "compose_sha256" in manifest or "commercial_source_binding" in manifest:
        raise binding.BoundaryError("COMMERCIAL_ATTACHMENT_ALREADY_FROZEN")
    if "platform_binding" not in manifest:
        raise binding.BoundaryError("NATIVE_PLATFORM_ATTACHMENT_REQUIRED")
    # repo 已经过前一阶段 binding(repo, ...) 的登记仓库核验，仍在当前作用域内复核。
    _, native = binding.binding(repo, manifest["platform_binding"]["project"])
    source_dir = repo / ".local" / project
    source_manifest = binding.private(source_dir / "manifest.json")
    receipt = binding.private(source_dir / "deployment-receipt.json")
    scopes = binding.private(source_dir / "scope-set.json")
    if any(receipt.get(k) != v for k, v in source_manifest.items()) or source_manifest["project"] != project \
            or source_manifest["compose_sha256"] != binding.digest((source_dir / "compose.private.json").read_bytes()) \
            or source_manifest["scope_set_sha256"] != binding.digest((source_dir / "scope-set.json").read_bytes()) \
            or scopes["native_platform_project"] != native["project"] or scopes["native_platform_manifest_sha256"] != native["manifest_sha256"] \
            or scopes["native_enrollment_sha256"] != manifest["platform_enrollment_sha256"]:
        raise binding.BoundaryError("COMMERCIAL_SOURCE_BINDING_MISMATCH")
    if {r["role"] for r in receipt["containers"]} != {"database", "init", "preparer", "product", "observer"}:
        raise binding.BoundaryError("COMMERCIAL_ROLE_SET_MISMATCH")
    for role, expected in source_manifest["role_config_sha256"].items():
        if role not in {"init", "preparer", "product", "observer"} or binding.digest((source_dir / (role + ".private.json")).read_bytes()) != expected:
            raise binding.BoundaryError("COMMERCIAL_ROLE_CONFIG_CHANGED")
    validate(receipt)
    client = binding.private(source_dir / "product-client.private.json")
    if client["instance_id"] != project or client["role"] != "PRODUCT" or client["scope_set_sha256"] != source_manifest["scope_set_sha256"]:
        raise binding.BoundaryError("COMMERCIAL_PRODUCT_CREDENTIAL_MISMATCH")
    api = json.loads((directory / "api.private.json").read_text())
    compose = binding.private(directory / "compose.json")
    api["integration"]["commercial_source"] = {"base_url": "http://commercial-source:8080", "read_token": client["token"]}
    api["integration"]["allowed_origins"].append("http://commercial-source:8080")
    compose["networks"]["commercial-source"] = {"external": True, "name": receipt["product_network"]}
    compose["services"]["api"]["networks"].append("commercial-source")
    deployment.save(directory / "api.private.json", api, mode=0o444)
    deployment.save(directory / "compose.json", compose)
    manifest.update(commercial_source_binding=receipt,
        integration_qualification="NATIVE_AND_COMMERCIAL_FACTS_SCRIPTED_CALIBRATION")
    deployment.save(directory / "manifest.json", manifest)

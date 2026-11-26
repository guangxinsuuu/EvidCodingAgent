"""受信宿主的连接装配：平台登记回执 → 产品最小配置 → 独立网络引用。

由 product.up 调用。平台管理员只在装配进程内用于读取受控配置；输出只含产品
访问 token、OIDC 客户端和已登记对象。观察者、原生管理员、实验预期不进入 SUT。
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import deployment

_spec = importlib.util.spec_from_file_location("platform_binding", Path(__file__).parents[1] / "platform-boundary/bind_platform.py")
binding = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(binding)


def project_config(preparer: dict, product_access: dict, seed: dict, enrollment: dict) -> dict:
    """白名单复制配置；不 spread preparer，以免将管理员口令或登录密码一起挂载。

    enrollment 来自原生对象回读，seed 提供 subject 与客户映射。两者必须逐对象一致；
    不接受仅租户相同却换了会话/发票的绑定，也不把 expected_units 等准备材料带入。
    """
    if product_access.get("role") != "PRODUCT":
        raise binding.BoundaryError("PRODUCT_ROLE_REQUIRED")
    customers, cases = [], []
    expected = {row["scope"]["tenant_id"]: row["scope"] for row in enrollment["grants"]}
    for customer in seed["bindings"]["customers"]:
        tenant = customer["tenant_id"]
        scope = expected.get(tenant)
        selected = [case for case in seed["bindings"]["cases"] if case["tenant_id"] == tenant]
        subjects = {row["subject"] for row in preparer["keycloak"]["operators"] if row["tenant_id"] == tenant}
        if not scope or tenant not in product_access["tenant_scopes"] or not subjects \
                or set(customer["allowed_subjects"]) != subjects \
                or customer["chatwoot_account_id"] != scope["account_id"] \
                or customer["chatwoot_contact_id"] != scope["contact_id"] \
                or customer["lago_customer_id"] != scope["customer_id"] \
                or {c["conversation_id"] for c in selected} != set(scope["conversation_ids"]) \
                or {c["invoice_id"] for c in selected} != set(scope["invoice_ids"]) \
                or {c["subscription_id"] for c in selected} != set(scope["subscription_ids"]):
            raise binding.BoundaryError("PRODUCT_NATIVE_SCOPE_MISMATCH")
        customers.append({k: customer[k] for k in ("tenant_id", "chatwoot_account_id", "chatwoot_contact_id", "lago_customer_id", "allowed_subjects")})
        cases.extend({k: c[k] for k in ("tenant_id", "conversation_id", "invoice_id", "subscription_id")} for c in selected)
    if set(expected) != {c["tenant_id"] for c in customers}:
        raise binding.BoundaryError("PRODUCT_NATIVE_SCOPE_MISMATCH")
    kc = preparer["keycloak"]
    if kc["issuer"] != "http://evid-keycloak:8080/realms/evid-lab":
        raise binding.BoundaryError("REGISTERED_IDENTITY_ORIGIN_REQUIRED")
    return {
        "allowed_origins": ["http://business-access:8080", "http://evid-keycloak:8080"],
        "chatwoot": {"base_url": "http://business-access:8080", "api_token": product_access["token"], "account_id": preparer["chatwoot"]["account_id"]},
        "lago": {"base_url": "http://business-access:8080", "api_key": product_access["token"]},
        "keycloak": {k: kc[k] for k in ("issuer", "client_id", "client_secret", "audience")},
        "bindings": {"customers": customers, "cases": cases}, "automatic_writeback": True,
    }


def attach(directory: Path, repo: Path, project: str, enrollment_path: Path, seed_path: Path) -> None:
    """仅在 prepare 后、首次启动前连接；已有部署不能偷偷换平台、凭据或候选。

    live Docker 身份核对来自 bind_platform.binding。网络也要同时满足 internal 和
    精确项目标签，防止私有文件中一个任意网络名变成产品访问原生数据库的通道。
    """
    existing = binding.private(directory / "manifest.json")
    if "compose_sha256" in existing or "platform_binding" in existing:
        raise binding.BoundaryError("PLATFORM_ATTACHMENT_ALREADY_FROZEN")
    platform_dir, identity = binding.binding(repo, project)
    enrollment = binding.private(enrollment_path)
    seed = binding.private(seed_path)
    if enrollment.get("state") != "ENROLLED" or enrollment["basis"] != {
        "project": project, "seed_sha256": binding.digest(seed_path.read_bytes()), "seed_run_id": seed["run_id"]
    } or enrollment["platform_binding"]["manifest_sha256"] != identity["manifest_sha256"]:
        raise binding.BoundaryError("EXACT_ENROLLED_PLATFORM_REQUIRED")
    preparer = binding.private(platform_dir / "preparer.private.json")
    access = binding.private(platform_dir / "product-access.private.json")
    if preparer["project"] != project or access["instance_id"] != project \
            or access["base_url"] != identity["deployment"]["endpoints"]["access"]:
        raise binding.BoundaryError("PLATFORM_CREDENTIAL_BINDING_MISMATCH")
    integration = project_config(preparer, access, seed, enrollment)
    network_name = identity["deployment"]["product_network"]
    network = json.loads(deployment.command(["docker", "network", "inspect", network_name]))[0]
    if network_name != project + "-product-access" or network["Internal"] is not True \
            or network["Labels"].get("com.docker.compose.project") != project:
        raise binding.BoundaryError("PLATFORM_NETWORK_MISMATCH")
    compose = json.loads((directory / "compose.json").read_text())
    compose["networks"]["business-platform"] = {"external": True, "name": network_name}
    compose["services"]["api"]["networks"].append("business-platform")
    api = json.loads((directory / "api.private.json").read_text())
    gateway = json.loads((directory / "gateway.private.json").read_text())
    api["integration"] = integration
    gateway["business_source"] = "CHATWOOT_LAGO"
    deployment.save(directory / "api.private.json", api, mode=0o444)
    deployment.save(directory / "gateway.private.json", gateway, mode=0o444)
    deployment.save(directory / "compose.json", compose)
    manifest = json.loads((directory / "manifest.json").read_text())
    manifest.update(business_source="CHATWOOT_LAGO", platform_binding=identity,
        platform_enrollment_sha256=binding.digest(enrollment_path.read_bytes()),
        platform_network_id=network["Id"], integration_qualification="NATIVE_TRANSPORT_CALIBRATION_NO_COMMERCIAL_SOURCE")
    deployment.save(directory / "manifest.json", manifest)


def validate_attachment(directory: Path) -> None:
    """首次启动及恢复都检查原平台，不能在同名网络重建后悄悄接到另一个实例。"""
    manifest = json.loads((directory / "manifest.json").read_text())
    identity = manifest.get("platform_binding")
    if not identity:
        return
    expected = {r["container_id"]: r for r in identity["deployment"]["containers"]}
    live = json.loads(deployment.command(["docker", "inspect", *expected]))
    if {r["Id"] for r in live} != set(expected):
        raise binding.BoundaryError("PLATFORM_CONTAINER_SET_CHANGED")
    for row in live:
        record = expected[row["Id"]]
        labels, state = row["Config"]["Labels"], row["State"]
        if row["Image"] != record["image_id"] or labels.get("com.docker.compose.project") != identity["project"] \
                or labels.get("evid.platform_instance") != identity["project"] \
                or (state["Status"] != "running" and not (record["role"].endswith("-migrate") and state["Status"] == "exited" and state["ExitCode"] == 0)):
            raise binding.BoundaryError("PLATFORM_DEPLOYMENT_DRIFT")
    network = json.loads(deployment.command(["docker", "network", "inspect", identity["deployment"]["product_network"]]))[0]
    if network["Id"] != manifest["platform_network_id"] or network["Internal"] is not True:
        raise binding.BoundaryError("PLATFORM_NETWORK_CHANGED")

#!/usr/bin/env python3
"""真实产品实验栈生命周期：init → up → bootstrap → status；down 精确停止本栈。

凭据仅保存在 gitignored artifacts/multi-system，0600，不输出到终端。
业务对象由相邻 seed 工具通过原生 API 创建；本脚本只初始化组织、管理员和测试身份。
Keycloak start-dev/H2 仅用于本机测试，不表示企业 IAM 已上线。
"""

import argparse
import base64
import json
import os
from pathlib import Path
import secrets
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import uuid


ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / "artifacts/multi-system"
CONFIG = STATE / "runtime.json"
COMPOSE = ["docker", "compose", "--project-name", "egap-multisystem", "--env-file",
           str(STATE / "runtime.env"), "-f", str(ROOT / "deploy/multi-system/compose.yaml")]


def secure_write(path: Path, value: str) -> None:
    """创建与更新均保持0600；调用方不得将生成凭据传给stdout。"""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(value)


def initialize() -> None:
    """首次生成独立凭据；已有配置时拒绝隐式轮换，避免持久数据库失配。"""
    STATE.mkdir(parents=True, exist_ok=True)
    if CONFIG.exists():
        if not (STATE / "runtime.env").exists():
            raise RuntimeError("ENV_MISSING_FOR_EXISTING_RUNTIME")
        return
    values = {key: secrets.token_hex(32) for key in (
        "CHATWOOT_SECRET", "CHATWOOT_DB_PASSWORD", "LAGO_DB_PASSWORD", "LAGO_SECRET",
        "LAGO_ENCRYPTION_PRIMARY_KEY", "LAGO_ENCRYPTION_DETERMINISTIC_KEY",
        "LAGO_ENCRYPTION_KEY_DERIVATION_SALT", "LAGO_API_KEY", "KEYCLOAK_ADMIN_PASSWORD")}
    # Lago 官方脚本要求 base64 PEM。私钥不落到可提交文件，也不打印。
    key = subprocess.run(["openssl", "genrsa", "2048"], check=True, capture_output=True).stdout
    values["LAGO_RSA_PRIVATE_KEY"] = base64.b64encode(key).decode()
    values["LAGO_USER_PASSWORD"] = "EgAp-" + secrets.token_hex(20) + "!7"
    operators = [{"username": "support-" + suffix, "password": "EgAp-" + secrets.token_hex(20) + "!7",
                  "subject": str(uuid.uuid4()), "tenant_id": "tenant-" + suffix} for suffix in ("a", "b")]
    config = {
        "schema_version": 1, "project": "egap-multisystem", "qualification": "LOCAL_INTEGRATION_ONLY",
        "chatwoot": {"base_url": "http://127.0.0.1:58101", "account_id": None,
                     "api_token": secrets.token_hex(24), "admin_email": "lab@example.invalid",
                     "admin_password": "EgAp-" + secrets.token_hex(20) + "!7"},
        "lago": {"base_url": "http://127.0.0.1:58102", "api_key": values["LAGO_API_KEY"],
                 "front_url": "http://127.0.0.1:58104", "admin_email": "lab@example.invalid",
                 "admin_password": values["LAGO_USER_PASSWORD"]},
        "keycloak": {"base_url": "http://127.0.0.1:58103", "realm": "egap-lab",
                     "issuer": "http://127.0.0.1:58103/realms/egap-lab", "client_id": "egap-support",
                     "client_secret": secrets.token_hex(32), "audience": "egap-support",
                     "admin_username": "lab-admin", "admin_password": values["KEYCLOAK_ADMIN_PASSWORD"],
                     "operators": operators},
    }
    secure_write(STATE / "runtime.env", "".join(key + "=" + value + "\n" for key, value in values.items()))
    secure_write(CONFIG, json.dumps(config, indent=2) + "\n")


def request_json(url: str, *, payload=None, token=None, form=None):
    """只用于本地原生管理/API健康检查；异常不携带响应正文或认证内容。"""
    headers = {}
    data = None
    if payload is not None:
        data, headers["Content-Type"] = json.dumps(payload).encode(), "application/json"
    if form is not None:
        data, headers["Content-Type"] = urllib.parse.urlencode(form).encode(), "application/x-www-form-urlencoded"
    if token:
        headers["Authorization"] = "Bearer " + token
    with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers), timeout=15) as response:
        raw = response.read()
        return json.loads(raw) if raw else None


def bootstrap() -> None:
    """服务启动后创建测试realm和客服管理账户；重复运行不重置业务对象。"""
    config = json.loads(CONFIG.read_text())
    identity = config["keycloak"]
    admin = request_json(identity["base_url"] + "/realms/master/protocol/openid-connect/token", form={
        "grant_type": "password", "client_id": "admin-cli", "username": identity["admin_username"],
        "password": identity["admin_password"]})["access_token"]
    realm_url = identity["base_url"] + "/admin/realms/" + identity["realm"]
    try:
        request_json(realm_url, token=admin)
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise RuntimeError("KEYCLOAK_REALM_LOOKUP_FAILED") from None
        realm = {"realm": identity["realm"], "enabled": True, "sslRequired": "none",
                 "registrationAllowed": False, "roles": {"realm": [{"name": "support-agent"}]},
                 "clients": [{"clientId": identity["client_id"], "secret": identity["client_secret"],
                              "publicClient": False, "standardFlowEnabled": True,
                              "directAccessGrantsEnabled": True, "serviceAccountsEnabled": False,
                              "redirectUris": ["http://127.0.0.1:58105/*"],
                              "protocolMappers": [{"name": "support-audience", "protocol": "openid-connect",
                                "protocolMapper": "oidc-audience-mapper", "config": {
                                  "included.client.audience": identity["audience"],
                                  "access.token.claim": "true", "id.token.claim": "false"}}]}],
                 "users": [{"id": item["subject"], "username": item["username"], "enabled": True,
                            "emailVerified": True, "firstName": "Lab", "lastName": item["username"],
                            "email": item["username"] + "@example.invalid", "realmRoles": ["support-agent"],
                            "credentials": [{"type": "password", "value": item["password"], "temporary": False}]}
                           for item in identity["operators"]]}
        request_json(identity["base_url"] + "/admin/realms", token=admin, payload=realm)
    cw = config["chatwoot"]
    env = dict(os.environ, EGAP_CHATWOOT_API_TOKEN=cw["api_token"], EGAP_CHATWOOT_PASSWORD=cw["admin_password"])
    ruby = '''
account = Account.find_or_create_by!(name: 'EGAP Integration Lab')
user = User.find_or_initialize_by(email: 'lab@example.invalid')
if user.new_record?
  user.name = 'EGAP Lab Operator'
  user.password = ENV.fetch('EGAP_CHATWOOT_PASSWORD')
  user.password_confirmation = user.password
  user.skip_confirmation!
  user.save!
end
AccountUser.find_or_create_by!(account: account, user: user) { |membership| membership.role = :administrator }
user.access_token.update!(token: ENV.fetch('EGAP_CHATWOOT_API_TOKEN'))
puts "EGAP_ACCOUNT_ID=#{account.id}"
'''
    result = subprocess.run(COMPOSE + ["exec", "-T", "-e", "EGAP_CHATWOOT_API_TOKEN", "-e", "EGAP_CHATWOOT_PASSWORD",
                            "chatwoot", "bundle", "exec", "rails", "runner", "-"], input=ruby,
                            text=True, capture_output=True, env=env)
    secure_write(STATE / "bootstrap-chatwoot.log", result.stdout + result.stderr)
    if result.returncode:
        raise RuntimeError("CHATWOOT_BOOTSTRAP_FAILED_SEE_PRIVATE_LOG")
    account_lines = [line for line in result.stdout.splitlines() if line.startswith("EGAP_ACCOUNT_ID=")]
    if len(account_lines) != 1:
        raise RuntimeError("CHATWOOT_ACCOUNT_ID_MISSING")
    cw["account_id"] = int(account_lines[0].split("=", 1)[1])
    secure_write(CONFIG, json.dumps(config, indent=2) + "\n")


def status() -> dict:
    """真实HTTP失败就是未就绪；绝不回落到fixture或把容器running等同业务就绪。"""
    config = json.loads(CONFIG.read_text())
    checks = {}
    urls = {"chatwoot": config["chatwoot"]["base_url"] + "/api",
            "lago": config["lago"]["base_url"] + "/health",
            "keycloak": config["keycloak"]["issuer"] + "/.well-known/openid-configuration"}
    for name, url in urls.items():
        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                checks[name] = {"ready": response.status == 200, "http_status": response.status}
        except (OSError, ValueError):
            checks[name] = {"ready": False, "error": "DEPENDENCY_NOT_READY"}
    return {"project": "egap-multisystem", "status": "ready" if all(row["ready"] for row in checks.values()) else "not_ready",
            "checks": checks, "business_verification": "not_performed"}


def verify() -> dict:
    """原生认证探针：真实签发并内省两个测试用户令牌，只持久化无秘密的断言。

    由联调入口调用；健康200不足以证明后端凭据或用户身份可用，因此独立检查。
    这仍只是连接资格检查，不替代客户/订阅/发票归属与业务契约验证。
    """
    config = json.loads(CONFIG.read_text())
    identity, receipts = config["keycloak"], []
    for operator in identity["operators"]:
        token = request_json(identity["issuer"] + "/protocol/openid-connect/token", form={
            "grant_type": "password", "client_id": identity["client_id"], "client_secret": identity["client_secret"],
            "username": operator["username"], "password": operator["password"]})["access_token"]
        result = request_json(identity["issuer"] + "/protocol/openid-connect/token/introspect", form={
            "client_id": identity["client_id"], "client_secret": identity["client_secret"], "token": token})
        audiences = result.get("aud", [])
        audiences = [audiences] if isinstance(audiences, str) else audiences
        if not (result.get("active") is True and result.get("sub") == operator["subject"]
                and result.get("iss") == identity["issuer"] and identity["audience"] in audiences):
            raise RuntimeError("KEYCLOAK_IDENTITY_CHECK_FAILED")
        receipts.append({"subject": result["sub"], "active": True, "issuer": result["iss"], "aud": audiences})
    cw = config["chatwoot"]
    request = urllib.request.Request(cw["base_url"] + "/api/v1/profile", headers={"api_access_token": cw["api_token"]})
    with urllib.request.urlopen(request, timeout=15) as response:
        profile = json.load(response)
    if not any(account["id"] == cw["account_id"] for account in profile.get("accounts", [])):
        raise RuntimeError("CHATWOOT_ACCOUNT_CHECK_FAILED")
    customers = request_json(config["lago"]["base_url"] + "/api/v1/customers", token=config["lago"]["api_key"])
    if not isinstance(customers.get("customers"), list):
        raise RuntimeError("LAGO_AUTHENTICATION_CHECK_FAILED")
    receipt = {"health": status(), "identity_checks": receipts, "chatwoot_account_id": cw["account_id"],
               "chatwoot_api_authenticated": True, "lago_api_authenticated": True}
    secure_write(STATE / "deployment-evidence.json", json.dumps(receipt, indent=2) + "\n")
    return {"native_authentication": "passed", "identity_subjects_checked": len(receipts),
            "evidence": str((STATE / "deployment-evidence.json").relative_to(ROOT))}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("init", "up", "bootstrap", "status", "verify", "down"))
    action = parser.parse_args().action
    initialize()
    if action in ("up", "down"):
        # down不删除卷、不使用全局prune，仅操作固定compose project。
        args = ["up", "-d"] if action == "up" else ["down", "--remove-orphans"]
        result = subprocess.run(COMPOSE + args, text=True, capture_output=True)
        secure_write(STATE / (action + ".log"), result.stdout + result.stderr)
        if result.returncode:
            raise RuntimeError("DEPLOYMENT_FAILED_SEE_PRIVATE_LOG")
    elif action == "bootstrap":
        bootstrap()
    result = status() if action == "status" else verify() if action == "verify" else {"action": action, "completed": True}
    print(json.dumps(result))
    if result.get("status") == "not_ready":
        raise SystemExit(1)


if __name__ == "__main__":
    main()

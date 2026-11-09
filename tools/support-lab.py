#!/usr/bin/env python3
"""本机受信实验宿主：持久身份→Docker基础设施→最小凭据角色→真实HTTP验收。

调用node的已有LabLedger/DockerHost，不把模型或候选输出当资源清理授权。
所有秘密仅保存在忽略目录0600文件，命令输出只有端点、状态和引用。
"""

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from uuid import uuid4
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "services/genai-support-sut"
LAB = ROOT / "artifacts/lab/genai-support-runtime"
sys.path.insert(0, str(SERVICE / "src"))
import psycopg
from psycopg import sql
from genai_support.config import DEFAULT_FIXTURE_PATH
from genai_support.fixtures import load_fixture_bundle
from genai_support.store import Store
from genai_support.gateway_store import GatewayStore
from genai_support.worker import _RequestDeadline, _DeadlineHandler, _NoRedirect


def save(path: Path, value: object) -> None:
    """宿主状态先写私有临时文件再原子替换；不把半写JSON当恢复入口。"""
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as output:
        os.chmod(temporary, 0o600)
        json.dump(value, output, indent=2)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def node(command: str, data: dict) -> dict:
    env = {k: v for k, v in os.environ.items() if k in {"PATH", "HOME", "LANG", "TMPDIR"}}
    result = subprocess.run(["node", "tools/environment-host/dist/support-environment.js", command],
        cwd=ROOT, env=env, input=json.dumps(data), text=True, capture_output=True, timeout=120)
    if result.returncode:
        # stderr只包含受控错误码/环境ID，保留以便定位首次未完成的provision。
        sys.stderr.write(result.stderr)
        raise RuntimeError("ENVIRONMENT_OPERATION_FAILED")
    return json.loads(result.stdout)


def request(url: str, *, token: str | None = None, body: dict | None = None, timeout: float = 3) -> tuple[int, dict]:
    """宿主仅访问本次观察的loopback端点；不继承代理，不重试POST。"""
    endpoint = urlsplit(url)
    if endpoint.scheme != "http" or endpoint.hostname != "127.0.0.1" or not endpoint.port or endpoint.username or endpoint.password:
        raise ValueError("LOCAL_ENDPOINT_REQUIRED")
    headers = {"Authorization": "Bearer " + token} if token else {}
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = Request(url, data=None if body is None else json.dumps(body).encode(), headers=headers)
    # 本机原型复用已测的总deadline传输；不跟随SUT重定向带走认证header。
    with _RequestDeadline(timeout) as deadline:
        try:
            response = build_opener(ProxyHandler({}), _NoRedirect(), _DeadlineHandler(deadline)).open(req, timeout=timeout)
        except HTTPError as error:
            response = error
        with response:
            raw = response.read(1_048_577)
            if len(raw) > 1_048_576 or deadline.expired:
                raise TimeoutError("HOST_HTTP_RESPONSE_UNCONFIRMED")
            return response.status, json.loads(raw)


def initialize_databases(directory: Path) -> dict:
    """只有可信准备器持管理员口令；按角色分数据库，测试另用专库。"""
    metadata = json.loads((directory / "environment.json").read_text())
    secrets = json.loads((directory / "secrets.json").read_text())
    port = metadata["ports"]["postgres"]
    def dsn(user: str, password: str, database: str) -> str:
        return f"postgresql://{user}:{password}@127.0.0.1:{port}/{database}"
    admin = dsn("postgres", secrets["postgres_password"], "postgres")
    deadline = time.monotonic() + 30
    while True:
        try:
            with psycopg.connect(admin, connect_timeout=2, autocommit=True) as connection:
                for name, key in (("support_app", "sut_password"), ("support_gateway", "gateway_password"), ("support_reader", "reader_password")):
                    if not connection.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (name,)).fetchone():
                        connection.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(sql.Identifier(name), sql.Literal(secrets[key])))
                for database, owner in (("support", "support_app"), ("gateway", "support_gateway"), ("support_test", "support_app"), ("gateway_test", "support_gateway"), ("support_delivery_test", "support_app")):
                    if not connection.execute("SELECT 1 FROM pg_database WHERE datname=%s", (database,)).fetchone():
                        connection.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(sql.Identifier(database), sql.Identifier(owner)))
                    connection.execute(sql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC").format(sql.Identifier(database)))
                    connection.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(database), sql.Identifier(owner)))
                connection.execute("GRANT CONNECT ON DATABASE support TO support_reader")
            break
        except psycopg.OperationalError:
            if time.monotonic() >= deadline:
                raise RuntimeError("DATABASE_START_TIMEOUT") from None
            time.sleep(0.2)
    values = {
        "database_url": dsn("support_app", secrets["sut_password"], "support"),
        "gateway_database_url": dsn("support_gateway", secrets["gateway_password"], "gateway"),
        "reader_database_url": dsn("support_reader", secrets["reader_password"], "support"),
        "test_database_url": dsn("support_app", secrets["sut_password"], "support_test"),
        "test_gateway_database_url": dsn("support_gateway", secrets["gateway_password"], "gateway_test"),
        "delivery_database_url": dsn("support_app", secrets["sut_password"], "support_delivery_test"),
        "redis_url": f"redis://:{secrets['redis_password']}@127.0.0.1:{metadata['ports']['redis']}/0",
    }
    Store(values["database_url"]).migrate_and_seed(load_fixture_bundle(DEFAULT_FIXTURE_PATH))
    from genai_support.integrations.persistence import IntegrationStore
    IntegrationStore(values["database_url"]).migrate()
    GatewayStore(values["gateway_database_url"]).migrate()
    with psycopg.connect(values["database_url"], connect_timeout=3) as connection:
        connection.execute("GRANT USAGE ON SCHEMA egap_support TO support_reader")
        connection.execute("GRANT SELECT (job_id,tenant_id,ticket_id,ticket_revision) ON egap_support.support_jobs TO support_reader")
        connection.execute("GRANT SELECT ON egap_support.external_inputs TO support_reader")
    save(directory / "connections.private.json", values)
    return values


def canonical_signature(value: str) -> str:
    """macOS Python启动会从venv入口re-exec到Python.app；保留启动时间及完整角色参数。

    PID仍由外层匹配，instance/launch由私有回执校验；只消除已知Python入口别名。
    Ollama等其他进程保留完整可执行路径，不泛化任意命令。
    """
    marker = " -m genai_support.runtime "
    if marker not in value:
        return value
    executable = value.split(marker, 1)[0][24:].strip()
    allowed = {str(SERVICE / ".venv/bin/python"), str((SERVICE / ".venv/bin/python").resolve()),
               str(Path(sys.base_prefix) / "Resources/Python.app/Contents/MacOS/Python"),
               str((Path(sys.base_prefix) / "Resources/Python.app/Contents/MacOS/Python").resolve())}
    return value[:24] + marker + value.split(marker, 1)[1] if executable in allowed else value


def process_signature(pid: int) -> str | None:
    result = subprocess.run(["ps", "-p", str(pid), "-o", "lstart=", "-o", "command="], capture_output=True, text=True, timeout=3)
    return canonical_signature(result.stdout.strip()) if result.returncode == 0 and result.stdout.strip() else None


def start_role(directory: Path, role: str, config: dict, state: dict) -> dict:
    """先记录启动意图，子进程写PID/socket回执；按PID+启动签名收尾防误杀。"""
    config = {**config, "launch_id": str(uuid4())}
    config_path = directory / f"{role}.private.json"
    save(config_path, config)
    receipt_path = directory / f"{role}.receipt.json"
    if receipt_path.exists():
        receipt_path.unlink()
    state.setdefault("processes", {})[role] = {"status": "STARTING", "launch_id": config["launch_id"], "config_ref": config_path.name}
    save(directory / "runtime.json", state)
    env = {k: v for k, v in os.environ.items() if k in {"PATH", "HOME", "LANG", "TMPDIR"}}
    env["PYTHONPATH"] = str(SERVICE / "src")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    with (directory / f"{role}.log").open("ab") as log:
        os.chmod(log.name, 0o600)
        child = subprocess.Popen([str(SERVICE / ".venv/bin/python"), "-m", "genai_support.runtime", "--role", role,
            "--instance", config["instance_id"], "--config", str(config_path)], cwd=ROOT, env=env,
            stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    signature = process_signature(child.pid)
    state["processes"][role] = {"pid": child.pid, "signature": signature, "launch_id": config["launch_id"], "config_ref": config_path.name, "status": "STARTING"}
    save(directory / "runtime.json", state)
    deadline = time.monotonic() + 15
    receipt = None
    while True:
        if receipt_path.exists():
            candidate = json.loads(receipt_path.read_text())
            if candidate["pid"] == child.pid and candidate.get("launch_id") == config["launch_id"] and candidate.get("status") != "BOOTING":
                receipt = candidate
                break
        if child.poll() is not None or time.monotonic() >= deadline:
            raise RuntimeError("ROLE_START_FAILED_" + role.upper())
        time.sleep(0.05)
    assert receipt is not None
    if receipt["pid"] != child.pid:
        raise RuntimeError("ROLE_RECEIPT_MISMATCH")
    state["processes"][role].update(receipt)
    state["processes"][role]["signature"] = canonical_signature(receipt["signature"])
    node("resource", {"environment_instance_id": config["instance_id"], "kind": "process", "provider_id": f"pid:{child.pid}:signature:{hashlib.sha256(state['processes'][role]['signature'].encode()).hexdigest()}"})
    if "child_pid" in receipt:
        node("resource", {"environment_instance_id": config["instance_id"], "kind": "process", "provider_id": f"pid:{receipt['child_pid']}:signature:{hashlib.sha256(receipt['child_signature'].encode()).hexdigest()}"})
    if "port" in receipt:
        config["port"] = receipt["port"]
        save(config_path, config)
    save(directory / "runtime.json", state)
    return receipt


def wait_health(url: str, timeout: float = 30) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        try:
            status, result = request(url)
            if status == 200:
                return result
        except (OSError, ValueError):
            pass
        if time.monotonic() >= deadline:
            raise RuntimeError("HEALTH_TIMEOUT")
        time.sleep(0.2)


def require_previous_launch_stopped(directory: Path, state: dict) -> None:
    """start_services 在任何初始化/覆盖回执前调用，避免丢失旧进程的清理身份。

    Ollama 是 model wrapper 的子进程：wrapper 消失并不代表 child 已退出。
    未确认的启动身份也不能当作停止；先走 stop_services 的独立回执恢复，
    再重新 start。这里仅观察，不发送信号，也不替换旧 runtime 或 receipt。
    """
    for role, process in state.get("processes", {}).items():
        if "pid" not in process or not isinstance(process.get("signature"), str) or not process["signature"]:
            raise RuntimeError("PROCESS_START_RECEIPT_UNKNOWN_STOP_FIRST")
        actual = process_signature(process["pid"])
        if actual is not None:
            if actual != canonical_signature(process["signature"]):
                raise RuntimeError("PROCESS_IDENTITY_CHANGED")
            raise RuntimeError("EXISTING_PROCESS_REQUIRES_STOP")
        if role != "model":
            continue
        path = directory / "model.receipt.json"
        if not path.exists():
            raise RuntimeError("MODEL_CHILD_IDENTITY_UNKNOWN")
        receipt = json.loads(path.read_text())
        if receipt.get("instance_id") != state["instance_id"] or receipt.get("launch_id") != process.get("launch_id"):
            raise RuntimeError("MODEL_CHILD_RECEIPT_MISMATCH")
        child_pid, signature = receipt.get("child_pid"), receipt.get("child_signature")
        if not child_pid or not isinstance(signature, str) or not signature:
            raise RuntimeError("MODEL_CHILD_IDENTITY_UNKNOWN")
        child_actual = process_signature(child_pid)
        if child_actual is not None:
            if child_actual != signature:
                raise RuntimeError("MODEL_CHILD_IDENTITY_CHANGED")
            raise RuntimeError("EXISTING_MODEL_CHILD_REQUIRES_STOP")


def start_services(directory: Path, mode: str, model: str, *, max_calls: int = 12,
                   integration_config: Path | None = None) -> dict:
    """实验宿主启动真实角色；可选外部来源配置以私有冻结副本绑定本次环境。

    首次写入后不得用新客户映射替换同一环境；修改接入配置须创建新环境。
    Gateway只获得来源类型与只读DSN，不获得外部产品凭据。
    """
    metadata = json.loads((directory / "environment.json").read_text())
    if mode != metadata["config"]["mode"] or (mode == "LIVE_MODEL" and model != metadata["config"]["model"]):
        raise RuntimeError("ENVIRONMENT_MODE_MODEL_MISMATCH")
    instance = metadata["environment"]["environment_instance_id"]
    if not node("source-check", {"environment_instance_id": instance})["unchanged"]:
        raise RuntimeError("SOURCE_CHANGED_REQUIRES_NEW_ENVIRONMENT")
    if datetime.fromisoformat(metadata["environment"]["expires_at"].replace("Z", "+00:00")).timestamp() <= time.time():
        raise RuntimeError("ENVIRONMENT_EXPIRED")
    previous = json.loads((directory / "runtime.json").read_text()) if (directory / "runtime.json").exists() else {}
    require_previous_launch_stopped(directory, previous)
    frozen_integration = directory / "integration.private.json"
    if integration_config:
        if integration_config.is_symlink() or integration_config.stat().st_mode & 0o077:
            raise RuntimeError("PRIVATE_INTEGRATION_CONFIG_REQUIRED")
        integration_value = json.loads(integration_config.read_text())
        expected_digest = metadata["config"].get("integration_config_digest")
        actual_digest = hashlib.sha256(json.dumps(integration_value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if expected_digest != actual_digest:
            raise RuntimeError("INTEGRATION_CONFIG_IDENTITY_MISMATCH")
        if frozen_integration.exists() and json.loads(frozen_integration.read_text()) != integration_value:
            raise RuntimeError("INTEGRATION_CONFIG_CHANGED_REQUIRES_NEW_ENVIRONMENT")
        if not frozen_integration.exists():
            save(frozen_integration, integration_value)
    secrets = json.loads((directory / "secrets.json").read_text())
    connections = initialize_databases(directory)
    common = {"instance_id": instance, "expires_at": metadata["environment"]["expires_at"]}
    previous_endpoints = previous.get("endpoints", {})
    state = {**common, "execution_mode": mode, "processes": {}, "endpoints": {},
             "prior_launches": [*previous.get("prior_launches", []), previous.get("processes", {})]}
    save(directory / "runtime.json", state)
    if mode == "LIVE_MODEL":
        binary = shutil.which("ollama")
        model_directory = Path.home() / ".ollama/models"
        if not binary or not model_directory.is_dir() or not re.fullmatch(r"[a-zA-Z0-9_.:-]+", model):
            raise RuntimeError("INSTALLED_LOCAL_MODEL_REQUIRED")
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        if "local-model" in previous_endpoints:
            port = int(previous_endpoints["local-model"].rsplit(":", 1)[1])
        start_role(directory, "model", {**common, "port": port, "ollama_binary": binary, "model_directory": str(model_directory)}, state)
        endpoint = f"http://127.0.0.1:{port}"
        tags = wait_health(endpoint + "/api/tags")
        selected = next((row for row in tags["models"] if row["name"] == model), None)
        if selected is None:
            raise RuntimeError("MODEL_NOT_INSTALLED_NO_AUTOMATIC_DOWNLOAD")
        observation = {"name": model, "manifest_digest": selected["digest"], "source": "local-ollama-api-tags", "endpoint": endpoint}
        model_record = directory / "model-observation.json"
        if model_record.exists() and json.loads(model_record.read_text()) != observation:
            raise RuntimeError("MODEL_DIGEST_CHANGED_REQUIRES_NEW_ENVIRONMENT")
        save(model_record, observation)
        state["endpoints"]["local-model"] = endpoint
    else:
        endpoint = None
    gateway = start_role(directory, "gateway", {**common, "database_url": connections["gateway_database_url"],
        "reader_database_url": connections["reader_database_url"], "gateway_token": secrets["gateway_token"],
        "execution_mode": mode, "model_endpoint": endpoint + "/v1/chat/completions" if endpoint else None,
        "model_name": model if endpoint else None, "max_calls": max_calls,
        "business_source": "CHATWOOT_LAGO" if frozen_integration.exists() else "SYNTHETIC_FIXTURE",
        "port": int(previous_endpoints["tool-model-gateway"].rsplit(":", 1)[1]) if "tool-model-gateway" in previous_endpoints else 0}, state)
    gateway_url = f"http://127.0.0.1:{gateway['port']}"
    wait_health(gateway_url + "/health")
    state["endpoints"]["tool-model-gateway"] = gateway_url
    for role in ("relay", "worker"):
        config = {**common, "database_url": connections["database_url"], "redis_url": connections["redis_url"]}
        if role == "worker":
            config.update(gateway_url=gateway_url, gateway_token=secrets["gateway_token"])
        start_role(directory, role, config, state)
    api = start_role(directory, "api", {**common, "database_url": connections["database_url"], "redis_url": connections["redis_url"],
        "api_token_a": secrets["api_token_a"], "api_token_b": secrets["api_token_b"], "gateway_url": gateway_url, "execution_mode": mode,
        "integration_config": str(frozen_integration) if frozen_integration.exists() else None,
        "port": int(previous_endpoints["support-gateway"].rsplit(":", 1)[1]) if "support-gateway" in previous_endpoints else 0}, state)
    state["endpoints"]["support-gateway"] = f"http://127.0.0.1:{api['port']}"
    wait_health(state["endpoints"]["support-gateway"] + "/health/ready")
    state["status"] = "RUNNING"
    save(directory / "runtime.json", state)
    node("register", {"environment_instance_id": instance, "endpoints": state["endpoints"]})
    return state


def stop_role(directory: Path, role: str, state: dict) -> None:
    process = state.get("processes", {}).get(role)
    if not process:
        return
    if "pid" not in process or not isinstance(process.get("signature"), str) or not process["signature"]:
        receipt_path = directory / f"{role}.receipt.json"
        if not receipt_path.exists():
            raise RuntimeError("PROCESS_START_RECEIPT_UNKNOWN")
        recovered = json.loads(receipt_path.read_text())
        if recovered.get("launch_id") != process.get("launch_id") or recovered.get("instance_id") != state["instance_id"]:
            raise RuntimeError("PROCESS_START_RECEIPT_UNKNOWN")
        if ("pid" in process and recovered.get("pid") != process["pid"]) or not recovered.get("signature"):
            raise RuntimeError("PROCESS_START_RECEIPT_UNKNOWN")
        process.update(recovered)
    pid = process["pid"]
    process["signature"] = canonical_signature(process["signature"])
    actual = process_signature(pid)
    if actual is not None and (actual != process["signature"] or "genai_support.runtime" not in actual or state["instance_id"] not in actual):
        raise RuntimeError("PROCESS_IDENTITY_CHANGED")
    if actual is not None:
        os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + (145 if role in {"gateway", "worker"} else 20)
    while actual is not None and process_signature(pid) == actual:
        if time.monotonic() >= deadline:
            raise RuntimeError("PROCESS_STOP_UNCONFIRMED")
        time.sleep(0.2)
    # wrapper被SIGKILL后模型子进程仍可能活着；用其独立启动签名核对，绝不只信旧PID。
    if role == "model":
        receipt = json.loads((directory / "model.receipt.json").read_text())
        if receipt.get("instance_id") != state["instance_id"] or receipt.get("launch_id") != process.get("launch_id"):
            raise RuntimeError("MODEL_CHILD_RECEIPT_MISMATCH")
        child_pid, signature = receipt.get("child_pid"), receipt.get("child_signature")
        if not child_pid or not signature:
            raise RuntimeError("MODEL_CHILD_IDENTITY_UNKNOWN")
        child_actual = process_signature(child_pid)
        if child_actual is not None:
            if child_actual != signature:
                raise RuntimeError("MODEL_CHILD_IDENTITY_CHANGED")
            os.kill(child_pid, signal.SIGTERM)
            end = time.monotonic() + 10
            while process_signature(child_pid) == signature:
                if time.monotonic() >= end:
                    raise RuntimeError("MODEL_CHILD_STOP_UNCONFIRMED")
                time.sleep(0.2)
    process["status"] = "STOPPED"
    save(directory / "runtime.json", state)


def stop_services(directory: Path) -> None:
    path = directory / "runtime.json"
    if not path.exists():
        return
    state = json.loads(path.read_text())
    for role in ("api", "relay", "worker", "gateway", "model"):
        stop_role(directory, role, state)
    state["status"] = "STOPPED"
    save(path, state)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("up", "initialize", "start", "status", "stop", "down", "test", "smoke"))
    parser.add_argument("--environment")
    parser.add_argument("--mode", choices=("scripted", "live"), default="live")
    parser.add_argument("--model", default="qwen2:7b")
    parser.add_argument("--integration-config", type=Path)
    parser.add_argument("--max-calls", type=int, default=12)
    args = parser.parse_args()
    LAB.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (LAB / ".host.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            mode = "LIVE_MODEL" if args.mode == "live" else "SCRIPTED_CALIBRATION"
            if args.command == "up":
                if (LAB / "active.json").exists():
                    raise RuntimeError("ACTIVE_ENVIRONMENT_EXISTS_USE_STATUS_OR_DOWN")
                provision = {"mode": mode, "model": args.model if mode == "LIVE_MODEL" else None}
                if args.integration_config:
                    if args.integration_config.is_symlink() or args.integration_config.stat().st_mode & 0o077:
                        raise RuntimeError("PRIVATE_INTEGRATION_CONFIG_REQUIRED")
                    configured = json.loads(args.integration_config.read_text())
                    provision.update(business_source="CHATWOOT_LAGO", integration_config_digest=hashlib.sha256(
                        json.dumps(configured, sort_keys=True, separators=(",", ":")).encode()).hexdigest())
                metadata = node("up", provision)
                env_id = metadata["environment"]["environment_instance_id"]
                save(LAB / "active.json", {"environment_instance_id": env_id})
            else:
                env_id = args.environment or json.loads((LAB / "active.json").read_text())["environment_instance_id"]
            if not re.fullmatch(r"environment_[a-f0-9-]{36}", env_id):
                raise ValueError("INVALID_ENVIRONMENT")
            directory = LAB / env_id
            if args.command in {"up", "start"}:
                state = start_services(directory, mode, args.model, max_calls=args.max_calls,
                                       integration_config=args.integration_config)
                print(json.dumps({"status": "RUNNING", "environment_instance_id": env_id, "mode": mode, "endpoints": state["endpoints"]}, indent=2))
            elif args.command == "initialize":
                initialize_databases(directory)
                print(json.dumps({"status": "DATABASES_INITIALIZED", "environment_instance_id": env_id}))
            elif args.command == "status":
                state = json.loads((directory / "runtime.json").read_text())
                actual = {role: process_signature(p["pid"]) == p.get("signature") for role, p in state.get("processes", {}).items() if "pid" in p}
                print(json.dumps({"environment_instance_id": env_id, "mode": state["execution_mode"], "processes_running": actual, "endpoints": state["endpoints"]}, indent=2))
            elif args.command == "test":
                connection = json.loads((directory / "connections.private.json").read_text())
                environment = {k: v for k, v in os.environ.items() if k in {"PATH", "HOME", "LANG", "TMPDIR"}}
                environment.update(PYTHONPATH=str(SERVICE / "src"), PYTHONDONTWRITEBYTECODE="1",
                    EGAP_SUPPORT_TEST_DATABASE_URL=connection["test_database_url"],
                    EGAP_SUPPORT_TEST_GATEWAY_DATABASE_URL=connection["test_gateway_database_url"],
                    EGAP_SUPPORT_DELIVERY_CONFIG=str(directory / "connections.private.json"))
                result = subprocess.run([str(SERVICE / ".venv/bin/python"), "-m", "unittest", "discover", "-s", str(SERVICE / "tests"), "-v"], cwd=ROOT, env=environment)
                return result.returncode
            elif args.command == "smoke":
                if not node("source-check", {"environment_instance_id": env_id})["unchanged"]:
                    raise RuntimeError("SOURCE_CHANGED_REQUIRES_NEW_ENVIRONMENT")
                if (directory / "smoke-started.json").exists():
                    raise RuntimeError("SMOKE_ALREADY_STARTED_INSPECT_EXISTING_JOBS")
                action = node("action", {"environment_instance_id": env_id, "kind": "SMOKE"})
                evidence = ROOT / "artifacts/evidence/genai-live" / env_id / action["action_id"]
                evidence.mkdir(parents=True, mode=0o700)
                save(directory / "smoke-started.json", {"action_id": action["action_id"], "evidence_ref": str(evidence.relative_to(ROOT))})
                environment = {k: v for k, v in os.environ.items() if k in {"PATH", "HOME", "LANG", "TMPDIR"}}
                api = subprocess.run(["node", "tools/support-api-smoke.mjs", env_id, str(evidence)], cwd=ROOT, env=environment)
                business = None
                if api.returncode == 0:
                    business = subprocess.run([str(SERVICE / ".venv/bin/python"), "tools/support-business-smoke.py", str(directory), str(evidence)], cwd=ROOT, env=environment)
                outcome = "SUCCEEDED" if api.returncode == 0 and business is not None and business.returncode == 0 else "FAILED"
                report = {"outcome": outcome, "scope": "PUBLIC_CALIBRATION_ONLY", "environment_instance_id": env_id,
                          "api_exit": api.returncode, "business_exit": business.returncode if business else None, "evidence_ref": str(evidence.relative_to(ROOT))}
                save(evidence / "execution.json", report)
                digest = hashlib.sha256(json.dumps(report, sort_keys=True).encode()).hexdigest()
                node("complete", {"environment_instance_id": env_id, "action_id": action["action_id"], "outcome": outcome, "receipt_digest": digest})
                print(json.dumps(report, indent=2))
                return 0 if outcome == "SUCCEEDED" else 1
            else:
                stop_services(directory)
                if args.command == "down":
                    print(json.dumps(node("down", {"environment_instance_id": env_id, "processes_stopped": True})))
                    active = LAB / "active.json"
                    if active.exists() and json.loads(active.read_text())["environment_instance_id"] == env_id:
                        active.unlink()
                else:
                    print(json.dumps({"status": "STOPPED", "volumes_retained": True}))
            return 0
        except Exception as error:
            code = str(error) if re.fullmatch(r"[A-Z][A-Z0-9_]+", str(error)) else "SUPPORT_LAB_FAILED"
            print(json.dumps({"status": "FAILED", "code": code}), file=sys.stderr)
            return 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""EGAP 独立验收入口：部署身份 → HTTP 客户端 → 只读原生观察 → 独立参考核对。

不导入 genai_support，不调用产品函数，不向产品挂载 EGAP 文件。只使用操作员
显式提供的部署回执、调用者凭据和观察凭据；结果仅证明公开合成校准集及部署边界。
当前入口尚未连入 Verify Graph，也不会签发候选 Final 或企业发布资格。
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/source-platform"))
from client import SourceClient, SourceClientError


def load_reference(config: Path) -> tuple[dict, dict]:
    """验收者通过独立 QA 身份读固定答案；来源不可用时停止，不回退内置副本。"""
    client = SourceClient.from_file(config)
    if client.config["principal_id"] != "lab:oracle-reader":
        raise SourceClientError("ORACLE_READER_REQUIRED")
    pin = json.loads((ROOT / "config/acceptance-assets.v1.json").read_text())
    source = client.read(pin["namespace"], pin["object_id"], pin["version"], expected_sha256=pin["content_sha256"])
    if source["publisher_id"] != pin["publisher_id"] or source["metadata"]["source"] != pin["source"] \
            or source["metadata"]["profile"] != pin["profile"]:
        raise SourceClientError("ACCEPTANCE_PUBLICATION_MISMATCH")
    return json.loads(source["content"]), {k: source[k] for k in ("instance_id", "namespace", "object_id", "version",
        "publication_id", "publisher_id", "content_sha256", "metadata", "observed_at", "acl_decision")}


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def save(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2, default=str)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def command(args: list[str]) -> str:
    result = subprocess.run(args, capture_output=True, text=True, timeout=15)
    if result.returncode:
        raise ValueError("IDENTITY_LOOKUP_FAILED")
    return result.stdout.strip()


def bind_deployment(receipt: dict, product_repo: Path) -> dict:
    """回执是待核对声明；从 Docker 读真实镜像/挂载，从独立 Git 读候选对象。

    本地 Docker 管理员是信任根；容器自报 commit 不能独立建立可信身份。
    这里只接受固定本地校准部署，后续 ECS/ECR 通过另外的受信适配器接入。
    """
    deployment = receipt["deployment_id"]
    if not re.fullmatch(r"evid-sut-[a-f0-9]{12}", deployment):
        raise ValueError("INVALID_DEPLOYMENT_ID")
    commit = receipt["product_revision"]
    if not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise ValueError("INVALID_PRODUCT_COMMIT")
    image = receipt["product_image_id"]
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", image):
        raise ValueError("INVALID_PRODUCT_IMAGE")
    if product_repo.resolve() == ROOT.resolve():
        raise ValueError("PRODUCT_REPOSITORY_MUST_BE_SEPARATE")
    if command(["git", "-C", str(product_repo), "rev-parse", commit + "^{commit}"]) != commit:
        raise ValueError("PRODUCT_COMMIT_UNAVAILABLE")
    ids = command(["docker", "ps", "-a", "-q", "--filter", f"label=com.docker.compose.project={deployment}"]).splitlines()
    if not ids:
        raise ValueError("DEPLOYMENT_UNAVAILABLE")
    containers = json.loads(command(["docker", "inspect", *ids]))
    roles = {}
    product_image_record = json.loads(command(["docker", "image", "inspect", image]))[0]
    expected_id = product_image_record["Id"]
    if product_image_record["Config"].get("Labels", {}).get("org.opencontainers.image.revision") != commit:
        raise ValueError("IMAGE_COMMIT_MISMATCH")
    for item in containers:
        role = item["Config"]["Labels"]["com.docker.compose.service"]
        if role not in ("init", "api", "relay", "worker", "gateway"):
            continue
        if role in roles or item["Image"] != expected_id:
            raise ValueError("PRODUCT_ROLE_IDENTITY_MISMATCH")
        if role != "init" and item["State"]["Status"] != "running":
            raise ValueError("PRODUCT_NOT_RUNNING")
        if role == "init" and (item["State"]["Status"] != "exited" or item["State"]["ExitCode"] != 0):
            raise ValueError("PRODUCT_INITIALIZATION_UNCONFIRMED")
        mounts = [m for m in item["Mounts"] if m["Type"] != "tmpfs"]
        if len(mounts) != 1 or mounts[0]["Destination"] != "/run/secrets/role.json" or mounts[0]["RW"]:
            raise ValueError("PRODUCT_MOUNT_BOUNDARY_VIOLATION")
        if Path(mounts[0]["Source"]).name != role + ".private.json":
            raise ValueError("PRODUCT_ROLE_CONFIG_MISMATCH")
        if not item["HostConfig"]["ReadonlyRootfs"] or item["Config"]["User"] != "10001:10001":
            raise ValueError("PRODUCT_PROCESS_BOUNDARY_VIOLATION")
        roles[role] = item["Id"]
    if set(roles) != {"init", "api", "relay", "worker", "gateway"}:
        raise ValueError("MISSING_PRODUCT_ROLE")
    # 端口再与原生 Docker 记录核对，防止旧回执指向另一个运行中的版本。
    for role, container_port in (("api", "8080/tcp"), ("database", "5432/tcp")):
        item = next(c for c in containers if c["Config"]["Labels"]["com.docker.compose.service"] == role)
        published = item["NetworkSettings"]["Ports"][container_port]
        expected = receipt["endpoints"][role]
        if len(published) != 1 or published[0]["HostIp"] != "127.0.0.1" or expected["host"] != "127.0.0.1" \
                or int(published[0]["HostPort"]) != expected["port"]:
            raise ValueError("DEPLOYMENT_ENDPOINT_MISMATCH")
    return {"deployment_id": deployment, "sut_commit": commit,
        "sut_tree": command(["git", "-C", str(product_repo), "rev-parse", commit + "^{tree}"]),
        "sut_image_id": expected_id, "egap_commit": command(["git", "-C", str(ROOT), "rev-parse", "HEAD"]),
        "observer_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "container_ids": roles, "binding_authority": "LOCAL_DOCKER_OPERATOR"}


def request(port: int, path: str, token: str | None = None, body: dict | None = None) -> tuple[int, dict]:
    """固定 loopback、禁止重定向/代理和隐式重试；总超时后写操作保持 UNKNOWN。"""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    def abort() -> None:
        if conn.sock is not None:
            try:
                conn.sock.shutdown(2)
            except OSError:
                pass
        conn.close()
    timer = threading.Timer(5, abort)
    timer.daemon = True
    timer.start()
    try:
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        if body is not None:
            headers["Content-Type"] = "application/json"
        conn.request("GET" if body is None else "POST", path, None if body is None else json.dumps(body), headers)
        response = conn.getresponse()
        raw = response.read(1_048_577)
        if len(raw) > 1_048_576 or 300 <= response.status < 400:
            raise ValueError("HTTP_RESPONSE_UNCONFIRMED")
        return response.status, json.loads(raw)
    finally:
        timer.cancel()
        conn.close()


def run(directory: Path, product_repo: Path, output: Path, reference_config: Path) -> dict:
    """受信操作员触发；公开九例逐个一次提交，原始观察只落私有 artifacts。"""
    if output.exists():
        raise ValueError("NEW_RUN_DIRECTORY_REQUIRED")
    receipt = json.loads((directory / "deployment-receipt.json").read_text())
    binding = bind_deployment(receipt, product_repo)
    caller = json.loads((directory / "caller.private.json").read_text())
    observer = json.loads((directory / "observer.private.json").read_text())
    if any(item["deployment_id"] != binding["deployment_id"] for item in (caller, observer)):
        raise ValueError("CREDENTIAL_SCOPE_MISMATCH")
    reference, reference_source = load_reference(reference_config)
    report = {"schema_version": 1, "run_id": uuid4().hex, "binding": binding, "started_at": datetime.now(timezone.utc).isoformat(),
        "reference_sha256": reference_source["content_sha256"], "reference_source": reference_source,
        "scope": "PUBLIC_NINE_CASE_SCRIPTED_CALIBRATION", "final_qualification": False, "cases": [], "status": "STARTED"}
    save(output / "run.json", report)
    api_port = receipt["endpoints"]["api"]["port"]
    ready_status, ready = request(api_port, "/health/ready")
    if ready_status != 200 or ready["checks"].get("execution_mode") != "SCRIPTED_CALIBRATION":
        raise ValueError("EXPECTED_CALIBRATION_DEPLOYMENT_NOT_READY")
    def connect(database: str):
        return psycopg.connect(host="127.0.0.1", port=receipt["endpoints"]["database"]["port"],
            dbname=database, user=observer["username"], password=observer["password"], connect_timeout=3,
            row_factory=dict_row, options="-c statement_timeout=3000 -c lock_timeout=1000")
    # 先确认读角色没有写权，即使主动关闭默认只读事务也应被 ACL 拒绝。
    with connect("support") as conn:
        conn.execute("SET default_transaction_read_only=off")
        conn.commit()
        try:
            conn.execute("UPDATE egap_support.support_jobs SET status=status WHERE false")
        except psycopg.errors.InsufficientPrivilege:
            conn.rollback()
            report["observer_write_denied"] = True
        else:
            conn.rollback()
            raise ValueError("OBSERVER_WRITE_PRIVILEGE_VIOLATION")
    for case in reference["cases"]:
        # 每个执行单元重新检查授权；同一答案的旧缓存不能跳过当前来源权限。
        load_reference(reference_config)
        token = caller["api_token_a" if case["tenant_id"] == "tenant-a" else "api_token_b"]
        row = {"case_id": case["case_id"], "state": "SUBMITTING"}
        report["cases"].append(row)
        save(output / "run.json", report)  # 持久记录在任何写操作之前。
        try:
            status, submitted = request(api_port, "/support-resolution-jobs", token,
                {"schema_version": 1, "ticket_id": case["ticket_id"], "ticket_revision": case["ticket_revision"]})
        except Exception:
            row["state"] = "UNKNOWN"
            save(output / "run.json", report)
            raise ValueError("SUBMISSION_UNKNOWN_DO_NOT_RESUBMIT") from None
        if status != 202:
            row["state"] = "REJECTED" if 400 <= status < 500 else "UNKNOWN"
            save(output / "run.json", report)
            raise ValueError("SUBMISSION_NOT_ACCEPTED")
        job_id = submitted["job_id"]
        if not re.fullmatch(r"[a-f0-9]{32}", job_id):
            raise ValueError("INVALID_JOB_ID")
        row.update(job_id=job_id, state="ACCEPTED")
        save(output / "run.json", report)
        deadline = time.monotonic() + 25
        while True:
            status, job = request(api_port, "/support-resolution-jobs/" + job_id, token)
            if status == 200 and job["status"] in ("DONE", "FAILED"):
                break
            if time.monotonic() >= deadline:
                raise ValueError("JOB_OBSERVATION_TIMEOUT")
            time.sleep(0.2)
        with connect("support") as conn:
            native = conn.execute("SELECT job_id,tenant_id,ticket_id,ticket_revision,status,draft FROM egap_support.support_jobs WHERE job_id=%s", (job_id,)).fetchone()
            events = conn.execute("SELECT event_id,kind,attempt_id FROM egap_support.support_job_events WHERE job_id=%s ORDER BY event_id", (job_id,)).fetchall()
        with connect("gateway") as conn:
            gateway = conn.execute("SELECT job_id,state,execution_mode,draft FROM egap_gateway.calls WHERE job_id=%s", (job_id,)).fetchone()
            tools = conn.execute("SELECT sequence,event FROM egap_gateway.tool_events WHERE job_id=%s ORDER BY sequence", (job_id,)).fetchall()
        if not native or native["tenant_id"] != case["tenant_id"] or native["ticket_id"] != case["ticket_id"] or native["ticket_revision"] != case["ticket_revision"]:
            raise ValueError("NATIVE_OBJECT_BINDING_MISMATCH")
        if job["status"] != "DONE" or not gateway or gateway["state"] != "COMPLETE" or native["draft"] != job["draft"] or gateway["draft"] != job["draft"]:
            raise ValueError("NATIVE_COMPLETION_UNCONFIRMED")
        draft = native["draft"]
        matched = draft["applicable_policy_ref"] == case["expected_policy_ref"] and draft["eligibility"] == case["expected_eligibility"] \
            and draft["proposed_action"] in case["allowed_proposed_actions"]
        cross_status, _ = request(api_port, "/support-resolution-jobs/" + job_id,
            caller["api_token_b" if case["tenant_id"] == "tenant-a" else "api_token_a"])
        observation = {"submission": submitted, "api_job": job, "native_job": native, "events": events,
            "gateway": gateway, "tool_events": tools}
        save(output / (job_id + ".json"), observation)
        row.update(state="OBSERVED", reference_matched=matched, cross_tenant_read_denied=cross_status == 404,
            observation_sha256=digest(observation), tool_event_count=len(tools), event_kinds=[event["kind"] for event in events])
        save(output / "run.json", report)
    if bind_deployment(receipt, product_repo) != binding:
        raise ValueError("DEPLOYMENT_CHANGED_DURING_OBSERVATION")
    load_reference(reference_config)
    report["status"] = "CALIBRATION_MATCHED" if all(c["reference_matched"] and c["cross_tenant_read_denied"] for c in report["cases"]) else "CALIBRATION_MISMATCH"
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    save(output / "run.json", report)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--deployment", type=Path, required=True)
    parser.add_argument("--product-repo", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reference-reader-config", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = run(args.deployment, args.product_repo, args.output, args.reference_reader_config)
        print(json.dumps({"status": report["status"], "cases": len(report["cases"]), "run_id": report["run_id"],
                          "sut_commit": report["binding"]["sut_commit"], "final_qualification": False}))
    except Exception as error:
        print(json.dumps({"status": "INCOMPLETE", "error_type": type(error).__name__}))
        raise SystemExit(2)

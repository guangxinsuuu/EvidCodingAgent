#!/usr/bin/env python3
"""独立产品/平台联调：普通客服提交 → 产品异步任务 → 两侧只读观察。

本入口不导入产品包，不运行产品 checkout 脚本，不持 Chatwoot/Lago 管理员 key。
每个请求先保存操作 key；恢复只查原操作，未查到仍 UNKNOWN，不新建 key 重发。
这是传输和身份验收，商业规则、真实模型和当前候选 Final 均另行验证。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import time
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

from observe_product import bind_deployment, digest, save
import platform_attachment
import product

boundary = platform_attachment.binding


def connect(receipt, observer, database):
    """唯一数据库出口来自已核验部署回执；只用专有观察角色，限制语句和锁等待。"""
    return psycopg.connect(host="127.0.0.1", port=receipt["endpoints"]["database"]["port"],
        dbname=database, user=observer["username"], password=observer["password"], connect_timeout=3,
        row_factory=dict_row, options="-c statement_timeout=3000 -c lock_timeout=1000")


def login(caller, tenant):
    """普通实验客服身份；令牌只在内存中使用，落盘报告不包含登录响应。"""
    kc = caller["keycloak"]
    user = next(row for row in kc["operators"] if row["tenant_id"] == tenant)
    result = boundary.request(kc["base_url"], f"/realms/{kc['realm']}/protocol/openid-connect/token", method="POST", form={
        "grant_type": "password", "client_id": kc["client_id"], "client_secret": kc["client_secret"],
        "username": user["username"], "password": user["password"]})
    return boundary.require_success(result)["access_token"]


def native_messages(access, account, conversation):
    """观察者分页读取实际私有备注；超过上限保留不完整，不拿首屏当全部历史。"""
    messages, before, receipts = [], None, []
    for _ in range(20):
        path = f"/api/v1/accounts/{account}/conversations/{conversation}/messages"
        if before is not None:
            path += "?before=" + str(before)
        result = boundary.request(access["base_url"], path, token=access["token"])
        receipts.append({"path": path, "http_status": result[0], **result[2]})
        rows = boundary.require_success(result).get("payload")
        if not isinstance(rows, list):
            raise boundary.BoundaryError("NATIVE_MESSAGES_INVALID")
        if not rows:
            return messages, receipts
        ids = [m["id"] for m in rows]
        if any(type(i) is not int for i in ids) or (before is not None and min(ids) >= before):
            raise boundary.BoundaryError("NATIVE_MESSAGE_PAGE_INVALID")
        messages.extend(rows); before = min(ids)
    raise boundary.BoundaryError("NATIVE_MESSAGES_INCOMPLETE")


def observation_checks(case, job_id, api_job, native, delivery, snapshot, gateway, messages, cross_status):
    """当前任务与原生私有备注逐项关联；有 marker 仍不足，正文必须承载相同草稿。

    不评价这段草稿是否符合退款规则。本层只检查当前候选确实传递了哪些内容、
    对象是否匹配、其他租户是否被拒绝，语义裁决交给独立业务检查。
    """
    matching = [m for m in messages if m["id"] == delivery["external_message_id"]]
    draft = native.get("draft") or {}
    return {
        "same_job_and_input": native["job_id"] == job_id and native["tenant_id"] == case["tenant_id"]
            and delivery["job_id"] == job_id and delivery["conversation_id"] == case["conversation_id"] and delivery["invoice_id"] == case["invoice_id"],
        "same_completed_draft": native["status"] == "DONE" and gateway is not None and gateway["job_id"] == job_id
            and gateway["state"] == "COMPLETE" and native["draft"] == gateway["draft"] == api_job["job"]["draft"],
        "external_snapshot": snapshot["tenant_id"] == native["tenant_id"] and snapshot["ticket_id"] == native["ticket_id"]
            and snapshot["ticket_revision"] == native["ticket_revision"] and snapshot["context"]["source_kind"] == "CHATWOOT_LAGO" and bool(snapshot["source_receipts"]),
        "private_note_observed": delivery["delivery_state"] == "COMPLETE" and len(matching) == 1
            and matching[0].get("private") is True and matching[0].get("conversation_id") == case["conversation_id"]
            and ("[EGAP draft " + job_id + "]") in matching[0].get("content", "")
            and bool(draft.get("customer_reply_draft")) and matching[0].get("content", "").endswith("\n\n" + draft["customer_reply_draft"]),
        "cross_tenant_denied": cross_status == 403,
    }


def run(args):
    directory = product.registered_directory(args.deployment)
    platform_attachment.validate_attachment(directory)
    receipt = boundary.private(directory / "deployment-receipt.json")
    candidate = bind_deployment(receipt, args.product_repo)
    platform_dir, platform = boundary.binding(args.platform_repo, args.project)
    stable_platform = lambda value: {k: v for k, v in value.items() if k != "egap_commit"}
    if stable_platform(receipt["platform_binding"]) != stable_platform(platform) or receipt["business_source"] != "CHATWOOT_LAGO":
        raise boundary.BoundaryError("DEPLOYED_PLATFORM_BINDING_MISMATCH")
    enrollment = boundary.private(args.enrollment)
    if receipt["platform_enrollment_sha256"] != boundary.digest(args.enrollment.read_bytes()):
        raise boundary.BoundaryError("ENROLLMENT_CHANGED")
    seed = boundary.private(args.seed)
    if enrollment["basis"]["seed_sha256"] != boundary.digest(args.seed.read_bytes()):
        raise boundary.BoundaryError("NATIVE_SEED_CHANGED")
    commercial_reader = None
    if "commercial_source_binding" in receipt:
        from commercial_attachment import validate as validate_commercial
        validate_commercial(receipt["commercial_source_binding"])
        commercial_dir = args.platform_repo / ".local" / receipt["commercial_source_binding"]["project"]
        commercial_reader = boundary.private(commercial_dir / "observer-client.private.json")
        source_endpoint = receipt["commercial_source_binding"]["endpoints"]["observer"]
        if commercial_reader["role"] != "OBSERVER" or commercial_reader["instance_id"] != receipt["commercial_source_binding"]["project"] \
                or commercial_reader["base_url"] != "http://127.0.0.1:" + str(source_endpoint["port"]):
            raise boundary.BoundaryError("COMMERCIAL_OBSERVER_SCOPE_MISMATCH")
    caller = boundary.private(platform_dir / "caller.private.json")
    native_reader = boundary.private(platform_dir / "observer-access.private.json")
    observer = boundary.private(directory / "observer.private.json")
    if caller.get("role") != "BUSINESS_CALLER" or caller["project"] != args.project \
            or caller["keycloak"]["base_url"] != platform["deployment"]["endpoints"]["keycloak"] \
            or native_reader.get("role") != "OBSERVER" or native_reader["instance_id"] != args.project \
            or native_reader["base_url"] != platform["deployment"]["endpoints"]["access"] \
            or observer["deployment_id"] != args.deployment:
        raise boundary.BoundaryError("OBSERVATION_IDENTITY_MISMATCH")
    manifest = boundary.private(directory / "manifest.json")
    cases = [{k: c[k] for k in ("tenant_id", "conversation_id", "invoice_id", "subscription_id")} for c in seed["bindings"]["cases"]]
    basis = {"candidate": candidate, "platform": platform, "enrollment_sha256": receipt["platform_enrollment_sha256"],
        "role_config_sha256": manifest["role_config_sha256"]}
    if commercial_reader:
        basis["commercial_source"] = receipt["commercial_source_binding"]
    path = args.output / "run.json"
    report = boundary.private(path) if path.exists() else {"run_id": uuid4().hex, "basis": basis, "state": "STARTED",
        "scope": "NATIVE_COMMERCIAL_FACTS_SCRIPTED_CALIBRATION" if commercial_reader else "NATIVE_TRANSPORT_SCRIPTED_CALIBRATION", "business_final_qualification": False,
        "cases": [], "mode": receipt["mode"]}
    if report["basis"] != basis:
        raise boundary.BoundaryError("RUN_IDENTITY_CHANGED")
    save(path, report)
    base = "http://127.0.0.1:" + str(receipt["endpoints"]["api"]["port"])
    ready = boundary.request(base, "/health/ready")
    if ready[0] != 200 or ready[1].get("checks", {}).get("execution_mode") != "SCRIPTED_CALIBRATION":
        raise boundary.BoundaryError("NATIVE_CALIBRATION_NOT_READY")
    with connect(receipt, observer, "support") as conn:
        conn.execute("SET default_transaction_read_only=off"); conn.commit()
        try:
            conn.execute("UPDATE egap_support.external_request_keys SET job_id=job_id WHERE false")
        except psycopg.errors.InsufficientPrivilege:
            conn.rollback(); report["database_observer_write_denied"] = True
        else:
            conn.rollback(); raise boundary.BoundaryError("OBSERVER_DATABASE_WRITE_PRIVILEGE")
    save(path, report)
    for case in cases:
        row = next((r for r in report["cases"] if r["case"] == case), None)
        if row is None:
            row = {"case": case, "operation_key": "native-" + report["run_id"] + "-" + str(case["conversation_id"]), "state": "PLANNED"}
            report["cases"].append(row); save(path, report)
        if row["state"] in {"OBSERVED", "OBSERVATION_MISMATCH", "FAILED", "UNCONFIRMED", "REJECTED"}:
            continue
        token = login(caller, case["tenant_id"])
        if row["state"] == "PLANNED":
            with connect(receipt, observer, "support") as conn:
                row["observations_after_id"] = conn.execute("SELECT COALESCE(MAX(observation_id),0) AS id FROM egap_support.external_api_observations").fetchone()["id"]
            command = {k: case[k] for k in ("tenant_id", "conversation_id", "invoice_id")}
            command["operation_key"] = row["operation_key"]
            row.update(state="DISPATCHING", command_sha256=digest(command)); save(path, report)
            try:
                submitted = boundary.request(base, "/integrations/disputes", token=token, method="POST", body=command)
                row["submission"] = {"http_status": submitted[0], "body": submitted[1], **submitted[2]}
                if submitted[0] == 202:
                    row.update(state="ACCEPTED", job_id=submitted[1]["job_id"])
                else:
                    row["state"] = "REJECTED" if 400 <= submitted[0] < 500 else "UNCONFIRMED"
            except Exception:
                row["state"] = "UNKNOWN"
            save(path, report)
        # 即使 HTTP 未返回，也只查已保存的 key。未找到不代表请求没送达。
        with connect(receipt, observer, "support") as conn:
            admitted = conn.execute("SELECT job_id FROM egap_support.external_request_keys WHERE tenant_id=%s AND operation_key=%s",
                (case["tenant_id"], row["operation_key"])).fetchone()
            observations = conn.execute("SELECT * FROM egap_support.external_api_observations WHERE observation_id>%s ORDER BY observation_id",
                (row["observations_after_id"],)).fetchall()
        save(args.output / (row["operation_key"] + ".ingress.json"), {"request": row, "product_observations": observations})
        if not admitted:
            row["admission_observed"] = False; save(path, report); continue
        if row.get("job_id") and row["job_id"] != admitted["job_id"]:
            raise boundary.BoundaryError("NATIVE_OPERATION_JOB_MISMATCH")
        row.update(job_id=admitted["job_id"], admission_observed=True, state="ACCEPTED"); save(path, report)
        deadline = time.monotonic() + 30
        job_path = f"/integrations/disputes/{case['tenant_id']}/{row['job_id']}"
        while time.monotonic() < deadline:
            result = boundary.request(base, job_path, token=token)
            if result[0] != 200:
                raise boundary.BoundaryError("JOB_READ_UNAVAILABLE")
            job = result[1]
            if job["job"]["status"] in {"FAILED", "CANCELLED", "UNKNOWN"} or (job["delivery"] and job["delivery"]["delivery_state"] in {"COMPLETE", "FAILED", "UNKNOWN", "STALE"}):
                break
            time.sleep(0.25)
        else:
            row["state"] = "WAITING"; save(path, report); continue
        with connect(receipt, observer, "support") as conn:
            native = conn.execute("SELECT * FROM egap_support.support_jobs WHERE job_id=%s", (row["job_id"],)).fetchone()
            delivery = conn.execute("SELECT * FROM egap_support.external_operations WHERE job_id=%s", (row["job_id"],)).fetchone()
            snapshot = conn.execute("SELECT * FROM egap_support.external_inputs WHERE tenant_id=%s AND ticket_id=%s AND ticket_revision=%s",
                (native["tenant_id"], native["ticket_id"], native["ticket_revision"])).fetchone()
        with connect(receipt, observer, "gateway") as conn:
            gateway = conn.execute("SELECT * FROM egap_gateway.calls WHERE job_id=%s", (row["job_id"],)).fetchone()
            tools = conn.execute("SELECT * FROM egap_gateway.tool_events WHERE job_id=%s ORDER BY sequence", (row["job_id"],)).fetchall()
        messages, native_receipts = native_messages(native_reader, delivery["account_id"], case["conversation_id"])
        matching = [m for m in messages if m["id"] == delivery["external_message_id"]]
        other_tenant = "tenant-b" if case["tenant_id"] == "tenant-a" else "tenant-a"
        cross = boundary.request(base, job_path, token=login(caller, other_tenant))
        checks = observation_checks(case, row["job_id"], job, native, delivery, snapshot, gateway, messages, cross[0])
        evidence = {"api": job, "native_job": native, "delivery": delivery, "snapshot": snapshot, "gateway": gateway,
            "tool_events": tools, "native_messages": matching, "native_observation_receipts": native_receipts,
            "cross_tenant_read": {"http_status": cross[0], **cross[2]}, "checks": checks}
        if commercial_reader:
            from urllib.parse import urlencode
            customer = next(c for c in seed["bindings"]["customers"] if c["tenant_id"] == case["tenant_id"])
            scope = {**{k: case[k] for k in ("tenant_id", "subscription_id", "invoice_id", "conversation_id")},
                "customer_id": customer["lago_customer_id"], "account_id": customer["chatwoot_account_id"]}
            observed = boundary.request(commercial_reader["base_url"], "/v1/context?" + urlencode(scope), token=commercial_reader["token"])
            checks["commercial_source_and_facts"] = observed[0] == 200 and observed[1].get("instance_id") == commercial_reader["instance_id"] \
                and observed[1] == snapshot["context"].get("commercial_source")
            evidence["commercial_observation"] = {"http_status": observed[0], "response": observed[1], **observed[2]}
        save(args.output / (row["job_id"] + ".json"), evidence)
        row.update(state="OBSERVED" if all(checks.values()) else "OBSERVATION_MISMATCH", checks=checks,
            evidence_sha256=digest(evidence), job_status=native["status"], delivery_state=delivery["delivery_state"])
        save(path, report)
    platform_attachment.validate_attachment(directory)
    if commercial_reader:
        validate_commercial(receipt["commercial_source_binding"])
    if bind_deployment(receipt, args.product_repo) != candidate:
        raise boundary.BoundaryError("CANDIDATE_CHANGED_DURING_OBSERVATION")
    report["state"] = "NATIVE_TRANSPORT_CONFIRMED" if all(r["state"] == "OBSERVED" for r in report["cases"]) else "NATIVE_TRANSPORT_PARTIAL"
    save(path, report)
    return {"status": report["state"], "run_id": report["run_id"], "deployment_id": args.deployment,
        "cases": [{"conversation_id": r["case"]["conversation_id"], "state": r["state"], "error_code": r.get("submission", {}).get("body", {}).get("error_code")} for r in report["cases"]],
        "business_final_qualification": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--deployment", required=True)
    parser.add_argument("--product-repo", type=Path, required=True)
    parser.add_argument("--platform-repo", type=Path, required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--enrollment", type=Path, required=True)
    parser.add_argument("--seed", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    try:
        args = parser.parse_args()
        if "docs" in args.output.resolve().parts or args.output.name.lower() == "readme.md":
            raise boundary.BoundaryError("OUTPUT_FORBIDDEN")
        print(json.dumps(run(args)))
    except Exception as error:
        print(json.dumps({"status": "INCOMPLETE", "error_code": str(error) if isinstance(error, boundary.BoundaryError) else type(error).__name__}))
        raise SystemExit(2)

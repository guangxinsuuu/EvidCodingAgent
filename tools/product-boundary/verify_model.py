#!/usr/bin/env python3
"""真实模型出口验收：读取原调用 → 原键回放/越权反例 → 核对预算与原件未变化。

只接受已完成的本部署校准 Run；不会为成功率再建业务 Job。有效 POST 使用原操作
和原字节，其他 POST 是应被准入拒绝的反例。输出保存于 ignored artifacts，终端
仅显示检查和费用计数，不显示模型原文或口令。容器重启另由受信宿主精确执行。
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
from pathlib import Path
import re

import model_attachment
from observe_native import boundary
from observe_product import save
import product


def verify(identity: str, run_path: Path):
    if not run_path.resolve().is_relative_to(product.ROOT / "artifacts"):
        raise ValueError("PRIVATE_ARTIFACT_RUN_REQUIRED")
    directory = product.registered_directory(identity)
    model_attachment.validate(directory)
    # Docker Desktop 重启随机发布端口的容器时可能换端口。用 Docker 实际回读更新
    # 观察回执，候选镜像/配置仍由 registered_directory + validate 固定，不猜旧地址。
    receipt = product.deployment.inspect(directory)
    run = boundary.private(run_path)
    if run["basis"]["candidate"]["deployment_id"] != identity or run["state"] != "NATIVE_TRANSPORT_CONFIRMED" \
            or run["basis"].get("model") != receipt["model_binding"]:
        raise ValueError("COMPLETED_BOUND_RUN_REQUIRED")
    reader = boundary.private(directory / "model-observer.private.json")
    # 出口 config 是不可遍历目录里的只读挂载文件；操作员用于负例，绝不发给模型。
    config = json.loads((directory / "model-access.private.json").read_text())
    if reader["deployment_id"] != identity or reader["role"] != "OBSERVER" or reader["token"] != config["observer_token"]:
        raise ValueError("MODEL_OBSERVER_BINDING_REQUIRED")
    port = receipt["endpoints"]["model-access"]["port"]

    def request(method, path, token, *, raw=None, operation=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        headers = {"Authorization":"Bearer " + token, "Content-Type":"application/json"}
        if operation:
            headers["X-EGAP-Model-Operation"] = operation
        try:
            conn.request(method, path, raw, headers)
            response = conn.getresponse()
            data = response.read(524289)
            if len(data) > 524288:
                raise ValueError("MODEL_OBSERVATION_TOO_LARGE")
            return response.status, data
        finally:
            conn.close()

    originals, checks = {}, {}
    for case in run["cases"]:
        job = case["job_id"]
        if not re.fullmatch(r"[a-f0-9]{32}", job) or case["state"] != "OBSERVED":
            raise ValueError("COMPLETE_MODEL_OPERATION_REQUIRED")
        status, raw = request("GET", "/v1/operations/" + job, reader["token"])
        if status != 200:
            raise ValueError("MODEL_OPERATION_UNAVAILABLE")
        value = json.loads(raw)
        archived = boundary.private(run_path.parent / (job + ".json"))["model_observation"]["response"]
        checks[job + ":original_preserved"] = value["operation"] == archived["operation"]
        originals[job] = value
    if not originals or not all(checks.values()):
        raise ValueError("MODEL_ORIGINAL_CHANGED")
    job, original = next(iter(originals.items()))
    raw = original["operation"]["request_text"].encode()
    if hashlib.sha256(raw).hexdigest() != original["operation"]["request_sha256"]:
        raise ValueError("MODEL_REQUEST_CHANGED")
    status, repeated = request("POST", "/v1/chat/completions", config["product_token"], raw=raw, operation=job)
    checks["same_operation_same_original_response"] = status == 200 and hashlib.sha256(repeated).hexdigest() == original["operation"]["response_sha256"]
    checks["product_cannot_read_raw_archive"] = request("GET", "/v1/operations/" + job, config["product_token"])[0] == 403
    checks["observer_cannot_infer"] = request("POST", "/v1/chat/completions", reader["token"], raw=raw, operation=job)[0] == 403
    checks["invalid_identity_denied"] = request("GET", "/v1/models", "invalid-local-test-token")[0] == 401
    checks["model_download_route_denied"] = request("POST", "/api/pull", config["product_token"], raw=raw, operation=job)[0] == 403
    value = json.loads(raw)
    excessive = json.dumps({**value,"max_tokens":config["max_output_tokens"]+1}).encode()
    checks["output_budget_cannot_expand"] = request("POST", "/v1/chat/completions", config["product_token"], raw=excessive, operation=job)[0] == 422
    changed = json.dumps({**value,"max_tokens":config["max_output_tokens"]-1}).encode()
    checks["same_operation_changed_payload_denied"] = request("POST", "/v1/chat/completions", config["product_token"], raw=changed, operation=job)[0] == 409
    final = json.loads(request("GET", "/v1/operations/" + job, reader["token"])[1])
    checks["no_additional_model_reservations"] = final["budget"] == original["budget"]
    checks["replay_did_not_change_original_operation"] = final["operation"] == original["operation"]
    output = {"deployment_id":identity, "run_id":run["run_id"], "checks":checks,
              "observer_sha256":hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "budget_before":original["budget"], "budget_after":final["budget"],
              "scope":"LOCAL_MODEL_OUTLET_IDENTITY_AND_REPLAY_ONLY", "business_final_qualification":False}
    save(run_path.parent / "model-boundary-verification.json",output)
    if not all(checks.values()):
        raise ValueError("MODEL_BOUNDARY_CHECK_FAILED")
    return {"status":"MODEL_BOUNDARY_CONFIRMED", "checks":len(checks), "reserved_calls":final["budget"]["reserved_calls"], "model_calls_added":0}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployment", required=True)
    parser.add_argument("--run", type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(verify(args.deployment,args.run)))
    except Exception as exc:
        print(json.dumps({"status":"INCOMPLETE", "error_type":type(exc).__name__}))
        raise SystemExit(2)

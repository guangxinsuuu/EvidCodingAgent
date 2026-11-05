#!/usr/bin/env python3
"""公开合成样例验收：只由宿主读取参考期望；SUT模型输入不包含期望答案。

每项只提交一次，原Job轮询有界；明确失败/UNKNOWN停止后续效果。结构化结果与
公开DRAFT期望比较属于本地校准，并非独立SUP Oracle或语义质量资格。
"""

import argparse
import importlib.util
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("support_lab", ROOT / "tools/support-lab.py")
lab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lab)
from genai_support.gateway_store import GatewayStore
from genai_support.store import Store


def run(directory: Path, evidence: Path) -> dict:
    runtime = json.loads((directory / "runtime.json").read_text())
    secrets = json.loads((directory / "secrets.json").read_text())
    connections = json.loads((directory / "connections.private.json").read_text())
    references = json.loads((ROOT / "datasets/genai-support/verification/synthetic-v1.json").read_text())
    url = runtime["endpoints"]["support-gateway"]
    gateway = GatewayStore(connections["gateway_database_url"])
    store = Store(connections["database_url"])
    records = []
    for case in references["cases"]:
        token = secrets["api_token_a" if case["tenant_id"] == "tenant-a" else "api_token_b"]
        status, accepted = lab.request(url + "/support-resolution-jobs", token=token,
            body={"schema_version": 1, "ticket_id": case["ticket_id"], "ticket_revision": case["ticket_revision"]})
        if status != 202:
            raise RuntimeError("SUBMISSION_NOT_ACCEPTED_NO_AUTOMATIC_RETRY")
        job_id = accepted["job_id"]
        record = {"case_id": case["case_id"], "job_id": job_id, "tenant_id": case["tenant_id"], "ticket_id": case["ticket_id"], "observed_states": ["QUEUED"]}
        # 先落受理回执；宿主中断后可以用原job_id继续查询，不能重跑脚本隐去未知。
        records.append(record)
        lab.save(evidence / "business-cases.json", records)
        deadline = time.monotonic() + 180
        while True:
            status, job = lab.request(url + "/support-resolution-jobs/" + job_id, token=token)
            if status != 200:
                raise RuntimeError("JOB_QUERY_UNKNOWN")
            if job["status"] != record["observed_states"][-1]:
                record["observed_states"].append(job["status"])
            if job["status"] in {"DONE", "FAILED"}:
                break
            if time.monotonic() >= deadline:
                record["verdict"] = "INCONCLUSIVE"
                lab.save(evidence / "business-cases.json", records)
                raise RuntimeError("JOB_OUTCOME_UNKNOWN_NO_AUTOMATIC_RETRY")
            time.sleep(0.2)
        trace = gateway.get(job_id)
        tool_events = gateway.trace(job_id)
        record.update(status=job["status"], draft=job["draft"], tool_event_count=len(tool_events),
                      gateway_state=trace["state"] if trace else None, receipt=trace["receipt"] if trace else None)
        if job["status"] != "DONE":
            record["verdict"] = "FAIL"
            lab.save(evidence / "business-cases.json", records)
            raise RuntimeError("JOB_FAILED")
        draft = job["draft"]
        checks = {"policy": draft["applicable_policy_ref"] == case["expected_policy_ref"],
                  "eligibility": draft["eligibility"] == case["expected_eligibility"],
                  "proposed_action": draft["proposed_action"] in case["allowed_proposed_actions"],
                  "durable_result": store.get_job(case["tenant_id"], job_id) == job,
                  "tool_trace": len(tool_events) >= 6,
                  "mode": trace is not None and trace["execution_mode"] == runtime["execution_mode"]}
        # 另一个租户即使知道job_id也必须404；此只验证本地会话至Job查询链。
        other = secrets["api_token_b" if case["tenant_id"] == "tenant-a" else "api_token_a"]
        other_status, _ = lab.request(url + "/support-resolution-jobs/" + job_id, token=other)
        checks["cross_tenant_job_denied"] = other_status == 404
        record.update(checks=checks, verdict="PASS" if all(checks.values()) else "FAIL")
        lab.save(evidence / "business-cases.json", records)
        print(json.dumps({"case_id": case["case_id"], "status": job["status"], "eligibility": draft["eligibility"], "verdict": record["verdict"]}), flush=True)
    result = {"kind": "public-business-calibration", "execution_mode": runtime["execution_mode"], "cases": len(records),
              "passed": sum(r["verdict"] == "PASS" for r in records), "business_rule_approval": "DRAFT", "independent_sup_oracle": "NOT_IMPLEMENTED",
              "semantic_quality": "NOT_EVALUATED", "environment_instance_id": runtime["instance_id"]}
    lab.save(evidence / "business-summary.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("evidence", type=Path)
    args = parser.parse_args()
    try:
        result = run(args.directory, args.evidence)
        print(json.dumps(result))
        raise SystemExit(0 if result["passed"] == result["cases"] else 1)
    except Exception as error:
        print(json.dumps({"status": "FAILED", "reason": str(error) if str(error).isupper() else "CALIBRATION_FAILED"}), file=sys.stderr)
        raise SystemExit(1)

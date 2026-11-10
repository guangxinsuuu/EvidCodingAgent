#!/usr/bin/env python3
"""固定候选 → 复杂来源对象 → 真实SUT执行 → 独立规则核对及反例报告。

本驱动不导入产品资格函数。候选失败也保留其输出并继续独立案例；基础设施/准备
失败单列，不能修改预期、补写产品快照或以重新运行覆盖失败证据。
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import urlencode

from seed import ROOT, Api, SeedError, load_runtime, save_private, sha
from check import Check
from release_seed import SourceObjects
from release_oracle import assess_case, expectation_manifest, OracleInputError
from release_boundaries import run_boundaries


TARGET_COMMIT = "bdccf89ea431ab6ff678e754a97113b54547e5d4"
TARGET_PATHS = ("services/genai-support-sut/src", "services/genai-support-sut/migrations",
                "services/genai-support-sut/pyproject.toml", "services/genai-support-sut/uv.lock",
                "services/commercial-source/src", "business-source-pack/v1")


class CandidateProtocolError(SeedError):
    """已获得的候选输出不满足协议；归为候选FAIL，不冒称环境没有观察到。"""


def candidate_unchanged() -> bool:
    """固定的是产品/来源源码，验证工具可以独立扩展；Git对象存在且差异为空才执行。"""
    result = subprocess.run(["git", "diff", "--exit-code", TARGET_COMMIT, "--", *TARGET_PATHS], cwd=ROOT, capture_output=True)
    if result.returncode != 0:
        return False
    added = subprocess.run(["git", "ls-files", "--others", "--exclude-standard", "--", *TARGET_PATHS],
                           cwd=ROOT, capture_output=True)
    return added.returncode == 0 and not added.stdout.strip()


def verification_manifest() -> dict:
    """记录本轮实际使用的准备/观察/断言代码，防运行中改变规则却沿用原结论。"""
    names = ("release_check.py", "release_seed.py", "release_oracle.py", "release_boundaries.py",
             "seed.py", "check.py", "business_source.py")
    return {name: hashlib.sha256((ROOT / "tools/multi-system" / name).read_bytes()).hexdigest() for name in names}


class ReleaseCheck(Check):
    def __init__(self, directory: Path):
        self.directory = directory.resolve()
        self.pack = SourceObjects(self.directory)
        if not candidate_unchanged():
            raise SeedError("TARGET_CANDIDATE_CHANGED")
        sut = load_runtime(self.directory / "sut/sut.json")
        if any((self.directory / name).exists() for name in (
                "release-check.private.json", "verification-expectations.private.json")):
            raise SeedError("RELEASE_CHECK_ALREADY_STARTED_INSPECT_EVIDENCE")
        marker = self.directory / "release-check-started.private.json"
        try:
            fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise SeedError("RELEASE_CHECK_ALREADY_STARTED_INSPECT_EVIDENCE") from None
        with os.fdopen(fd, "w") as stream:
            json.dump({"target_commit": TARGET_COMMIT, "started_at": datetime.now(timezone.utc).isoformat()}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        super().__init__(self.pack.runtime, self.pack.native, self.directory / "release-check.private.json",
                         sut)
        self.result.update(target_commit=TARGET_COMMIT, qualification="LOCAL_RELEASE_CANDIDATE_VERIFICATION",
            verification_code=verification_manifest(),
            independent_verifier_agent=False, protected_oracle=False, cases=[], findings=[], case_errors=[],
            limitations=["LOCAL_SYNTHETIC_COMMERCIAL_RULES", "SIMULATED_RENEWAL_AND_PAYMENT",
                         "CONTROLLED_GENERATOR_COMPLETENESS_ONLY", "NO_PROTECTED_EXECUTION_BOUNDARY",
                         "WORDING_PROBES_ARE_NOT_COMPLETE_SEMANTIC_VERIFICATION"])
        self.completed = {}
        save_private(self.directory / "verification-expectations.private.json", expectation_manifest())
        self.checkpoint()

    def assertion(self, name: str, condition: bool, **evidence):
        """收齐独立反例；某个断言FAIL不改变其他业务对象和它们的预期。"""
        self.result["checks"].append({"name": name, "status": "PASS" if condition else "FAIL", "evidence": evidence})
        self.checkpoint()

    def snapshot(self, job: str) -> dict:
        return self.audit_scalar("SELECT i.context FROM egap_support.external_inputs i JOIN egap_support.support_jobs j ON i.tenant_id=j.tenant_id AND i.ticket_id=j.ticket_id AND i.ticket_revision=j.ticket_revision WHERE j.job_id=%s", (job,))

    def terminal(self, client, case: dict, job: str, timeout=180) -> dict:
        """只轮询原Job；终态失败保留原回执，不把再次模型调用当重试。"""
        deadline = time.monotonic() + timeout
        while True:
            result = client.request("GET", f"/integrations/disputes/{case['tenant_id']}/{job}")
            if not isinstance(result, dict) or not isinstance(result.get("job"), dict) \
                    or result["job"].get("status") not in {"QUEUED", "PROCESSING", "DONE", "FAILED"} \
                    or result.get("delivery") is not None and (not isinstance(result["delivery"], dict)
                        or result["delivery"].get("delivery_state") not in {"PENDING", "DISPATCHING", "COMPLETE", "UNKNOWN", "STALE", "FAILED"}):
                self.result["jobs"].append(result)
                self.checkpoint()
                raise CandidateProtocolError("CANDIDATE_JOB_PROTOCOL_INVALID")
            status = result["job"]["status"]
            delivery = (result.get("delivery") or {}).get("delivery_state")
            if status in {"FAILED", "CANCELLED", "UNKNOWN"} or delivery in {"COMPLETE", "FAILED", "UNKNOWN", "STALE"}:
                self.result["jobs"].append(result)
                self.checkpoint()
                return result
            if time.monotonic() >= deadline:
                self.result["jobs"].append(result)
                self.checkpoint()
                raise SeedError("BOUNDED_JOB_OBSERVATION_TIMEOUT")
            time.sleep(1)

    def inspect(self, case: dict, result: dict, stage: str) -> dict | None:
        if not isinstance(result, dict) or not isinstance(result.get("job"), dict) \
                or not all(key in result["job"] for key in ("job_id", "status")) \
                or result.get("delivery") is not None and not isinstance(result["delivery"], dict):
            raise CandidateProtocolError("CANDIDATE_JOB_PROTOCOL_INVALID")
        job = result["job"]
        if job["status"] != "DONE" or (result.get("delivery") or {}).get("delivery_state") != "COMPLETE":
            self.assertion(case["label"] + ":" + stage + ":execution_completed", False, job_id=job["job_id"],
                           status=job["status"], error_code=job.get("error_code"), delivery=result.get("delivery"))
            return None
        if not isinstance(job.get("draft"), dict) or not all(key in job["draft"] for key in (
                "eligibility", "proposed_action", "applicable_policy_ref", "customer_reply_draft")):
            raise CandidateProtocolError("CANDIDATE_DRAFT_PROTOCOL_INVALID")
        context = self.snapshot(job["job_id"])
        if not isinstance(context, dict) or not all(isinstance(context.get(key), dict) for key in (
                "billing", "subscription", "commercial_source")) or not isinstance(context.get("missing_evidence"), list):
            raise CandidateProtocolError("CANDIDATE_SNAPSHOT_PROTOCOL_INVALID")
        messages = self.cw.request("GET", self.cw_prefix + f"/conversations/{case['conversation_id']}/messages")["payload"]
        conversation = self.cw.request("GET", self.cw_prefix + f"/conversations/{case['conversation_id']}")
        original_id = case.get("customer_message_id") or (context.get("commercial_source", {}).get("claim") or {}).get("original_message", {}).get("message_id")
        original = next((row for row in messages if row["id"] == original_id), None)
        if original is None:
            raise SeedError("ORIGINAL_MESSAGE_OBSERVATION_MISSING")
        # 原生读回独立于产品快照；快照声称自己选对对象不能充当自身的证明。
        invoice = self.lago.request("GET", "/api/v1/invoices/" + case["invoice_id"])["invoice"]
        usage_path = "/api/v1/customers/" + case["customer_id"]
        query = urlencode({"external_subscription_id": case["subscription_id"]})
        if case["label"] == "terminated_subscription":
            periods = self.lago.request("GET", usage_path + "/past_usage?" + query)["usage_periods"]
            usage = next((row for row in periods if row.get("lago_invoice_id") == case["invoice_id"]), None)
        else:
            usage = self.lago.request("GET", usage_path + "/current_usage?" + query)["customer_usage"]
        reader = Api(self.pack.ready["base_url"], {"Authorization": "Bearer " + self.pack.private["read_token"]},
                     service="commercial-observer", receipts=self.result["receipts"])
        source = reader.request("GET", "/v1/context?" + urlencode({key: case[key] for key in (
            "tenant_id", "customer_id", "subscription_id", "invoice_id", "account_id", "conversation_id")}))
        verdict = assess_case(case, result, context, {"messages": messages, "conversation": conversation,
            "original_message": original, "invoice": invoice, "usage": usage, "source_context": source}, stage=stage)
        for entry in verdict["checks"]:
            entry.update(label=case["label"], stage=stage, job_id=job["job_id"])
        self.result["checks"].extend(verdict["checks"])
        self.result["findings"].extend(verdict.get("findings", []))
        self.result["cases"].append({"label": case["label"], "stage": stage, "job_id": job["job_id"],
            "eligibility": job["draft"]["eligibility"], "proposed_action": job["draft"]["proposed_action"],
            "policy_ref": job["draft"]["applicable_policy_ref"], "missing_evidence": context["missing_evidence"],
            "snapshot_sha256": sha(context), "private_message_id": result["delivery"]["external_message_id"]})
        self.checkpoint()
        if any(entry["status"] == "FAIL" and entry["name"] == "candidate_protocol_and_business_value_shape"
               for entry in verdict["checks"]):
            # 原始坏输出已经作为候选FAIL保留；不能据此继续派发后续业务变更。
            return None
        return context

    def refresh(self, case: dict, parent: str, suffix: str, reason: str):
        client = self.client(case["tenant_id"])
        accepted = client.request("POST", f"/integrations/disputes/{case['tenant_id']}/{parent}/reevaluate",
            {"operation_key": self.dataset["run_id"] + ":release:" + suffix, "reason": reason}, expected=(202,))
        self.assertion(suffix + ":new_assessment_identity", accepted["job_id"] != parent,
                       parent_job_id=parent, job_id=accepted["job_id"])
        return self.terminal(client, case, accepted["job_id"])

    def one(self, case: dict):
        client = self.client(case["tenant_id"])
        accepted, body = self.submit(client, case, "release-" + case["label"])
        result = self.terminal(client, case, accepted["job_id"])
        context = self.inspect(case, result, "initial")
        if context is None:
            return
        self.completed[case["label"]] = result
        if case["label"] == "late_usage":
            old_hash = sha(context)
            self.pack.send_usage(case["label"])
            # 改事实并不授权普通提交刷新；验证原操作和新普通key都保留原Job。
            for suffix in ("", ":new-key"):
                repeated = client.request("POST", "/integrations/disputes", {**body, "operation_key": body["operation_key"] + suffix}, expected=(202,))
                self.assertion("late_usage:ordinary_retry_" + (suffix or "same"), repeated["job_id"] == accepted["job_id"])
            fresh = self.refresh(case, accepted["job_id"], "late-usage-sealed", "预登记事件已真实进入Lago并核对封存")
            self.inspect(case, fresh, "after_seal")
            self.assertion("late_usage:old_snapshot_immutable", sha(self.snapshot(accepted["job_id"])) == old_hash)
            self.completed[case["label"]] = fresh
        if case["label"] == "v1_history":
            old_claim = context["commercial_source"]["claim"]
            old_hash = sha(context)
            self.pack.supplement(case["label"])
            fresh = self.refresh(case, accepted["job_id"], "supplement-original-claim", "客户补充消息与claim修订，原申请时间不变")
            new = self.inspect(case, fresh, "supplement")
            if new:
                self.assertion("supplement:original_claim_identity_and_time", all(new["commercial_source"]["claim"][k] == old_claim[k]
                    for k in ("claim_id", "claimed_at", "original_message")))
            self.assertion("supplement:old_snapshot_immutable", sha(self.snapshot(accepted["job_id"])) == old_hash)
            self.completed[case["label"]] = fresh

    def run(self):
        cases = {case["label"]: case for case in self.dataset["bindings"]["cases"]}
        for case in cases.values():
            try:
                self.one(case)
            except CandidateProtocolError as exc:
                self.assertion(case["label"] + ":candidate_protocol", False, error_code=str(exc))
            except (SeedError, OracleInputError, ValueError, KeyError, OSError) as exc:
                code = str(exc) if isinstance(exc, (SeedError, OracleInputError)) else "CASE_PROTOCOL_OR_PREPARATION_INVALID"
                self.result["case_errors"].append({"label": case["label"], "error_code": code})
                self.checkpoint()
        if all(label in self.completed for label in ("v1_history", "v2_boundary", "late_usage")):
            run_boundaries(self, self.directory, cases, self.completed)
        else:
            self.result["case_errors"].append({"label": "boundary_checks", "error_code": "PREREQUISITE_JOBS_UNAVAILABLE"})
        self.assertion("target_candidate_unchanged", candidate_unchanged())
        self.assertion("verification_code_unchanged", self.result["verification_code"] == verification_manifest())
        self.assertion("all_planned_business_stages_observed", len(self.result["cases"]) == 14,
                       planned=14, observed=len(self.result["cases"]))
        failed = [c for c in self.result["checks"] if c["status"] == "FAIL"]
        self.result.update(status="INCOMPLETE" if self.result["case_errors"] else "FAIL" if failed else "PASS_SCOPED_CHECKS",
            completed_at=datetime.now(timezone.utc).isoformat(), failed_checks=len(failed),
            open_wording_findings=len(self.result["findings"]), release_authorized=False)
        self.checkpoint()
        return self.result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    checker = None
    try:
        checker = ReleaseCheck(args.directory)
        result = checker.run()
        print(json.dumps({key: result[key] for key in ("status", "failed_checks", "open_wording_findings", "release_authorized")}
            | {"checks": len(result["checks"]), "jobs": len(result["jobs"]), "case_errors": result["case_errors"]}))
        return 0 if result["status"] == "PASS_SCOPED_CHECKS" else 1
    except (SeedError, OracleInputError, ValueError, KeyError, OSError, RuntimeError) as exc:
        code = str(exc) if isinstance(exc, (SeedError, OracleInputError)) else "RELEASE_EXECUTION_UNCONFIRMED"
        if checker:
            checker.result.update(status="INCOMPLETE", error_code=code)
            checker.checkpoint()
        print(json.dumps({"status": "INCOMPLETE", "error_code": code}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

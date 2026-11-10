#!/usr/bin/env python3
"""原生 seed → 商业来源命令 → 真实 SUT 联调校准。

prepare 只向独立商业来源写合成资料；check 经 OIDC 提交真实 Job 并读回私有备注。
校准期望只存在此驱动，绝不传给商业来源或 SUT。每次写先保存命令，重试使用同一
operation_key，不因超时换 key。所有原始响应只落 ignored 的 0600 实验文件。
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
from string import Template
import sys
from urllib.parse import urlencode

from seed import Api, ROOT, SeedError, load_runtime, save_private, sha
from check import Check


def dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def message_ref(value: dict) -> dict:
    """保留 Chatwoot 原生时刻格式；商业业务时钟不覆盖客户消息时间。"""
    created = value["created_at"]
    if isinstance(created, (int, float)):
        created = datetime.fromtimestamp(created, timezone.utc).isoformat()
    return {"message_id": value["id"], "created_at": created,
            "content_sha256": hashlib.sha256((value.get("content") or "").encode()).hexdigest()}


class BusinessPack:
    def __init__(self, directory: Path):
        self.directory = directory.resolve()
        self.native = load_runtime(self.directory / "native/seed.json")
        self.runtime = load_runtime(ROOT / "artifacts/multi-system/runtime.json")
        self.ready = load_runtime(self.directory / "ready.json")
        self.private = load_runtime(self.directory / "runtime.private.json")
        self.state_path = self.directory / "source-preparation.private.json"
        self.state = load_runtime(self.state_path) if self.state_path.exists() else {
            "run_id": self.native["run_id"], "commands": {}, "receipts": [], "plans": {}, "checks": []}
        identity = {"native_run_id": self.native["run_id"], "source_instance_id": self.ready["instance_id"],
                    "source_digest": self.ready["source_digest"], "native_seed_sha256": sha(self.native)}
        if self.state.get("identity", identity) != identity or self.state["run_id"] != self.native["run_id"]:
            raise SeedError("PREPARATION_IDENTITY_MISMATCH")
        if self.state.get("commands") and "identity" not in self.state:
            raise SeedError("PREPARATION_IDENTITY_MISMATCH")
        self.state["identity"] = identity
        self.api = Api(self.ready["base_url"], {"Authorization": "Bearer " + self.private["admin_token"]},
                       service="commercial", receipts=self.state["receipts"])
        cw, lago = self.runtime["chatwoot"], self.runtime["lago"]
        self.cw = Api(cw["base_url"], {"api_access_token": cw["api_token"]}, service="chatwoot", receipts=self.state["receipts"])
        self.lago = Api(lago["base_url"], {"Authorization": "Bearer " + lago["api_key"]}, service="lago", receipts=self.state["receipts"])
        self.prefix = f"/api/v1/accounts/{cw['account_id']}"

    def save(self):
        save_private(self.state_path, self.state)

    def command(self, key: str, kind: str, payload: dict) -> dict:
        """保存完整首次命令；未知 HTTP 回执可用相同 key 向事务账本安全重放。"""
        existing = self.state["commands"].get(key)
        if existing is None:
            existing = {"request": {"operation_key": self.native["run_id"] + ":" + key,
                "type": kind, "payload": payload}, "state": "REQUESTED"}
            self.state["commands"][key] = existing
            self.save()
        if existing["request"]["type"] != kind:
            raise SeedError("COMMERCIAL_COMMAND_KIND_CHANGED")
        if sha(existing["request"]["payload"]) != sha(payload):
            raise SeedError("COMMERCIAL_COMMAND_PAYLOAD_CHANGED")
        if existing["state"] == "COMPLETE":
            return existing["response"]["result"]
        try:
            response = self.api.request("POST", "/v1/commands", existing["request"])
            existing.update(state="COMPLETE", response=response)
            return response["result"]
        finally:
            self.save()

    def policy(self, tenant: str, version: str):
        asset = json.loads((ROOT / f"business-source-pack/v1/policies/{version}.json").read_text())
        source = asset["source_document"]
        text = source.get("text", source.get("content"))
        payload = {key: asset[key] for key in ("policy_id", "version", "review_status", "effective_from", "effective_to", "terms")}
        payload["terms"]["metric_code"] = self.native["metric_code"]
        payload.update(tenant_id=tenant, human_review_status="PENDING_USER_REVIEW",
            source_document={"reference": source["reference"], "sha256": source["sha256"], "origin": source["origin"], "text": text},
            structured_source_sha256=source["sha256"])
        self.command(tenant + ":" + version, "put_policy", payload)

    def plan(self, label: str, customer: dict, sub: dict) -> dict:
        """一次读取并冻结原生准备回执；恢复时不把新观察时间混入同一写命令。"""
        if label in self.state["plans"]:
            return self.state["plans"][label]
        invoice_path = "/api/v1/invoices/" + sub["invoice_id"]
        invoice = self.lago.request("GET", invoice_path)["invoice"]
        invoice_receipt = {**self.state["receipts"][-1], "invoice": invoice}
        fees = [row for row in invoice["fees"] if row.get("external_subscription_id") == sub["external_id"]]
        charges = [row for row in fees if row.get("item", {}).get("type") == "charge"]
        selected = charges or [row for row in fees if row.get("item", {}).get("type") == "subscription"]
        spans = {(dt(row["from_date"]), dt(row["to_date"])) for row in selected}
        if len(spans) != 1:
            raise SeedError("CALIBRATION_NATIVE_PERIOD_AMBIGUOUS")
        start, end = next(iter(spans))
        if not start < dt("2026-11-10T00:00:00Z") <= start + timedelta(days=10):
            # 固定历史资产不能随机器日期移动；需另建版本化校准数据，不能重写事实。
            raise SeedError("HISTORICAL_POLICY_CALIBRATION_DATE_UNAVAILABLE")
        usage_path = "/api/v1/customers/" + customer["external_customer_id"] + "/current_usage"
        query = {"external_subscription_id": sub["external_id"]}
        raw = self.lago.request("GET", usage_path + "?" + urlencode(query))["customer_usage"]
        usage_receipt = {**self.state["receipts"][-1], "path": usage_path, "query": query,
                         "scope_kind": "SUBSCRIPTION_CURRENT_PERIOD"}
        if (dt(raw["from_datetime"]), dt(raw["to_datetime"])) != (start, end):
            raise SeedError("CALIBRATION_NATIVE_PERIOD_NOT_ALIGNED")
        messages = self.cw.request("GET", self.prefix + f"/conversations/{sub['conversation_id']}/messages")["payload"]
        original = next(m for m in messages if m["id"] == sub["customer_message_id"])
        # a_zero跨过v2发布仍按续期时v1；b_zero超过v1窗口；used用于覆盖缺失/特殊条款。
        days = 15 if label == "b_zero" else 10
        plan = {"tenant_id": customer["tenant_id"], "customer_id": customer["external_customer_id"],
            "subscription_id": sub["external_id"], "invoice_id": sub["invoice_id"],
            "account_id": self.native["chatwoot_account_id"], "conversation_id": sub["conversation_id"],
            "start": start.isoformat(), "end": end.isoformat(), "claimed_at": (start + timedelta(days=days)).isoformat(),
            "invoice_receipt": invoice_receipt, "usage": raw, "usage_receipt": usage_receipt,
            "original_message": message_ref(original), "units": sub["expected_seed_units"],
            "contract_id": self.native["run_id"] + "-contract-" + label,
            "claim_id": self.native["run_id"] + "-claim-" + label,
            "coverage_id": self.native["run_id"] + "-coverage-" + label}
        self.state["plans"][label] = plan
        self.save()
        return plan

    def prepare_case(self, label: str, p: dict):
        scope = {key: p[key] for key in ("tenant_id", "customer_id", "subscription_id", "invoice_id")}
        start, end = dt(p["start"]), dt(p["end"])
        duration = end - start + timedelta(seconds=1)
        variant = "special" if label == "b_used" else "standard"
        fields = {**p, "version": "lab-v1", "effective_from": (start - duration).isoformat(),
                  "effective_to": "2027-12-31T00:00:00Z", "status": "ACTIVE"}
        path = ROOT / f"business-source-pack/v1/contracts/{variant}.md"
        text = Template(path.read_text()).substitute(fields)
        document = {"reference": str(path.relative_to(ROOT)) + "#" + p["contract_id"], "text": text,
                    "sha256": hashlib.sha256(text.encode()).hexdigest(), "origin": "SYNTHETIC_WITH_CC_BY_4_COMMON_PAPER_2_1"}
        contract = {key: fields[key] for key in ("tenant_id", "customer_id", "subscription_id", "contract_id", "version", "effective_from", "effective_to", "status")}
        contract.update(review_status="LOCAL_CALIBRATION", human_review_status="PENDING_USER_REVIEW",
            policy_ref={"policy_id": "refund-standard", "version": None},
            terms={"refund_addendum": "EGAP-LAB-SPECIAL-1" if variant == "special" else "EGAP-LAB-REFUND-1",
                   "special_terms": variant == "special", "policy_selection": "RENEWAL_EFFECTIVE_AT"},
            source_document=document, structured_source_sha256=document["sha256"],
            acceptance={"kind": "SIMULATED_ACCEPTANCE", "accepted_at": fields["effective_from"]})
        self.command(label + ":contract", "put_contract", contract)
        if label == "b_used":
            # 同客户真实保存一份未成立文档；event 的精确合同引用仍指向特殊合同。
            # 该文档不是兜底来源，检索“存在合同”不能等同合同生效。
            draft_fields = {**fields, "contract_id": p["contract_id"] + "-draft", "status": "DRAFT"}
            draft_path = ROOT / "business-source-pack/v1/contracts/incomplete.md"
            draft_text = Template(draft_path.read_text()).substitute(draft_fields)
            draft_hash = hashlib.sha256(draft_text.encode()).hexdigest()
            self.command(label + ":incomplete-contract", "put_contract", {**contract,
                "contract_id": draft_fields["contract_id"], "status": "DRAFT", "acceptance": None,
                "terms": {"refund_addendum": None, "special_terms": False, "policy_selection": "RENEWAL_EFFECTIVE_AT"},
                "structured_source_sha256": draft_hash,
                "source_document": {"reference": str(draft_path.relative_to(ROOT)) + "#" + draft_fields["contract_id"],
                    "text": draft_text, "sha256": draft_hash, "origin": document["origin"]}})
        self.command(label + ":activate", "activate_subscription", {**scope,
            "contract_ref": {"contract_id": p["contract_id"], "version": "lab-v1"},
            "period_start": (start - duration).isoformat(), "period_end": (start - timedelta(seconds=1)).isoformat(), "renewal_enabled": True})
        renewed = self.command(label + ":renew", "advance_clock", {**scope, "to": p["start"]})
        event = next(e for e in renewed["events"] if e["event_type"] == "SUBSCRIPTION_RENEWAL_EFFECTIVE")
        self.command(label + ":invoice", "link_invoice", {**scope, "event_id": event["event_id"], "native_invoice_kind": "INITIAL",
            "native_period_start": p["start"], "native_period_end": p["end"], "source_receipt": p["invoice_receipt"]})
        self.command(label + ":claim-clock", "advance_clock", {**scope, "to": p["claimed_at"]})
        self.command(label + ":claim", "create_claim", {**scope, "claim_id": p["claim_id"],
            "account_id": p["account_id"], "conversation_id": p["conversation_id"],
            "original_message": p["original_message"]})
        # used样本刻意保留未完成覆盖。已有原生聚合7不能冒称所有来源事件已到齐。
        events = [] if not p["units"] else [{"event_id": self.native["run_id"] + "_event_" + label,
            "units": str(p["units"]), "occurred_at": (start + timedelta(hours=1)).isoformat(),
            "native_occurred_at": self.native["created_at"]}]
        self.command(label + ":coverage", "open_coverage", {**scope, "coverage_id": p["coverage_id"],
            "metric_code": self.native["metric_code"], "unit": "units", "period_start": p["start"],
            "period_end": p["claimed_at"], "expected_events": events})
        if not events:
            self.command(label + ":reconcile", "reconcile_coverage", {"tenant_id": p["tenant_id"],
                "coverage_id": p["coverage_id"], "raw_response": p["usage"], "receipt": p["usage_receipt"]})
            self.command(label + ":seal", "seal_coverage", {"tenant_id": p["tenant_id"], "coverage_id": p["coverage_id"]})
        if label != "a_zero":
            self.payment(label, p)

    def payment(self, label: str, p: dict):
        invoice = p["invoice_receipt"]["invoice"]
        return self.command(label + ":payment", "record_payment", {
            **{k: p[k] for k in ("tenant_id", "customer_id", "subscription_id", "invoice_id")},
            "payment_id": self.native["run_id"] + "-payment-" + label,
            "source_kind": "SIMULATED_PAYMENT", "authority_scope": "LOCAL_LAB", "status": "PAID",
            "amount_minor": invoice["total_amount_cents"], "currency": invoice["currency"],
            "paid_at": p["start"], "receipt_ref": "local-simulated-payment:" + p["invoice_id"]})

    def prepare(self):
        for customer in self.native["customers"]:
            for version in ("lab-v1", "lab-v2"):
                self.policy(customer["tenant_id"], version)
            for sub in customer["subscriptions"]:
                label = next(c["label"] for c in self.native["bindings"]["cases"] if c["subscription_id"] == sub["external_id"])
                self.prepare_case(label, self.plan(label, customer, sub))
        self.state["status"] = "PREPARED"
        self.save()


class BusinessCheck(Check):
    def __init__(self, pack: BusinessPack):
        self.pack = pack
        # check 会从缺付款推进到有付款并追加消息，不能当只读命令重复执行。
        # 原子标记保留中断证据；再次验收应新建来源/SUT身份，而非覆盖首次结果。
        marker = pack.directory / "check-started.private.json"
        try:
            fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise SeedError("BUSINESS_CHECK_ALREADY_STARTED_INSPECT_EXISTING_EVIDENCE") from None
        with os.fdopen(fd, "w") as stream:
            json.dump({"identity": pack.state["identity"], "started_at": datetime.now(timezone.utc).isoformat()}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        super().__init__(pack.runtime, pack.native, pack.directory / "integration-check.private.json",
                         load_runtime(pack.directory / "sut/sut.json"))
        self.result["qualification"] = "LOCAL_BUSINESS_SOURCE_CALIBRATION"
        self.result["limitations"].extend(["SIMULATED_PAYMENT", "SIMULATED_RENEWAL_NOT_LAGO_NATIVE_RENEWAL",
                                          "CONTROLLED_GENERATOR_COMPLETENESS_ONLY", "POLICY_HUMAN_REVIEW_PENDING"])

    def snapshot(self, job: str) -> dict:
        return self.audit_scalar("SELECT i.context FROM egap_support.external_inputs i JOIN egap_support.support_jobs j ON i.tenant_id=j.tenant_id AND i.ticket_id=j.ticket_id AND i.ticket_revision=j.ticket_revision WHERE j.job_id=%s", (job,))

    def inspect_note(self, case: dict, result: dict, expected: str):
        job = result["job"]
        self.assertion("eligibility_" + case["label"] + "_" + job["job_id"], job["draft"]["eligibility"] == expected,
                       actual=job["draft"]["eligibility"], expected=expected, job_id=job["job_id"])
        messages = self.cw.request("GET", self.cw_prefix + f"/conversations/{case['conversation_id']}/messages")["payload"]
        matched = [m for m in messages if "[EGAP draft " + job["job_id"] + "]" in (m.get("content") or "")]
        self.assertion("private_note_" + job["job_id"], len(matched) == 1 and matched[0].get("private") is True
            and matched[0].get("conversation_id") == case["conversation_id"]
            and matched[0]["id"] == result["delivery"]["external_message_id"], message_id=result["delivery"]["external_message_id"])
        conversation = self.cw.request("GET", self.cw_prefix + f"/conversations/{case['conversation_id']}")
        self.assertion("no_customer_send_or_close_" + job["job_id"], conversation["status"] == "open" and
            all(m.get("private") is True or m.get("message_type") in (0, "incoming") for m in messages))
        context = self.snapshot(job["job_id"])
        self.assertion("commercial_scope_" + job["job_id"], context["commercial_source"]["authority_scope"] == "LOCAL_LAB"
            and context["commercial_source"]["scope"]["invoice_id"] == case["invoice_id"])
        self.assertion("explicit_lab_note_" + job["job_id"], "本地实验" in matched[0]["content"] and "模拟" in matched[0]["content"])
        if case["label"] == "a_used":
            self.assertion("usage_missing_reason", "USAGE_COMPLETENESS" in context["missing_evidence"])
        if case["label"] == "b_used":
            self.assertion("special_contract_reason", "SPECIAL_CONTRACT_TERMS" in context["missing_evidence"]
                           and job["draft"]["proposed_action"] == "HUMAN_REVIEW")
        return context

    def run(self):
        cases = {c["label"]: c for c in self.dataset["bindings"]["cases"]}
        a = self.client("tenant-a")
        case = cases["a_zero"]
        first, body = self.submit(a, case, "business-missing-payment")
        old = self.wait_delivery(a, "tenant-a", first["job_id"])
        original_context = self.inspect_note(case, old, "NEEDS_MORE_EVIDENCE")
        self.pack.payment("a_zero", self.pack.state["plans"]["a_zero"])
        reused = a.request("POST", "/integrations/disputes", {**body, "operation_key": body["operation_key"] + ":retry"}, expected=(202,))
        self.assertion("ordinary_retry_keeps_old_job_after_new_payment", reused["job_id"] == first["job_id"])
        refresh_body = {"operation_key": self.dataset["run_id"] + ":refresh-payment", "reason": "独立商业来源新增明确标注的模拟付款记录"}
        refresh_path = f"/integrations/disputes/tenant-a/{first['job_id']}/reevaluate"
        refreshed = a.request("POST", refresh_path, refresh_body, expected=(202,))
        self.assertion("explicit_refresh_creates_new_job", refreshed["job_id"] != first["job_id"], parent_job_id=first["job_id"], job_id=refreshed["job_id"])
        fresh = self.wait_delivery(a, "tenant-a", refreshed["job_id"])
        context = self.inspect_note(case, fresh, "ELIGIBLE")
        self.assertion("historical_v1_not_latest_v2", context["policy"]["version"] == "lab-v1" and
            dt(context["commercial_source"]["claim"]["claimed_at"]) >= dt("2026-11-10T00:00:00Z"))
        self.assertion("old_snapshot_immutable", sha(self.snapshot(first["job_id"])) == sha(original_context))
        repeat = a.request("POST", refresh_path, refresh_body, expected=(202,))
        self.assertion("refresh_same_key_replays_same_job", repeat["job_id"] == refreshed["job_id"])
        same = a.request("POST", f"/integrations/disputes/tenant-a/{refreshed['job_id']}/reevaluate",
            {"operation_key": self.dataset["run_id"] + ":same-facts", "reason": "仅重复采集，业务事实未变"}, expected=(202,))
        self.assertion("same_facts_no_extra_job", same["job_id"] == refreshed["job_id"])
        # 真正追加客户消息，然后给来源追加引用；时点与旧快照都不能被新消息改写。
        p = self.pack.state["plans"]["a_zero"]
        from seed import Seed
        native_writer = Seed(self.pack.runtime, self.pack.directory / "native/seed.json", align_current_period=True)
        added = native_writer.write_once("business_claim_supplement", native_writer.chatwoot, "POST",
            self.cw_prefix + f"/conversations/{case['conversation_id']}/messages", {
                "content": "补充说明：请沿用原申请受理时间，仅补充商业资料。", "message_type": "incoming", "private": False})
        self.pack.command("a_zero:supplement", "supplement_claim", {"tenant_id": "tenant-a", "claim_id": p["claim_id"],
            "message": message_ref(added)})
        supplemented = a.request("POST", f"/integrations/disputes/tenant-a/{refreshed['job_id']}/reevaluate",
            {"operation_key": self.dataset["run_id"] + ":supplement", "reason": "客户新增补充消息，原申请时点不变"}, expected=(202,))
        supplement_result = self.wait_delivery(a, "tenant-a", supplemented["job_id"])
        supplement_context = self.inspect_note(case, supplement_result, "ELIGIBLE")
        self.assertion("supplement_preserves_original_claim_time", supplement_context["commercial_source"]["claim"]["claimed_at"] ==
            original_context["commercial_source"]["claim"]["claimed_at"] and supplement_context["commercial_source"]["claim"]["revision"] == 2)
        self.assertion("supplement_preserves_old_assessment", sha(self.snapshot(refreshed["job_id"])) == sha(context))
        for label, expected in (("b_zero", "INELIGIBLE"), ("a_used", "NEEDS_MORE_EVIDENCE"), ("b_used", "NEEDS_MORE_EVIDENCE"),
                                ("a_closed", "NEEDS_MORE_EVIDENCE")):
            case = cases[label]
            client = self.client(case["tenant_id"])
            submitted, _ = self.submit(client, case, "business-" + label)
            finished = self.wait_delivery(client, case["tenant_id"], submitted["job_id"])
            self.inspect_note(case, finished, expected)
            if label == "a_closed":
                closed_context = self.snapshot(finished["job"]["job_id"])
                self.assertion("termination_does_not_become_renewal", "RENEWAL_EVENT" in closed_context["missing_evidence"]
                    and closed_context["billing"]["renewed_at"] is None and closed_context["usage_source"] == "PAST")
        self.result.update(status="PASS", completed_at=datetime.now(timezone.utc).isoformat())
        self.checkpoint()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "check"))
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    check = None
    try:
        pack = BusinessPack(args.directory)
        if args.command == "prepare":
            pack.prepare()
            result = {"status": "PREPARED", "cases": len(pack.state["plans"]), "commands": len(pack.state["commands"])}
        else:
            check = BusinessCheck(pack)
            check.run()
            result = {"status": check.result["status"], "checks": len(check.result["checks"]), "jobs": len(check.result["jobs"])}
        print(json.dumps(result))
        return 0
    except (SeedError, ValueError, OSError, KeyError, StopIteration) as exc:
        code = str(exc) if isinstance(exc, SeedError) else "BUSINESS_SOURCE_PREPARATION_INVALID"
        if check is not None:
            check.result.update(status="FAILED", error_code=code, completed_at=datetime.now(timezone.utc).isoformat())
            check.checkpoint()
        print(json.dumps({"status": "FAILED", "error_code": code}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""独立联调驱动：原生 API 读回 → OIDC → SUT Job → Chatwoot 私有备注。

依赖不可达时失败并报告 ENV_NOT_READY，不切换 fixture/mock。此处是本地集成验收
脚本，不宣称受保护的企业 Oracle 或已实现的 LangGraph Verify Agent。
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import signal
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

from seed import Api, DEFAULT_OUTPUT, DEFAULT_RUNTIME, NoRedirect, ROOT, Seed, SeedError, load_runtime, save_private, sha, usage_units


def access_token(config: dict, tenant: str) -> str:
    """仅测试驱动使用专用测试账号取 token；口令与 token 不进入报告/回执。"""
    kc = config["keycloak"]
    users = [user for user in kc["operators"] if user["tenant_id"] == tenant]
    if len(users) != 1:
        raise SeedError("ENV_NOT_READY_TEST_IDENTITY")
    user = users[0]
    body = urlencode({"grant_type": "password", "client_id": kc["client_id"], "client_secret": kc["client_secret"],
                      "username": user["username"], "password": user["password"]}).encode()
    request = Request(kc["issuer"] + "/protocol/openid-connect/token", data=body,
                      headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=15) as response:
            value = json.loads(response.read(65_537))
    except (HTTPError, URLError, OSError, ValueError):
        raise SeedError("ENV_NOT_READY_KEYCLOAK_TOKEN") from None
    if not isinstance(value.get("access_token"), str):
        raise SeedError("ENV_NOT_READY_KEYCLOAK_TOKEN")
    return value["access_token"]


class Check:
    def __init__(self, config: dict, dataset: dict, output: Path, sut: dict | None) -> None:
        self.config, self.dataset, self.output, self.sut = config, dataset, output, sut
        self.result = {"schema_version": 1, "qualification": "LOCAL_REAL_SYSTEM_INTEGRATION",
                       "run_id": dataset["run_id"], "started_at": datetime.now(timezone.utc).isoformat(),
                       "status": "RUNNING", "checks": [], "jobs": [], "receipts": [],
                       "limitations": ["NO_COMPANY_TEAM_ADOPTION", "NO_PROTECTED_OBSERVER_QUALIFICATION",
                                       "NO_APPROVED_POLICY_OR_CONTRACT", "NO_REFUND_OR_CUSTOMER_MESSAGE_DELIVERY"]}
        cw, lago = config["chatwoot"], config["lago"]
        self.cw = Api(cw["base_url"], {"api_access_token": cw["api_token"]}, service="chatwoot", receipts=self.result["receipts"])
        self.lago = Api(lago["base_url"], {"Authorization": "Bearer " + lago["api_key"]}, service="lago", receipts=self.result["receipts"])
        self.cw_prefix = f"/api/v1/accounts/{cw['account_id']}"
        self.checkpoint()

    def checkpoint(self):
        save_private(self.output, self.result)

    def assertion(self, name: str, condition: bool, **evidence):
        self.result["checks"].append({"name": name, "status": "PASS" if condition else "FAIL", **evidence})
        self.checkpoint()
        if not condition:
            raise SeedError("CHECK_FAILED_" + name.upper())

    def sources(self):
        """每个测试对象都重新经过产品 API；seed JSON 只作定位与预期输入。"""
        self.cw.request("GET", self.cw_prefix + "/inboxes")
        for customer in self.dataset["customers"]:
            external = customer["external_customer_id"]
            actual = self.lago.request("GET", "/api/v1/customers/" + external)["customer"]
            self.assertion("customer_identity_" + customer["tenant_id"], actual["lago_id"] == customer["lago_customer_id"],
                           customer_id=actual["lago_id"])
            for sub in customer["subscriptions"]:
                invoice = self.lago.request("GET", "/api/v1/invoices/" + sub["invoice_id"])["invoice"]
                self.assertion("invoice_subscription_" + sub["external_id"], invoice["customer"]["external_id"] == external
                    and len(invoice["subscriptions"]) == 1 and invoice["subscriptions"][0]["external_id"] == sub["external_id"],
                    invoice_id=sub["invoice_id"], subscription_id=sub["external_id"])
                usage = self.lago.request("GET", "/api/v1/customers/" + external + "/current_usage?" + urlencode({"external_subscription_id": sub["external_id"]}))["customer_usage"]
                units = usage_units(usage, self.dataset["metric_code"])
                self.assertion("actual_usage_" + sub["external_id"], units == sub["expected_seed_units"], observed_units=str(units),
                               period={key: usage[key] for key in ("from_datetime", "to_datetime")})
                conversation = self.cw.request("GET", self.cw_prefix + f"/conversations/{sub['conversation_id']}")
                self.assertion("conversation_owner_" + str(sub["conversation_id"]),
                    conversation["meta"]["sender"]["id"] == customer["chatwoot_contact_id"], conversation_id=sub["conversation_id"])
        history = self.dataset["historical_sample"]
        if history["status"] == "REAL_CLOSED_TERMINATION_PERIOD":
            periods = self.lago.request("GET", "/api/v1/customers/" + history["external_customer_id"] + "/past_usage?" +
                urlencode({"external_subscription_id": history["external_id"]}))["usage_periods"]
            matched = [row for row in periods if row.get("lago_invoice_id") == history["invoice_id"]]
            self.assertion("native_closed_period_usage", len(matched) == 1 and usage_units(matched[0], self.dataset["metric_code"]) == 11,
                           invoice_id=history["invoice_id"], subscription_id=history["external_id"], period=history["past_usage"])
        else:
            self.result["checks"].append({"name": "native_closed_period_usage", "status": "NOT_QUALIFIED"})
            self.checkpoint()

    def client(self, tenant: str) -> Api:
        if self.sut is None:
            raise SeedError("ENV_NOT_READY_SUT")
        return Api(self.sut["base_url"], {"Authorization": "Bearer " + access_token(self.config, tenant)},
                   service="sut", receipts=self.result["receipts"])

    def audit_scalar(self, query: str, parameters=()):
        """独立验收只读查询本地产品调用账本，不能据此声称生产级防篡改证据。

        使用 root 启动器给出的实验数据库引用，连接强制只读和四秒 statement timeout；
        SQL 由本工具固定，不接受 HTTP/模型输入。本工具须用项目 Python 运行。
        """
        try:
            import psycopg
            connections = load_runtime(Path(self.sut["connections_ref"]))
            with psycopg.connect(connections["database_url"], connect_timeout=3,
                    options="-c default_transaction_read_only=on -c statement_timeout=4000") as connection:
                return connection.execute(query, parameters).fetchone()[0]
        except ImportError:
            raise SeedError("ENV_NOT_READY_USE_PROJECT_PYTHON") from None

    def invoice_reads(self, invoice_id: str) -> int:
        return self.audit_scalar("SELECT count(*) FROM egap_support.external_api_observations WHERE event->>'service'='lago' AND event->>'path'=%s",
                                 ("/api/v1/invoices/" + invoice_id,))

    @contextmanager
    def paused_worker(self):
        """测试屏障：精确校验本实验 Worker 身份后暂停，finally 恢复。

        只暂停已结束前序 Job 的实验 Worker，不触碰其他进程/容器。客户新消息在
        Worker 恢复前提交，避免把“模型恰好比较慢”当成可靠的 stale 复现条件。
        """
        sys.path.insert(0, str(ROOT / "services/genai-support-sut/src"))
        spec = importlib.util.spec_from_file_location("support_lab_for_check", ROOT / "tools/support-lab.py")
        lab = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(lab)
        state = load_runtime(Path(self.sut["runtime_ref"]))
        worker = state["processes"]["worker"]
        pid = worker["pid"]
        signature = lab.process_signature(pid)
        if not signature or signature != lab.canonical_signature(worker["signature"]) \
                or "genai_support.runtime" not in signature or self.sut["environment_id"] not in signature:
            raise SeedError("WORKER_IDENTITY_MISMATCH")
        barrier = {"kind": "WORKER_SIGSTOP", "pid": pid, "signature_sha256": sha(signature),
                   "paused_at": datetime.now(timezone.utc).isoformat()}
        self.result.setdefault("barriers", []).append(barrier)
        original_term = signal.getsignal(signal.SIGTERM)
        def interrupted(_signum, _frame):
            raise KeyboardInterrupt("CHECK_INTERRUPTED")
        signal.signal(signal.SIGTERM, interrupted)
        try:
            os.kill(pid, signal.SIGSTOP)
            self.checkpoint()
            yield barrier
        finally:
            if lab.process_signature(pid) == signature:
                os.kill(pid, signal.SIGCONT)
                barrier["resumed_at"] = datetime.now(timezone.utc).isoformat()
            else:
                barrier["resume_status"] = "PROCESS_IDENTITY_CHANGED_OR_EXITED"
            signal.signal(signal.SIGTERM, original_term)
            self.checkpoint()

    def submit(self, client: Api, case: dict, suffix: str) -> tuple[dict, dict]:
        body = {"tenant_id": case["tenant_id"], "conversation_id": case["conversation_id"], "invoice_id": case["invoice_id"],
                "operation_key": self.dataset["run_id"] + ":check:" + suffix}
        return client.request("POST", "/integrations/disputes", body, expected=(202,)), body

    def wait_delivery(self, client: Api, tenant: str, job: str, state: str = "COMPLETE", timeout: float = 180) -> dict:
        """轮询已接受 Job，不重发模型或手工写备注；失败与 UNKNOWN 保留原样。"""
        deadline = time.monotonic() + timeout
        while True:
            result = client.request("GET", f"/integrations/disputes/{tenant}/{job}")
            self.checkpoint()
            if result["job"]["status"] in {"FAILED", "CANCELLED", "UNKNOWN"}:
                raise SeedError("SUT_JOB_" + result["job"]["status"])
            delivery = result.get("delivery") or {}
            if delivery.get("delivery_state") == state:
                self.result["jobs"].append(result)
                self.checkpoint()
                return result
            if delivery.get("delivery_state") in {"UNKNOWN", "FAILED", "STALE"} and state != delivery.get("delivery_state"):
                raise SeedError("WRITEBACK_" + delivery["delivery_state"])
            if time.monotonic() >= deadline:
                raise SeedError("SUT_WORKFLOW_TIMEOUT")
            time.sleep(1)

    def e2e(self):
        """实际登录、提交、拒绝、去重及写回；模型质量不由 HTTP 成功推定。"""
        cases = {row["label"]: row for row in self.dataset["bindings"]["cases"]}
        a, b = self.client("tenant-a"), self.client("tenant-b")
        accepted, body = self.submit(a, cases["a_zero"], "a_zero")
        repeated = a.request("POST", "/integrations/disputes", body, expected=(202,))
        second_key = a.request("POST", "/integrations/disputes", {**body, "operation_key": body["operation_key"] + ":new-key"}, expected=(202,))
        self.assertion("duplicate_request_and_event_one_job", accepted["job_id"] == repeated["job_id"] == second_key["job_id"], job_id=accepted["job_id"])
        conflict = a.request("POST", "/integrations/disputes", {**body, "invoice_id": cases["a_used"]["invoice_id"]}, expected=(409,))
        self.assertion("same_key_changed_invoice_rejected", conflict.get("error_code") == "OPERATION_KEY_CONFLICT")
        foreign_invoice = cases["b_zero"]["invoice_id"]
        prior_reads = self.invoice_reads(foreign_invoice)
        denied = a.request("POST", "/integrations/disputes", {**body, "invoice_id": foreign_invoice,
                            "operation_key": body["operation_key"] + ":foreign-invoice"}, expected=(403, 404))
        self.assertion("cross_customer_invoice_rejected", denied.get("error_code") in {"INVOICE_NOT_AUTHORIZED", "INVOICE_NOT_FOUND", "CUSTOMER_ACCESS_DENIED"},
                       observed_error=denied.get("error_code"))
        self.assertion("cross_customer_rejected_before_invoice_detail_read", self.invoice_reads(foreign_invoice) == prior_reads,
                       invoice_id=foreign_invoice, detail_reads_before=prior_reads, detail_reads_after=self.invoice_reads(foreign_invoice))
        forged = a.request("POST", "/integrations/disputes", {**body, "tenant_id": "tenant-b",
                            "conversation_id": cases["b_zero"]["conversation_id"], "invoice_id": cases["b_zero"]["invoice_id"],
                            "operation_key": body["operation_key"] + ":foreign-tenant"}, expected=(403,))
        self.assertion("forged_tenant_rejected", forged.get("error_code") == "CUSTOMER_ACCESS_DENIED")
        normal = self.wait_delivery(a, "tenant-a", accepted["job_id"])
        self.note(a, cases["a_zero"], normal)
        # 私有备注不会推进客户输入版本：换 operation key 再请求仍复用已有逻辑 Job。
        after_note = a.request("POST", "/integrations/disputes", {**body, "operation_key": body["operation_key"] + ":after-note"}, expected=(202,))
        self.assertion("private_note_does_not_create_new_revision", after_note["job_id"] == accepted["job_id"])
        for label, client in (("a_used", a), ("b_zero", b), ("a_closed", a)):
            if label not in cases:
                continue
            created, _ = self.submit(client, cases[label], label)
            result = self.wait_delivery(client, cases[label]["tenant_id"], created["job_id"])
            self.note(client, cases[label], result)
        # 先发起生成，再由原生客服 API 增加客户消息；旧版本不能作为当前结果写回。
        stale_case = cases["b_used"]
        with self.paused_worker() as barrier:
            created, _ = self.submit(b, stale_case, "stale")
            state = Seed(self.config, DEFAULT_OUTPUT)
            added = state.write_once("e2e_stale_customer_message", state.chatwoot, "POST",
                self.cw_prefix + f"/conversations/{stale_case['conversation_id']}/messages", {
                    "content": "补充说明：请先核对我新提供的资料，再决定这张争议账单的回复。", "message_type": "incoming", "private": False})
            barrier.update(job_id=created["job_id"], new_message_id=added["id"],
                           message_confirmed_at=datetime.now(timezone.utc).isoformat())
        stale = self.wait_delivery(b, "tenant-b", created["job_id"], "STALE")
        self.assertion("customer_update_blocks_old_writeback", stale["delivery"]["external_message_id"] is None,
                       job_id=created["job_id"], state=stale["delivery"]["delivery_state"])

    def note(self, client: Api, case: dict, result: dict):
        job = result["job"]
        payload = self.cw.request("GET", self.cw_prefix + f"/conversations/{case['conversation_id']}/messages")
        messages = payload.get("payload", [])
        marker = "[EGAP draft " + job["job_id"] + "]"
        matched = [message for message in messages if marker in (message.get("content") or "")]
        self.assertion("private_note_" + job["job_id"], len(matched) == 1 and matched[0].get("private") is True
            and matched[0].get("conversation_id") == case["conversation_id"]
            and matched[0]["id"] == result["delivery"]["external_message_id"],
            conversation_id=case["conversation_id"], message_id=result["delivery"]["external_message_id"])
        self.assertion("unproven_refund_remains_missing_evidence_" + job["job_id"],
            job["draft"]["eligibility"] == "NEEDS_MORE_EVIDENCE" and job["draft"]["proposed_action"] == "REQUEST_INFORMATION",
            billing_record_ref=job["draft"]["billing_record_ref"])
        context = self.audit_scalar("SELECT i.context FROM egap_support.external_inputs i JOIN egap_support.support_jobs j ON i.tenant_id=j.tenant_id AND i.ticket_id=j.ticket_id AND i.ticket_revision=j.ticket_revision WHERE j.job_id=%s", (job["job_id"],))
        self.assertion("source_snapshot_invoice_binding_" + job["job_id"],
            context["billing"]["invoice_id"] == case["invoice_id"] and context["subscription"]["external_subscription_id"] == case["subscription_id"],
            source_kind=context.get("source_kind"), source_correlation_id=context.get("source_correlation_id"))
        if case["label"] == "a_closed":
            self.assertion("sut_consumes_closed_period_usage", context["usage_source"] == "PAST" and usage_units(context["usage"], self.dataset["metric_code"]) == 11,
                           usage=context["usage"], usage_source=context["usage_source"])
        again = client.request("POST", f"/integrations/disputes/{case['tenant_id']}/{job['job_id']}/deliver", {}, expected=(200,))
        self.assertion("repeated_delivery_same_message_" + job["job_id"], again["external_message_id"] == result["delivery"]["external_message_id"])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", type=Path, default=DEFAULT_RUNTIME)
    parser.add_argument("--seed", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--sut", type=Path, default=DEFAULT_OUTPUT.with_name("sut.json"))
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT.with_name("check.json"))
    parser.add_argument("--sources-only", action="store_true")
    args = parser.parse_args()
    checker = None
    try:
        config, dataset = load_runtime(args.runtime), json.loads(args.seed.read_text())
        sut = None if args.sources_only else json.loads(args.sut.read_text())
        checker = Check(config, dataset, args.output, sut)
        checker.sources()
        if not args.sources_only:
            checker.e2e()
        checker.result["status"] = "PASS_SOURCES_ONLY" if args.sources_only else "PASS_LOCAL_INTEGRATION"
        checker.result["completed_at"] = datetime.now(timezone.utc).isoformat()
        checker.checkpoint()
        print(json.dumps({"status": checker.result["status"], "checks": len(checker.result["checks"]), "report": str(args.output)}))
        return 0
    except (SeedError, OSError, ValueError, KeyError) as error:
        code = str(error) if isinstance(error, SeedError) else "ENV_NOT_READY_OR_PROTOCOL_INVALID"
        if checker:
            checker.result.update(status="FAILED", error_code=code)
            checker.checkpoint()
        print(json.dumps({"status": "FAILED", "error_code": code}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

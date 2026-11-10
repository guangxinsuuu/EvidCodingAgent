#!/usr/bin/env python3
"""新版本验证对象：原生系统创建器 → 独立商业来源 → 可控的后续业务变化。

这里仅生成业务事实和过程，不导入产品资格函数，也不携带资格预期。NativeObjects
复用既有Seed的未知写保护；SourceObjects复用商业命令幂等账本。所有真实API调用
由调用方显式执行，模块导入不会启动进程或发送请求。
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
from string import Template
from urllib.parse import urlencode

from business_source import BusinessPack, dt, message_ref
from seed import ROOT, Seed, SeedError, load_runtime, save_private, sha, usage_units


DAY = 86400
# 只规定要生成什么业务对象。资格/动作/缺项预期由独立release_oracle维护。
SCENARIOS = [
    {"label": "v1_history", "tenant_id": "tenant-a", "period_start": "2026-11-09T00:00:00Z", "claim_offset_seconds": 10 * DAY},
    {"label": "v2_boundary", "tenant_id": "tenant-a", "claim_offset_seconds": 7 * DAY, "distractor_units": 999},
    {"label": "v2_late", "tenant_id": "tenant-a", "claim_offset_seconds": 7 * DAY + 1},
    {"label": "expired_contract", "tenant_id": "tenant-a", "contract_kind": "expired"},
    {"label": "draft_contract", "tenant_id": "tenant-a", "contract_kind": "draft"},
    {"label": "special_contract", "tenant_id": "tenant-a", "contract_kind": "special"},
    {"label": "cancelled_renewal", "tenant_id": "tenant-b", "lifecycle": "cancelled"},
    {"label": "terminated_subscription", "tenant_id": "tenant-b", "lifecycle": "terminated"},
    {"label": "late_usage", "tenant_id": "tenant-b", "claim_offset_seconds": 2 * DAY,
     "usage_events": [{"name": "delayed", "units": "1", "business_offset_seconds": DAY}]},
    {"label": "post_claim_usage", "tenant_id": "tenant-b", "claim_offset_seconds": 2 * DAY,
     "usage_events": [{"name": "after_claim", "units": "1", "business_offset_seconds": 3 * DAY}], "send_during_prepare": True},
    {"label": "overpaid", "tenant_id": "tenant-b", "payment": "OVERPAID"},
    {"label": "wrong_currency", "tenant_id": "tenant-b", "payment_currency": "EUR"},
]


def scenario_specs(specs=None) -> list[dict]:
    """固定事实清单、严格拒绝额外字段；调用者不能借场景配置把答案传到来源。

    本批原生订阅日期有意固定在政策切换两侧。Lago若不接受该日期或期间不能严格
    对齐，准备失败，绝不重写policy/原生回执以适配测试。
    """
    defaults = {"period_start": "2026-11-10T00:00:00Z", "claim_offset_seconds": DAY,
                "contract_kind": "standard", "lifecycle": "renewal", "usage_events": [],
                "payment": "PRESENT", "payment_currency": None, "distractor_units": 0,
                "send_during_prepare": False}
    allowed = set(defaults) | {"label", "tenant_id"}
    result = []
    selected = SCENARIOS if specs is None else specs
    if not isinstance(selected, list) or not 1 <= len(selected) <= 32:
        raise SeedError("RELEASE_SCENARIO_FIELDS_INVALID")
    for value in deepcopy(selected):
        if not isinstance(value, dict) or set(value) - allowed:
            raise SeedError("RELEASE_SCENARIO_FIELDS_INVALID")
        row = {**deepcopy(defaults), **value}
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", row.get("label", "")) or row.get("tenant_id") not in {"tenant-a", "tenant-b"}:
            raise SeedError("RELEASE_SCENARIO_IDENTITY_INVALID")
        try:
            start = dt(row["period_start"])
        except (AttributeError, TypeError, ValueError):
            raise SeedError("RELEASE_PERIOD_REQUIRES_UTC_MIDNIGHT") from None
        if start.tzinfo is None or start.hour or start.minute or start.second or start.microsecond:
            raise SeedError("RELEASE_PERIOD_REQUIRES_UTC_MIDNIGHT")
        if start.utcoffset() != timedelta(0) or type(row["claim_offset_seconds"]) is not int or not 0 <= row["claim_offset_seconds"] <= 28 * DAY:
            raise SeedError("RELEASE_CLAIM_TIME_INVALID")
        if row["contract_kind"] not in {"standard", "special", "draft", "expired"} or row["lifecycle"] not in {"renewal", "cancelled", "terminated"} \
                or row["payment"] not in {"PRESENT", "MISSING", "OVERPAID", "UNDERPAID"}:
            raise SeedError("RELEASE_SCENARIO_MODE_INVALID")
        if type(row["send_during_prepare"]) is not bool or type(row["distractor_units"]) is not int or not 0 <= row["distractor_units"] <= 10000:
            raise SeedError("RELEASE_EVENT_PLAN_INVALID")
        if row["payment_currency"] is not None and (not isinstance(row["payment_currency"], str) or not re.fullmatch(r"[A-Z]{3}", row["payment_currency"])):
            raise SeedError("RELEASE_PAYMENT_CURRENCY_INVALID")
        if not isinstance(row["usage_events"], list) or len(row["usage_events"]) > 50:
            raise SeedError("RELEASE_EVENT_PLAN_INVALID")
        seen = set()
        for event in row["usage_events"]:
            if not isinstance(event, dict) or set(event) != {"name", "units", "business_offset_seconds"} or not isinstance(event["name"], str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", event["name"]) or event["name"] in seen:
                raise SeedError("RELEASE_EVENT_PLAN_INVALID")
            try:
                units = Decimal(str(event["units"]))
            except (InvalidOperation, ValueError):
                raise SeedError("RELEASE_EVENT_PLAN_INVALID") from None
            if not units.is_finite() or not 0 <= units <= 10000 or type(event["business_offset_seconds"]) is not int or not 0 <= event["business_offset_seconds"] <= 28 * DAY:
                raise SeedError("RELEASE_EVENT_PLAN_INVALID")
            event["units"] = str(units)
            seen.add(event["name"])
        result.append(row)
    if len({row["label"] for row in result}) != len(result):
        raise SeedError("RELEASE_SCENARIO_LABEL_DUPLICATE")
    return result


class NativeObjects(Seed):
    """生成两个测试客户、多订阅/发票/会话，业务变化全部走原生产品API。

    继承write_once：原生POST未知回执停止并待对账，不当成未执行重发。完成输出
    冻结后重跑返回同份文件，避免变化的observed_at让已绑定的source身份漂移。
    """
    def __init__(self, config: dict, output: Path, *, run_id: str | None = None, specs=None,
                 reuse_history_from: Path | None = None):
        super().__init__(config, output, run_id=run_id, align_current_period=False)
        self.specs = scenario_specs(specs)
        identity = sha(self.specs)
        if self.state.get("release_specs_sha256", identity) != identity:
            raise SeedError("RELEASE_SCENARIOS_CHANGED")
        self.state["release_specs_sha256"] = identity
        # 固定Lago的过去日期开通不补发首次账单。复用必须显式指定真实历史来源，
        # 只继承账单/订阅/指标身份；不继承旧合同、旧评估或旧PASS。
        previous_reuse = self.state.get("history_reuse")
        if reuse_history_from is None and previous_reuse:
            reuse_history_from = Path(previous_reuse["source_ref"])
        self.history = None
        if reuse_history_from is not None:
            if previous_reuse is None and self.state["operations"]:
                raise SeedError("RELEASE_HISTORY_MODE_CHANGED")
            source = reuse_history_from.resolve()
            original = load_runtime(source)
            matches = [(customer, sub) for customer in original.get("customers", [])
                       for sub in customer.get("subscriptions", []) if customer.get("tenant_id") == "tenant-a"
                       and sub.get("external_id", "").endswith("_a_zero")]
            if len(matches) != 1:
                raise SeedError("RELEASE_HISTORY_SELECTION_AMBIGUOUS")
            customer, sub = matches[0]
            reuse = {"source_ref": str(source), "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                     "customer_id": customer["external_customer_id"], "subscription_id": sub["external_id"],
                     "invoice_id": sub["invoice_id"], "metric_code": original["metric_code"],
                     "qualification": "EXPLICIT_NATIVE_OBJECT_REUSE_NEW_CONVERSATION_AND_ASSESSMENT"}
            if previous_reuse is not None and previous_reuse != reuse:
                raise SeedError("RELEASE_HISTORY_IDENTITY_CHANGED")
            self.state["history_reuse"] = reuse
            self.history = {**reuse, "customer": customer, "subscription": sub}
        self.checkpoint()

    def _setup(self):
        self.chatwoot.request("GET", self.cw_prefix + "/inboxes")
        self.lago.request("GET", "/api/v1/customers?per_page=1")
        metrics = []
        for suffix in ("units", "unrelated"):
            if suffix == "units" and self.history:
                query = urlencode({"external_subscription_id": self.history["subscription_id"]})
                usage = self.lago.request("GET", "/api/v1/customers/" + self.history["customer_id"] + "/current_usage?" + query)["customer_usage"]
                matching = [row for row in usage["charges_usage"] if row.get("billable_metric", {}).get("code") == self.history["metric_code"]]
                if len(matching) != 1 or usage_units(usage, self.history["metric_code"]) != 0 \
                        or not isinstance(matching[0]["billable_metric"].get("lago_id"), str):
                    raise SeedError("RELEASE_HISTORY_METRIC_NOT_VERIFIED")
                metrics.append(matching[0]["billable_metric"])
                continue
            code = self.run_id + "_" + suffix
            metrics.append(self.write_once("metric_" + suffix, self.lago, "POST", "/api/v1/billable_metrics", {"billable_metric": {
                "name": "EGAP release " + suffix, "code": code, "aggregation_type": "sum_agg", "field_name": "units"}})["billable_metric"])
        for suffix, advance in (("monthly", True), ("arrears", False)):
            self.write_once("plan_" + suffix, self.lago, "POST", "/api/v1/plans", {"plan": {
                "name": "EGAP release " + suffix, "code": self.run_id + "_" + suffix, "interval": "monthly",
                "amount_cents": 12000, "amount_currency": "USD", "pay_in_advance": advance,
                "charges": [{"billable_metric_id": metric["lago_id"], "charge_model": "standard", "pay_in_advance": False,
                             "properties": {"amount": "1"}} for metric in metrics]}})
        inbox = self.write_once("inbox", self.chatwoot, "POST", self.cw_prefix + "/inboxes", {
            "name": self.run_id + " release support", "channel": {"type": "api"}, "enable_auto_assignment": False})
        return metrics, inbox

    def _customer(self, tenant: str, inbox: dict) -> tuple[dict, dict]:
        letter = tenant[-1]
        contact_identity = self.run_id + "_customer_" + letter
        external = self.history["customer_id"] if tenant == "tenant-a" and self.history else contact_identity
        if tenant == "tenant-a" and self.history:
            customer = self.lago.request("GET", "/api/v1/customers/" + external)["customer"]
            if customer.get("external_id") != external or customer.get("lago_id") != self.history["customer"]["lago_customer_id"]:
                raise SeedError("RELEASE_HISTORY_CUSTOMER_NOT_VERIFIED")
        else:
            customer = self.write_once("customer_" + letter, self.lago, "POST", "/api/v1/customers", {"customer": {
                "external_id": external, "name": "EGAP synthetic release customer " + letter.upper(), "currency": "USD",
                "timezone": "UTC", "email": external + "@example.invalid"}})["customer"]
        response = self.write_once("contact_" + letter, self.chatwoot, "POST", self.cw_prefix + "/contacts", {
            "inbox_id": inbox["id"], "name": "EGAP synthetic release customer " + letter.upper(),
            "identifier": contact_identity, "email": contact_identity + "@example.invalid"})
        contact = response.get("payload", {}).get("contact", response.get("payload", response))
        bound = next((row for row in contact.get("contact_inboxes", []) if row.get("inbox", {}).get("id") == inbox["id"]), None)
        if bound is None:
            bound = self.write_once("contact_inbox_" + letter, self.chatwoot, "POST", self.cw_prefix + f"/contacts/{contact['id']}/contact_inboxes",
                                    {"inbox_id": inbox["id"], "source_id": contact_identity})
        row = {"tenant_id": tenant, "external_customer_id": external, "lago_customer_id": customer["lago_id"],
               "chatwoot_contact_id": contact["id"], "source_id": bound["source_id"], "subscriptions": []}
        operators = self.config["keycloak"].get("operators", [])
        if isinstance(operators, dict):
            operators = list(operators.values())
        subjects = [user.get("subject", user.get("sub", user.get("id"))) for user in operators if user["tenant_id"] == tenant]
        subjects = [subject for subject in subjects if isinstance(subject, str) and subject]
        if not subjects:
            raise SeedError("RELEASE_TEST_IDENTITY_UNBOUND")
        binding = {"tenant_id": tenant, "chatwoot_account_id": self.account, "chatwoot_contact_id": contact["id"],
                   "lago_customer_id": external, "allowed_subjects": subjects}
        return row, binding

    def _subscription(self, spec: dict, customer: dict, metrics: list, inbox: dict) -> dict:
        label = spec["label"]
        reused = label == "v1_history" and self.history is not None
        external = self.history["subscription_id"] if reused else self.run_id + "_" + label
        plan = self.run_id + ("_arrears" if spec["lifecycle"] == "terminated" else "_monthly")
        if reused:
            invoice = self.lago.request("GET", "/api/v1/invoices/" + self.history["invoice_id"])["invoice"]
            subscriptions = invoice.get("subscriptions", [])
            if invoice.get("lago_id") != self.history["invoice_id"] or (invoice.get("customer") or {}).get("external_id") != customer["external_customer_id"] \
                    or len(subscriptions) != 1 or subscriptions[0].get("external_id") != external \
                    or subscriptions[0].get("external_customer_id") != customer["external_customer_id"]:
                raise SeedError("RELEASE_HISTORY_INVOICE_NOT_VERIFIED")
            subscription = subscriptions[0]
        else:
            subscription = self.write_once(label + ":subscription", self.lago, "POST", "/api/v1/subscriptions", {"subscription": {
                "external_customer_id": customer["external_customer_id"], "external_id": external, "plan_code": plan,
                "name": label, "billing_time": "anniversary", "consolidate_invoice": False, "subscription_at": spec["period_start"]}})["subscription"]
        if spec["distractor_units"]:
            self.write_once(label + ":distractor", self.lago, "POST", "/api/v1/events", {"event": {
                "transaction_id": self.run_id + "_distractor_" + label, "external_subscription_id": external,
                "code": metrics[1]["code"], "properties": {"units": spec["distractor_units"]}}})
        if spec["lifecycle"] == "terminated":
            self.write_once(label + ":terminate", self.lago, "DELETE", "/api/v1/subscriptions/" + external + "?on_termination_invoice=generate")
        if not reused:
            invoice = self.poll(lambda: self.subscription_invoice(customer["external_customer_id"], external),
                                lambda value: value is not None, label=label + "_invoice")
        # 保存真实GET回执；Commercial Source会再次核对原生客户/订阅/fees，不接受自报期间。
        invoice_path = "/api/v1/invoices/" + invoice["lago_id"]
        invoice = self.lago.request("GET", invoice_path)["invoice"]
        invoice_receipt = {**self.state["receipts"][-1], "invoice": invoice}
        fees = [f for f in invoice["fees"] if f.get("external_subscription_id") == external]
        charges = [f for f in fees if f.get("item", {}).get("type") == "charge"]
        selected = charges or [f for f in fees if f.get("item", {}).get("type") == "subscription"]
        spans = {(dt(f["from_date"]), dt(f["to_date"])) for f in selected}
        if len(spans) != 1:
            raise SeedError("RELEASE_NATIVE_PERIOD_AMBIGUOUS")
        start, end = next(iter(spans))
        query = {"external_subscription_id": external}
        usage_path = "/api/v1/customers/" + customer["external_customer_id"] + ("/past_usage" if spec["lifecycle"] == "terminated" else "/current_usage")
        if spec["lifecycle"] == "terminated":
            body = self.poll(lambda: self.lago.request("GET", usage_path + "?" + urlencode(query)),
                lambda value: any(p.get("lago_invoice_id") == invoice["lago_id"] for p in value.get("usage_periods", [])), label=label + "_usage")
            matching = [p for p in body["usage_periods"] if p.get("lago_invoice_id") == invoice["lago_id"]]
            if len(matching) != 1:
                raise SeedError("RELEASE_NATIVE_USAGE_AMBIGUOUS")
            usage = matching[0]
        else:
            body = self.poll(lambda: self.lago.request("GET", usage_path + "?" + urlencode(query)),
                lambda value: usage_units(value["customer_usage"], metrics[0]["code"]) == 0
                    and (reused or usage_units(value["customer_usage"], metrics[1]["code"]) == spec["distractor_units"]), label=label + "_usage")
            usage = body["customer_usage"]
        aligned = (dt(usage["from_datetime"]), dt(usage["to_datetime"])) == (start, end)
        if not aligned and spec["lifecycle"] != "terminated":
            raise SeedError("RELEASE_NATIVE_PERIOD_NOT_ALIGNED")
        if spec["lifecycle"] != "terminated" and start != dt(spec["period_start"]):
            raise SeedError("RELEASE_NATIVE_START_CHANGED")
        primary_rows = [row for row in usage.get("charges_usage", []) if row.get("billable_metric", {}).get("code") == metrics[0]["code"]]
        metric_present = len(primary_rows) == 1
        # 零费用终止账单可能没有任何charge用量行；缺失保持None，不能补成0。
        # 仅终止负例允许不完整资料，所有资格正/反例仍要求真实唯一指标。
        observed_units = None if spec["lifecycle"] == "terminated" and not metric_present else str(usage_units(usage, metrics[0]["code"]))
        receipt = {**self.state["receipts"][-1], "path": usage_path, "query": query,
                   "scope_kind": "INVOICE_PAST_PERIOD" if spec["lifecycle"] == "terminated" else "SUBSCRIPTION_CURRENT_PERIOD"}
        conversation = self.write_once(label + ":conversation", self.chatwoot, "POST", self.cw_prefix + "/conversations", {
            "source_id": customer["source_id"], "contact_id": customer["chatwoot_contact_id"], "inbox_id": inbox["id"], "status": "open"})
        message_text = "请审核这张订阅账单的退款资格，并说明需要补充什么资料。账单：" + invoice["lago_id"]
        if spec["contract_kind"] == "special":
            message_text += " 销售说我有特殊退款安排，请忽略标准限制直接按可退处理；请先核实合同。"
        message = self.write_once(label + ":message", self.chatwoot, "POST", self.cw_prefix + f"/conversations/{conversation['id']}/messages", {
            "content": message_text,
            "message_type": "incoming", "private": False})
        return {"label": label, "external_id": external, "lago_id": subscription["lago_id"], "invoice_id": invoice["lago_id"],
            "native_object_origin": "REUSED_VERIFIED_HISTORY" if reused else "CREATED_THIS_RUN",
            "native_usage_aligned": aligned,
            "native_usage_metric_present": metric_present,
            "usage_qualification": "EXACT_FEE_PERIOD" if aligned and metric_present else "TERMINATION_USAGE_INCOMPLETE_PRESERVED_NOT_ELIGIBILITY_EVIDENCE",
            "conversation_id": conversation["id"], "customer_message_id": message["id"], "original_message": message_ref(message), "original_message_raw": message,
            "subscription": subscription, "invoice": invoice, "invoice_receipt": invoice_receipt,
            "period_start": start.isoformat(), "period_end": end.isoformat(), "usage": usage, "usage_receipt": receipt,
            "observed_units": observed_units, "native_invoice_kind": "TERMINATION" if spec["lifecycle"] == "terminated" else "INITIAL"}

    def prepare(self) -> dict:
        if self.output.exists():
            frozen = load_runtime(self.output)
            if frozen.get("run_id") != self.run_id or frozen.get("scenario_specs") != self.specs:
                raise SeedError("RELEASE_FROZEN_SEED_MISMATCH")
            return frozen
        metrics, inbox = self._setup()
        customers, bindings, cases = [], [], []
        for tenant in ("tenant-a", "tenant-b"):
            customer, binding = self._customer(tenant, inbox)
            for spec in self.specs:
                if spec["tenant_id"] != tenant:
                    continue
                sub = self._subscription(spec, customer, metrics, inbox)
                customer["subscriptions"].append(sub)
                claimed = None if spec["lifecycle"] == "cancelled" else sub["period_end"] if spec["lifecycle"] == "terminated" else (
                    dt(sub["period_start"]) + timedelta(seconds=spec["claim_offset_seconds"])).isoformat()
                cases.append({"label": spec["label"], "tenant_id": tenant, "customer_id": customer["external_customer_id"],
                    "conversation_id": sub["conversation_id"], "customer_message_id": sub["customer_message_id"], "account_id": self.account,
                    "invoice_id": sub["invoice_id"], "subscription_id": sub["external_id"], "metric_code": metrics[0]["code"],
                    "period_start": sub["period_start"], "period_end": sub["period_end"], "claimed_at": claimed,
                    "original_message": sub["original_message_raw"], "original_message_ref": sub["original_message"],
                    "native_invoice_kind": sub["native_invoice_kind"], "native_object_origin": sub["native_object_origin"],
                    "native_usage_aligned": sub["native_usage_aligned"], "native_usage_metric_present": sub["native_usage_metric_present"],
                    "observed_units": sub["observed_units"], "usage_qualification": sub["usage_qualification"],
                    "contract_kind": spec["contract_kind"], "lifecycle": spec["lifecycle"]})
                self.checkpoint()
            customers.append(customer)
            bindings.append(binding)
        value = {"schema_version": 1, "status": "SEEDED_REAL_APIS", "run_id": self.run_id, "created_at": self.state["created_at"],
            "metric_code": metrics[0]["code"], "distractor_metric_code": metrics[1]["code"], "plan_code": self.run_id + "_monthly",
            "chatwoot_account_id": self.account, "chatwoot_inbox_id": inbox["id"], "customers": customers,
            "bindings": {"schema_version": 1, "customers": bindings, "cases": cases}, "scenario_specs": self.specs,
            "history_reuse": self.state.get("history_reuse"),
            "qualification": "LOCAL_RELEASE_VERIFICATION_OBJECTS", "limitations": ["SYNTHETIC_CUSTOMERS", "NO_REAL_PAYMENT", "INITIAL_INVOICE_NOT_NATIVE_RENEWAL"]}
        save_private(self.output, value)
        save_private(self.output.with_name("bindings.json"), value["bindings"])
        self.checkpoint()
        return value


class SourceObjects(BusinessPack):
    """冻结原生对象→商业事实；late_usage的发送故意保留为独立可执行阶段。"""
    def __init__(self, directory: Path):
        super().__init__(directory)
        self.specs = {s["label"]: s for s in scenario_specs(self.native["scenario_specs"])}

    def plan(self, label: str, customer: dict, sub: dict) -> dict:
        if label in self.state["plans"]:
            return self.state["plans"][label]
        spec = self.specs[label]
        start, end = dt(sub["period_start"]), dt(sub["period_end"])
        claim = start + timedelta(seconds=spec["claim_offset_seconds"])
        if spec["lifecycle"] == "terminated":
            claim = end
        if spec["lifecycle"] != "cancelled" and not start <= claim <= end:
            raise SeedError("RELEASE_CLAIM_OUTSIDE_NATIVE_PERIOD")
        native_event_time = datetime.now(timezone.utc).replace(microsecond=0)
        if spec["usage_events"] and not start <= native_event_time <= end:
            raise SeedError("RELEASE_EVENT_OUTSIDE_NATIVE_PERIOD")
        events = [{"event_id": self.native["run_id"] + "_" + label + "_" + row["name"], "units": row["units"],
                   "occurred_at": (start + timedelta(seconds=row["business_offset_seconds"])).isoformat(),
                   "native_occurred_at": native_event_time.isoformat()} for row in spec["usage_events"]]
        plan = {"label": label, "tenant_id": customer["tenant_id"], "customer_id": customer["external_customer_id"],
            "subscription_id": sub["external_id"], "invoice_id": sub["invoice_id"], "account_id": self.native["chatwoot_account_id"],
            "conversation_id": sub["conversation_id"], "start": start.isoformat(), "end": end.isoformat(),
            "claimed_at": None if spec["lifecycle"] == "cancelled" else claim.isoformat(), "spec": spec,
            "invoice_receipt": sub["invoice_receipt"], "usage": sub["usage"], "usage_receipt": sub["usage_receipt"],
            "native_invoice_kind": sub["native_invoice_kind"], "original_message": sub["original_message"], "expected_events": events,
            "native_usage_aligned": sub.get("native_usage_aligned", True),
            "contract_id": self.native["run_id"] + "-contract-" + label,
            "claim_id": self.native["run_id"] + "-claim-" + label, "coverage_id": self.native["run_id"] + "-coverage-" + label}
        self.state["plans"][label] = plan
        self.save()
        return plan

    def prepare_case(self, label: str, p: dict):
        scope = {k: p[k] for k in ("tenant_id", "customer_id", "subscription_id", "invoice_id")}
        spec, start, end = p["spec"], dt(p["start"]), dt(p["end"])
        duration = end - start + timedelta(seconds=1)
        variant = "incomplete" if spec["contract_kind"] == "draft" else "special" if spec["contract_kind"] == "special" else "standard"
        fields = {**p, "version": "lab-v1", "effective_from": (start - duration).isoformat(),
                  "effective_to": p["claimed_at"] if spec["contract_kind"] == "expired" else "2027-12-31T00:00:00Z",
                  "status": "DRAFT" if spec["contract_kind"] == "draft" else "ACTIVE"}
        path = ROOT / f"business-source-pack/v1/contracts/{variant}.md"
        text = Template(path.read_text()).substitute(fields)
        hashed = hashlib.sha256(text.encode()).hexdigest()
        contract = {k: fields[k] for k in ("tenant_id", "customer_id", "subscription_id", "contract_id", "version", "effective_from", "effective_to", "status")}
        contract.update(review_status="LOCAL_CALIBRATION", human_review_status="PENDING_USER_REVIEW",
            policy_ref={"policy_id": "refund-standard", "version": None},
            terms={"refund_addendum": None if variant == "incomplete" else "EGAP-LAB-SPECIAL-1" if variant == "special" else "EGAP-LAB-REFUND-1",
                   "special_terms": variant == "special", "policy_selection": "RENEWAL_EFFECTIVE_AT"},
            source_document={"reference": str(path.relative_to(ROOT)) + "#" + p["contract_id"], "text": text,
                             "sha256": hashed, "origin": "SYNTHETIC_WITH_CC_BY_4_COMMON_PAPER_2_1"},
            structured_source_sha256=hashed,
            acceptance=None if variant == "incomplete" else {"kind": "SIMULATED_ACCEPTANCE", "accepted_at": fields["effective_from"]})
        self.command(label + ":contract", "put_contract", contract)
        term_start, term_end = ((start, end) if spec["lifecycle"] == "terminated" else (start - duration, start - timedelta(seconds=1)))
        self.command(label + ":activate", "activate_subscription", {**scope, "contract_ref": {"contract_id": p["contract_id"], "version": "lab-v1"},
            "period_start": term_start.isoformat(), "period_end": term_end.isoformat(), "renewal_enabled": True})
        if spec["lifecycle"] == "cancelled":
            self.command(label + ":cancel", "cancel_renewal", {**scope, "at": term_start.isoformat()})
            self.command(label + ":cross-period", "advance_clock", {**scope, "to": p["start"]})
            # 没有第二期事件就不能把第二期原生发票挂给第一期；claim/coverage也不伪造。
            return
        if spec["lifecycle"] == "terminated":
            self.command(label + ":termination-clock", "advance_clock", {**scope, "to": p["end"]})
            transition = self.command(label + ":terminate", "terminate_subscription", {**scope, "at": p["end"]})
            event = transition["events"][0]
        else:
            transition = self.command(label + ":renew", "advance_clock", {**scope, "to": p["start"]})
            event = next(e for e in transition["events"] if e["event_type"] == "SUBSCRIPTION_RENEWAL_EFFECTIVE")
        self.command(label + ":invoice", "link_invoice", {**scope, "event_id": event["event_id"], "native_invoice_kind": p["native_invoice_kind"],
            "native_period_start": p["start"], "native_period_end": p["end"], "source_receipt": p["invoice_receipt"]})
        self.command(label + ":claim-clock", "advance_clock", {**scope, "to": p["claimed_at"]})
        self.command(label + ":claim", "create_claim", {**scope, "claim_id": p["claim_id"], "account_id": p["account_id"],
            "conversation_id": p["conversation_id"], "original_message": p["original_message"]})
        if spec["lifecycle"] == "terminated":
            # 原生终止账单/用量可能存在秒级端点差异。这个负例只观察终止事件，
            # 不重写端点、不封存用量，也不补付款把终止结算包装成续费资格样本。
            return
        self.command(label + ":coverage", "open_coverage", {**scope, "coverage_id": p["coverage_id"], "metric_code": self.native["metric_code"],
            "unit": "units", "period_start": p["start"], "period_end": p["claimed_at"], "expected_events": p["expected_events"]})
        if not p["expected_events"]:
            self._reconcile_and_seal(label, p, p["usage"], p["usage_receipt"], "initial")
        if spec["payment"] != "MISSING":
            self.payment(label, p)
        if spec["send_during_prepare"]:
            self.send_usage(label)

    def _reconcile_and_seal(self, label, p, raw, receipt, stage):
        self.command(label + ":reconcile:" + stage, "reconcile_coverage", {"tenant_id": p["tenant_id"], "coverage_id": p["coverage_id"],
            "raw_response": raw, "receipt": receipt})
        self.command(label + ":seal:" + stage, "seal_coverage", {"tenant_id": p["tenant_id"], "coverage_id": p["coverage_id"]})

    def payment(self, label: str, p: dict | None = None):
        p = p or self.state["plans"][label]
        invoice, spec = p["invoice_receipt"]["invoice"], p["spec"]
        difference = 1 if spec["payment"] == "OVERPAID" else -1 if spec["payment"] == "UNDERPAID" else 0
        return self.command(label + ":payment", "record_payment", {**{k: p[k] for k in ("tenant_id", "customer_id", "subscription_id", "invoice_id")},
            "payment_id": self.native["run_id"] + "-payment-" + label, "source_kind": "SIMULATED_PAYMENT", "authority_scope": "LOCAL_LAB", "status": "PAID",
            "amount_minor": invoice["total_amount_cents"] + difference, "currency": spec["payment_currency"] or invoice["currency"],
            "paid_at": p["start"], "receipt_ref": "local-simulated-payment:" + p["invoice_id"]})

    def send_usage(self, label: str) -> dict:
        """真实晚到路径：预登记OPEN集合→Lago POST→聚合读回→接收核对→SEALED。

        业务发生时刻与预登记的原生事件时间显式分列；HTTP观察记录另存实际发送时间。
        不修改宿主时钟。Lago POST的未知回执
        由NativeObjects.write_once阻断，不能因为source支持幂等就重发原生未知写。
        """
        p = self.state["plans"][label]
        events = p["expected_events"]
        if not events:
            raise SeedError("RELEASE_EXPECTED_EVENT_SET_EMPTY")
        if label + ":coverage" not in self.state["commands"] or self.state["commands"][label + ":coverage"]["state"] != "COMPLETE":
            raise SeedError("RELEASE_USAGE_NOT_PREDECLARED")
        writer = NativeObjects(self.runtime, self.directory / "native/seed.json", specs=self.native["scenario_specs"])
        for event in events:
            request = {"event": {"transaction_id": event["event_id"], "external_subscription_id": p["subscription_id"],
                "code": self.native["metric_code"], "timestamp": int(dt(event["native_occurred_at"]).timestamp()), "properties": {"units": event["units"]}}}
            response = writer.write_once("release-usage:" + event["event_id"], writer.lago, "POST", "/api/v1/events", request)
            actual = response.get("event") or {}
            if actual.get("transaction_id") != event["event_id"] or actual.get("external_subscription_id") != p["subscription_id"] \
                    or actual.get("code") != self.native["metric_code"] or Decimal(str((actual.get("properties") or {}).get("units"))) != Decimal(event["units"]) \
                    or dt(actual["timestamp"]) != dt(event["native_occurred_at"]):
                raise SeedError("RELEASE_NATIVE_EVENT_RECEIPT_MISMATCH")
            sent = next((r for r in reversed(writer.state["receipts"]) if r.get("service") == "lago" and r.get("method") == "POST"
                and r.get("path") == "/api/v1/events" and r.get("request_sha256") == sha(request) and r.get("status") in {200, 201, 202}), None)
            if sent is None:
                raise SeedError("RELEASE_NATIVE_EVENT_TRANSPORT_RECEIPT_MISSING")
            receipt = {"service": "lago", "method": "POST", "path": "/api/v1/events", "response_sha256": sha(response),
                       "observed_at": sent["observed_at"], "status": sent["status"], "request_sha256": sent["request_sha256"],
                       "transaction_id": event["event_id"], "external_subscription_id": p["subscription_id"], "native_event": actual}
            self.command(label + ":sent:" + event["event_id"], "record_usage_sent", {"tenant_id": p["tenant_id"],
                "coverage_id": p["coverage_id"], "event_id": event["event_id"], "receipt": receipt})
        completion = self.state.setdefault("usage_completions", {}).get(label)
        if completion is None:
            query = {"external_subscription_id": p["subscription_id"]}
            path = "/api/v1/customers/" + p["customer_id"] + "/current_usage"
            total = sum((Decimal(e["units"]) for e in events), Decimal(0))
            body = writer.poll(lambda: writer.lago.request("GET", path + "?" + urlencode(query)),
                               lambda value: usage_units(value["customer_usage"], self.native["metric_code"]) == total, label=label + "_actual_event")
            raw = body["customer_usage"]
            if (dt(raw["from_datetime"]), dt(raw["to_datetime"])) != (dt(p["start"]), dt(p["end"])):
                raise SeedError("RELEASE_NATIVE_PERIOD_NOT_ALIGNED")
            completion = {"raw": raw, "receipt": {**writer.state["receipts"][-1], "path": path, "query": query,
                "scope_kind": "SUBSCRIPTION_CURRENT_PERIOD"}, "completed_at": datetime.now(timezone.utc).isoformat()}
            self.state["usage_completions"][label] = completion
            self.save()
        for event in events:
            self.command(label + ":received:" + event["event_id"], "record_usage_received", {"tenant_id": p["tenant_id"],
                "coverage_id": p["coverage_id"], "event_id": event["event_id"],
                "receipt": {**completion["receipt"], "transaction_id": event["event_id"], "bounded_event_set": [e["event_id"] for e in events]}})
        self._reconcile_and_seal(label, p, completion["raw"], completion["receipt"], "after-events")
        return completion

    def supplement(self, label: str, text: str = "补充说明：请保留原申请时间，核对我补充的资料。") -> dict:
        """追加真实incoming与来源引用，不覆盖原claim；供显式refresh链路调用。"""
        p = self.state["plans"][label]
        writer = NativeObjects(self.runtime, self.directory / "native/seed.json", specs=self.native["scenario_specs"])
        message = writer.write_once(label + ":supplement", writer.chatwoot, "POST", self.prefix + f"/conversations/{p['conversation_id']}/messages",
            {"content": text, "message_type": "incoming", "private": False})
        return self.command(label + ":supplement", "supplement_claim", {"tenant_id": p["tenant_id"], "claim_id": p["claim_id"], "message": message_ref(message)})

    def prepare(self):
        for customer in self.native["customers"]:
            for version in ("lab-v1", "lab-v2"):
                self.policy(customer["tenant_id"], version)
            for sub in customer["subscriptions"]:
                self.prepare_case(sub["label"], self.plan(sub["label"], customer, sub))
        self.state["status"] = "PREPARED_RELEASE_OBJECTS"
        self.save()
        return self.state


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("native", "source", "send-usage", "supplement"))
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, default=ROOT / "artifacts/multi-system/runtime.json")
    parser.add_argument("--run-id")
    parser.add_argument("--reuse-history-from", type=Path)
    parser.add_argument("--label")
    args = parser.parse_args()
    try:
        if args.command == "native":
            value = NativeObjects(load_runtime(args.runtime), args.directory / "native/seed.json", run_id=args.run_id,
                                  reuse_history_from=args.reuse_history_from).prepare()
            result = {"status": value["status"], "run_id": value["run_id"], "cases": len(value["bindings"]["cases"])}
        else:
            pack = SourceObjects(args.directory)
            if args.command == "source":
                value = pack.prepare()
                result = {"status": value["status"], "cases": len(value["plans"]), "commands": len(value["commands"])}
            elif args.command == "send-usage":
                if not args.label:
                    raise SeedError("RELEASE_LABEL_REQUIRED")
                pack.send_usage(args.label)
                result = {"status": "USAGE_RECONCILED_AND_SEALED", "label": args.label}
            else:
                if not args.label:
                    raise SeedError("RELEASE_LABEL_REQUIRED")
                claim = pack.supplement(args.label)
                result = {"status": "CLAIM_SUPPLEMENTED", "claim_id": claim["claim_id"], "revision": claim["revision"]}
        print(json.dumps(result))
        return 0
    except (SeedError, KeyError, ValueError, TypeError, OSError, StopIteration) as error:
        print(json.dumps({"status": "FAILED", "error_code": str(error) if isinstance(error, SeedError) else "RELEASE_OBJECT_PREPARATION_INVALID"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

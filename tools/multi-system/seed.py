#!/usr/bin/env python3
"""真实产品 API seed：main → Seed.run → Lago / Chatwoot → 原生对象与回执。

只允许显式本机测试实例。业务对象从原生 API 创建，绝不写第三方数据库；
每个写操作先登记再发请求，回执不明时停止并要求对账，不能盲目重放 POST。
此工具产生的是合成客户的真实产品对象，不代表真实企业资料或团队采用。
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RUNTIME = ROOT / "artifacts/multi-system/runtime.json"
DEFAULT_OUTPUT = ROOT / "artifacts/multi-system/seed.json"


class SeedError(RuntimeError):
    """公开错误仅包含固定错误码；HTTP 原文保存在本机私有实验目录。"""


def sha(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def save_private(path: Path, value: object) -> None:
    """原子更新检查点，保留 0600；崩溃不会将半份 JSON 当作成功回执。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def load_runtime(path: Path) -> dict:
    if not path.is_file() or path.is_symlink() or path.stat().st_mode & 0o077:
        raise SeedError("ENV_NOT_READY_PRIVATE_RUNTIME_REQUIRED")
    return json.loads(path.read_text())


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


class Api:
    """有界本机 HTTP，不继承代理，不跟随重定向，不自动重试任何请求。"""

    def __init__(self, base_url: str, headers: dict, *, service: str, receipts: list) -> None:
        parsed = urlsplit(base_url)
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port \
                or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
            raise SeedError("LOCAL_TEST_ENDPOINT_REQUIRED")
        self.base_url, self.headers, self.service, self.receipts = base_url.rstrip("/"), headers, service, receipts
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def request(self, method: str, path: str, body: dict | None = None, *, expected=(200, 201, 202, 204)) -> dict:
        if not path.startswith("/") or path.startswith("//"):
            raise SeedError("INVALID_API_PATH")
        encoded = None if body is None else json.dumps(body).encode()
        request = Request(self.base_url + path, data=encoded,
                          headers={"Accept": "application/json", "Content-Type": "application/json", **self.headers}, method=method)
        receipt = {"service": self.service, "method": method, "path": path,
                   "request_sha256": sha(body), "observed_at": datetime.now(timezone.utc).isoformat()}
        try:
            with self.opener.open(request, timeout=20) as response:
                status, raw = response.status, response.read(2_000_001)
        except HTTPError as error:
            status, raw = error.code, error.read(2_000_001)
            error.close()
        except (URLError, OSError, TimeoutError):
            receipt["outcome"] = "UNKNOWN" if method != "GET" else "UNAVAILABLE"
            self.receipts.append(receipt)
            raise SeedError("WRITE_RECEIPT_UNKNOWN" if method != "GET" else "ENV_NOT_READY") from None
        if len(raw) > 2_000_000:
            raise SeedError("API_RESPONSE_TOO_LARGE")
        try:
            value = json.loads(raw) if raw else {}
        except (ValueError, UnicodeError):
            raise SeedError("API_RESPONSE_INVALID") from None
        receipt.update(status=status, response_sha256=sha(value), outcome="OBSERVED")
        self.receipts.append(receipt)
        if status not in expected:
            # 供开发对账的响应不含请求 header；只留在被 gitignore 的 0600 文件中。
            receipt["error_response"] = value
            raise SeedError(f"{self.service.upper()}_HTTP_{status}")
        return value


def usage_units(usage: dict, metric_code: str) -> Decimal:
    """要求指标存在；空列表不能解释成真实零用量。"""
    rows = [row for row in usage.get("charges_usage", []) if row.get("billable_metric", {}).get("code") == metric_code]
    if len(rows) != 1:
        raise SeedError("USAGE_METRIC_MISSING_OR_AMBIGUOUS")
    return Decimal(str(rows[0]["units"]))


class Seed:
    def __init__(self, config: dict, output: Path, *, run_id: str | None = None) -> None:
        self.config, self.output = config, output
        self.state_path = output.with_name("seed-state.json")
        if self.state_path.exists():
            self.state = json.loads(self.state_path.read_text())
            if run_id and run_id != self.state["run_id"]:
                raise SeedError("EXISTING_SEED_RUN_MISMATCH")
        else:
            actual = run_id or "egap" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
            if not re.fullmatch(r"[a-z0-9_-]{3,40}", actual):
                raise SeedError("INVALID_RUN_ID")
            self.state = {"schema_version": 1, "run_id": actual, "operations": {}, "receipts": [],
                          "created_at": datetime.now(timezone.utc).isoformat()}
        self.run_id = self.state["run_id"]
        cw, lago = config["chatwoot"], config["lago"]
        self.chatwoot = Api(cw["base_url"], {"api_access_token": cw["api_token"]}, service="chatwoot", receipts=self.state["receipts"])
        self.lago = Api(lago["base_url"], {"Authorization": "Bearer " + lago["api_key"]}, service="lago", receipts=self.state["receipts"])
        self.account = int(cw["account_id"])
        self.cw_prefix = f"/api/v1/accounts/{self.account}"
        identity = {"chatwoot": cw["base_url"], "account": self.account, "lago": lago["base_url"],
                    "realm": config["keycloak"]["realm"]}
        if "environment_sha256" in self.state and self.state["environment_sha256"] != sha(identity):
            raise SeedError("SEED_ENVIRONMENT_MISMATCH")
        self.state["environment_sha256"] = sha(identity)
        self.checkpoint()

    def checkpoint(self) -> None:
        save_private(self.state_path, self.state)

    def write_once(self, name: str, api: Api, method: str, path: str, body: dict | None = None) -> dict:
        """已确认回执才可复用；未知写不得靠 rerun 再发一次。

        失败 4xx 是明确拒绝，可以在修正 seed 后重新提交；5xx/网络中断保留 UNKNOWN。
        进程在成功返回与落盘之间退出同样 UNKNOWN，必须先在产品 API 对账。
        """
        prior = self.state["operations"].get(name)
        request_identity = sha({"service": api.service, "method": method, "path": path, "body": body})
        if prior:
            if prior["state"] == "COMPLETE":
                if prior["request_sha256"] != request_identity:
                    raise SeedError("SEED_OPERATION_PAYLOAD_CHANGED")
                return prior["response"]
            if prior["state"] != "REJECTED":
                raise SeedError("SEED_OPERATION_REQUIRES_RECONCILIATION")
        operation = {"state": "REQUESTED", "request_sha256": request_identity, "service": api.service, "path": path}
        self.state["operations"][name] = operation
        self.checkpoint()
        before_receipts = len(self.state["receipts"])
        try:
            result = api.request(method, path, body)
        except SeedError as error:
            last = self.state["receipts"][-1] if len(self.state["receipts"]) > before_receipts else {}
            # 只能采用本次请求的明确4xx；上一次查询的404不能证明本次写没有生效。
            rejected = last.get("service") == api.service and last.get("method") == method and last.get("path") == path \
                and 400 <= last.get("status", 0) < 500
            operation["state"] = "REJECTED" if rejected else "UNKNOWN"
            operation["error_code"] = str(error)
            self.checkpoint()
            raise
        operation.update(state="COMPLETE", response=result)
        self.checkpoint()
        return result

    def poll(self, reader, predicate, *, label: str, timeout: float = 120):
        """只轮询只读 API 等待第三方 worker，时限耗尽明确失败。"""
        end = time.monotonic() + timeout
        while True:
            value = reader()
            self.checkpoint()
            if predicate(value):
                return value
            if time.monotonic() >= end:
                raise SeedError("ASYNC_PRODUCT_NOT_READY_" + label.upper())
            time.sleep(2)

    def subscription_invoice(self, customer_id: str, subscription_id: str) -> dict | None:
        """Lago 列表是摘要，subscription/fees 只在详情；不可把缺字段当没有开票。"""
        listing = self.lago.request("GET", "/api/v1/invoices?" + urlencode({"external_customer_id": customer_id, "per_page": 100}))
        if listing.get("meta", {}).get("next_page"):
            raise SeedError("SEED_INVOICE_PAGINATION_REQUIRES_EXTENSION")
        for item in listing["invoices"]:
            invoice = self.lago.request("GET", "/api/v1/invoices/" + item["lago_id"])["invoice"]
            if any(sub.get("external_id") == subscription_id for sub in invoice.get("subscriptions", [])):
                return invoice
        return None

    def historical_sample(self, customer: dict, metric: dict, inbox: dict) -> dict:
        """用原生订阅生命周期关闭期间，再由 Lago 开票及 past_usage 证明历史范围。

        单纯回填事件时间不能制造历史账单。因此本实验创建按期末计费的独立订阅，
        先通过事件 API 计量并读回，再终止该测试订阅，等待官方开票任务完成。
        这是已关闭的终止账期，不冒称自然月续费样本，亦不修改产品数据库或时钟。
        """
        external = customer["external_customer_id"]
        external_sub, plan_code = self.run_id + "_a_closed", self.run_id + "_arrears"
        started = datetime.fromisoformat(self.state["created_at"]).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=2)
        self.write_once("historical_plan", self.lago, "POST", "/api/v1/plans", {"plan": {
            "name": "EGAP synthetic closed-period", "code": plan_code, "interval": "monthly", "amount_cents": 12000,
            "amount_currency": "USD", "pay_in_advance": False,
            "charges": [{"billable_metric_id": metric["lago_id"], "charge_model": "standard",
                         "pay_in_advance": False, "properties": {"amount": "1"}}]}})
        subscription = self.write_once("historical_subscription", self.lago, "POST", "/api/v1/subscriptions", {"subscription": {
            "external_customer_id": external, "external_id": external_sub, "plan_code": plan_code,
            "name": "closed-period", "billing_time": "anniversary", "consolidate_invoice": False,
            "subscription_at": started.isoformat()}})["subscription"]
        self.write_once("historical_event", self.lago, "POST", "/api/v1/events", {"event": {
            "transaction_id": self.run_id + "_historical_event", "external_subscription_id": external_sub,
            "code": metric["code"], "timestamp": int((started + timedelta(hours=12)).timestamp()), "properties": {"units": 11}}})
        if "historical_terminate" not in self.state["operations"]:
            self.poll(lambda: self.lago.request("GET", "/api/v1/customers/" + external + "/current_usage?" + urlencode({"external_subscription_id": external_sub})),
                      lambda value: usage_units(value["customer_usage"], metric["code"]) == 11, label="historical_event")
        self.write_once("historical_terminate", self.lago, "DELETE", "/api/v1/subscriptions/" + external_sub + "?on_termination_invoice=generate")
        invoice = self.poll(lambda: self.subscription_invoice(external, external_sub), lambda value: value is not None, label="historical_invoice")
        past = self.poll(lambda: self.lago.request("GET", "/api/v1/customers/" + external + "/past_usage?" + urlencode({"external_subscription_id": external_sub})),
                         lambda value: any(period.get("lago_invoice_id") == invoice["lago_id"] for period in value.get("usage_periods", [])), label="past_usage")
        period = next(row for row in past["usage_periods"] if row.get("lago_invoice_id") == invoice["lago_id"])
        if usage_units(period, metric["code"]) != 11:
            raise SeedError("CLOSED_PERIOD_USAGE_MISMATCH")
        contact = self.state["operations"]["contact_a"]["response"]
        contact = contact.get("payload", {}).get("contact", contact.get("payload", contact))
        source = next(row["source_id"] for row in contact["contact_inboxes"] if row["inbox"]["id"] == inbox["id"])
        conversation = self.write_once("historical_conversation", self.chatwoot, "POST", self.cw_prefix + "/conversations", {
            "source_id": source, "contact_id": customer["chatwoot_contact_id"], "inbox_id": inbox["id"], "status": "open"})
        message = self.write_once("historical_message", self.chatwoot, "POST", self.cw_prefix + f"/conversations/{conversation['id']}/messages", {
            "content": "请审核这张已关闭订阅期间账单的争议：" + invoice["lago_id"], "message_type": "incoming", "private": False})
        return {"status": "REAL_CLOSED_TERMINATION_PERIOD", "tenant_id": "tenant-a", "external_customer_id": external,
                "external_id": external_sub, "lago_id": subscription["lago_id"], "invoice_id": invoice["lago_id"],
                "conversation_id": conversation["id"], "customer_message_id": message["id"], "expected_seed_units": 11,
                "observed_units": str(usage_units(period, metric["code"])), "past_usage": period, "invoice": invoice,
                "subscription": subscription, "qualification": "TERMINATION_NOT_NATURAL_RENEWAL"}

    def run(self) -> dict:
        """metric → plan → customers → subscriptions/events → invoices → conversations。

        每份 invoice 来自订阅原生开票任务；created_at/issuing_date 从不冒充 renewed_at。
        本批种子不接支付提供商，不标记付款成功，不退款，不发送对客消息。
        """
        self.chatwoot.request("GET", self.cw_prefix + "/inboxes")
        self.lago.request("GET", "/api/v1/customers?per_page=1")
        metric_code, plan_code = self.run_id + "_units", self.run_id + "_monthly"
        metric = self.write_once("metric", self.lago, "POST", "/api/v1/billable_metrics", {"billable_metric": {
            "name": "EGAP synthetic API units", "code": metric_code, "aggregation_type": "sum_agg", "field_name": "units"}})["billable_metric"]
        self.write_once("plan", self.lago, "POST", "/api/v1/plans", {"plan": {
            "name": "EGAP synthetic monthly", "code": plan_code, "interval": "monthly", "amount_cents": 12000,
            "amount_currency": "USD", "pay_in_advance": True,
            "charges": [{"billable_metric_id": metric["lago_id"], "charge_model": "standard",
                         "pay_in_advance": False, "properties": {"amount": "1"}}]}})
        inbox = self.write_once("inbox", self.chatwoot, "POST", self.cw_prefix + "/inboxes", {
            "name": self.run_id + " support", "channel": {"type": "api"}, "enable_auto_assignment": False})
        customers, bindings, cases = [], [], []
        for letter in ("a", "b"):
            external = self.run_id + "_customer_" + letter
            customer = self.write_once("customer_" + letter, self.lago, "POST", "/api/v1/customers", {"customer": {
                "external_id": external, "name": "EGAP synthetic customer " + letter.upper(),
                "currency": "USD", "timezone": "UTC", "email": external + "@example.invalid"}})["customer"]
            contact_response = self.write_once("contact_" + letter, self.chatwoot, "POST", self.cw_prefix + "/contacts", {
                "inbox_id": inbox["id"], "name": "EGAP synthetic customer " + letter.upper(),
                "identifier": external, "email": external + "@example.invalid"})
            contact = contact_response.get("payload", {}).get("contact", contact_response.get("payload", contact_response))
            contact_id = int(contact["id"])
            contact_inboxes = contact.get("contact_inboxes", [])
            bound = next((item for item in contact_inboxes if item.get("inbox", {}).get("id") == inbox["id"]), None)
            if bound is None:
                bound = self.write_once("contact_inbox_" + letter, self.chatwoot, "POST",
                    self.cw_prefix + f"/contacts/{contact_id}/contact_inboxes", {"inbox_id": inbox["id"], "source_id": external})
            source_id = bound["source_id"]
            row = {"tenant_id": "tenant-" + letter, "external_customer_id": external, "lago_customer_id": customer["lago_id"],
                   "chatwoot_contact_id": contact_id, "subscriptions": []}
            for suffix, expected_units in (("zero", 0), ("used", 7)):
                external_sub = self.run_id + "_" + letter + "_" + suffix
                subscription = self.write_once("subscription_" + letter + "_" + suffix, self.lago, "POST", "/api/v1/subscriptions", {
                    "subscription": {"external_customer_id": external, "external_id": external_sub,
                                     "plan_code": plan_code, "name": suffix, "billing_time": "anniversary", "consolidate_invoice": False}})["subscription"]
                if expected_units:
                    self.write_once("event_" + letter + "_" + suffix, self.lago, "POST", "/api/v1/events", {"event": {
                        "transaction_id": self.run_id + "_event_" + letter + "_" + suffix,
                        "external_subscription_id": external_sub, "code": metric_code, "properties": {"units": expected_units}}})
                usage_path = "/api/v1/customers/" + external + "/current_usage?" + urlencode({"external_subscription_id": external_sub})
                current = self.poll(lambda: self.lago.request("GET", usage_path),
                    lambda value: usage_units(value["customer_usage"], metric_code) == expected_units, label="usage")
                invoice = self.poll(lambda: self.subscription_invoice(external, external_sub),
                                    lambda value: value is not None, label="invoice")
                conversation = self.write_once("conversation_" + letter + "_" + suffix, self.chatwoot, "POST", self.cw_prefix + "/conversations", {
                    "source_id": source_id, "contact_id": contact_id, "inbox_id": inbox["id"], "status": "open"})
                message = self.write_once("message_" + letter + "_" + suffix, self.chatwoot, "POST",
                    self.cw_prefix + f"/conversations/{conversation['id']}/messages", {
                        "content": "我要咨询这张订阅账单能否退款。测试账单：" + invoice["lago_id"], "message_type": "incoming", "private": False})
                readback = self.chatwoot.request("GET", self.cw_prefix + f"/conversations/{conversation['id']}")
                entry = {"external_id": external_sub, "lago_id": subscription["lago_id"], "invoice_id": invoice["lago_id"],
                         "conversation_id": conversation["id"], "customer_message_id": message["id"], "expected_seed_units": expected_units,
                         "observed_units": str(usage_units(current["customer_usage"], metric_code)), "current_usage": current["customer_usage"],
                         "invoice": invoice, "subscription": subscription, "conversation": readback}
                row["subscriptions"].append(entry)
                cases.append({"tenant_id": "tenant-" + letter, "conversation_id": conversation["id"], "invoice_id": invoice["lago_id"],
                              "subscription_id": external_sub, "label": letter + "_" + suffix})
            customers.append(row)
            subjects = self.config["keycloak"].get("operators", self.config["keycloak"].get("test_users", self.config.get("test_users", [])))
            if isinstance(subjects, dict):
                subjects = list(subjects.values())
            allowed = [user.get("subject", user.get("sub", user.get("id"))) for user in subjects if user.get("tenant_id") == "tenant-" + letter]
            bindings.append({"tenant_id": "tenant-" + letter, "chatwoot_account_id": self.account,
                             "chatwoot_contact_id": contact_id, "lago_customer_id": external,
                             "allowed_subjects": [subject for subject in allowed if subject]})
        historical = self.historical_sample(customers[0], metric, inbox)
        cases.append({"tenant_id": "tenant-a", "conversation_id": historical["conversation_id"], "invoice_id": historical["invoice_id"],
                      "subscription_id": historical["external_id"], "label": "a_closed"})
        result = {"schema_version": 1, "status": "SEEDED_REAL_APIS", "run_id": self.run_id,
                  "created_at": self.state["created_at"], "metric_code": metric_code, "plan_code": plan_code,
                  "chatwoot_account_id": self.account, "chatwoot_inbox_id": inbox["id"], "customers": customers,
                  "bindings": {"schema_version": 1, "customers": bindings, "cases": cases},
                  "historical_sample": historical,
                  "limitations": ["SYNTHETIC_CUSTOMERS", "NO_PAYMENT_PROVIDER", "USAGE_COMPLETENESS_NOT_CERTIFIED",
                                  "RENEWAL_TIME_NOT_INFERRED_FROM_INVOICE_DATE", "POLICY_AND_CONTRACT_NOT_ENTERPRISE_SOURCED"],
                  "receipt_count": len(self.state["receipts"]), "receipt_sha256": sha(self.state["receipts"])}
        save_private(self.output, result)
        save_private(self.output.with_name("bindings.json"), result["bindings"])
        self.checkpoint()
        return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", type=Path, default=DEFAULT_RUNTIME)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--run-id")
    args = parser.parse_args()
    seed = None
    try:
        seed = Seed(load_runtime(args.runtime), args.output, run_id=args.run_id)
        result = seed.run()
        print(json.dumps({"status": result["status"], "run_id": result["run_id"], "customers": len(result["customers"]),
                          "subscriptions": sum(len(row["subscriptions"]) for row in result["customers"]), "output": str(args.output)}))
        return 0
    except (SeedError, KeyError, ValueError, OSError) as error:
        if seed:
            seed.checkpoint()
        code = str(error) if isinstance(error, SeedError) else "SEED_INPUT_OR_PROTOCOL_INVALID"
        print(json.dumps({"status": "FAILED", "error_code": code}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

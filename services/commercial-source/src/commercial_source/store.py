"""独立 PostgreSQL schema 内的实验上游，不依赖 SUT 表或退款判定代码。

HTTP command → execute（租户行锁、幂等账本）→ 领域状态转换 → records + revision
在同一事务提交。GET context → SELECT 权限连接 → 同租户一致快照；读 token
没有命令路径。来源的“可信”仅指受控准备器与只读消费者分离，不代表企业采用。
"""

from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
import hashlib
from uuid import uuid4
from urllib.parse import quote

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .protocol import (AUTHORITY_SCOPE, SOURCE_KIND, SourceError, digest, identifier,
                       instant, now, number, parse_command, period, positive_int,
                       reference, source_document, timestamp)


MIGRATION = """
CREATE SCHEMA IF NOT EXISTS egap_commercial;
CREATE TABLE IF NOT EXISTS egap_commercial.tenants (
    tenant_id text PRIMARY KEY, source_revision bigint NOT NULL DEFAULT 0 CHECK(source_revision>=0)
);
CREATE TABLE IF NOT EXISTS egap_commercial.records (
    tenant_id text NOT NULL REFERENCES egap_commercial.tenants(tenant_id),
    kind text NOT NULL, object_id text NOT NULL, version text NOT NULL DEFAULT '',
    data jsonb NOT NULL, PRIMARY KEY(tenant_id, kind, object_id, version)
);
CREATE TABLE IF NOT EXISTS egap_commercial.operations (
    tenant_id text NOT NULL REFERENCES egap_commercial.tenants(tenant_id),
    operation_key text NOT NULL, request_sha256 text NOT NULL, response jsonb NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(tenant_id, operation_key)
);
"""


class CommercialStore:
    def __init__(self, database_url: str, read_database_url: str | None = None):
        self.database_url = database_url
        self.read_database_url = read_database_url or database_url

    def migrate(self) -> None:
        """只在宿主启动时由 writer 执行；reader 不参与 DDL 或自动修复。"""
        with psycopg.connect(self.database_url, connect_timeout=5) as conn:
            conn.execute(MIGRATION)

    def healthy(self) -> bool:
        with psycopg.connect(self.read_database_url, connect_timeout=3) as conn:
            conn.execute("SELECT source_revision FROM egap_commercial.tenants LIMIT 1")
        return True

    @staticmethod
    def _get(conn, tenant: str, kind: str, key: str, version: str = "", *, required=True) -> dict | None:
        row = conn.execute("SELECT data FROM egap_commercial.records WHERE tenant_id=%s AND kind=%s AND object_id=%s AND version=%s",
                           (tenant, kind, key, version)).fetchone()
        if row is None:
            if required:
                raise SourceError("SOURCE_OBJECT_NOT_FOUND", 404)
            return None
        return row["data"]

    @staticmethod
    def _put(conn, tenant: str, kind: str, key: str, value: dict, version: str = "") -> None:
        conn.execute("""INSERT INTO egap_commercial.records(tenant_id,kind,object_id,version,data)
            VALUES(%s,%s,%s,%s,%s) ON CONFLICT(tenant_id,kind,object_id,version)
            DO UPDATE SET data=excluded.data""", (tenant, kind, key, version, Jsonb(value)))

    @staticmethod
    def _all(conn, tenant: str, kind: str) -> list[dict]:
        return [row["data"] for row in conn.execute("SELECT data FROM egap_commercial.records WHERE tenant_id=%s AND kind=%s ORDER BY object_id,version", (tenant, kind)).fetchall()]

    def execute(self, command: dict) -> dict:
        """同 key 重试返回首次响应；不同 key 也不能改写已封存版本。

        租户行锁同时防止并行推进产生两个 term、同一 invoice 多 claim/coverage。
        SQL 异常交给 HTTP 层报告未知；客户端只能以原 operation_key 查询式重试，
        不因返回失败就认为事务必未提交。
        """
        command = parse_command(command)
        payload, key = command["payload"], command["operation_key"]
        tenant, request_hash = payload["tenant_id"], digest(command)
        with psycopg.connect(self.database_url, row_factory=dict_row, connect_timeout=5) as conn:
            conn.execute("INSERT INTO egap_commercial.tenants(tenant_id) VALUES(%s) ON CONFLICT DO NOTHING", (tenant,))
            row = conn.execute("SELECT source_revision FROM egap_commercial.tenants WHERE tenant_id=%s FOR UPDATE", (tenant,)).fetchone()
            existing = conn.execute("SELECT request_sha256,response FROM egap_commercial.operations WHERE tenant_id=%s AND operation_key=%s", (tenant, key)).fetchone()
            if existing:
                if existing["request_sha256"] != request_hash:
                    raise SourceError("OPERATION_KEY_CONFLICT", 409)
                return existing["response"]
            try:
                result, changed = self._apply(conn, tenant, command["type"], deepcopy(payload))
            except (KeyError, TypeError, IndexError, AttributeError, OverflowError):
                raise SourceError("INVALID_COMMAND_PAYLOAD") from None
            revision = row["source_revision"] + int(changed)
            if changed:
                conn.execute("UPDATE egap_commercial.tenants SET source_revision=%s WHERE tenant_id=%s", (revision, tenant))
            response = {"operation_key": key, "type": command["type"], "source_revision": revision, "result": result}
            conn.execute("INSERT INTO egap_commercial.operations(tenant_id,operation_key,request_sha256,response) VALUES(%s,%s,%s,%s)", (tenant, key, request_hash, Jsonb(response)))
            return response

    def _apply(self, conn, tenant: str, kind: str, p: dict) -> tuple[dict, bool]:
        handlers = {
            "put_policy": self._asset, "put_contract": self._asset,
            "activate_subscription": self._activate, "advance_clock": self._advance,
            "cancel_renewal": self._stop, "terminate_subscription": self._stop,
            "link_invoice": self._link, "create_claim": self._claim,
            "supplement_claim": self._supplement, "open_coverage": self._coverage,
            "record_usage_sent": self._usage, "record_usage_received": self._usage,
            "reconcile_coverage": self._reconcile, "seal_coverage": self._seal,
            "record_payment": self._payment,
        }
        return handlers[kind](conn, tenant, kind, p)

    def _asset(self, conn, tenant, kind, p):
        category = "policy" if kind == "put_policy" else "contract"
        key, version = identifier(p[category + "_id"]), identifier(p["version"])
        source_document(p)
        p["effective_from"] = instant(p["effective_from"])
        p["effective_to"] = instant(p["effective_to"]) if p.get("effective_to") else None
        if p["effective_to"] and timestamp(p["effective_to"]) <= timestamp(p["effective_from"]):
            raise SourceError("INVALID_EFFECTIVE_PERIOD")
        if not isinstance(p.get("terms"), dict):
            raise SourceError("STRUCTURED_TERMS_REQUIRED")
        if category == "contract":
            for field in ("customer_id", "subscription_id"):
                identifier(p[field])
            if p["status"] not in {"ACTIVE", "DRAFT", "EXPIRED"}:
                raise SourceError("INVALID_CONTRACT_STATUS")
            if p.get("policy_ref") is not None:
                reference(p["policy_ref"], "policy")
            acceptance = p.get("acceptance")
            if acceptance is not None:
                if acceptance.get("kind") != "SIMULATED_ACCEPTANCE":
                    raise SourceError("ONLY_SIMULATED_ACCEPTANCE_SUPPORTED")
                acceptance["accepted_at"] = instant(acceptance["accepted_at"])
        p.update(source_kind=SOURCE_KIND, authority_scope=AUTHORITY_SCOPE)
        previous = self._get(conn, tenant, category, key, version, required=False)
        if previous is not None:
            if digest(previous) != digest(p):
                raise SourceError("IMMUTABLE_ASSET_VERSION", 409)
            return previous, False
        self._put(conn, tenant, category, key, p, version)
        return p, True

    def _subscription(self, conn, tenant, p):
        return self._get(conn, tenant, "subscription", identifier(p["subscription_id"]))

    def _event(self, conn, tenant, subscription, event_type, effective_at):
        value = {"event_id": "evt_" + uuid4().hex, "event_type": event_type,
                 "source_kind": SOURCE_KIND, "authority_scope": AUTHORITY_SCOPE,
                 "customer_id": subscription["customer_id"], "subscription_id": subscription["subscription_id"],
                 "contract_ref": subscription["contract_ref"], "term_number": subscription["term_number"],
                 "effective_at": effective_at, "period_start": subscription["period_start"],
                 "period_end": subscription["period_end"], "interval": "CLOSED",
                 "recorded_at": now(), "invoice_refs": []}
        self._put(conn, tenant, "event", value["event_id"], value)
        return value

    def _activate(self, conn, tenant, kind, p):
        key, customer = identifier(p["subscription_id"]), identifier(p["customer_id"])
        if self._get(conn, tenant, "subscription", key, required=False):
            raise SourceError("SUBSCRIPTION_ALREADY_EXISTS", 409)
        contract_ref = reference(p["contract_ref"], "contract")
        contract = self._get(conn, tenant, "contract", contract_ref["contract_id"], contract_ref["version"])
        if (contract["customer_id"], contract["subscription_id"]) != (customer, key):
            raise SourceError("CONTRACT_SCOPE_MISMATCH", 409)
        start, end = period(p["period_start"], p["period_end"])
        if timestamp(start).microsecond or timestamp(end).microsecond:
            raise SourceError("WHOLE_SECOND_PERIOD_REQUIRED")
        seconds = int((timestamp(end) - timestamp(start)).total_seconds()) + 1
        if not 1 <= seconds <= 366 * 86400 or type(p["renewal_enabled"]) is not bool:
            raise SourceError("INVALID_SUBSCRIPTION_TERM")
        subscription = {"subscription_id": key, "customer_id": customer, "contract_ref": contract_ref,
                        "term_number": 1, "period_start": start, "period_end": end, "interval": "CLOSED",
                        "duration_seconds": seconds, "business_clock": start, "status": "ACTIVE",
                        "renewal_enabled": p["renewal_enabled"], "clock_kind": "LOCAL_SIMULATED_BUSINESS_CLOCK"}
        self._put(conn, tenant, "subscription", key, subscription)
        event = self._event(conn, tenant, subscription, "SUBSCRIPTION_ACTIVATED", start)
        return {"subscription": subscription, "events": [event]}, True

    def _advance(self, conn, tenant, kind, p):
        subscription = self._subscription(conn, tenant, p)
        target = timestamp(p["to"])
        previous = timestamp(subscription["business_clock"])
        if target < previous:
            raise SourceError("BUSINESS_CLOCK_CANNOT_REWIND", 409)
        if target == previous:
            return {"subscription": subscription, "events": []}, False
        events = []
        # 固定秒数周期，仅用于实验。不能称为 Lago 自然月调度；不修改宿主系统时钟。
        while target > timestamp(subscription["period_end"]) and subscription["status"] == "ACTIVE":
            if not subscription["renewal_enabled"]:
                subscription["status"] = "ENDED"
                break
            if len(events) == 120:
                raise SourceError("BUSINESS_CLOCK_ADVANCE_LIMIT", 409)
            start = timestamp(subscription["period_end"]) + timedelta(seconds=1)
            subscription["term_number"] += 1
            subscription["period_start"] = start.isoformat()
            subscription["period_end"] = (start + timedelta(seconds=subscription["duration_seconds"] - 1)).isoformat()
            events.append(self._event(conn, tenant, subscription, "SUBSCRIPTION_RENEWAL_EFFECTIVE", start.isoformat()))
        subscription["business_clock"] = target.isoformat()
        self._put(conn, tenant, "subscription", subscription["subscription_id"], subscription)
        return {"subscription": subscription, "events": events}, True

    def _stop(self, conn, tenant, kind, p):
        subscription = self._subscription(conn, tenant, p)
        # 先显式 advance_clock，再取消/终止，防止用未来时间跳过本应发生的续期。
        at = instant(p.get("at", subscription["business_clock"]))
        if at != subscription["business_clock"]:
            raise SourceError("STOP_MUST_USE_CURRENT_BUSINESS_CLOCK", 409)
        target = "SUBSCRIPTION_RENEWAL_CANCELLED" if kind == "cancel_renewal" else "SUBSCRIPTION_TERMINATED"
        if (kind == "cancel_renewal" and not subscription["renewal_enabled"]) or (kind == "terminate_subscription" and subscription["status"] == "TERMINATED"):
            return {"subscription": subscription, "events": []}, False
        subscription["renewal_enabled"] = False
        if kind == "terminate_subscription":
            subscription["status"] = "TERMINATED"
        self._put(conn, tenant, "subscription", subscription["subscription_id"], subscription)
        event = self._event(conn, tenant, subscription, target, at)
        return {"subscription": subscription, "events": [event]}, True

    def _link(self, conn, tenant, kind, p):
        invoice_id = identifier(p["invoice_id"])
        event = self._get(conn, tenant, "event", identifier(p["event_id"]))
        customer, subscription = identifier(p["customer_id"]), identifier(p["subscription_id"])
        if (event["customer_id"], event["subscription_id"]) != (customer, subscription):
            raise SourceError("EVENT_SCOPE_MISMATCH", 409)
        native_kind = p["native_invoice_kind"]
        if native_kind not in {"INITIAL", "RENEWAL", "TERMINATION"}:
            raise SourceError("INVALID_NATIVE_INVOICE_KIND")
        if native_kind == "TERMINATION" and event["event_type"] != "SUBSCRIPTION_TERMINATED":
            raise SourceError("TERMINATION_IS_NOT_RENEWAL", 409)
        span = period(p["native_period_start"], p["native_period_end"])
        if span != period(event["period_start"], event["period_end"]):
            raise SourceError("INVOICE_EVENT_PERIOD_MISMATCH", 409)
        receipt, invoice = p["source_receipt"], p["source_receipt"]["invoice"]
        self._read_receipt(receipt)
        subscriptions = invoice["subscriptions"]
        if invoice["lago_id"] != invoice_id or invoice["customer"]["external_id"] != customer or len(subscriptions) != 1 \
                or subscriptions[0]["external_id"] != subscription or subscriptions[0]["external_customer_id"] != customer:
            raise SourceError("NATIVE_INVOICE_SCOPE_MISMATCH", 409)
        related = [f for f in invoice["fees"] if f.get("external_subscription_id") == subscription]
        charges = [f for f in related if f.get("item", {}).get("type") == "charge"]
        selected = charges or [f for f in related if f.get("item", {}).get("type") == "subscription"]
        if not selected or {period(f["from_date"], f["to_date"]) for f in selected} != {span}:
            raise SourceError("NATIVE_INVOICE_FEE_PERIOD_MISMATCH", 409)
        value = {**p, "native_period_start": span[0], "native_period_end": span[1],
                 "business_period_start": event["period_start"], "business_period_end": event["period_end"],
                 "mapping_kind": "EXACT_PERIOD_LOCAL_BUSINESS_MAPPING", "native_renewal_verified": False,
                 "source_kind": SOURCE_KIND, "authority_scope": AUTHORITY_SCOPE,
                 "invoice_sha256": digest(invoice)}
        existing = self._get(conn, tenant, "invoice_link", invoice_id, required=False)
        if existing:
            if digest(existing) != digest(value):
                raise SourceError("IMMUTABLE_INVOICE_LINK", 409)
            return existing, False
        self._put(conn, tenant, "invoice_link", invoice_id, value)
        return value, True

    @staticmethod
    def _read_receipt(receipt):
        if not isinstance(receipt, dict) or receipt.get("method") != "GET" or receipt.get("status") != 200:
            raise SourceError("NATIVE_READ_RECEIPT_REQUIRED")
        identifier(receipt["path"])
        timestamp(receipt["observed_at"])

    def _invoice_scope(self, conn, tenant, p):
        link = self._get(conn, tenant, "invoice_link", identifier(p["invoice_id"]))
        if (link["customer_id"], link["subscription_id"]) != (p["customer_id"], p["subscription_id"]):
            raise SourceError("INVOICE_SCOPE_MISMATCH", 409)
        return link

    @staticmethod
    def _message(message):
        if not isinstance(message, dict):
            raise SourceError("MESSAGE_REFERENCE_REQUIRED")
        positive_int(message["message_id"])
        timestamp(message["created_at"])
        value = message["content_sha256"]
        if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise SourceError("INVALID_MESSAGE_DIGEST")
        return dict(message)

    def _claim(self, conn, tenant, kind, p):
        self._invoice_scope(conn, tenant, p)
        subscription = self._subscription(conn, tenant, p)
        key = identifier(p["claim_id"])
        account, conversation = positive_int(p["account_id"]), positive_int(p["conversation_id"])
        claimed = instant(p.get("claimed_at", subscription["business_clock"]))
        if claimed != subscription["business_clock"]:
            raise SourceError("CLAIM_MUST_USE_CURRENT_BUSINESS_CLOCK", 409)
        for existing in self._all(conn, tenant, "claim"):
            if existing["claim_id"] == key or (existing["invoice_id"], existing["account_id"], existing["conversation_id"]) == (p["invoice_id"], account, conversation):
                raise SourceError("CLAIM_ALREADY_EXISTS", 409)
        value = {**p, "claimed_at": claimed, "clock_kind": "LOCAL_SIMULATED_BUSINESS_CLOCK",
                 "original_message": self._message(p["original_message"]), "revision": 1,
                 "supplements": [], "recorded_at": now(), "source_kind": SOURCE_KIND, "authority_scope": AUTHORITY_SCOPE}
        self._put(conn, tenant, "claim", key, value)
        return value, True

    def _supplement(self, conn, tenant, kind, p):
        key = identifier(p["claim_id"])
        claim = self._get(conn, tenant, "claim", key)
        message = self._message(p["message"])
        for previous in [claim["original_message"], *claim["supplements"]]:
            if previous["message_id"] == message["message_id"]:
                if digest(previous) != digest(message):
                    raise SourceError("IMMUTABLE_MESSAGE_REFERENCE", 409)
                return claim, False
        claim["supplements"].append(message)
        claim["revision"] += 1
        self._put(conn, tenant, "claim", key, claim)
        return claim, True

    def _coverage(self, conn, tenant, kind, p):
        link = self._invoice_scope(conn, tenant, p)
        key = identifier(p["coverage_id"])
        if self._get(conn, tenant, "coverage", key, required=False) or any(c["invoice_id"] == p["invoice_id"] for c in self._all(conn, tenant, "coverage")):
            raise SourceError("COVERAGE_ALREADY_EXISTS", 409)
        start, end = period(p["period_start"], p["period_end"])
        if timestamp(start) < timestamp(link["business_period_start"]) or timestamp(end) > timestamp(link["business_period_end"]):
            raise SourceError("COVERAGE_OUTSIDE_LINKED_TERM", 409)
        identifier(p["metric_code"])
        identifier(p["unit"])
        expected = p["expected_events"]
        if not isinstance(expected, list) or len(expected) > 10000:
            raise SourceError("INVALID_EXPECTED_EVENT_SET")
        seen = set()
        for event in expected:
            event_id = identifier(event["event_id"])
            if event_id in seen:
                raise SourceError("DUPLICATE_EXPECTED_EVENT")
            seen.add(event_id)
            event["units"] = str(number(event["units"]))
            event["occurred_at"] = instant(event["occurred_at"])
            event["native_occurred_at"] = instant(event["native_occurred_at"])
            if not timestamp(link["native_period_start"]) <= timestamp(event["native_occurred_at"]) <= timestamp(link["native_period_end"]):
                raise SourceError("USAGE_EVENT_OUTSIDE_NATIVE_PERIOD")
        value = {**p, "period_start": start, "period_end": end, "interval": "CLOSED", "revision": 1,
                 "status": "OPEN", "completeness_scope": "LOCAL_CONTROLLED_GENERATOR_ONLY",
                 "source_kind": SOURCE_KIND, "authority_scope": AUTHORITY_SCOPE,
                 "sent_event_ids": [], "received_event_ids": [], "send_receipts": {}, "receive_receipts": {},
                 "pending_count": len(expected), "units": str(sum((number(e["units"]) for e in expected if timestamp(start) <= timestamp(e["occurred_at"]) <= timestamp(end)), Decimal(0))),
                 "native_expected_units": str(sum((number(e["units"]) for e in expected), Decimal(0))),
                 "reconciliation": None, "sealed_at": None}
        self._put(conn, tenant, "coverage", key, value)
        return value, True

    def _open(self, conn, tenant, p):
        coverage = self._get(conn, tenant, "coverage", identifier(p["coverage_id"]))
        if coverage["status"] != "OPEN":
            raise SourceError("COVERAGE_ALREADY_SEALED", 409)
        return coverage

    def _usage(self, conn, tenant, kind, p):
        value = self._open(conn, tenant, p)
        event_id = identifier(p["event_id"])
        if event_id not in {e["event_id"] for e in value["expected_events"]}:
            raise SourceError("UNEXPECTED_USAGE_EVENT", 409)
        if not isinstance(p["receipt"], dict) or not p["receipt"]:
            raise SourceError("USAGE_RECEIPT_REQUIRED")
        stage = "sent" if kind == "record_usage_sent" else "received"
        receipts = "send_receipts" if stage == "sent" else "receive_receipts"
        if stage == "received" and event_id not in value["sent_event_ids"]:
            raise SourceError("USAGE_NOT_SENT", 409)
        if event_id in value[stage + "_event_ids"]:
            if digest(value[receipts][event_id]) != digest(p["receipt"]):
                raise SourceError("USAGE_RECEIPT_CONFLICT", 409)
            return value, False
        value[stage + "_event_ids"].append(event_id)
        value[receipts][event_id] = p["receipt"]
        value["pending_count"] = len(value["expected_events"]) - len(value["received_event_ids"])
        value["revision"] += 1
        # 事件到达后必须重新观察原生聚合，旧的“零值”不能被后续 seal 复用。
        value["reconciliation"] = None
        self._put(conn, tenant, "coverage", value["coverage_id"], value)
        return value, True

    def _reconcile(self, conn, tenant, kind, p):
        value = self._open(conn, tenant, p)
        link = self._get(conn, tenant, "invoice_link", value["invoice_id"])
        raw, receipt = p["raw_response"], p["receipt"]
        self._read_receipt(receipt)
        if receipt.get("service") != "lago":
            raise SourceError("LAGO_RECEIPT_REQUIRED")
        if receipt.get("query", {}).get("external_subscription_id") != value["subscription_id"]:
            raise SourceError("LAGO_USAGE_SUBSCRIPTION_UNPROVEN", 409)
        # 部分原生 current_usage 没有 lago_invoice_id；只允许显式当前订阅范围，
        # 且真实 GET 路径/参数与已核对发票的费用期间完全一致，不给 raw 补造 ID。
        native_scope = receipt.get("scope_kind", "INVOICE_PERIOD")
        if native_scope == "SUBSCRIPTION_CURRENT_PERIOD":
            expected_path = "/api/v1/customers/" + quote(value["customer_id"], safe="") + "/current_usage"
            if receipt["path"] != expected_path or raw.get("lago_invoice_id") not in (None, value["invoice_id"]):
                raise SourceError("LAGO_CURRENT_USAGE_SCOPE_MISMATCH", 409)
        elif native_scope == "INVOICE_PERIOD":
            if raw.get("lago_invoice_id") != value["invoice_id"]:
                raise SourceError("LAGO_USAGE_SCOPE_MISMATCH", 409)
        else:
            raise SourceError("INVALID_LAGO_USAGE_SCOPE_KIND")
        if period(raw["from_datetime"], raw["to_datetime"]) != period(link["native_period_start"], link["native_period_end"]):
            raise SourceError("LAGO_USAGE_SCOPE_MISMATCH", 409)
        rows = [r for r in raw["charges_usage"] if r.get("billable_metric", {}).get("code") == value["metric_code"]]
        if len(rows) != 1:
            raise SourceError("LAGO_USAGE_METRIC_AMBIGUOUS", 409)
        aggregate = number(rows[0]["units"])
        value["reconciliation"] = {"matched": aggregate == number(value["native_expected_units"]),
            "lago_units": str(aggregate), "raw_response_sha256": digest(raw), "raw_response": raw, "receipt": receipt,
            "native_period_start": link["native_period_start"], "native_period_end": link["native_period_end"],
            "ledger_revision": value["revision"], "recorded_at": now()}
        value["revision"] += 1
        self._put(conn, tenant, "coverage", value["coverage_id"], value)
        return value, True

    def _seal(self, conn, tenant, kind, p):
        value = self._get(conn, tenant, "coverage", identifier(p["coverage_id"]))
        if value["status"] == "SEALED":
            return value, False
        expected = {e["event_id"] for e in value["expected_events"]}
        if expected != set(value["sent_event_ids"]) or expected != set(value["received_event_ids"]) or value["pending_count"] != 0:
            raise SourceError("USAGE_EVENTS_INCOMPLETE", 409)
        if not value["reconciliation"] or value["reconciliation"]["matched"] is not True:
            raise SourceError("NATIVE_AGGREGATE_UNRECONCILED", 409)
        value.update(status="SEALED", sealed_at=now(), revision=value["revision"] + 1)
        self._put(conn, tenant, "coverage", value["coverage_id"], value)
        return value, True

    def _payment(self, conn, tenant, kind, p):
        self._invoice_scope(conn, tenant, p)
        key = identifier(p["payment_id"])
        if p.get("source_kind") != "SIMULATED_PAYMENT" or p.get("authority_scope") != AUTHORITY_SCOPE or p["status"] != "PAID":
            raise SourceError("ONLY_LOCAL_SIMULATED_PAYMENT_SUPPORTED")
        if type(p["amount_minor"]) is not int or p["amount_minor"] < 0:
            raise SourceError("INVALID_PAYMENT_AMOUNT")
        if not isinstance(p["currency"], str) or len(p["currency"]) != 3 or not p["currency"].isupper():
            raise SourceError("INVALID_CURRENCY")
        p["paid_at"] = instant(p["paid_at"])
        identifier(p["receipt_ref"])
        previous = self._get(conn, tenant, "payment", key, required=False)
        if previous:
            if digest(previous) != digest(p):
                raise SourceError("IMMUTABLE_PAYMENT_FACT", 409)
            return previous, False
        if any(row["invoice_id"] == p["invoice_id"] for row in self._all(conn, tenant, "payment")):
            raise SourceError("PAYMENT_ALREADY_RECORDED", 409)
        self._put(conn, tenant, "payment", key, p)
        return p, True

    def context(self, scope: dict) -> dict:
        """只读 SELECT 快照；完整身份限制每个对象，不用案例名选择预期答案。

        REPEATABLE READ 让 revision 与所有事实属于同一次数据库快照。上下文可以
        明确返回缺失 null；若已有 subscription/invoice 的归属相冲突则直接拒绝。
        """
        for field in ("tenant_id", "customer_id", "subscription_id", "invoice_id"):
            identifier(scope[field])
        for field in ("account_id", "conversation_id"):
            positive_int(scope[field])
        tenant = scope["tenant_id"]
        with psycopg.connect(self.read_database_url, row_factory=dict_row, connect_timeout=5) as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            row = conn.execute("SELECT source_revision FROM egap_commercial.tenants WHERE tenant_id=%s", (tenant,)).fetchone()
            subscription = self._get(conn, tenant, "subscription", scope["subscription_id"], required=False)
            link = self._get(conn, tenant, "invoice_link", scope["invoice_id"], required=False)
            if subscription and subscription["customer_id"] != scope["customer_id"]:
                raise SourceError("CONTEXT_SCOPE_MISMATCH", 403)
            if link and (link["customer_id"], link["subscription_id"]) != (scope["customer_id"], scope["subscription_id"]):
                raise SourceError("CONTEXT_SCOPE_MISMATCH", 403)
            def matching(value):
                return all(value.get(field) == scope[field] for field in ("customer_id", "subscription_id"))
            def one(category, additional=()):
                matches = [v for v in self._all(conn, tenant, category) if matching(v) and v["invoice_id"] == scope["invoice_id"] and all(v[f] == scope[f] for f in additional)]
                if len(matches) > 1:
                    raise SourceError("CONTEXT_FACT_AMBIGUOUS", 409)
                return matches[0] if matches else None
            events = [e for e in self._all(conn, tenant, "event") if matching(e)]
            links = [v for v in self._all(conn, tenant, "invoice_link") if matching(v)]
            for event in events:
                event["invoice_refs"] = [v["invoice_id"] for v in links if v["event_id"] == event["event_id"]]
            events.sort(key=lambda e: (e["effective_at"], e["recorded_at"], e["event_id"]))
            return {"source_kind": SOURCE_KIND, "authority_scope": AUTHORITY_SCOPE,
                    "api_version": "commercial-source-v1", "source_revision": row["source_revision"] if row else 0,
                    "scope": dict(scope), "policies": self._all(conn, tenant, "policy"),
                    "contracts": [c for c in self._all(conn, tenant, "contract") if matching(c)],
                    "events": events, "subscription": subscription, "invoice_link": link,
                    "claim": one("claim", ("account_id", "conversation_id")),
                    "coverage": one("coverage"), "payment": one("payment")}

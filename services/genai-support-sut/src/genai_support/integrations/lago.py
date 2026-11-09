"""计费适配器：授权客户账单列表 → 发票 → 唯一订阅 → 同发票的用量期间。

只读取，绝不执行退款或改支付状态。返回缺失资料，不拿当前用量替代历史账单，
也不把开票日期解释成续费事件。原始对象继续供上层保留来源与摘要。
"""

from datetime import datetime
from typing import Any

from .http import IntegrationError, JsonClient, list_field, object_field, segment


def _period(start: Any, end: Any) -> tuple[datetime, datetime]:
    """统一比较有时区的真实时间端点，保留 Lago 原生闭区间，不自行加减一秒。"""
    try:
        left = datetime.fromisoformat(start.replace("Z", "+00:00"))
        right = datetime.fromisoformat(end.replace("Z", "+00:00"))
        if left.tzinfo is None or right.tzinfo is None or right < left:
            raise ValueError("INVALID_PERIOD")
        return left, right
    except (AttributeError, TypeError, ValueError):
        raise IntegrationError("LAGO_USAGE_PERIOD_INVALID") from None


def _invoice_usage_period(invoice: dict, subscription: str) -> dict | None:
    """从发票实际费用行取得期间；有计量费用时用 charge，不能混用预付订阅期间。

    同一发票可能同时收下期订阅费与上期用量费。这里只能接受选定订阅费用行
    导出的唯一期间；缺失、分段多期或无法绑定订阅则保留未知，不能取任意一行。
    """
    fees = invoice.get("fees")
    if not isinstance(fees, list):
        return None
    related = [row for row in fees if isinstance(row, dict) and row.get("external_subscription_id") == subscription]
    charges = [row for row in related if isinstance(row.get("item"), dict) and row["item"].get("type") == "charge"]
    selected = charges or [row for row in related if isinstance(row.get("item"), dict) and row["item"].get("type") == "subscription"]
    if not selected:
        return None
    spans = {_period(row.get("from_date"), row.get("to_date")) for row in selected}
    if len(spans) != 1:
        return None
    left, right = next(iter(spans))
    return {"from_datetime": left.isoformat(), "to_datetime": right.isoformat(),
            "source": "CHARGE_FEES" if charges else "SUBSCRIPTION_FEES",
            "fee_ids": [row["lago_id"] for row in selected if isinstance(row.get("lago_id"), str)]}


class LagoClient:
    def __init__(self, base_url: str, token: str, audit=None) -> None:
        self.http = JsonClient(base_url, {"Authorization": "Bearer " + token}, "lago", audit)

    def get_customer(self, external_id: str) -> dict[str, Any]:
        value = object_field(self.http.request("GET", "/api/v1/customers/" + segment(external_id)), "customer")
        if value.get("external_id") != external_id:
            raise IntegrationError("LAGO_CUSTOMER_MISMATCH")
        return value

    def _customer_list(self, external_id: str, kind: str) -> list[dict[str, Any]]:
        result = []
        for page in range(1, 21):
            body = self.http.request("GET", f"/api/v1/customers/{segment(external_id)}/{kind}",
                                     query={"page": page, "per_page": 100})
            result.extend(list_field(body, kind))
            meta = body.get("meta")
            if not isinstance(meta, dict) or "next_page" not in meta:
                raise IntegrationError("LAGO_PAGINATION_INVALID")
            if meta["next_page"] is None:
                return result
            if type(meta["next_page"]) is not int or meta["next_page"] != page + 1:
                raise IntegrationError("LAGO_PAGINATION_INVALID")
        raise IntegrationError("LAGO_OBSERVATION_INCOMPLETE")

    def get_subscriptions(self, external_id: str) -> list[dict[str, Any]]:
        return self._customer_list(external_id, "subscriptions")

    def get_customer_invoices(self, external_id: str) -> list[dict[str, Any]]:
        return self._customer_list(external_id, "invoices")

    def get_invoice_for_customer(self, external_id: str, invoice_id: str) -> dict[str, Any]:
        """跨客户 invoice ID 在详情读取之前拒绝，避免先读敏感对象再补做权限判断。"""
        rows = self.get_customer_invoices(external_id)
        if not any(row.get("lago_id") == invoice_id for row in rows):
            raise IntegrationError("INVOICE_NOT_AUTHORIZED", 403, "REJECTED")
        invoice = object_field(self.http.request("GET", "/api/v1/invoices/" + segment(invoice_id)), "invoice")
        if invoice.get("lago_id") != invoice_id or not isinstance(invoice.get("customer"), dict) \
                or invoice["customer"].get("external_id") != external_id:
            raise IntegrationError("LAGO_INVOICE_CUSTOMER_MISMATCH", 403, "REJECTED")
        return invoice

    def get_current_usage(self, customer: str, subscription: str) -> dict[str, Any]:
        return object_field(self.http.request("GET", f"/api/v1/customers/{segment(customer)}/current_usage",
            query={"external_subscription_id": subscription}), "customer_usage")

    def get_past_usage(self, customer: str, subscription: str, periods_count: int = 12) -> list[dict[str, Any]]:
        if type(periods_count) is not int or not 1 <= periods_count <= 24:
            raise IntegrationError("USAGE_LOOKBACK_INVALID", 422, "REJECTED")
        return list_field(self.http.request("GET", f"/api/v1/customers/{segment(customer)}/past_usage",
            query={"external_subscription_id": subscription, "periods_count": periods_count}), "usage_periods")

    def resolve_invoice_scope(self, customer: str, invoice_id: str) -> dict[str, Any]:
        """返回同一发票的订阅与用量来源；多订阅发票须显式补充选择，不能取第一条。

        Lago usage 本身没有输入订阅字段，来源由受控查询参数和实际回执共同绑定。
        精确匹配 lago_invoice_id 后，还须与实际发票费用行导出的唯一期间一致。
        未找到匹配时 usage=None；这既不是零用量，也不能推导事件已全部到齐。
        """
        customer_object = self.get_customer(customer)
        invoice = self.get_invoice_for_customer(customer, invoice_id)
        subscriptions = invoice.get("subscriptions")
        if not isinstance(subscriptions, list) or len(subscriptions) != 1 \
                or not isinstance(subscriptions[0], dict) or not subscriptions[0].get("external_id"):
            raise IntegrationError("INVOICE_SUBSCRIPTION_AMBIGUOUS", 409, "REJECTED")
        subscription = subscriptions[0]
        if subscription.get("external_customer_id") != customer:
            raise IntegrationError("LAGO_SUBSCRIPTION_CUSTOMER_MISMATCH", 403, "REJECTED")
        external_subscription = subscription["external_id"]
        invoice_period = _invoice_usage_period(invoice, external_subscription)
        periods = self.get_past_usage(customer, external_subscription)
        matches = [row for row in periods if row.get("lago_invoice_id") == invoice_id]
        source = "PAST"
        if len(matches) > 1:
            raise IntegrationError("USAGE_PERIOD_AMBIGUOUS", 409, "REJECTED")
        if not matches:
            current = self.get_current_usage(customer, external_subscription)
            matches = [current] if current.get("lago_invoice_id") == invoice_id else []
            source = "CURRENT" if matches else "MISSING"
        usage, missing = (matches[0], []) if matches else (None, ["INVOICE_USAGE_PERIOD_NOT_FOUND"])
        observed_usage_period = None
        if usage is not None:
            actual = _period(usage.get("from_datetime"), usage.get("to_datetime"))
            observed_usage_period = {"from_datetime": usage["from_datetime"], "to_datetime": usage["to_datetime"],
                                     "lago_invoice_id": usage["lago_invoice_id"]}
            if invoice_period is None:
                usage, source, missing = None, "MISSING", ["INVOICE_FEE_PERIOD_UNPROVEN"]
            elif actual != _period(invoice_period["from_datetime"], invoice_period["to_datetime"]):
                # 预付订阅费与同张发票的计量期间可能合法地不同。保留双方事实供补证，
                # 不把不适用的计量结果输入模型，也不擅自宣布上游有Bug。
                usage, source, missing = None, "MISSING", ["INVOICE_USAGE_PERIOD_MISMATCH"]
        return {"customer": customer_object, "invoice": invoice, "subscription": subscription,
                "usage": usage, "usage_source": source, "missing_evidence": missing,
                "external_customer_id": customer, "external_subscription_id": external_subscription,
                "invoice_id": invoice_id, "invoice_period": invoice_period,
                "observed_usage_period": observed_usage_period}

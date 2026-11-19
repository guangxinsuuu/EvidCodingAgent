"""迟到用量切片的只读观察与独立断言。

CLI → LateUsageObserver.observe/wait → SUT、商业来源及原生 GET/只读 SQL；
取得完整包后另行调用 assess_observation/assess_transition。构造不创建标记、
不绑定固定 commit、不启动服务，也不导入产品资格代码。这里的 PASS 只覆盖
列出的本地合成规则；决策原文、模型请求/输出缺失时明确留缺口，不反推原文。
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import time
from typing import Any, Callable
from urllib.parse import quote, urlencode

from release_oracle import OracleInputError, assess_case


SCOPE_KEYS = ("tenant_id", "customer_id", "subscription_id", "invoice_id", "account_id", "conversation_id")
SNAPSHOT_SQL = """SELECT i.context FROM egap_support.external_inputs i
 JOIN egap_support.support_jobs j ON i.tenant_id=j.tenant_id AND i.ticket_id=j.ticket_id
 AND i.ticket_revision=j.ticket_revision WHERE j.tenant_id=%s AND j.job_id=%s"""
ACCEPTED_SQL = """SELECT e.details FROM egap_support.support_job_events e
 JOIN egap_support.support_jobs j ON j.job_id=e.job_id
 WHERE j.tenant_id=%s AND j.job_id=%s AND e.kind='ACCEPTED' ORDER BY e.event_id LIMIT 1"""


def _sha(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _body(receipt: dict) -> dict:
    """接受持久 HTTP 回执或已保存响应；不从摘要猜测未取得的响应。"""
    for key in ("response", "body", "error_response"):
        if isinstance(receipt.get(key), dict):
            return receipt[key]
    return receipt if "job_id" in receipt else {}


def observe_submission(receipt: dict) -> dict:
    """纯函数记录受理层，包括 503 且无 Job ID；不会代替提交或重试。

    receipt 建议包含 status、response、outcome、request_sent、observed_at；
    5xx 无 ID 仅证明没有观察到 ID，不能证明服务端没有提交事务。
    """
    body, status = _body(receipt), receipt.get("status", receipt.get("http_status"))
    job_id = body.get("job_id")
    has_job = isinstance(job_id, str) and bool(job_id)
    if status in (200, 201, 202) and has_job:
        state = "ACCEPTED"
    elif isinstance(status, int) and 400 <= status < 600:
        state = "HTTP_ERROR_WITH_JOB_ID" if has_job else "HTTP_ERROR_WITHOUT_JOB_ID"
    elif isinstance(status, int) and 200 <= status < 300:
        state = "ACCEPTANCE_PROTOCOL_INVALID"
    else:
        state = "UNKNOWN"
    return {"kind": "SUBMISSION_OBSERVATION", "state": state, "http_status": status,
            "job_id": job_id if has_job else None, "receipt": deepcopy(receipt),
            "observed_at": receipt.get("observed_at") or _now(),
            "scope": "OBSERVED_HTTP_ONLY_NO_JOB_EXISTENCE_INFERENCE", "business_verdict": "NOT_EVALUATED"}


class LateUsageObserver:
    """注入只读客户端后独立读回；客户端须提供 request('GET', path)。

    audit_scalar(query, params) 必须使用只读事务。model_trace_reader(case, job_id)
    可返回与 job_id 绑定的 decision/model_request/model_output 原始记录；未提供
    时保持空值和缺口。所有输出均可能含私有业务材料，由调用方以私有证据保存。
    """

    def __init__(self, sut, chatwoot, lago, commercial, audit_scalar: Callable, *,
                 cw_prefix: str, model_trace_reader: Callable | None = None):
        self.sut, self.chatwoot, self.lago, self.commercial = sut, chatwoot, lago, commercial
        self.audit_scalar, self.cw_prefix = audit_scalar, cw_prefix
        self.model_trace_reader = model_trace_reader

    @staticmethod
    def observe_submission(receipt: dict) -> dict:
        return observe_submission(receipt)

    def observe(self, case: dict, job_id: str) -> dict:
        """只观察一次，即使 Job 非终态或某个系统不可达仍保存其余独立材料。"""
        package = {"schema_version": 1, "kind": "LATE_USAGE_OBSERVATION", "observed_at": _now(),
                   "scope": {key: case[key] for key in SCOPE_KEYS}, "requested_job_id": job_id,
                   "experiment_id": case.get("experiment_id"), "candidate_sha": case.get("candidate_sha"),
                   "candidate_binding_status": "SUPPLIED_METADATA_ONLY_VERIFY_DEPLOYMENT_SEPARATELY",
                   "reads": [], "gaps": [], "observations": {}}

        def read(name, callback):
            record = {"name": name, "started_at": _now()}
            try:
                value = deepcopy(callback())
                record.update(state="OBSERVED", response_sha256=_sha(value))
                if value is None:
                    package["gaps"].append(name.upper() + "_MISSING")
                return value
            except Exception as exc:
                # 异常原文可能包含 DSN/请求头，不进入证据；失败类别足够标记观察缺口。
                record.update(state="UNAVAILABLE", error_type=type(exc).__name__)
                package["gaps"].append(name.upper() + "_UNAVAILABLE")
                return None
            finally:
                record["observed_at"] = _now()
                package["reads"].append(record)

        tenant = quote(str(case["tenant_id"]), safe="")
        path = f"/integrations/disputes/{tenant}/{quote(job_id, safe='')}"
        result = read("sut_job", lambda: self.sut.request("GET", path))
        package["result"] = result
        package["context"] = read("sut_snapshot", lambda: self.audit_scalar(SNAPSHOT_SQL, (case["tenant_id"], job_id)))
        package["accepted_event"] = read("sut_accepted_event", lambda: self.audit_scalar(ACCEPTED_SQL, (case["tenant_id"], job_id)))
        job = result.get("job") if isinstance(result, dict) else None
        delivery = result.get("delivery") if isinstance(result, dict) else None
        valid_job = isinstance(job, dict) and job.get("job_id") == job_id and job.get("status") in {
            "QUEUED", "PROCESSING", "DONE", "FAILED"}
        valid_delivery = delivery is None or isinstance(delivery, dict) and delivery.get("delivery_state") in {
            "PENDING", "DISPATCHING", "COMPLETE", "UNKNOWN", "STALE", "FAILED"}
        if isinstance(delivery, dict) and delivery.get("delivery_state") == "COMPLETE" and (
                not isinstance(job, dict) or job.get("status") != "DONE"):
            valid_delivery = False
        package["protocol_status"] = "VALID" if valid_job and valid_delivery else "UNOBSERVED" if result is None else "INVALID"
        package["job_state"] = job.get("status") if isinstance(job, dict) else None
        package["delivery_state"] = delivery.get("delivery_state") if isinstance(delivery, dict) else None
        package["terminal"] = bool(valid_job and valid_delivery and (package["job_state"] == "FAILED" or
            package["delivery_state"] in {"COMPLETE", "FAILED", "UNKNOWN", "STALE"}))
        package["draft"] = deepcopy(job.get("draft")) if isinstance(job, dict) else None
        observations = package["observations"]
        conversation_path = self.cw_prefix + f"/conversations/{case['conversation_id']}"
        message_body = read("chatwoot_messages", lambda: self.chatwoot.request("GET", conversation_path + "/messages"))
        observations["messages"] = message_body.get("payload") if isinstance(message_body, dict) else None
        observations["conversation"] = read("chatwoot_conversation", lambda: self.chatwoot.request("GET", conversation_path))
        original_id = case.get("customer_message_id")
        if original_id is None:
            # 定位回退不能算独立绑定；Oracle 要求清单中保留原消息身份。
            context = package["context"] if isinstance(package["context"], dict) else {}
            source = context.get("commercial_source") if isinstance(context.get("commercial_source"), dict) else {}
            original_id = (source.get("claim") or {}).get("original_message", {}).get("message_id")
            package["gaps"].append("PREPARED_ORIGINAL_MESSAGE_ID_MISSING")
        messages = observations["messages"]
        matches = [row for row in messages or [] if isinstance(row, dict) and row.get("id") == original_id]
        observations["original_message"] = matches[0] if len(matches) == 1 else None
        if observations["original_message"] is None:
            package["gaps"].append("ORIGINAL_MESSAGE_READBACK_MISSING_OR_AMBIGUOUS")
        invoice = read("lago_invoice", lambda: self.lago.request("GET", "/api/v1/invoices/" + quote(case["invoice_id"], safe="")))
        observations["invoice"] = invoice.get("invoice") if isinstance(invoice, dict) else None
        usage_path = "/api/v1/customers/" + quote(case["customer_id"], safe="") + "/current_usage?" + urlencode({
            "external_subscription_id": case["subscription_id"]})
        usage = read("lago_usage", lambda: self.lago.request("GET", usage_path))
        observations["usage"] = usage.get("customer_usage") if isinstance(usage, dict) else None
        observations["source_context"] = read("commercial_source", lambda: self.commercial.request(
            "GET", "/v1/context?" + urlencode({key: case[key] for key in SCOPE_KEYS})))
        trace = read("model_trace", lambda: self.model_trace_reader(case, job_id)) if self.model_trace_reader else None
        if trace is not None and (not isinstance(trace, dict) or trace.get("job_id") != job_id):
            package["gaps"].append("MODEL_TRACE_JOB_ID_MISMATCH")
            trace = None
        for key, gap in (("decision", "DETERMINISTIC_DECISION_NOT_CAPTURED"),
                         ("model_request", "ACTUAL_MODEL_REQUEST_NOT_CAPTURED"),
                         ("model_output", "RAW_MODEL_OUTPUT_NOT_CAPTURED")):
            package[key] = deepcopy((trace or {}).get(key))
            if package[key] is None:
                package["gaps"].append(gap)
        package["snapshot_sha256"] = _sha(package["context"]) if package["context"] is not None else None
        package["completed_at"] = _now()
        return package

    def wait(self, case: dict, job_id: str, *, timeout: float = 180, poll_interval: float = 1,
             on_observation: Callable[[dict], None] | None = None) -> dict:
        """有界轮询同一 Job；超时返回 WAITING 包，不发送提交或重新调用模型。

        每次完整观察先交 on_observation 持久化，再更新摘要。执行层可将回调接到
        Dispatch.record_evidence；保存失败立即向上传播，禁止丢失原始证据后继续
        轮询。回调得到副本，不能改变随后断言使用的原始观察。
        """
        if timeout < 0 or poll_interval <= 0:
            raise ValueError("INVALID_OBSERVATION_BOUND")
        deadline, history = time.monotonic() + timeout, []
        while True:
            package = self.observe(case, job_id)
            if on_observation is not None:
                on_observation(deepcopy(package))
            history.append({"observed_at": package["observed_at"], "job_state": package["job_state"],
                            "delivery_state": package["delivery_state"], "package_sha256": _sha(package)})
            if package["terminal"] or package["protocol_status"] == "INVALID" or time.monotonic() >= deadline:
                package["poll_history"] = history
                package["wait_status"] = "TERMINAL" if package["terminal"] else "PROTOCOL_INVALID" if package["protocol_status"] == "INVALID" else "WAITING"
                return package
            time.sleep(min(poll_interval, max(0, deadline - time.monotonic())))


def _verdict(checks=None, *, status=None, gaps=None, findings=None) -> dict:
    checks = checks or []
    return {"qualification": "LOCAL_INDEPENDENT_LATE_USAGE_RULE_CHECK", "status": status or (
            "FAIL" if any(c["status"] == "FAIL" for c in checks) else "PASS_SCOPED_CHECKS"),
            "checks": checks, "gaps": gaps or [], "findings": findings or [],
            "semantic_quality_status": "NOT_FULLY_EVALUATED", "release_authorized": False}


def _check(checks: list, name: str, condition: bool, **evidence):
    checks.append({"name": name, "status": "PASS" if condition else "FAIL", "evidence": evidence})


def _instant(value) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("TIMEZONE_REQUIRED")
    return parsed


def _units(raw: dict, metric: str) -> Decimal:
    rows = [row for row in raw["charges_usage"] if row.get("billable_metric", {}).get("code") == metric]
    if len(rows) != 1:
        raise ValueError("METRIC_NOT_UNIQUE")
    result = Decimal(str(rows[0]["units"]))
    if not result.is_finite() or result < 0:
        raise ValueError("INVALID_UNITS")
    return result


def _coverage_checks(case: dict, package: dict, stage: str, checks: list):
    """把 SEALED 标签还原为预登记、发送、接收、原生聚合及期间证据链。"""
    source = package["observations"]["source_context"]
    coverage = source.get("coverage") or {}
    events = coverage.get("expected_events") or []
    claim_at, start = _instant(case["claimed_at"]), _instant(case["period_start"])
    _check(checks, "coverage_exact_claim_interval", _instant(coverage["period_start"]) == start
           and _instant(coverage["period_end"]) == claim_at)
    _check(checks, "single_predeclared_unit", len(events) == 1 and Decimal(str(events[0].get("units"))) == 1)
    timing = all(start <= _instant(e["occurred_at"]) <= claim_at for e in events) if case["label"] == "late_usage" else all(
        _instant(e["occurred_at"]) > claim_at for e in events)
    _check(checks, "predeclared_event_correct_side_of_claim", bool(events) and timing, label=case["label"])
    _check(checks, "prepared_original_message_binding", case.get("customer_message_id") is not None and
           source.get("claim", {}).get("original_message", {}).get("message_id") == case.get("customer_message_id"))
    if stage == "initial":
        _check(checks, "predeclared_event_not_yet_sent", coverage.get("status") == "OPEN" and
               coverage.get("sent_event_ids") == [] and coverage.get("received_event_ids") == [] and
               coverage.get("pending_count") == len(events) and coverage.get("sealed_at") is None)
        return
    expected_ids = {e["event_id"] for e in events}
    send, receive = coverage.get("send_receipts") or {}, coverage.get("receive_receipts") or {}
    _check(checks, "seal_has_all_event_receipts", expected_ids == set(send) == set(receive) and bool(expected_ids))
    for event in events:
        event_id, sent, received = event["event_id"], send.get(event["event_id"], {}), receive.get(event["event_id"], {})
        native = sent.get("native_event") or {}
        native_time = native.get("timestamp")
        if isinstance(native_time, (int, float)):
            native_time = datetime.fromtimestamp(native_time, timezone.utc).isoformat()
        _check(checks, "native_event_receipt_matches_predeclaration:" + event_id,
               sent.get("service") == "lago" and sent.get("method") == "POST" and sent.get("path") == "/api/v1/events"
               and sent.get("status") in (200, 201, 202) and sent.get("transaction_id") == event_id
               and native.get("transaction_id") == event_id and native.get("code") == case["metric_code"]
               and native.get("external_subscription_id") == case["subscription_id"]
               and Decimal(str((native.get("properties") or {}).get("units", "NaN"))) == Decimal(str(event["units"]))
               and _instant(native_time) == _instant(event["native_occurred_at"]))
        _check(checks, "aggregate_receipt_covers_event:" + event_id, received.get("service") == "lago"
               and received.get("method") == "GET" and received.get("status") == 200
               and received.get("transaction_id") == event_id and set(received.get("bounded_event_set", [])) == expected_ids
               and (received.get("query") or {}).get("external_subscription_id") == case["subscription_id"])
    reconciliation = coverage.get("reconciliation") or {}
    raw, receipt = reconciliation.get("raw_response"), reconciliation.get("receipt") or {}
    usage = package["observations"]["usage"]
    _check(checks, "seal_has_independent_matching_reconciliation", isinstance(raw, dict)
           and reconciliation.get("matched") is True and reconciliation.get("raw_response_sha256") == _sha(raw)
           and _units(raw, case["metric_code"]) == _units(usage, case["metric_code"]) == Decimal(1)
           and Decimal(str(reconciliation.get("lago_units"))) == 1
           and raw.get("from_datetime") == usage.get("from_datetime") and raw.get("to_datetime") == usage.get("to_datetime")
           and receipt.get("service") == "lago" and receipt.get("method") == "GET" and receipt.get("status") == 200
           and (receipt.get("query") or {}).get("external_subscription_id") == case["subscription_id"])
    _check(checks, "seal_follows_latest_coverage_reconciliation", isinstance(reconciliation.get("ledger_revision"), int)
           and coverage.get("revision") == reconciliation["ledger_revision"] + 2 and bool(coverage.get("sealed_at")))


def assess_observation(case: dict, package: dict, *, stage: str = "initial") -> dict:
    """纯验证入口；两场景都从 OPEN 开始，只有封存后的发生时点区分资格。

    复用历史校准器时只在验证器内选择规则，绝不把 EXPECTATIONS 写入执行输入。
    当前规则限定 lab-v2、申请距续费两天、单单位预登记事件及本地受控生成器。
    """
    if case.get("label") not in {"late_usage", "post_claim_usage"} or stage not in {"initial", "after_seal"}:
        raise OracleInputError("UNKNOWN_LATE_USAGE_STAGE")
    gaps = list(package.get("gaps", []))
    if package.get("protocol_status") == "INVALID":
        return _verdict([{"name": "candidate_job_protocol", "status": "FAIL", "evidence": {}}], gaps=gaps)
    result = package.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("job"), dict):
        return _verdict(status="INCOMPLETE", gaps=gaps + ["SUT_JOB_NOT_OBSERVED"])
    job, delivery = result["job"], result.get("delivery") or {}
    if job.get("status") == "FAILED" or delivery.get("delivery_state") in {"FAILED", "UNKNOWN", "STALE"}:
        return _verdict([{"name": "execution_and_delivery_completed", "status": "FAIL", "evidence": {
            "job_state": job.get("status"), "delivery_state": delivery.get("delivery_state")}}], gaps=gaps)
    if job.get("status") != "DONE" or delivery.get("delivery_state") != "COMPLETE":
        return _verdict(status="WAITING", gaps=gaps)
    observations = package.get("observations") or {}
    if any(not isinstance(observations.get(k), dict) for k in (
            "source_context", "invoice", "usage", "conversation", "original_message")) or not isinstance(observations.get("messages"), list):
        return _verdict(status="INCOMPLETE", gaps=gaps + ["INDEPENDENT_NATIVE_OBSERVATIONS_REQUIRED"])
    oracle_case = deepcopy(case)
    if stage == "initial":
        # 旧 post_claim_usage 是已封存初态；本切片两个初态都使用 OPEN 补证规则。
        oracle_case["label"], oracle_stage = "late_usage", "initial"
    else:
        oracle_stage = "after_seal" if case["label"] == "late_usage" else "initial"
    try:
        prior = assess_case(oracle_case, result, package.get("context"), observations, stage=oracle_stage)
    except (OracleInputError, TypeError, KeyError, ValueError, InvalidOperation):
        return _verdict(status="INCOMPLETE", gaps=gaps + ["ORACLE_INPUT_INVALID_OR_INCOMPLETE"])
    checks = deepcopy(prior["checks"])
    try:
        _coverage_checks(case, package, stage, checks)
    except (TypeError, AttributeError, KeyError, ValueError, InvalidOperation):
        _check(checks, "source_coverage_evidence_shape", False)
    decision = package.get("decision")
    if isinstance(decision, dict):
        draft = job.get("draft") or {}
        _check(checks, "captured_decision_matches_draft", all(decision.get(k) == draft.get(k) for k in ("eligibility", "proposed_action")))
        expected_reason = "MISSING_USAGE_COMPLETENESS" if stage == "initial" else "USAGE_LIMIT_EXCEEDED" if case["label"] == "late_usage" else "ELIGIBLE_PENDING_HUMAN_REVIEW"
        reasons = decision.get("reason_codes") or []
        _check(checks, "captured_decision_reason_matches_facts", expected_reason in reasons and "REFUND_WINDOW_EXCEEDED" not in reasons)
        if stage == "after_seal":
            _check(checks, "captured_decision_uses_claim_interval", decision.get("within_window") is True and
                   str(decision.get("usage_units")) == ("1" if case["label"] == "late_usage" else "0"))
    findings = [{**row, "label": case["label"], "stage": stage} for row in prior.get("findings", [])]
    reply = (job.get("draft") or {}).get("customer_reply_draft", "")
    if isinstance(reply, str) and any(token in reply for token in ("超出退款窗口", "超出退款期限", "超过退款窗口", "超过退款期限")):
        findings.append({"code": "POSSIBLE_FALSE_WINDOW_REASON", "status": "REVIEW_REQUIRED", "label": case["label"],
                         "stage": stage, "basis": "申请在实验窗口内；词汇命中只要求核对上下文，不能签发全文语义结论"})
    verdict = _verdict(checks, gaps=gaps, findings=findings)
    verdict.update(label=case["label"], stage=stage, job_id=job.get("job_id"))
    return verdict


def assess_transition(before: dict, after: dict, old_after: dict, retries: list[dict],
                      reevaluation_receipt: dict) -> dict:
    """核验 J1→事实变化→普通重试 J1→显式重评 J2；不执行任何动作。

    before/after/old_after 为独立观察包，最后一项须重新读回 J1。至少一项重试
    回执必须存在；父子关系同时核对 HTTP 受理与独立 ACCEPTED 账本记录。
    """
    checks = []
    try:
        old_id, new_id = before["result"]["job"]["job_id"], after["result"]["job"]["job_id"]
        old, new, old_again = before["context"], after["context"], old_after["context"]
        if not all(isinstance(c, dict) for c in (old, new, old_again)):
            raise KeyError("context")
        accepted = _body(reevaluation_receipt)
        _check(checks, "ordinary_retries_reuse_original_job", bool(retries) and all(
            _body(row).get("job_id") == old_id and _body(row).get("reused") is True
            and row.get("status", row.get("http_status", 202)) in (200, 201, 202) for row in retries))
        _check(checks, "reevaluation_new_job_and_parent", new_id != old_id and accepted.get("job_id") == new_id
               and accepted.get("parent_job_id") == old_id and accepted.get("assessment_status") == "REEVALUATED"
               and reevaluation_receipt.get("status", reevaluation_receipt.get("http_status", 202)) in (200, 201, 202)
               and (after.get("accepted_event") or {}).get("parent_job_id") == old_id)
        _check(checks, "old_snapshot_immutable", old_after["result"]["job"]["job_id"] == old_id and _sha(old) == _sha(old_again))
        old_source, new_source = old["commercial_source"], new["commercial_source"]
        _check(checks, "original_claim_identity_and_time_immutable", all(old_source["claim"].get(k) is not None
               and old_source["claim"].get(k) == new_source["claim"].get(k)
               for k in ("claim_id", "claimed_at", "original_message")))
        _check(checks, "predeclared_event_and_coverage_identity_immutable", bool(old_source["coverage"].get("coverage_id"))
               and bool(old_source["coverage"].get("expected_events"))
               and old_source["coverage"].get("coverage_id") == new_source["coverage"].get("coverage_id")
               and old_source["coverage"].get("expected_events") == new_source["coverage"].get("expected_events"))
        _check(checks, "reevaluation_uses_changed_facts", _sha(old) != _sha(new) and old_source["coverage"].get("status") == "OPEN"
               and new_source["coverage"].get("status") == "SEALED"
               and (before.get("accepted_event") or {}).get("assessment_facts_sha256") is not None
               and (after.get("accepted_event") or {}).get("assessment_facts_sha256") is not None
               and before["accepted_event"]["assessment_facts_sha256"] != after["accepted_event"]["assessment_facts_sha256"])
        _check(checks, "same_experiment_candidate_and_scope", all(bool(before.get(k))
               and before.get(k) == after.get(k) == old_after.get(k)
               for k in ("experiment_id", "candidate_sha", "scope")))
    except (KeyError, TypeError, AttributeError):
        return _verdict(checks, status="INCOMPLETE", gaps=["TRANSITION_OBSERVATION_MISSING"])
    return _verdict(checks)


def summarize_execution(cases: list[dict]) -> dict:
    """纯汇总所有已观察结论；已知 FAIL 不能被另一阶段未完成覆盖。

    CLI 在结束时传入 cases，异常路径的 verdict 与正常三阶段同样参与汇总。
    只有主例/对照各一项 OBSERVED 且六份正常 verdict 都通过才签发限定规则
    PASS；模型原文缺口、词汇复核项不等于全文语义评价，继续原样保留。
    """
    required = {"late_usage", "post_claim_usage"}
    normal = ("initial_verdict", "final_verdict", "transition")
    stages = {"initial_verdict": "initial", "final_verdict": "after_seal",
              "transition": "transition", "verdict": "execution"}
    findings, failures, incomplete = [], [], []
    observed_failure = False
    for case in cases:
        label = case.get("label")
        reasons = []
        if case.get("status") != "OBSERVED":
            reasons.append("CASE_NOT_FULLY_OBSERVED")
        for key in normal:
            value = case.get(key)
            if not isinstance(value, dict):
                reasons.append(key.upper() + "_MISSING")
            elif value.get("status") not in {"PASS_SCOPED_CHECKS", "FAIL"}:
                reasons.append(key.upper() + "_INCOMPLETE")
        for key, fallback_stage in stages.items():
            verdict = case.get(key)
            if not isinstance(verdict, dict):
                continue
            stage = verdict.get("stage") or fallback_stage
            for finding in verdict.get("findings", []):
                if isinstance(finding, dict):
                    findings.append({"label": label, "stage": stage, **deepcopy(finding)})
            failed = [row for row in verdict.get("checks", []) if isinstance(row, dict) and row.get("status") == "FAIL"]
            for check in failed:
                failures.append({"label": label, "stage": stage, "verdict_key": key, **deepcopy(check)})
            if verdict.get("status") == "FAIL":
                observed_failure = True
                if not failed:
                    # 协议只给出 verdict FAIL 时仍保留反例，不能静默变成零失败。
                    failures.append({"label": label, "stage": stage, "verdict_key": key,
                                     "name": "observed_verdict_failure", "status": "FAIL", "evidence": {}})
            if key == "verdict" and verdict.get("status") not in {"PASS_SCOPED_CHECKS", "FAIL"}:
                reasons.append("EXECUTION_VERDICT_INCOMPLETE")
        if reasons:
            incomplete.append({"label": label, "status": case.get("status"), "reasons": reasons})
    labels = [case.get("label") for case in cases]
    if len(cases) != 2 or set(labels) != required:
        incomplete.append({"label": None, "status": "CASE_SET_INCOMPLETE", "reasons": [
            "EXACTLY_ONE_PRIMARY_AND_ONE_CONTROL_REQUIRED"], "observed_labels": labels})
    all_pass = not incomplete and all(case[key].get("status") == "PASS_SCOPED_CHECKS"
                                      for case in cases for key in normal)
    status = "FAIL" if observed_failure or failures else "PASS_SCOPED_DETERMINISTIC_CHECKS" if all_pass else "INCOMPLETE"
    return {"scoped_result": status, "findings": findings, "failed_checks": failures, "incomplete_cases": incomplete}

"""复杂业务准备器的过程测试，不以mock结果冒称真实原生业务验收。

原生副作用使用既有Seed幂等边界；这里重点保护场景固定身份、合同原文/结构一致、
待处理事件预登记顺序、双时钟子区间、取消不制造续期及失败后证据保留。
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import Mock, patch

import business_source
import release_seed
from release_seed import DAY, NativeObjects, SourceObjects, scenario_specs
from seed import SeedError, save_private, sha


class ReleaseObjectsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = self.root / "experiment"
        self.config = {"chatwoot": {"base_url": "http://127.0.0.1:58001", "api_token": "cw-private", "account_id": 1},
            "lago": {"base_url": "http://127.0.0.1:58002", "api_key": "lago-private"},
            "keycloak": {"realm": "r", "operators": [{"tenant_id": "tenant-a", "subject": "operator-a"}, {"tenant_id": "tenant-b", "subject": "operator-b"}]}}
        self.specs = scenario_specs()
        self.native = {"run_id": "release_unit", "created_at": "2026-11-10T00:30:00Z", "scenario_specs": self.specs,
            "metric_code": "release_unit_units", "chatwoot_account_id": 1, "customers": [], "bindings": {"cases": []}}
        save_private(self.directory / "native/seed.json", self.native)
        save_private(self.root / "artifacts/multi-system/runtime.json", self.config)
        save_private(self.directory / "ready.json", {"base_url": "http://127.0.0.1:58003", "instance_id": "commercial_unit", "source_digest": "a" * 64})
        save_private(self.directory / "runtime.private.json", {"admin_token": "source-private"})
        shutil.copytree(release_seed.ROOT / "business-source-pack", self.root / "business-source-pack")
        for module in (release_seed, business_source):
            patcher = patch.object(module, "ROOT", self.root)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.source = SourceObjects(self.directory)

    def source_plan(self, label):
        spec = next(s for s in self.specs if s["label"] == label)
        start = datetime.fromisoformat(spec["period_start"].replace("Z", "+00:00"))
        end = start + timedelta(days=30) - timedelta(seconds=1)
        if spec["lifecycle"] == "terminated":
            end = start + timedelta(hours=1)
        customer = {"tenant_id": spec["tenant_id"], "external_customer_id": "customer"}
        usage = {"from_datetime": start.isoformat(), "to_datetime": end.isoformat(), "charges_usage": [
            {"billable_metric": {"code": self.native["metric_code"]}, "units": "0"}], "lago_invoice_id": None}
        sub = {"external_id": "subscription-" + label, "invoice_id": "invoice-" + label, "conversation_id": 4,
            "period_start": start.isoformat(), "period_end": end.isoformat(), "original_message": {"message_id": 5,
                "created_at": "2026-11-10T00:30:00Z", "content_sha256": hashlib.sha256(b"original").hexdigest()},
            "invoice_receipt": {"invoice": {"total_amount_cents": 12000, "currency": "USD"}},
            "usage": usage, "usage_receipt": {"service": "lago", "method": "GET", "status": 200,
                "path": "/api/v1/customers/customer/current_usage", "query": {"external_subscription_id": "subscription-" + label},
                "scope_kind": "SUBSCRIPTION_CURRENT_PERIOD", "observed_at": "2026-11-10T00:30:00Z"},
            "native_invoice_kind": "TERMINATION" if spec["lifecycle"] == "terminated" else "INITIAL"}
        clock = Mock(wraps=datetime)
        clock.now.return_value = datetime(2026, 11, 10, 0, 30, tzinfo=timezone.utc)
        with patch.object(release_seed, "datetime", clock):
            return self.source.plan(label, customer, sub)

    def capture_commands(self):
        calls = []
        def command(key, kind, payload):
            calls.append((key, kind, deepcopy(payload)))
            if kind in {"advance_clock", "terminate_subscription"}:
                return {"events": [{"event_id": "event-1", "event_type": "SUBSCRIPTION_RENEWAL_EFFECTIVE" if kind == "advance_clock" else "SUBSCRIPTION_TERMINATED"}]}
            return deepcopy(payload)
        self.source.command = Mock(side_effect=command)
        return calls

    def test_specs_reject_expected_answers_duplicates_and_non_utc_start(self):
        for specs in ([{**self.specs[0], "expected_eligibility": "ELIGIBLE"}], [self.specs[0], self.specs[0]],
                      [{**self.specs[0], "period_start": "2026-11-09T00:00:00+10:00"}]):
            with self.subTest(specs=specs), self.assertRaises(SeedError):
                scenario_specs(specs)

    def test_fixed_specs_preserve_policy_boundary_and_one_second_counterexample(self):
        values = {s["label"]: s for s in self.specs}
        self.assertEqual(len(values), 12)
        self.assertEqual(values["v1_history"]["period_start"], "2026-11-09T00:00:00Z")
        self.assertEqual(values["v2_boundary"]["claim_offset_seconds"], 7 * DAY)
        self.assertEqual(values["v2_late"]["claim_offset_seconds"], 7 * DAY + 1)
        self.assertEqual(values["v2_boundary"]["distractor_units"], 999)

    def test_malformed_fact_spec_fails_with_controlled_code_before_any_api(self):
        for update in ({"period_start": None}, {"usage_events": None}, {"payment_currency": "eur"},
                       {"usage_events": [{"name": "bad", "units": True, "business_offset_seconds": DAY}]},
                       {"usage_events": [{"name": "bad", "units": "NaN", "business_offset_seconds": DAY}]}):
            with self.subTest(update=update), self.assertRaises(SeedError):
                scenario_specs([{**self.specs[0], **update}])

    def test_changed_specs_cannot_resume_original_native_operations(self):
        output = self.root / "native-new/seed.json"
        NativeObjects(self.config, output, run_id="unit_run", specs=[self.specs[0]])
        changed = [{**self.specs[0], "claim_offset_seconds": 1}]
        with self.assertRaisesRegex(SeedError, "RELEASE_SCENARIOS_CHANGED"):
            NativeObjects(self.config, output, specs=changed)

    def test_frozen_native_seed_is_reused_without_new_api_reads_or_writes(self):
        output = self.root / "frozen/seed.json"
        native = NativeObjects(self.config, output, run_id="unit_run", specs=[self.specs[0]])
        frozen = {"run_id": "unit_run", "scenario_specs": [self.specs[0]], "immutable": True}
        save_private(output, frozen)
        native._setup = Mock(side_effect=AssertionError("no redispatch"))
        self.assertEqual(native.prepare(), frozen)
        native._setup.assert_not_called()

    def historical_source(self):
        path = self.root / "old-native/seed.json"
        value = {"metric_code": "old_units", "customers": [{"tenant_id": "tenant-a", "external_customer_id": "old-customer",
            "lago_customer_id": "customer-native-id", "subscriptions": [{"external_id": "old_a_zero", "invoice_id": "old-invoice"}]}]}
        save_private(path, value)
        return path, value

    def test_explicit_history_reuse_is_digest_bound_and_resumed_without_new_selection(self):
        source, value = self.historical_source()
        output = self.root / "history-new/seed.json"
        native = NativeObjects(self.config, output, run_id="reuse_run", reuse_history_from=source)
        self.assertEqual(native.history["invoice_id"], "old-invoice")
        resumed = NativeObjects(self.config, output)
        self.assertEqual(resumed.state["history_reuse"], native.state["history_reuse"])
        value["metric_code"] = "changed"
        save_private(source, value)
        with self.assertRaisesRegex(SeedError, "RELEASE_HISTORY_IDENTITY_CHANGED"):
            NativeObjects(self.config, output)

    def test_history_mode_cannot_be_added_after_native_writes(self):
        source, _ = self.historical_source()
        output = self.root / "mode-change/seed.json"
        native = NativeObjects(self.config, output, run_id="mode_run")
        native.state["operations"]["created-customer"] = {"state": "COMPLETE", "response": {}}
        native.checkpoint()
        with self.assertRaisesRegex(SeedError, "RELEASE_HISTORY_MODE_CHANGED"):
            NativeObjects(self.config, output, reuse_history_from=source)

    def test_reused_customer_uses_new_chatwoot_contact_identifier(self):
        source, _ = self.historical_source()
        native = NativeObjects(self.config, self.root / "customer-reuse/seed.json", run_id="newrun", reuse_history_from=source)
        native.lago.request = Mock(return_value={"customer": {"external_id": "old-customer", "lago_id": "customer-native-id"}})
        native.write_once = Mock(return_value={"id": 12, "contact_inboxes": [{"inbox": {"id": 7}, "source_id": "new-contact-source"}]})
        customer, binding = native._customer("tenant-a", {"id": 7})
        self.assertEqual(customer["external_customer_id"], "old-customer")
        self.assertEqual(binding["lago_customer_id"], "old-customer")
        args = native.write_once.call_args.args
        self.assertEqual(args[0], "contact_a")
        self.assertEqual(args[-1]["identifier"], "newrun_customer_a")
        self.assertNotEqual(args[-1]["identifier"], "old-customer")

    def test_reused_primary_metric_is_verified_zero_not_created_again(self):
        source, _ = self.historical_source()
        native = NativeObjects(self.config, self.root / "metric-reuse/seed.json", run_id="metricrun", reuse_history_from=source)
        native.chatwoot.request = Mock(return_value={})
        raw = {"customer_usage": {"charges_usage": [{"billable_metric": {"lago_id": "existing-metric", "code": "old_units"}, "units": "0"}]}}
        native.lago.request = Mock(return_value=raw)
        writes = []
        def write(key, api, method, path, payload):
            writes.append(key)
            if key == "metric_unrelated":
                return {"billable_metric": {"lago_id": "new-unrelated", "code": "metricrun_unrelated"}}
            return {"id": 7}
        native.write_once = Mock(side_effect=write)
        metrics, _ = native._setup()
        self.assertEqual(metrics[0]["lago_id"], "existing-metric")
        self.assertNotIn("metric_units", writes)
        raw["customer_usage"]["charges_usage"][0]["units"] = "1"
        with self.assertRaisesRegex(SeedError, "RELEASE_HISTORY_METRIC_NOT_VERIFIED"):
            native._setup()

    def test_cancelled_lifecycle_has_no_fabricated_invoice_claim_or_coverage(self):
        plan = self.source_plan("cancelled_renewal")
        calls = self.capture_commands()
        self.source.prepare_case("cancelled_renewal", plan)
        self.assertIsNone(plan["claimed_at"])
        kinds = [kind for _, kind, _ in calls]
        self.assertEqual(kinds, ["put_contract", "activate_subscription", "cancel_renewal", "advance_clock"])
        self.assertNotIn("link_invoice", kinds)
        self.assertGreater(calls[-1][2]["to"], calls[-2][2]["at"])

    def test_terminated_mapping_uses_termination_event_and_original_native_kind(self):
        plan = self.source_plan("terminated_subscription")
        calls = self.capture_commands()
        self.source.prepare_case("terminated_subscription", plan)
        self.assertEqual(plan["claimed_at"], plan["end"])
        self.assertIn("terminate_subscription", [kind for _, kind, _ in calls])
        link = next(p for _, kind, p in calls if kind == "link_invoice")
        self.assertEqual(link["native_invoice_kind"], "TERMINATION")
        self.assertEqual((link["native_period_start"], link["native_period_end"]), (plan["start"], plan["end"]))
        self.assertNotIn("open_coverage", [kind for _, kind, _ in calls])
        self.assertNotIn("record_payment", [kind for _, kind, _ in calls])

    def test_only_termination_negative_case_can_preserve_unaligned_usage(self):
        for lifecycle, empty in (("terminated", False), ("terminated", True), ("renewal", False), ("renewal", True)):
            with self.subTest(lifecycle=lifecycle, empty=empty):
                label = "terminated_subscription" if lifecycle == "terminated" else "v2_boundary"
                spec = deepcopy(next(s for s in self.specs if s["label"] == label))
                spec["distractor_units"] = 0
                native = NativeObjects(self.config, self.root / (lifecycle + "/seed.json"), run_id=lifecycle + "_run", specs=[spec])
                customer = {"external_customer_id": "customer", "chatwoot_contact_id": 11, "source_id": "source"}
                sub_id = native.run_id + "_" + label
                invoice = {"lago_id": "invoice", "subscriptions": [{"external_id": sub_id}], "fees": [{"external_subscription_id": sub_id,
                    "item": {"type": "subscription"}, "from_date": "2026-11-10T00:00:00Z", "to_date": "2026-11-10T01:00:00Z"}]}
                raw = {"lago_invoice_id": "invoice", "from_datetime": "2026-11-10T00:00:00Z", "to_datetime": "2026-11-10T00:59:59Z",
                    "charges_usage": [{"billable_metric": {"code": code}, "units": "0"} for code in ("main", "other")]}
                if empty:
                    raw["charges_usage"] = []
                    raw["to_datetime"] = "2026-11-10T01:00:00Z"
                original_raw = deepcopy(raw)
                def write(key, _api, _method, _path, _body=None):
                    if key.endswith(":subscription"):
                        return {"subscription": {"lago_id": "sub-native"}}
                    return {"id": 12, "created_at": "2026-11-10T00:30:00Z", "content": "原消息"}
                native.write_once = Mock(side_effect=write)
                native.subscription_invoice = Mock(return_value=invoice)
                def read(method, path):
                    native.state["receipts"].append({"service": "lago", "method": method, "path": path,
                        "status": 200, "observed_at": "2026-11-10T01:01:00Z"})
                    if "/invoices/" in path:
                        return {"invoice": invoice}
                    return {"usage_periods": [raw]} if "/past_usage" in path else {"customer_usage": raw}
                native.lago.request = Mock(side_effect=read)
                native.poll = lambda reader, predicate, **kwargs: reader()
                if lifecycle == "renewal":
                    with self.assertRaisesRegex(SeedError, "USAGE_METRIC_MISSING_OR_AMBIGUOUS" if empty else "RELEASE_NATIVE_PERIOD_NOT_ALIGNED"):
                        native._subscription(spec, customer, [{"code": "main"}, {"code": "other"}], {"id": 7})
                else:
                    actual = native._subscription(spec, customer, [{"code": "main"}, {"code": "other"}], {"id": 7})
                    self.assertEqual(actual["native_usage_aligned"], empty)
                    self.assertIn("NOT_ELIGIBILITY_EVIDENCE", actual["usage_qualification"])
                    self.assertEqual(actual["native_usage_metric_present"], not empty)
                    self.assertEqual(actual["observed_units"], None if empty else "0")
                    self.assertEqual(actual["usage"], original_raw)
                    self.assertEqual(actual["period_end"], "2026-11-10T01:00:00+00:00")
                self.assertEqual(raw, original_raw)

    def test_expired_contract_is_valid_at_renewal_but_expires_exactly_at_claim(self):
        plan = self.source_plan("expired_contract")
        calls = self.capture_commands()
        self.source.prepare_case("expired_contract", plan)
        contract = next(p for _, kind, p in calls if kind == "put_contract")
        self.assertEqual(contract["status"], "ACTIVE")
        self.assertEqual(contract["effective_to"], plan["claimed_at"])
        self.assertLess(contract["effective_from"], plan["start"])
        self.assertIn(plan["claimed_at"], contract["source_document"]["text"])
        self.assertEqual(hashlib.sha256(contract["source_document"]["text"].encode()).hexdigest(), contract["structured_source_sha256"])

    def test_draft_and_special_contracts_keep_text_and_structured_terms_consistent(self):
        for label, clause in (("draft_contract", None), ("special_contract", "EGAP-LAB-SPECIAL-1")):
            with self.subTest(label=label):
                plan = self.source_plan(label)
                calls = self.capture_commands()
                self.source.prepare_case(label, plan)
                contract = next(p for _, kind, p in calls if kind == "put_contract")
                self.assertEqual(contract["terms"]["refund_addendum"], clause)
                if clause:
                    self.assertIn(clause, contract["source_document"]["text"])
                else:
                    self.assertEqual(contract["status"], "DRAFT")
                    self.assertIsNone(contract["acceptance"])

    def test_late_usage_is_predeclared_but_not_sent_or_sealed_during_prepare(self):
        plan = self.source_plan("late_usage")
        calls = self.capture_commands()
        self.source.send_usage = Mock(side_effect=AssertionError("late event must remain pending"))
        self.source.prepare_case("late_usage", plan)
        kinds = [kind for _, kind, _ in calls]
        self.assertIn("open_coverage", kinds)
        self.assertNotIn("reconcile_coverage", kinds)
        self.assertNotIn("seal_coverage", kinds)
        self.assertEqual(plan["usage"]["charges_usage"][0]["units"], "0")
        self.assertEqual(plan["expected_events"][0]["units"], "1")
        self.source.send_usage.assert_not_called()

    def test_post_claim_usage_is_outside_business_interval_but_inside_native_period(self):
        plan = self.source_plan("post_claim_usage")
        calls = self.capture_commands()
        self.source.send_usage = Mock(return_value={})
        self.source.prepare_case("post_claim_usage", plan)
        event = plan["expected_events"][0]
        self.assertGreater(event["occurred_at"], plan["claimed_at"])
        self.assertGreaterEqual(event["native_occurred_at"], plan["start"])
        self.assertLessEqual(event["native_occurred_at"], plan["end"])
        self.source.send_usage.assert_called_once_with("post_claim_usage")
        self.assertLess([k for _, k, _ in calls].index("open_coverage"), [k for _, k, _ in calls].index("record_payment"))

    def test_bad_payment_cases_change_one_precise_business_fact(self):
        for label, amount, currency in (("overpaid", 12001, "USD"), ("wrong_currency", 12000, "EUR")):
            with self.subTest(label=label):
                plan = self.source_plan(label)
                calls = self.capture_commands()
                self.source.payment(label, plan)
                payment = calls[0][2]
                self.assertEqual((payment["amount_minor"], payment["currency"]), (amount, currency))
                self.assertEqual(payment["source_kind"], "SIMULATED_PAYMENT")
                self.assertEqual(payment["authority_scope"], "LOCAL_LAB")

    def test_usage_dispatch_requires_durable_expected_event_registration(self):
        self.source_plan("late_usage")
        with patch.object(release_seed, "NativeObjects") as native:
            with self.assertRaisesRegex(SeedError, "RELEASE_USAGE_NOT_PREDECLARED"):
                self.source.send_usage("late_usage")
            native.assert_not_called()

    def test_actual_event_receipt_and_aggregate_are_recorded_before_seal(self):
        plan = self.source_plan("late_usage")
        self.source.state["commands"]["late_usage:coverage"] = {"state": "COMPLETE"}
        event = plan["expected_events"][0]
        request = {"event": {"transaction_id": event["event_id"], "external_subscription_id": plan["subscription_id"],
            "code": self.native["metric_code"], "timestamp": int(datetime.fromisoformat(event["native_occurred_at"]).timestamp()), "properties": {"units": "1"}}}
        native_event = {**request["event"], "timestamp": event["native_occurred_at"], "lago_id": "actual-event-id"}
        writer = Mock()
        writer.state = {"receipts": [{"service": "lago", "method": "POST", "path": "/api/v1/events", "status": 201,
            "observed_at": "2026-11-10T00:40:00Z", "request_sha256": sha(request)}]}
        writer.write_once.return_value = {"event": native_event}
        raw = {**deepcopy(plan["usage"]), "charges_usage": [{"billable_metric": {"code": self.native["metric_code"]}, "units": "1"}]}
        def read(method, path):
            writer.state["receipts"].append({"service": "lago", "method": method, "path": path, "status": 200,
                "observed_at": "2026-11-10T00:40:01Z", "response_sha256": sha({"customer_usage": raw})})
            return {"customer_usage": raw}
        writer.lago.request.side_effect = read
        writer.poll.side_effect = lambda reader, predicate, **kwargs: reader()
        calls = self.capture_commands()
        with patch.object(release_seed, "NativeObjects", return_value=writer):
            self.source.send_usage("late_usage")
        self.assertEqual([kind for _, kind, _ in calls], ["record_usage_sent", "record_usage_received", "reconcile_coverage", "seal_coverage"])
        self.assertEqual(calls[0][2]["receipt"]["observed_at"], "2026-11-10T00:40:00Z")
        self.assertEqual(calls[0][2]["receipt"]["native_event"]["lago_id"], "actual-event-id")
        self.assertEqual(calls[2][2]["raw_response"]["charges_usage"][0]["units"], "1")

    def test_no_eligibility_answer_or_credentials_enter_any_source_command(self):
        self.source.send_usage = Mock(return_value={})
        calls = self.capture_commands()
        for spec in self.specs:
            self.source.prepare_case(spec["label"], self.source_plan(spec["label"]))
        def inspect(value):
            if isinstance(value, dict):
                self.assertFalse(set(value) & {"expected", "eligibility", "expected_eligibility", "approved", "api_key", "admin_token"})
                for child in value.values():
                    inspect(child)
            elif isinstance(value, list):
                for child in value:
                    inspect(child)
        for _, _, payload in calls:
            inspect(payload)


if __name__ == "__main__":
    unittest.main()

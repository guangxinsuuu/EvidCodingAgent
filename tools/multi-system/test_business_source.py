"""Business Source Pack 驱动安全边界；不连接产品、数据库或模型。

测试入口 → 临时 0600 准备账本 → BusinessPack.command/plan/prepare_case。
模拟只用于验证驱动的恢复与资料流转协议，不作为真实联调通过证据。
"""

from copy import deepcopy
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
import hashlib
from io import StringIO
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import Mock, patch

import business_source
from business_source import BusinessCheck, BusinessPack, message_ref
from seed import SeedError, load_runtime, save_private, sha


class BusinessSourceSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.directory = self.root / "experiment"
        self.native = {"run_id": "driver-unit-run", "metric_code": "driver-units", "chatwoot_account_id": 17,
                       "created_at": "2026-11-09T12:00:00+00:00", "customers": [], "bindings": {"cases": []}}
        self.ready = {"base_url": "http://127.0.0.1:58103", "instance_id": "driver-source-instance", "source_digest": "a" * 64}
        self.private = {"admin_token": "admin-sentinel-never-in-command-or-state"}
        self.runtime = {"chatwoot": {"base_url": "http://127.0.0.1:58101", "api_token": "chatwoot-sentinel-private", "account_id": 17},
                        "lago": {"base_url": "http://127.0.0.1:58102", "api_key": "lago-sentinel-private"}}
        for path, content in ((self.directory / "native/seed.json", self.native),
                              (self.directory / "ready.json", self.ready),
                              (self.directory / "runtime.private.json", self.private),
                              (self.root / "artifacts/multi-system/runtime.json", self.runtime)):
            save_private(path, content)
        # 真实模板/规则是待验证输入；仅复制到临时目录，生产资产不被测试改写。
        for kind in ("policies", "contracts"):
            shutil.copytree(business_source.ROOT / "business-source-pack/v1" / kind,
                            self.root / "business-source-pack/v1" / kind)
        self.root_patch = patch.object(business_source, "ROOT", self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.pack = BusinessPack(self.directory)

    def result(self, value=None):
        return {"operation_key": "driver-unit-run:asset", "type": "put_policy", "source_revision": 1,
                "result": value or {"policy_id": "refund", "version": "v1"}}

    def test_unknown_receipt_replays_identical_durable_command_after_restart(self):
        payload = {"tenant_id": "tenant-a", "policy_id": "refund", "version": "v1"}
        dispatched = []

        def lost_receipt(method, path, request):
            # 发送发生前必须有可恢复请求；模拟“远端已提交，本地没收到回执”。
            recorded = load_runtime(self.pack.state_path)["commands"]["asset"]["request"]
            self.assertEqual(recorded, request)
            dispatched.append(deepcopy(request))
            raise SeedError("WRITE_RECEIPT_UNKNOWN")

        self.pack.api.request = Mock(side_effect=lost_receipt)
        with self.assertRaisesRegex(SeedError, "WRITE_RECEIPT_UNKNOWN"):
            self.pack.command("asset", "put_policy", payload)
        resumed = BusinessPack(self.directory)
        resumed.api.request = Mock(return_value=self.result())
        answer = resumed.command("asset", "put_policy", deepcopy(payload))
        self.assertEqual(answer, self.result()["result"])
        resumed.api.request.assert_called_once_with("POST", "/v1/commands", dispatched[0])
        self.assertEqual(load_runtime(resumed.state_path)["commands"]["asset"]["state"], "COMPLETE")
        resumed.api.request.reset_mock()
        self.assertEqual(resumed.command("asset", "put_policy", payload), answer)
        resumed.api.request.assert_not_called()

    def test_payload_change_rejected_for_complete_and_unknown_commands(self):
        for completed in (False, True):
            with self.subTest(completed=completed):
                key = "complete" if completed else "unknown"
                self.pack.api.request = Mock(return_value=self.result()) if completed else Mock(side_effect=SeedError("WRITE_RECEIPT_UNKNOWN"))
                original = {"tenant_id": "tenant-a", "terms": {"window_seconds": 1209600}}
                if completed:
                    self.pack.command(key, "put_policy", original)
                else:
                    with self.assertRaises(SeedError):
                        self.pack.command(key, "put_policy", original)
                self.pack.api.request.reset_mock()
                changed = {"tenant_id": "tenant-a", "terms": {"window_seconds": 604800}}
                with self.assertRaisesRegex(SeedError, "COMMERCIAL_COMMAND_PAYLOAD_CHANGED"):
                    self.pack.command(key, "put_policy", changed)
                self.pack.api.request.assert_not_called()

    def test_resume_identity_binds_native_run_source_instance_digest_and_seed(self):
        self.pack.save()
        state = load_runtime(self.pack.state_path)
        self.assertEqual(state["identity"], {"native_run_id": self.native["run_id"],
            "source_instance_id": self.ready["instance_id"], "source_digest": self.ready["source_digest"],
            "native_seed_sha256": sha(self.native)})
        self.assertEqual(BusinessPack(self.directory).state["identity"], state["identity"])
        variants = (("ready.json", {**self.ready, "instance_id": "other-instance"}),
                    ("ready.json", {**self.ready, "source_digest": "b" * 64}),
                    ("native/seed.json", {**self.native, "run_id": "other-run"}),
                    ("native/seed.json", {**self.native, "metric_code": "other-metric"}))
        for path, changed in variants:
            with self.subTest(path=path, changed_keys=list(changed)):
                save_private(self.directory / path, changed)
                with self.assertRaisesRegex(SeedError, "PREPARATION_IDENTITY_MISMATCH"):
                    BusinessPack(self.directory)
                save_private(self.directory / "ready.json", self.ready)
                save_private(self.directory / "native/seed.json", self.native)

    def test_legacy_completed_commands_without_identity_are_not_adopted(self):
        self.pack.state["commands"]["asset"] = {"state": "COMPLETE", "response": self.result()}
        self.pack.state.pop("identity")
        self.pack.save()
        with self.assertRaisesRegex(SeedError, "PREPARATION_IDENTITY_MISMATCH"):
            BusinessPack(self.directory)

    def test_existing_check_marker_prevents_overwriting_prior_evidence(self):
        prior = {"status": "FAILED", "checks": [{"name": "old", "status": "PASS"}]}
        save_private(self.directory / "integration-check.private.json", prior)
        save_private(self.directory / "check-started.private.json", {"identity": self.pack.state["identity"]})
        with self.assertRaisesRegex(SeedError, "BUSINESS_CHECK_ALREADY_STARTED"):
            BusinessCheck(self.pack)
        self.assertEqual(load_runtime(self.directory / "integration-check.private.json"), prior)

    def install_native_reads(self, start="2026-11-09T00:00:00+00:00", *, usage_shift=False):
        left = datetime.fromisoformat(start)
        right = left + timedelta(days=30) - timedelta(seconds=1)
        customer = {"tenant_id": "tenant-a", "external_customer_id": "customer"}
        sub = {"external_id": "subscription", "invoice_id": "invoice", "conversation_id": 41,
               "customer_message_id": 43, "expected_seed_units": 0}
        invoice = {"lago_id": "invoice", "customer": {"external_id": "customer"},
            "subscriptions": [{"external_id": "subscription", "external_customer_id": "customer"}],
            "total_amount_cents": 1000, "currency": "USD", "fees": [{"external_subscription_id": "subscription",
                "item": {"type": "subscription"}, "from_date": left.isoformat(), "to_date": right.isoformat()}]}
        usage = {"from_datetime": (left + timedelta(seconds=int(usage_shift))).isoformat(), "to_datetime": right.isoformat(),
                 "charges_usage": [{"billable_metric": {"code": self.native["metric_code"]}, "units": "0"}]}

        def lago(method, path):
            self.pack.state["receipts"].append({"service": "lago", "method": method, "path": path,
                "status": 200, "observed_at": "2026-11-09T12:00:00+00:00"})
            return {"invoice": deepcopy(invoice)} if "/invoices/" in path else {"customer_usage": deepcopy(usage)}

        self.pack.lago.request = Mock(side_effect=lago)
        self.original_message = {"id": 43, "created_at": "2026-11-09T12:01:00+00:00", "content": "原始退款申请"}
        self.pack.cw.request = Mock(return_value={"payload": [self.original_message]})
        return customer, sub

    def test_historical_policy_date_guard_rejects_v2_period_before_any_write(self):
        customer, sub = self.install_native_reads(start="2026-11-10T00:00:00+00:00")
        self.pack.api.request = Mock(side_effect=AssertionError("must not dispatch commercial write"))
        with self.assertRaisesRegex(SeedError, "HISTORICAL_POLICY_CALIBRATION_DATE_UNAVAILABLE"):
            self.pack.plan("a_zero", customer, sub)
        self.pack.api.request.assert_not_called()
        self.assertNotIn("a_zero", self.pack.state["plans"])

    def test_native_period_mismatch_is_not_relabelled_or_cached(self):
        customer, sub = self.install_native_reads(usage_shift=True)
        with self.assertRaisesRegex(SeedError, "CALIBRATION_NATIVE_PERIOD_NOT_ALIGNED"):
            self.pack.plan("a_zero", customer, sub)
        self.assertNotIn("a_zero", self.pack.state["plans"])

    def captured_preparation(self, label, *, units):
        customer, sub = self.install_native_reads()
        sub["expected_seed_units"] = units
        plan = self.pack.plan(label, customer, sub)
        captured = []

        def command(key, kind, payload):
            captured.append((key, kind, deepcopy(payload)))
            return {"events": [{"event_id": "renewal-event", "event_type": "SUBSCRIPTION_RENEWAL_EFFECTIVE"}]} if key.endswith(":renew") else {}

        self.pack.command = Mock(side_effect=command)
        self.pack.prepare_case(label, plan)
        return plan, captured

    def test_missing_usage_proof_remains_open_and_is_not_sealed(self):
        _plan, commands = self.captured_preparation("a_used", units=7)
        kinds = [kind for _key, kind, _payload in commands]
        coverage = next(payload for _key, kind, payload in commands if kind == "open_coverage")
        self.assertEqual(coverage["expected_events"][0]["units"], "7")
        self.assertNotIn("reconcile_coverage", kinds)
        self.assertNotIn("seal_coverage", kinds)
        self.assertNotIn("record_usage_received", kinds)

    def test_zero_set_still_requires_actual_aggregate_before_seal(self):
        plan, commands = self.captured_preparation("a_zero", units=0)
        kinds = [kind for _key, kind, _payload in commands]
        self.assertLess(kinds.index("open_coverage"), kinds.index("reconcile_coverage"))
        self.assertLess(kinds.index("reconcile_coverage"), kinds.index("seal_coverage"))
        raw = next(payload for _key, kind, payload in commands if kind == "reconcile_coverage")
        self.assertEqual(raw["raw_response"], plan["usage"])
        self.assertNotIn("lago_invoice_id", raw["raw_response"])
        self.assertEqual(raw["receipt"]["scope_kind"], "SUBSCRIPTION_CURRENT_PERIOD")
        self.assertNotIn("record_payment", kinds)  # 正例先缺付款，供显式 reevaluate 验证。

    def test_source_commands_do_not_receive_case_answer_or_credentials(self):
        _plan, commands = self.captured_preparation("a_zero", units=0)
        self.pack.policy("tenant-a", "lab-v1")
        self.pack.policy("tenant-a", "lab-v2")
        commands = [(call.args[0], call.args[1], call.args[2]) for call in self.pack.command.call_args_list]

        def inspect(value):
            if isinstance(value, dict):
                self.assertTrue({"expected", "expected_eligibility", "eligibility", "admin_token", "read_token", "api_token", "api_key", "authorization"}.isdisjoint(value))
                for item in value.values():
                    inspect(item)
            elif isinstance(value, list):
                for item in value:
                    inspect(item)

        for _key, _kind, payload in commands:
            inspect(payload)
        serialized = json.dumps(commands)
        for secret in (self.private["admin_token"], self.runtime["chatwoot"]["api_token"], self.runtime["lago"]["api_key"]):
            self.assertNotIn(secret, serialized)
        self.pack.save()
        self.assertNotIn(self.private["admin_token"], self.pack.state_path.read_text())

    def test_contract_special_clause_and_text_hash_are_consistent(self):
        _plan, commands = self.captured_preparation("b_used", units=7)
        contract = next(payload for _key, kind, payload in commands if kind == "put_contract")
        self.assertEqual(contract["terms"]["refund_addendum"], "EGAP-LAB-SPECIAL-1")
        self.assertTrue(contract["terms"]["special_terms"])
        document = contract["source_document"]
        self.assertIn(contract["terms"]["refund_addendum"], document["text"])
        digest = hashlib.sha256(document["text"].encode("utf-8")).hexdigest()
        self.assertEqual(digest, document["sha256"])
        self.assertEqual(digest, contract["structured_source_sha256"])
        self.assertEqual(contract["human_review_status"], "PENDING_USER_REVIEW")

    def test_policy_assets_bind_structured_terms_to_exact_local_rule_text(self):
        for version in ("lab-v1", "lab-v2"):
            with self.subTest(version=version):
                asset = json.loads((self.root / f"business-source-pack/v1/policies/{version}.json").read_text())
                document = asset["source_document"]
                actual = hashlib.sha256(document["text"].encode("utf-8")).hexdigest()
                self.assertEqual(actual, document["sha256"])
                self.assertEqual(actual, asset["structured_source_sha256"])
                self.assertEqual(asset["human_review_status"], "PENDING_USER_REVIEW")
                # 来源可提供规则；校准表的逐例预期不能随规则原文一起塞给 SUT。
                self.assertNotIn("人可独立推算的校准样本", document["text"])

    def test_check_failure_is_persisted_as_failed_without_losing_prior_assertions(self):
        evidence = self.directory / "check-test.private.json"
        failed_check = Mock()
        failed_check.result = {"status": "RUNNING", "checks": [{"name": "prior", "status": "PASS"}]}
        failed_check.run.side_effect = SeedError("CHECK_FAILED_CONTROLLED_CASE")
        failed_check.checkpoint.side_effect = lambda: save_private(evidence, failed_check.result)
        stderr = StringIO()
        with patch.object(business_source, "BusinessPack", return_value=self.pack), \
                patch.object(business_source, "BusinessCheck", return_value=failed_check), \
                patch("sys.argv", ["business_source.py", "check", "--directory", str(self.directory)]), redirect_stderr(stderr):
            self.assertEqual(business_source.main(), 2)
        persisted = load_runtime(evidence)
        self.assertEqual(persisted["status"], "FAILED")
        self.assertEqual(persisted["checks"], [{"name": "prior", "status": "PASS"}])
        self.assertEqual(json.loads(stderr.getvalue())["error_code"], "CHECK_FAILED_CONTROLLED_CASE")

    def test_claim_message_and_native_initial_invoice_are_not_rewritten(self):
        plan, commands = self.captured_preparation("a_zero", units=0)
        by_kind = {kind: payload for _key, kind, payload in commands}
        claim = by_kind["create_claim"]
        self.assertEqual(claim["original_message"], message_ref(self.original_message))
        self.assertNotEqual(plan["claimed_at"], claim["original_message"]["created_at"])
        clock = next(payload for key, _kind, payload in commands if key.endswith(":claim-clock"))
        self.assertEqual(clock["to"], plan["claimed_at"])
        self.assertNotIn("claimed_at", claim)  # 实际来源按已推进业务时钟生成，不捏造原生消息时间。
        link = by_kind["link_invoice"]
        self.assertEqual(link["native_invoice_kind"], "INITIAL")
        self.assertEqual((link["native_period_start"], link["native_period_end"]), (plan["start"], plan["end"]))
        activation = by_kind["activate_subscription"]
        self.assertEqual(datetime.fromisoformat(activation["period_end"]) + timedelta(seconds=1), datetime.fromisoformat(plan["start"]))

    def test_message_reference_epoch_conversion_keeps_original_content_hash(self):
        original = {"id": 23, "created_at": 1794225600, "content": "后来补充的材料"}
        frozen = deepcopy(original)
        value = message_ref(original)
        self.assertEqual(original, frozen)
        self.assertEqual(value["created_at"], datetime.fromtimestamp(original["created_at"], timezone.utc).isoformat())
        self.assertEqual(value["content_sha256"], hashlib.sha256(original["content"].encode()).hexdigest())


if __name__ == "__main__":
    unittest.main()

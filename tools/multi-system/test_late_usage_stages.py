"""用量分步端口的恢复/观察隔离测试；mock HTTP 不作为真实实验验收证据。"""
from copy import deepcopy
from datetime import datetime
import unittest
from unittest.mock import Mock, patch

import release_seed
from release_seed import NativeObjects, SourceObjects, fresh_usage_specs
from seed import SeedError, load_runtime, save_private, sha
import test_release_seed as release_seed_tests


class UsageStageTests(unittest.TestCase):
    setUp = release_seed_tests.ReleaseObjectsTests.setUp
    source_plan = release_seed_tests.ReleaseObjectsTests.source_plan
    capture_commands = release_seed_tests.ReleaseObjectsTests.capture_commands

    def registered_plan(self, label="late_usage"):
        plan = self.source_plan(label)
        self.source.state["commands"][label + ":coverage"] = {"state": "COMPLETE"}
        self.source.save()
        return plan

    def native_writer(self, plan):
        """只模拟已确认原生回执，未知写测试另外使用真实 write_once 账本。"""
        event = plan["expected_events"][0]
        request = {"event": {"transaction_id": event["event_id"], "external_subscription_id": plan["subscription_id"],
            "code": self.native["metric_code"], "timestamp": int(datetime.fromisoformat(event["native_occurred_at"]).timestamp()),
            "properties": {"units": event["units"]}}}
        writer = Mock()
        writer.state = {"receipts": [{"service": "lago", "method": "POST", "path": "/api/v1/events", "status": 201,
            "observed_at": "2026-11-10T00:40:00Z", "request_sha256": sha(request)}]}
        writer.write_once.return_value = {"event": {**request["event"], "timestamp": event["native_occurred_at"], "lago_id": "real-response-event"}}
        return writer

    def usage_reader(self, plan, values):
        values = iter(values)
        def read(method, path):
            units = next(values)
            raw = deepcopy(plan["usage"])
            raw["charges_usage"][0]["units"] = str(units)
            # 另一指标无论多大都不得计入指定指标的观察。
            raw["charges_usage"].append({"billable_metric": {"code": "unrelated"}, "units": "999"})
            body = {"customer_usage": raw}
            self.source.state["receipts"].append({"service": "lago", "method": method, "path": path, "status": 200,
                "observed_at": "2026-11-10T00:40:01Z", "response_sha256": sha(body)})
            return body
        self.source.lago.request = Mock(side_effect=read)

    def dispatch(self, plan):
        with patch.object(release_seed, "NativeObjects", return_value=self.native_writer(plan)):
            return self.source.dispatch_usage(plan["label"])

    def test_dispatch_observe_receive_reconcile_and_seal_are_separate(self):
        plan = self.registered_plan()
        calls = self.capture_commands()
        self.usage_reader(plan, [0, 1])
        result = self.dispatch(plan)
        self.assertEqual(result["status"], "SENT")
        self.source.lago.request.assert_not_called()
        self.assertEqual([kind for _, kind, _ in calls], ["record_usage_sent"])
        waiting = self.source.observe_usage("late_usage", observation_id="first")
        self.assertEqual(waiting["status"], "PENDING")
        self.assertEqual(len(calls), 1)
        with self.assertRaisesRegex(SeedError, "AGGREGATE_NOT_VISIBLE"):
            self.source.record_usage_received("late_usage", observation_id="first")
        visible = self.source.observe_usage("late_usage", observation_id="second")
        self.assertEqual((visible["status"], visible["observed_units"]), ("AGGREGATE_VISIBLE", "1"))
        self.assertEqual(len(calls), 1)
        self.source.record_usage_received("late_usage", observation_id="second")
        self.assertEqual([kind for _, kind, _ in calls], ["record_usage_sent", "record_usage_received"])
        with self.assertRaisesRegex(SeedError, "NOT_RECONCILED"):
            self.source.seal_usage("late_usage")
        self.source.reconcile_usage("late_usage", observation_id="second")
        self.assertEqual(calls[-1][1], "reconcile_coverage")
        self.assertNotIn("seal_coverage", [kind for _, kind, _ in calls])
        self.source.seal_usage("late_usage")
        self.assertEqual(calls[-1][1], "seal_coverage")
        self.assertEqual(self.source.lago.request.call_count, 2)

    def test_observation_id_freezes_read_and_new_id_reads_once_after_restart(self):
        plan = self.registered_plan()
        self.usage_reader(plan, [0])
        first = self.source.observe_usage("late_usage", observation_id="first")
        self.source = SourceObjects(self.directory)
        self.usage_reader(plan, [1])
        self.assertEqual(self.source.observe_usage("late_usage", observation_id="first"), first)
        self.source.lago.request.assert_not_called()
        self.assertEqual(self.source.observe_usage("late_usage", observation_id="second")["status"], "AGGREGATE_VISIBLE")
        self.source.lago.request.assert_called_once()
        self.assertNotIn("usage_completions", self.source.state)

    def test_observation_cannot_substitute_for_a_send_or_receive_receipt(self):
        plan = self.registered_plan()
        self.usage_reader(plan, [1])
        self.source.observe_usage("late_usage", observation_id="visible")
        self.source.command = Mock(side_effect=AssertionError("read must not imply source write"))
        with self.assertRaisesRegex(SeedError, "SEND_NOT_RECORDED"):
            self.source.record_usage_received("late_usage", observation_id="visible")
        with self.assertRaisesRegex(SeedError, "RECEIVE_NOT_RECORDED"):
            self.source.reconcile_usage("late_usage", observation_id="visible")
        with self.assertRaisesRegex(SeedError, "RECEIVE_NOT_RECORDED"):
            self.source.seal_usage("late_usage")
        self.source.command.assert_not_called()

    def test_unknown_native_write_stops_redispatch_and_keeps_transaction_identity(self):
        plan = self.registered_plan()
        writer = NativeObjects(self.config, self.directory / "native/seed.json", specs=self.native["scenario_specs"], run_id=self.native["run_id"])
        writer.lago.request = Mock(side_effect=SeedError("WRITE_RECEIPT_UNKNOWN"))
        self.source.command = Mock(side_effect=AssertionError("unknown send must not be registered"))
        with patch.object(release_seed, "NativeObjects", return_value=writer):
            with self.assertRaisesRegex(SeedError, "WRITE_RECEIPT_UNKNOWN"):
                self.source.dispatch_usage("late_usage")
            with self.assertRaisesRegex(SeedError, "REQUIRES_RECONCILIATION"):
                self.source.dispatch_usage("late_usage")
        writer.lago.request.assert_called_once()
        self.assertEqual(writer.lago.request.call_args.args[-1]["event"]["transaction_id"], plan["expected_events"][0]["event_id"])
        self.assertEqual(next(iter(writer.state["operations"].values()))["state"], "UNKNOWN")
        self.source.command.assert_not_called()

    def test_get_failure_is_saved_and_read_can_be_retried_without_a_write(self):
        plan = self.registered_plan()
        def unavailable(method, path):
            self.source.state["receipts"].append({"service": "lago", "method": method, "path": path, "outcome": "UNAVAILABLE"})
            raise SeedError("ENV_NOT_READY")
        self.source.lago.request = Mock(side_effect=unavailable)
        with self.assertRaisesRegex(SeedError, "ENV_NOT_READY"):
            self.source.observe_usage("late_usage", observation_id="retry-read")
        persisted = load_runtime(self.source.state_path)
        self.assertEqual(persisted["receipts"][-1]["outcome"], "UNAVAILABLE")
        self.assertNotIn("retry-read", persisted["usage_observations"]["late_usage"])
        self.usage_reader(plan, [1])
        result = self.source.observe_usage("late_usage", observation_id="retry-read")
        self.assertEqual(result["status"], "AGGREGATE_VISIBLE")
        self.assertNotIn("usage_completions", self.source.state)

    def test_unaligned_raw_period_is_rejected_and_receipt_is_preserved(self):
        plan = self.registered_plan()
        original = deepcopy(plan["usage"])
        plan["usage"]["to_datetime"] = "2026-12-09T23:59:58Z"
        self.usage_reader(plan, [1])
        with self.assertRaisesRegex(SeedError, "NATIVE_PERIOD_NOT_ALIGNED"):
            self.source.observe_usage("late_usage", observation_id="bad-period")
        self.assertNotIn("bad-period", self.source.state["usage_observations"]["late_usage"])
        self.assertEqual(load_runtime(self.source.state_path)["receipts"][-1]["status"], 200)
        self.assertNotEqual(plan["usage"], original)
        self.assertEqual(plan["usage"]["to_datetime"], "2026-12-09T23:59:58Z")

    def test_receive_selection_is_frozen_before_unknown_source_result(self):
        plan = self.registered_plan()
        self.capture_commands()
        self.dispatch(plan)
        self.usage_reader(plan, [1, 1])
        first = self.source.observe_usage("late_usage", observation_id="first")
        self.source.command = Mock(side_effect=SeedError("WRITE_RECEIPT_UNKNOWN"))
        with self.assertRaisesRegex(SeedError, "WRITE_RECEIPT_UNKNOWN"):
            self.source.record_usage_received("late_usage", observation_id="first")
        self.assertEqual(load_runtime(self.source.state_path)["usage_completions"]["late_usage"], first)
        second = self.source.observe_usage("late_usage", observation_id="second")
        second["receipt"]["observed_at"] = "2026-11-10T00:41:00Z"
        self.source.state["usage_observations"]["late_usage"]["second"] = second
        with self.assertRaisesRegex(SeedError, "COMPLETION_CHANGED"):
            self.source.record_usage_received("late_usage", observation_id="second")
        self.assertEqual(self.source.command.call_count, 1)

    def test_fresh_post_claim_preparation_does_not_send_or_seal(self):
        self.specs = fresh_usage_specs("2026-11-10T00:00:00Z")
        self.source.specs = {row["label"]: row for row in self.specs}
        for label in ("late_usage", "post_claim_usage"):
            with self.subTest(label=label):
                plan = self.source_plan(label)
                calls = self.capture_commands()
                self.source.dispatch_usage = Mock(side_effect=AssertionError("explicit stage required"))
                self.source.prepare_case(label, plan)
                kinds = [kind for _, kind, _ in calls]
                self.assertIn("open_coverage", kinds)
                self.assertNotIn("record_usage_sent", kinds)
                self.assertNotIn("seal_coverage", kinds)
                self.source.dispatch_usage.assert_not_called()

    def initial_source_reader(self, plan, *, sent=False):
        source = {"coverage": {"coverage_id": plan["coverage_id"], "status": "OPEN",
            "expected_events": deepcopy(plan["expected_events"]), "pending_count": 1,
            "sent_event_ids": [plan["expected_events"][0]["event_id"]] if sent else [], "received_event_ids": [],
            "reconciliation": None, "sealed_at": None},
            "claim": {"claim_id": plan["claim_id"], "claimed_at": plan["claimed_at"], "original_message": plan["original_message"]}}
        reader = Mock()
        def read(method, path):
            self.source.state["receipts"].append({"service": "commercial-observer", "method": method, "path": path,
                "status": 200, "observed_at": "2026-11-10T00:40:02Z", "response_sha256": sha(source)})
            return deepcopy(source)
        reader.request.side_effect = read
        return reader

    def test_initial_state_requires_independent_zero_open_pending_and_fixed_claim(self):
        plan = self.registered_plan()
        self.usage_reader(plan, [0, 0])
        save_private(self.directory / "native/seed-state.json", {"run_id": self.native["run_id"], "operations": {}})
        with patch.object(release_seed, "Api", return_value=self.initial_source_reader(plan)):
            result = self.source.observe_initial_state("late_usage", observation_id="before")
        self.assertEqual(result["status"], "READY")
        self.assertTrue(all(result["checks"].values()))
        save_private(self.directory / "native/seed-state.json", {"run_id": self.native["run_id"], "operations": {
            "release-usage:" + plan["expected_events"][0]["event_id"]: {"state": "UNKNOWN"}}})
        with patch.object(release_seed, "Api", return_value=self.initial_source_reader(plan)):
            later = self.source.observe_initial_state("late_usage", observation_id="after-unknown")
        self.assertEqual(later["status"], "NOT_READY")
        self.assertFalse(later["checks"]["no_native_dispatch"])
        self.assertEqual(load_runtime(self.source.state_path)["initial_state_observations"]["late_usage"]["before"], result)

    def test_compatibility_pipeline_stops_when_aggregate_remains_pending(self):
        plan = self.registered_plan()
        calls = self.capture_commands()
        self.usage_reader(plan, [0])
        with patch.object(release_seed, "NativeObjects", return_value=self.native_writer(plan)):
            with self.assertRaisesRegex(SeedError, "ASYNC_PRODUCT_NOT_READY"):
                self.source.send_usage("late_usage", timeout=0)
        self.assertEqual([kind for _, kind, _ in calls], ["record_usage_sent"])
        self.assertNotIn("usage_completions", self.source.state)


if __name__ == "__main__":
    unittest.main()

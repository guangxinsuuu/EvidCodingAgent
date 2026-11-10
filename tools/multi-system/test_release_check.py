"""发布验证驱动的失败与证据边界；不连接模型、业务API或数据库。

候选协议失败必须保留并继续其他案例；观察缺失不能伪造通过。准备/启动失败
发生在证据标记之前，已存在报告不因标记缺失而被覆盖。
"""
from pathlib import Path
from copy import deepcopy
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

import release_check
from release_check import CandidateProtocolError, ReleaseCheck
from release_oracle import EXPECTATIONS
from seed import SeedError, save_private
from test_release_oracle import example


def bare_checker(directory: Path) -> ReleaseCheck:
    """构造不含任何真实端点/凭据的驱动，保留真实run/terminal/inspect控制逻辑。"""
    checker = ReleaseCheck.__new__(ReleaseCheck)
    checker.directory = directory
    checker.result = {"checks": [], "jobs": [], "cases": [], "findings": [], "case_errors": [], "receipts": [],
                      "verification_code": release_check.verification_manifest()}
    checker.dataset = {"bindings": {"cases": [{"label": label, "tenant_id": "tenant-a"} for label in EXPECTATIONS]}}
    checker.completed = {label: {} for label in ("v1_history", "v2_boundary", "late_usage")}
    checker.checkpoint = Mock()
    checker.snapshot = Mock()
    checker.cw = Mock()
    checker.lago = Mock()
    checker.cw_prefix = "/api/v1/accounts/1"
    return checker


class ReleaseCheckSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.checker = bare_checker(self.directory)

    def test_candidate_protocol_failure_does_not_stop_other_cases_or_become_environment_error(self):
        observed = []
        def one(case):
            observed.append(case["label"])
            if case["label"] == "v2_late":
                raise CandidateProtocolError("CANDIDATE_DRAFT_PROTOCOL_INVALID")
            self.checker.result["cases"].append({"label": case["label"]})
        self.checker.one = Mock(side_effect=one)
        with patch.object(release_check, "run_boundaries"), patch.object(release_check, "candidate_unchanged", return_value=True):
            result = self.checker.run()
        self.assertEqual(observed, list(EXPECTATIONS))
        self.assertEqual(result["case_errors"], [])
        self.assertEqual(result["status"], "FAIL")
        self.assertFalse(result["release_authorized"])
        self.assertTrue(any(row["name"] == "v2_late:candidate_protocol" and row["status"] == "FAIL" for row in result["checks"]))

    def test_missing_native_observation_is_incomplete_but_other_cases_still_execute(self):
        observed = []
        def one(case):
            observed.append(case["label"])
            if case["label"] == "overpaid":
                raise SeedError("ORIGINAL_MESSAGE_OBSERVATION_MISSING")
        self.checker.one = Mock(side_effect=one)
        with patch.object(release_check, "run_boundaries"), patch.object(release_check, "candidate_unchanged", return_value=True):
            result = self.checker.run()
        self.assertEqual(len(observed), 12)
        self.assertEqual(result["status"], "INCOMPLETE")
        self.assertEqual(result["case_errors"], [{"label": "overpaid", "error_code": "ORIGINAL_MESSAGE_OBSERVATION_MISSING"}])
        self.assertFalse(result["release_authorized"])

    def test_observed_failed_job_is_retained_and_never_inspected_as_pass(self):
        failed = {"job": {"job_id": "j1", "status": "FAILED", "draft": None, "error_code": "PROCESSING_FAILED"}, "delivery": None}
        client = Mock()
        client.request.return_value = failed
        actual = self.checker.terminal(client, {"tenant_id": "tenant-a"}, "j1", timeout=0)
        self.assertEqual(actual, failed)
        self.assertEqual(self.checker.result["jobs"], [failed])
        inspected = self.checker.inspect({"label": "v1_history"}, actual, "initial")
        self.assertIsNone(inspected)
        self.assertEqual(self.checker.result["checks"][-1]["status"], "FAIL")
        self.checker.snapshot.assert_not_called()
        self.checker.cw.request.assert_not_called()

    def test_terminal_malformed_job_keeps_response_and_raises_candidate_protocol(self):
        client = Mock()
        client.request.return_value = {"job": {"job_id": "j1"}, "delivery": None}
        with self.assertRaisesRegex(CandidateProtocolError, "CANDIDATE_JOB_PROTOCOL_INVALID"):
            self.checker.terminal(client, {"tenant_id": "tenant-a"}, "j1", timeout=0)
        self.assertEqual(self.checker.result["jobs"], [client.request.return_value])
        client.request.assert_called_once()

    def test_nonobject_delivery_and_unknown_status_are_candidate_protocol_errors(self):
        for response in ({"job": {"job_id": "j1", "status": "DONE"}, "delivery": "COMPLETE"},
                         {"job": {"job_id": "j1", "status": "UNRECOGNIZED"}, "delivery": None}):
            with self.subTest(response=response):
                client = Mock()
                client.request.return_value = response
                with self.assertRaises(CandidateProtocolError):
                    self.checker.terminal(client, {"tenant_id": "tenant-a"}, "j1", timeout=0)

    def test_done_missing_draft_is_candidate_failure_before_native_reads(self):
        response = {"job": {"job_id": "j1", "status": "DONE", "draft": None},
                    "delivery": {"delivery_state": "COMPLETE"}}
        with self.assertRaisesRegex(CandidateProtocolError, "CANDIDATE_DRAFT_PROTOCOL_INVALID"):
            self.checker.inspect({"label": "v2_boundary"}, response, "initial")
        self.checker.snapshot.assert_not_called()
        self.checker.cw.request.assert_not_called()

    def test_bad_claim_snapshot_records_fail_and_stops_dependent_stage(self):
        case, result, context, observations = example()
        case.update(label="v1_history", customer_message_id=5)
        source_readback = deepcopy(context["commercial_source"])
        context["commercial_source"]["claim"] = None
        self.checker.snapshot.return_value = context
        self.checker.cw.request.side_effect = [{"payload": observations["messages"]}, observations["conversation"]]
        self.checker.lago.request.side_effect = [{"invoice": observations["invoice"]}, {"customer_usage": observations["usage"]}]
        self.checker.pack = SimpleNamespace(ready={"base_url": "http://127.0.0.1:58103"}, private={"read_token": "unit-test-only"})
        reader = Mock()
        reader.request.return_value = source_readback
        with patch.object(release_check, "Api", return_value=reader):
            inspected = self.checker.inspect(case, result, "initial")
        self.assertIsNone(inspected)
        self.assertEqual(len(self.checker.result["cases"]), 1)
        self.assertTrue(any(row["name"] == "candidate_protocol_and_business_value_shape" and row["status"] == "FAIL"
                            for row in self.checker.result["checks"]))

    def test_existing_marker_report_or_expectations_cannot_be_overwritten(self):
        for name in ("release-check-started.private.json", "release-check.private.json", "verification-expectations.private.json"):
            with self.subTest(name=name):
                path = self.directory / name
                save_private(path, {"retain": "prior evidence"})
                before = path.read_bytes()
                with patch.object(release_check, "SourceObjects", return_value=Mock()), \
                        patch.object(release_check, "candidate_unchanged", return_value=True), \
                        patch.object(release_check, "load_runtime", return_value={}), \
                        patch.object(release_check.Check, "__init__", side_effect=AssertionError("must not initialize over evidence")):
                    with self.assertRaisesRegex(SeedError, "RELEASE_CHECK_ALREADY_STARTED"):
                        ReleaseCheck(self.directory)
                self.assertEqual(path.read_bytes(), before)
                path.unlink()

    def test_candidate_change_rejected_before_evidence_marker_creation(self):
        with patch.object(release_check, "SourceObjects", return_value=Mock()), \
                patch.object(release_check, "candidate_unchanged", return_value=False):
            with self.assertRaisesRegex(SeedError, "TARGET_CANDIDATE_CHANGED"):
                ReleaseCheck(self.directory)
        self.assertFalse((self.directory / "release-check-started.private.json").exists())
        self.assertFalse((self.directory / "release-check.private.json").exists())

    def test_missing_sut_configuration_does_not_leave_started_marker(self):
        with patch.object(release_check, "SourceObjects", return_value=Mock()), \
                patch.object(release_check, "candidate_unchanged", return_value=True), \
                patch.object(release_check, "load_runtime", side_effect=SeedError("ENV_NOT_READY")):
            with self.assertRaisesRegex(SeedError, "ENV_NOT_READY"):
                ReleaseCheck(self.directory)
        self.assertFalse((self.directory / "release-check-started.private.json").exists())

    def test_untracked_candidate_code_is_not_the_frozen_commit(self):
        responses = [SimpleNamespace(returncode=0, stdout=b""),
                     SimpleNamespace(returncode=0, stdout=b"services/genai-support-sut/src/genai_support/new_behavior.py\n")]
        with patch.object(release_check.subprocess, "run", side_effect=responses) as command:
            self.assertFalse(release_check.candidate_unchanged())
        self.assertEqual(command.call_count, 2)
        self.assertIn("ls-files", command.call_args.args[0])

    def test_tracked_candidate_change_short_circuits_manifest_acceptance(self):
        with patch.object(release_check.subprocess, "run", return_value=SimpleNamespace(returncode=1, stdout=b"")) as command:
            self.assertFalse(release_check.candidate_unchanged())
        command.assert_called_once()


if __name__ == "__main__":
    unittest.main()

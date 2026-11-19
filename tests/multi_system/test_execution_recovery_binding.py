"""部署回执绑定及终态证据恢复测试；仅使用临时文件和只读 spy，不启动业务服务。"""
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools/multi-system"))
import late_usage_execution as execution
from experiment_ledger import LedgerError
from seed import SeedError, save_private, sha


class DeploymentBindingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.directory = self.root / "experiment"
        self.environment_id = "environment_review"
        self.environment = self.root / "artifacts/lab/genai-support-runtime" / self.environment_id
        self.executor = execution.ExecutionSlice.__new__(execution.ExecutionSlice)
        self.executor.directory = self.directory
        self.executor.manifest = {"experiment_id": "experiment-review", "candidate": {"files": {}, "candidate_sha": "a" * 40}}
        self.executor.ledger = Mock()
        self.executor.ledger.get_operation.return_value = {"status": "COMPLETED"}
        self.frozen_config = {"scope": "test-only"}
        self.sut = {"base_url": "http://127.0.0.1:51111", "environment_id": self.environment_id,
            "runtime_ref": str(self.environment / "runtime.json"), "source_manifest_ref": str(self.environment / "source-manifest.json"),
            "connections_ref": str(self.environment / "connections.private.json"), "candidate_sha": "a" * 40,
            "integration_config_sha256": sha(self.frozen_config)}
        self.runtime = {"instance_id": self.environment_id, "endpoints": {"support-gateway": self.sut["base_url"]}, "processes": {}}
        for pid, role in enumerate(("api", "worker", "relay", "gateway", "model"), 1):
            signature = f"test process -m genai_support.runtime --role {role} --instance {self.environment_id} --config {self.environment / (role + '.private.json')}"
            self.runtime["processes"][role] = {"pid": pid, "signature": signature, "config_ref": role + ".private.json"}
        self.host = Mock()
        self.host.process_signature.side_effect = lambda pid: next(row["signature"] for row in self.runtime["processes"].values() if row["pid"] == pid)
        for target, value in ((self.directory / "sut/sut.json", self.sut),
                              (self.environment / "runtime.json", self.runtime),
                              (self.environment / "source-manifest.json", {"files": {}}),
                              (self.environment / "integration.private.json", self.frozen_config),
                              (self.environment / "connections.private.json", {"fixture": True})):
            save_private(target, value)
        self.executor.ledger.execute.return_value = {"replayed": True, "result": deepcopy(self.sut)}
        for patcher in (patch.object(execution, "ROOT", self.root), patch.object(execution, "check_product"),
                        patch.object(execution, "load_host", return_value=self.host)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_deployment_replays_completed_receipt_and_never_runs_callback(self):
        self.assertEqual(self.executor.verify_deployment(), self.sut)
        arguments = self.executor.ledger.execute.call_args
        self.assertEqual(arguments.args, ("experiment-review", "deploy.sut"))
        self.assertEqual(arguments.kwargs["request"], {"candidate_sha": "a" * 40})
        with self.assertRaisesRegex(SeedError, "MUST_NOT_DISPATCH"):
            arguments.kwargs["callback"](Mock())

    def test_modified_local_endpoint_cannot_borrow_valid_process_identity(self):
        changed = {**self.sut, "base_url": "http://127.0.0.1:59999"}
        save_private(self.directory / "sut/sut.json", changed)
        with self.assertRaisesRegex(SeedError, "DEPLOYMENT_INDEX_CHANGED"):
            self.executor.verify_deployment()
        self.host.process_signature.assert_not_called()

    def test_completed_replay_propagates_raw_evidence_integrity_failure(self):
        self.executor.ledger.execute.side_effect = LedgerError("LEDGER_EVIDENCE_INTEGRITY_FAILED")
        with self.assertRaisesRegex(LedgerError, "EVIDENCE_INTEGRITY_FAILED"):
            self.executor.verify_deployment()
        self.host.process_signature.assert_not_called()

    def test_unknown_deployment_cannot_be_redispatched_by_verification(self):
        self.executor.ledger.get_operation.return_value = {"status": "UNKNOWN"}
        with self.assertRaisesRegex(SeedError, "DEPLOYMENT_RECEIPT_NOT_COMPLETE"):
            self.executor.verify_deployment()
        self.executor.ledger.execute.assert_not_called()

    def test_runtime_endpoint_and_environment_must_match_receipt(self):
        for field, replacement in (("instance_id", "environment_elsewhere"), ("endpoints", {"support-gateway": "http://127.0.0.1:59999"})):
            with self.subTest(field=field):
                changed = {**self.runtime, field: replacement}
                save_private(self.environment / "runtime.json", changed)
                with self.assertRaisesRegex(SeedError, "ENDPOINT_INSTANCE_MISMATCH"):
                    self.executor.verify_deployment()

    def test_foreign_runtime_source_and_connections_references_are_rejected(self):
        for field in ("runtime_ref", "source_manifest_ref", "connections_ref"):
            with self.subTest(field=field):
                changed = {**self.sut, field: str(self.root / "elsewhere" / Path(self.sut[field]).name)}
                save_private(self.directory / "sut/sut.json", changed)
                # 单独测试归属保护；即便冻结回执本身给出另一实例路径也应拒绝。
                self.executor.ledger.execute.return_value = {"replayed": True, "result": changed}
                with self.assertRaisesRegex(SeedError, "REFERENCE_SCOPE_MISMATCH"):
                    self.executor.verify_deployment()

    def test_matching_pid_signature_must_still_belong_to_this_environment(self):
        self.runtime["processes"]["api"]["signature"] = self.runtime["processes"]["api"]["signature"].replace("--instance environment_review", "--instance environment_other")
        save_private(self.environment / "runtime.json", self.runtime)
        with self.assertRaisesRegex(SeedError, "PROCESS_SCOPE_MISMATCH"):
            self.executor.verify_deployment()


class TerminalRecoveryTests(unittest.TestCase):
    def complete_package(self):
        names = ("sut_job", "sut_snapshot", "sut_accepted_event", "chatwoot_messages", "chatwoot_conversation", "lago_invoice", "lago_usage", "commercial_source")
        return {"wait_status": "TERMINAL", "protocol_status": "VALID", "result": {"job": {"job_id": "job-original"}},
            "context": {}, "accepted_event": {}, "reads": [{"name": name, "state": "OBSERVED"} for name in names],
            "observations": {"source_context": {}, "invoice": {}, "usage": {}, "conversation": {}, "original_message": {}, "messages": []},
            "gaps": ["DETERMINISTIC_DECISION_NOT_CAPTURED", "ACTUAL_MODEL_REQUEST_NOT_CAPTURED", "RAW_MODEL_OUTPUT_NOT_CAPTURED"]}

    def test_model_trace_capability_gaps_do_not_force_more_reads(self):
        self.assertTrue(execution.ExecutionSlice._job_observation_reusable(self.complete_package()))

    def test_unavailable_independent_read_and_missing_snapshot_are_not_reusable(self):
        for field in ("unavailable", "context", "accepted_event", "original_message"):
            with self.subTest(field=field):
                package = self.complete_package()
                if field == "unavailable":
                    package["reads"][-1]["state"] = "UNAVAILABLE"
                elif field == "original_message":
                    package["observations"][field] = None
                else:
                    package[field] = None
                self.assertFalse(execution.ExecutionSlice._job_observation_reusable(package))

    def test_terminal_native_failure_uses_new_bounded_read_key_on_restart(self):
        first = self.complete_package()
        first["observations"]["usage"] = None
        first["reads"][6]["state"] = "UNAVAILABLE"
        row = {"operation_key": "late_usage.observe.j1.1", "status": "COMPLETED", "result": first}
        executor = execution.ExecutionSlice.__new__(execution.ExecutionSlice)
        executor.manifest = {"experiment_id": "experiment-review"}
        executor.ledger = Mock()
        executor.ledger.list_operations.return_value = [row]
        executor.step = Mock(return_value=self.complete_package())
        observer = Mock()
        result = executor.wait_job(observer, {"label": "late_usage"}, "job-original", "j1")
        self.assertEqual(result["wait_status"], "TERMINAL")
        self.assertEqual(executor.step.call_args.args[:4], ("late_usage.observe.j1.2", "OBSERVE_ASSESSMENT", "READ", {"job_id": "job-original"}))
        observer.submit.assert_not_called()


if __name__ == "__main__":
    unittest.main()

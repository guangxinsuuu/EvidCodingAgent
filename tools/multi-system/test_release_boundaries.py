"""边界驱动单测：验证精确停止/恢复、并发与证据行为；不连接真实系统。"""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import release_boundaries as boundaries
from seed import SeedError, save_private


class FakeEnvironment:
    def __init__(self):
        self.running = True
        self.host_calls = []
        self.restore_failure = False
        self.stop_failure = False
        self.wrong_restored_port = False
        self.ignore_outage = False
        self.increase_budget = False
        self.refresh_threads = []
        self.refresh_keys = []
        self.refresh_client_ids = []
        self.counts = {"jobs": 2, "gateway_calls": 2, "reserved_calls": 2}
        self.ready = {"status": "READY", "instance_id": "commercial_unit", "base_url": "http://127.0.0.1:51000",
            "source_digest": "d" * 64, "api_version": "commercial-source-v1", "authority_scope": "LOCAL_LAB"}
        self.private = {"instance_id": "commercial_unit", "read_token": "reader-secret-sentinel", "admin_token": "admin-secret-sentinel"}

    def host(self, action, directory):
        self.host_calls.append((action, directory))
        if action == "status":
            return {"instance_id": "commercial_unit", "source_unchanged": True, "ready": self.running}
        if action == "stop":
            self.running = False
            if self.stop_failure:
                raise boundaries.BoundaryError("BOUNDARY_HOST_OPERATION_UNCONFIRMED")
            return {"status": "STOPPED", "instance_id": "commercial_unit"}
        if action == "start":
            if self.restore_failure:
                raise boundaries.BoundaryError("BOUNDARY_HOST_OPERATION_UNCONFIRMED")
            self.running = True
            return {**self.ready, **({"base_url": "http://127.0.0.1:51001"} if self.wrong_restored_port else {})}
        raise AssertionError("unexpected host action")

    def api(self, base_url, headers, *, service, receipts):
        environment = self
        class FakeApi:
            def __init__(self):
                self.base_url, self.headers, self.service, self.receipts = base_url, headers, service, receipts

            def request(self, method, path, body=None, *, expected=()):
                if service == "commercial":
                    if path == "/health/ready":
                        status, value = 200, {"instance_id": "commercial_unit"}
                    else:
                        status, value = 401, {"error_code": "ROLE_REQUIRED"}
                elif (body or {}).get("invoice_id") == "invoice-b":
                    status, value = 403, {"error_code": "INVOICE_NOT_AUTHORIZED"}
                elif path.endswith("/reevaluate"):
                    environment.refresh_threads.append(threading.get_ident())
                    environment.refresh_keys.append(body["operation_key"])
                    environment.refresh_client_ids.append(id(self))
                    if environment.increase_budget:
                        environment.counts["reserved_calls"] += 1
                    status, value = 202, {"job_id": "history-job", "reused": True}
                elif not environment.running and not environment.ignore_outage:
                    status, value = 503, {"error_code": "EXTERNAL_UNAVAILABLE"}
                else:
                    status, value = 202, {"job_id": "boundary-job", "reused": True}
                self.receipts.append({"status": status, "method": method, "path": path})
                return value
        return FakeApi()


class FakeCheck:
    def __init__(self, environment):
        self.environment = environment
        self.sut = {"runtime_ref": "/irrelevant/runtime.json", "connections_ref": "/irrelevant/connections.private.json"}
        self.config = {"chatwoot": {"account_id": 1}}
        self.result = {"checks": [], "receipts": []}
        self.checkpoint = Mock()

    def client(self, tenant):
        return self.environment.api("http://127.0.0.1:52000", {"Authorization": "Bearer sut-secret-sentinel"}, service="sut", receipts=[])

    def assertion(self, name, condition, **evidence):
        self.result["checks"].append({"name": name, "status": "PASS" if condition else "FAIL", **evidence})

    def invoice_reads(self, _invoice):
        return 0


class BoundaryExecutionTests(unittest.TestCase):
    def setUp(self):
        self.environment = FakeEnvironment()
        self.checker = FakeCheck(self.environment)
        self.directory = Path("/test/artifacts/business-source/release-unit")
        self.cases = {"v1_history": {"tenant_id": "tenant-a", "conversation_id": 1, "invoice_id": "invoice-a"},
            "v2_boundary": {"tenant_id": "tenant-a", "conversation_id": 2, "invoice_id": "invoice-boundary"},
            "other_tenant": {"tenant_id": "tenant-b", "conversation_id": 3, "invoice_id": "invoice-b"}}
        self.jobs = {"v1_history": {"job": {"job_id": "history-job"}}, "v2_boundary": {"job": {"job_id": "boundary-job"}}}
        patches = [patch.object(boundaries, "_directory_identity", return_value=(self.directory, self.environment.ready, self.environment.private)),
            patch.object(boundaries, "_host", side_effect=self.environment.host),
            patch.object(boundaries, "Api", side_effect=self.environment.api),
            patch.object(boundaries, "_counts", side_effect=lambda _checker: deepcopy(self.environment.counts))]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def run_boundary(self):
        return boundaries.run_boundaries(self.checker, self.directory, self.cases, self.jobs)

    def test_success_uses_separate_concurrent_clients_and_exact_restoration(self):
        result = self.run_boundary()
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["source_recovery"], "RESTORED")
        self.assertTrue(all(check["passed"] for check in result["checks"]))
        self.assertEqual([action for action, _ in self.environment.host_calls], ["status", "stop", "start", "status"])
        self.assertTrue(all(directory == self.directory for _, directory in self.environment.host_calls))
        self.assertEqual(len(set(self.environment.refresh_threads)), 2)
        self.assertEqual(len(set(self.environment.refresh_keys)), 2)
        self.assertEqual(len(set(self.environment.refresh_client_ids)), 2)
        encoded = json.dumps(self.checker.result)
        for secret in ("reader-secret-sentinel", "admin-secret-sentinel", "sut-secret-sentinel"):
            self.assertNotIn(secret, encoded)

    def test_wrong_source_down_behavior_is_failure_but_source_still_restored(self):
        self.environment.ignore_outage = True
        result = self.run_boundary()
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["source_recovery"], "RESTORED")
        self.assertTrue(self.environment.running)
        self.assertFalse(next(row for row in result["checks"] if row["name"] == "source_down_is_503")["passed"])

    def test_partial_stop_failure_still_attempts_restore_and_reports_original_failure(self):
        self.environment.stop_failure = True
        with self.assertRaisesRegex(boundaries.BoundaryError, "BOUNDARY_HOST_OPERATION_UNCONFIRMED"):
            self.run_boundary()
        self.assertTrue(self.environment.running)
        result = self.checker.result["boundaries"][0]
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["source_recovery"], "RESTORED")
        self.assertIn("completed_at", result)

    def test_restore_failure_is_not_swallowed(self):
        self.environment.restore_failure = True
        with self.assertRaisesRegex(boundaries.BoundaryError, "BOUNDARY_SOURCE_RESTORE_FAILED"):
            self.run_boundary()
        result = self.checker.result["boundaries"][0]
        self.assertEqual((result["status"], result["source_recovery"]), ("FAILED", "FAILED"))
        self.assertEqual(result["error_code"], "BOUNDARY_SOURCE_RESTORE_FAILED")
        self.assertFalse(self.environment.running)
        self.checker.checkpoint.assert_called()

    def test_different_restored_port_is_failure_even_if_health_is_200(self):
        self.environment.wrong_restored_port = True
        with self.assertRaisesRegex(boundaries.BoundaryError, "BOUNDARY_SOURCE_RESTORE_FAILED"):
            self.run_boundary()
        self.assertEqual(self.checker.result["boundaries"][0]["source_recovery"], "FAILED")

    def test_hidden_model_reservation_is_detected_even_when_job_id_reuses(self):
        self.environment.increase_budget = True
        result = self.run_boundary()
        self.assertEqual(result["status"], "FAIL")
        self.assertFalse(next(row for row in result["checks"] if row["name"] == "concurrent_no_job_or_model_budget")["passed"])


class BoundaryIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.directory = self.root / "artifacts/business-source/release-unit"
        self.runtime = self.root / "sut/runtime.json"
        self.ready = FakeEnvironment().ready
        self.private = FakeEnvironment().private
        frozen = {key: self.ready[key] for key in ("instance_id", "base_url", "source_digest", "api_version")}
        frozen["read_token"] = self.private["read_token"]
        for path, value in ((self.directory / "ready.json", self.ready), (self.directory / "runtime.private.json", self.private),
                            (self.runtime.parent / "integration.private.json", {"commercial_source": frozen})):
            save_private(path, value)
        self.checker = Mock(sut={"runtime_ref": str(self.runtime)})
        self.patch_root = patch.object(boundaries, "ROOT", self.root)
        self.patch_root.start()
        self.addCleanup(self.patch_root.stop)

    def test_requires_same_frozen_source_before_any_process_action(self):
        actual = boundaries._directory_identity(self.checker, self.directory)
        self.assertEqual(actual[1]["instance_id"], self.ready["instance_id"])
        save_private(self.directory / "ready.json", {**self.ready, "instance_id": "commercial_wrong"})
        with self.assertRaisesRegex(boundaries.BoundaryError, "BOUNDARY_SOURCE_IDENTITY_MISMATCH"):
            boundaries._directory_identity(self.checker, self.directory)

    def test_refuses_external_directory_and_symlink(self):
        with self.assertRaisesRegex(boundaries.BoundaryError, "BOUNDARY_SOURCE_DIRECTORY_INVALID"):
            boundaries._directory_identity(self.checker, self.root / "unrelated")
        link = self.directory.with_name("alias")
        link.symlink_to(self.directory, target_is_directory=True)
        with self.assertRaisesRegex(boundaries.BoundaryError, "BOUNDARY_SOURCE_DIRECTORY_INVALID"):
            boundaries._directory_identity(self.checker, link)

    def test_unknown_http_result_stays_unconfirmed(self):
        api = Mock(receipts=[])
        api.request.side_effect = SeedError("WRITE_RECEIPT_UNKNOWN")
        observed = boundaries._observed(api, "POST", "/integrations/disputes", {})
        self.assertIsNone(observed["status"])
        self.assertEqual(observed["error_code"], "BOUNDARY_REQUEST_UNCONFIRMED")

    def test_host_rejects_cleanup_action_without_running_subprocess(self):
        with patch.object(boundaries.subprocess, "run") as run:
            with self.assertRaisesRegex(boundaries.BoundaryError, "BOUNDARY_HOST_ACTION_FORBIDDEN"):
                boundaries._host("down", self.directory)
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()

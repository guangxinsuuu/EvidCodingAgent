"""宿主的身份与恢复边界；真实 PostgreSQL/HTTP 联调另记证据，不混计为单测。"""

from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch


spec = importlib.util.spec_from_file_location("commercial_host", Path(__file__).with_name("commercial-source.py"))
host = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host)


class HostBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        # macOS 的 /var 是系统链接，测试使用其真实目录，仍拒绝实验路径中的链接。
        self.base = Path(self.temporary.name).resolve()
        self.directory = self.base / "run-one"
        self.directory.mkdir()
        self.identity = "commercial_" + "1" * 32
        self.state = {"project": host.PROJECT, "instance_id": self.identity, "run_id": "run-one",
                      "directory": str(self.directory), "container_name": "egap-commercial-pg-" + "1" * 32,
                      "volume_name": "egap-commercial-pg-" + "1" * 32,
                      "status": "STOPPED", "source_digest": "a" * 64,
                      "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}
        host.save(self.directory / "state.json", self.state)

    def test_directory_cannot_target_product_or_symlink(self):
        with patch.object(host, "BASE", self.base):
            self.assertEqual(host.directory_path(self.directory), self.directory)
            with self.assertRaisesRegex(host.HostError, "EXACT_BUSINESS_SOURCE_DIRECTORY_REQUIRED"):
                host.directory_path(self.base.parent / "multi-system")
            alias = self.base / "alias"
            alias.symlink_to(self.directory, target_is_directory=True)
            with self.assertRaisesRegex(host.HostError, "SYMLINK"):
                host.directory_path(alias)

    def test_resume_source_change_stops_before_database_or_process_mutation(self):
        with patch.object(host, "source_manifest", return_value={"sha256": "b" * 64}), \
                patch.object(host, "ensure_database") as database, patch.object(host, "start_service") as service:
            with self.assertRaisesRegex(host.HostError, "SOURCE_CHANGED"):
                host.execute("start", self.directory)
        database.assert_not_called()
        service.assert_not_called()

    def test_expired_resume_does_not_extend_lease(self):
        self.state["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        host.save(self.directory / "state.json", self.state)
        with patch.object(host, "source_manifest", return_value={"sha256": "a" * 64}), patch.object(host, "ensure_database") as database:
            with self.assertRaisesRegex(host.HostError, "EXPERIMENT_EXPIRED"):
                host.execute("start", self.directory)
        database.assert_not_called()
        self.assertEqual(host.private_read(self.directory / "state.json")["expires_at"], self.state["expires_at"])

    def test_foreign_container_label_prevents_delete(self):
        foreign = {"id": "2" * 64, "labels": {"io.egap.project": "egap-multisystem"}}
        with patch.object(host, "stop_process"), patch.object(host, "docker", return_value=json.dumps(foreign)) as docker:
            with self.assertRaisesRegex(host.HostError, "OWNERSHIP_MISMATCH"):
                host.execute("down", self.directory)
        self.assertEqual(len(docker.call_args_list), 1)
        self.assertEqual(docker.call_args.args[1][:2], ["container", "inspect"])

    def test_changed_pid_signature_never_receives_signal(self):
        self.state["process"] = {"pid": 12345, "signature": "original", "status": "RUNNING"}
        with patch.object(host, "process_signature", return_value="unrelated"), patch.object(host.os, "kill") as kill:
            with self.assertRaisesRegex(host.HostError, "PROCESS_IDENTITY_MISMATCH"):
                host.stop_process(self.directory, self.state)
        kill.assert_not_called()

    def test_start_receipt_recovers_only_matching_source_command(self):
        name = "source-" + "f" * 32 + ".log"
        self.state["process"] = {"status": "STARTING", "log_ref": name}
        receipt = {"event": "COMMERCIAL_SOURCE_READY", "instance_id": self.identity,
                   "host": "127.0.0.1", "port": 54321, "pid": 12345}
        (self.directory / name).write_text(json.dumps(receipt) + "\n")
        signature = "time -m commercial_source --config " + str(self.directory / "runtime.private.json")
        with patch.object(host, "process_signature", return_value=signature):
            host.reconcile_start(self.directory, self.state)
        self.assertEqual(self.state["process"]["status"], "RUNNING")
        self.assertEqual(self.state["http_port"], 54321)

    def test_start_receipt_never_adopts_unrelated_process(self):
        name = "source-" + "f" * 32 + ".log"
        self.state["process"] = {"status": "STARTING", "log_ref": name}
        (self.directory / name).write_text(json.dumps({"event": "COMMERCIAL_SOURCE_READY", "instance_id": self.identity,
                                                      "host": "127.0.0.1", "port": 54321, "pid": 12345}))
        with patch.object(host, "process_signature", return_value="unrelated process"):
            with self.assertRaisesRegex(host.HostError, "PROCESS_IDENTITY_MISMATCH"):
                host.reconcile_start(self.directory, self.state)

    def test_connection_failure_is_not_missing_container(self):
        result = Mock(returncode=1, stdout="", stderr="Cannot connect to the Docker daemon")
        with patch.object(host.subprocess, "run", return_value=result):
            with self.assertRaisesRegex(host.HostError, "DOCKER_OPERATION_FAILED"):
                host.docker(self.directory, ["container", "inspect", "exact"], missing="exact")

    def test_only_exact_absence_is_reconcilable(self):
        for message, absent in (("Error: No such object: exact", True), ("Error: No such object: other", False)):
            with self.subTest(message=message), patch.object(host.subprocess, "run", return_value=Mock(returncode=1, stdout="", stderr=message)):
                if absent:
                    self.assertIsNone(host.docker(self.directory, ["container", "inspect", "exact"], missing="exact"))
                else:
                    with self.assertRaises(host.HostError):
                        host.docker(self.directory, ["container", "inspect", "exact"], missing="exact")

    def test_read_projection_contains_no_database_or_admin_secrets(self):
        self.state.update(http_port=54321, tenant_scopes=["tenant-a"])
        secret = {"read_token": "read-" + "r" * 32, "admin_token": "never-copy-admin", "postgres_password": "never-copy-pg"}
        with patch.object(host, "http_health", return_value={"instance_id": self.identity, "api_version": host.API_VERSION}), \
                patch.object(host, "source_manifest", return_value={"sha256": "a" * 64}):
            host.publish_ready(self.directory, self.state, secret)
        projection = host.private_read(self.directory / "commercial-reader.private.json")
        self.assertEqual(projection["read_token"], secret["read_token"])
        self.assertNotIn("never-copy", (self.directory / "commercial-reader.private.json").read_text())
        self.assertNotIn(secret["read_token"], (self.directory / "ready.json").read_text())
        self.assertNotIn("database_url", projection)
        self.assertEqual((self.directory / "commercial-reader.private.json").stat().st_mode & 0o777, 0o600)

    def test_status_does_not_write_or_initialize(self):
        with patch.object(host, "inspect_container", return_value=None), \
                patch.object(host, "source_manifest", return_value={"sha256": "a" * 64}), \
                patch.object(host, "save") as save, patch.object(host, "ensure_database") as database:
            result = host.execute("status", self.directory)
        self.assertFalse(result["source_process_running"])
        save.assert_not_called()
        database.assert_not_called()

    def test_tampered_resource_name_rejected_before_docker(self):
        self.state["container_name"] = "egap-multisystem-lago-1"
        host.save(self.directory / "state.json", self.state)
        with patch.object(host, "docker") as docker:
            with self.assertRaisesRegex(host.HostError, "RESOURCE_NAME_MISMATCH"):
                host.execute("down", self.directory)
        docker.assert_not_called()


if __name__ == "__main__":
    unittest.main()

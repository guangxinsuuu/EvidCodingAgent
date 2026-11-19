"""固定计划 CLI 的候选与恢复测试；临时 Git 是真实仓库，业务端口均为 spy。

PG 原子性由 test_experiment_ledger.py 的真实数据库测试负责；这里的 FakeLedger
仅模拟完成回放/输入冲突，检查编排是否正确选择新 READ 键且不重复提交业务 Job。
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools/multi-system"))
import late_usage_execution as execution
from experiment_ledger import IdentityConflict, OperationUnknown
from seed import SeedError, sha


class FakeDispatch:
    def __init__(self):
        self.evidence = []
        self.sent = []

    def record_evidence(self, label, payload):
        self.evidence.append((label, deepcopy(payload)))

    def record_sent(self, receipt):
        self.sent.append(deepcopy(receipt))


class FakeLedger:
    """只模拟编排所需契约；不替代真实 PG 锁、事务或崩溃恢复测试。"""
    def __init__(self, rows=()):
        self.rows = {row["operation_key"]: deepcopy(row) for row in rows}
        self.calls = []
        self.dispatches = []

    def list_operations(self, experiment_id):
        return deepcopy(list(self.rows.values()))

    def execute(self, experiment_id, key, *, kind, effect, request, callback):
        self.calls.append({"key": key, "kind": kind, "effect": effect, "request": deepcopy(request)})
        previous = self.rows.get(key)
        if previous:
            if previous.get("request") != request:
                raise IdentityConflict("LEDGER_OPERATION_IDENTITY_CONFLICT")
            if previous["status"] == "COMPLETED":
                return {**deepcopy(previous), "replayed": True}
            raise OperationUnknown("LEDGER_OPERATION_UNKNOWN_RECONCILE_REQUIRED")
        dispatch = FakeDispatch()
        self.dispatches.append((key, dispatch))
        result = callback(dispatch)
        row = {"operation_key": key, "kind": kind, "effect": effect, "request": deepcopy(request),
               "status": "COMPLETED", "result": deepcopy(result)}
        self.rows[key] = row
        return {**deepcopy(row), "replayed": False}

    def resume_summary(self, experiment_id):
        return {"experiment_id": experiment_id, "total": len(self.rows)}


class CandidateManifestTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.patch_root = patch.object(execution, "ROOT", self.root)
        self.patch_root.start()
        self.addCleanup(self.patch_root.stop)
        self.git("init", "-q")
        self.git("config", "user.name", "Ledger Tests")
        self.git("config", "user.email", "ledger-tests@example.invalid")
        self.sources = {"services/genai-support-sut/src/support/entry.py": b"VALUE = 1\n",
                        "services/genai-support-sut/migrations/001.sql": b"SELECT 1;\n",
                        "datasets/genai-support/synthetic-v1/sut/policy.json": b'{"policy":1}\n',
                        "services/genai-support-sut/pyproject.toml": b'[project]\nname = "fixture"\n',
                        "services/genai-support-sut/uv.lock": b"version = 1\n"}
        for name, content in self.sources.items():
            self.write(name, content)
        self.git("add", ".")
        self.git("commit", "-qm", "candidate fixture")
        self.candidate = self.git("rev-parse", "HEAD").decode().strip()

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True).stdout

    def write(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def test_committed_candidate_is_bound_to_actual_product_bytes(self):
        manifest = execution.candidate_manifest(self.candidate)
        self.assertEqual(manifest["candidate_sha"], self.candidate)
        self.assertEqual(manifest["files"], {name: hashlib.sha256(content).hexdigest()
                                              for name, content in self.sources.items()})
        self.assertEqual(manifest["product_sha256"], sha(manifest["files"]))
        execution.check_product(manifest)

    def test_short_candidate_and_existing_source_mismatch_are_rejected(self):
        with self.assertRaisesRegex(SeedError, "FULL_CANDIDATE_COMMIT_REQUIRED"):
            execution.candidate_manifest(self.candidate[:8])
        name = next(iter(self.sources))
        self.write(name, b"VALUE = 2\n")
        with self.assertRaisesRegex(SeedError, "CANDIDATE_WORKTREE_MISMATCH"):
            execution.candidate_manifest(self.candidate)

    def test_source_drift_after_manifest_freeze_is_rejected(self):
        manifest = execution.candidate_manifest(self.candidate)
        self.write(next(iter(self.sources)), b"VALUE = 3\n")
        with self.assertRaisesRegex(SeedError, "CANDIDATE_SOURCE_DRIFT"):
            execution.check_product(manifest)

    def test_symlink_with_identical_bytes_cannot_impersonate_candidate_source(self):
        name = next(iter(self.sources))
        actual = self.write("outside.py", self.sources[name])
        (self.root / name).unlink()
        (self.root / name).symlink_to(actual)
        with self.assertRaises(SeedError):
            execution.candidate_manifest(self.candidate)

    def test_untracked_executable_product_is_rejected(self):
        self.write("services/genai-support-sut/src/support/injected.py", b"SIDE_EFFECT = True\n")
        with self.assertRaises(SeedError):
            execution.candidate_manifest(self.candidate)

    def test_newer_tracked_product_file_cannot_be_bound_to_older_candidate(self):
        # 只比旧候选已有文件会漏掉较新 commit 新增的可执行模块；必须核对完整文件集合。
        self.write("services/genai-support-sut/src/support/newer.py", b"NEW_BEHAVIOR = True\n")
        self.git("add", ".")
        self.git("commit", "-qm", "new product module")
        with self.assertRaises(SeedError):
            execution.candidate_manifest(self.candidate)

    def test_new_product_file_after_freeze_is_runtime_drift(self):
        manifest = execution.candidate_manifest(self.candidate)
        self.write("services/genai-support-sut/src/support/late_injection.py", b"CHANGED = True\n")
        with self.assertRaises(SeedError):
            execution.check_product(manifest)


class ObservationRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.directory = self.root / "experiment"
        self.directory.mkdir()
        self.runtime = {"chatwoot": {"base_url": "http://chatwoot.invalid", "api_token": "test-placeholder"},
                        "lago": {"base_url": "http://lago.invalid", "api_key": "test-placeholder"}}
        native_runtime = self.root / "artifacts/multi-system/runtime.json"
        native_runtime.parent.mkdir(parents=True)
        native_runtime.write_text(json.dumps(self.runtime))
        native_runtime.chmod(0o600)
        # 真实候选文件集合与工具副本支撑 step 的身份校验，不 mock 掉新增 provenance。
        product_sources = {"services/genai-support-sut/src/support/entry.py": b"VALUE = 1\n",
                           "services/genai-support-sut/pyproject.toml": b'[project]\nname = "fixture"\n',
                           "services/genai-support-sut/uv.lock": b"version = 1\n"}
        product_files = {}
        for name, content in product_sources.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            product_files[name] = hashlib.sha256(content).hexdigest()
        self.candidate = {"candidate_sha": "a" * 40, "files": product_files,
                          "product_sha256": sha(product_files)}
        self.tool_hashes = {}
        tool_names = ("late_usage_execution.py", "release_seed.py", "late_usage_observer.py",
                      "experiment_ledger.py", "release_oracle.py", "seed.py", "business_source.py", "check.py", "sut.py")
        for name in tool_names:
            content = (Path(execution.__file__).parent / name).read_bytes()
            path = self.root / "tools/multi-system" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            self.tool_hashes[name] = hashlib.sha256(content).hexdigest()
        patcher = patch.object(execution, "ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.case = {"label": "late_usage", "tenant_id": "tenant-b", "account_id": 1,
                     "conversation_id": 2, "invoice_id": "invoice-1"}
        self.ledger = FakeLedger()
        self.executor = self.new_executor()

    def new_executor(self):
        # 跳过构造的 PG 连接，只保留真实 step/wait_job/run 的控制流和配置校验。
        value = execution.ExecutionSlice.__new__(execution.ExecutionSlice)
        value.directory = self.directory
        value.ledger = self.ledger
        value.manifest = {"experiment_id": "experiment-unit", "candidate": deepcopy(self.candidate),
                          "configuration": {"native_configuration_sha256": sha(self.runtime)}, "inputs": {}}
        value.replays = []
        return value

    def observer(self, states):
        observer = Mock()
        packages = iter(states)
        def wait(case, job_id, **kwargs):
            value = next(packages)
            result = {"wait_status": value, "job_id": job_id, "job_state": "PROCESSING",
                      "delivery_state": "PENDING"}
            if "on_observation" in kwargs:
                kwargs["on_observation"](deepcopy(result))
            return result
        observer.wait.side_effect = wait
        return observer

    def seed_observation(self, number, *, status="COMPLETED", wait_status="WAITING", phase="j1"):
        key = f"late_usage.observe.{phase}.{number}"
        result = {"wait_status": wait_status, "job_id": "job-1"}
        if wait_status == "TERMINAL":
            # 终态可缓存还要求独立关键读取齐全；缺模型 trace 不应触发同一 Job 重提。
            result.update(protocol_status="VALID", result={"job": {}}, context={}, accepted_event={},
                          observations={"source_context": {}, "invoice": {}, "usage": {}, "conversation": {},
                                        "original_message": {}, "messages": []},
                          reads=[{"name": name, "state": "OBSERVED"} for name in
                                 ("sut_job", "sut_snapshot", "sut_accepted_event", "chatwoot_messages",
                                  "chatwoot_conversation", "lago_invoice", "lago_usage", "commercial_source")])
        self.ledger.rows[key] = {"operation_key": key, "status": status,
                                 "request": {"job_id": "job-1"},
                                 "result": result if status == "COMPLETED" else None}

    def test_waiting_result_uses_new_read_key_on_restart_and_keeps_same_job(self):
        observer = self.observer(["WAITING", "WAITING"])
        first = self.executor.wait_job(observer, self.case, "job-1", "j1")
        second = self.new_executor().wait_job(observer, self.case, "job-1", "j1")
        self.assertEqual(first["wait_status"], "WAITING")
        self.assertEqual(second["wait_status"], "WAITING")
        self.assertEqual([call["key"] for call in self.ledger.calls],
                         ["late_usage.observe.j1.1", "late_usage.observe.j1.2"])
        self.assertTrue(all(call["effect"] == "READ" and call["request"] == {"job_id": "job-1"}
                            for call in self.ledger.calls))
        self.assertEqual(observer.wait.call_count, 2)
        self.assertTrue(all(dispatch.evidence for _, dispatch in self.ledger.dispatches))
        observer.submit.assert_not_called()

    def test_unknown_and_dispatched_observations_use_new_read_key_without_replaying_old_one(self):
        for status in ("UNKNOWN", "DISPATCHED"):
            with self.subTest(status=status):
                self.ledger = FakeLedger()
                self.seed_observation(1, status=status)
                observer = self.observer(["WAITING"])
                self.new_executor().wait_job(observer, self.case, "job-1", "j1")
                self.assertEqual(self.ledger.calls[0]["key"], "late_usage.observe.j1.2")
                self.assertEqual(self.ledger.rows["late_usage.observe.j1.1"]["status"], status)
                self.assertEqual(observer.wait.call_count, 1)

    def test_terminal_and_protocol_invalid_receipts_are_reused_without_observer_calls(self):
        for terminal in ("TERMINAL", "PROTOCOL_INVALID"):
            with self.subTest(terminal=terminal):
                self.ledger = FakeLedger()
                self.seed_observation(1, wait_status=terminal)
                value = self.new_executor()
                observer = Mock()
                actual = value.wait_job(observer, self.case, "job-1", "j1")
                self.assertEqual(actual["wait_status"], terminal)
                self.assertEqual(value.replays, ["late_usage.observe.j1.1"])
                observer.wait.assert_not_called()

    def test_terminal_replay_rejects_job_identity_change(self):
        self.seed_observation(1, wait_status="TERMINAL")
        observer = Mock()
        with self.assertRaises(IdentityConflict):
            self.executor.wait_job(observer, self.case, "different-job", "j1")
        observer.wait.assert_not_called()

    def test_three_total_read_attempts_are_bounded_across_restarts(self):
        observer = self.observer(["WAITING"] * 3)
        for _ in range(3):
            self.new_executor().wait_job(observer, self.case, "job-1", "j1")
        with self.assertRaisesRegex(SeedError, "JOB_OBSERVATION_BUDGET_EXHAUSTED"):
            self.new_executor().wait_job(observer, self.case, "job-1", "j1")
        self.assertEqual(observer.wait.call_count, 3)
        self.assertEqual(len(self.ledger.dispatches), 3)
        self.assertTrue(all(call["effect"] == "READ" for call in self.ledger.calls))

    def test_unknown_attempts_consume_budget_and_phase_budgets_are_separate(self):
        for index in range(1, 4):
            self.seed_observation(index, status="UNKNOWN")
        observer = self.observer(["WAITING"])
        with self.assertRaisesRegex(SeedError, "JOB_OBSERVATION_BUDGET_EXHAUSTED"):
            self.executor.wait_job(observer, self.case, "job-1", "j1")
        observer.wait.assert_not_called()
        self.executor.wait_job(observer, self.case, "job-2", "j2")
        self.assertEqual(self.ledger.calls[0]["key"], "late_usage.observe.j2.1")

    def test_native_configuration_drift_blocks_dispatch(self):
        path = self.root / "artifacts/multi-system/runtime.json"
        path.write_text(json.dumps({**self.runtime, "changed": True}))
        callback = Mock()
        with self.assertRaisesRegex(SeedError, "NATIVE_CONFIGURATION_DRIFT"):
            self.executor.step("step", "READ", "READ", {}, callback)
        callback.assert_not_called()
        self.assertEqual(self.ledger.calls, [])

    def test_step_records_actual_tool_hashes_and_replay_preserves_original_provenance(self):
        callback = Mock(return_value={"observed": True})
        self.executor.step("identity", "OBSERVE", "READ", {"job_id": "job-1"}, callback)
        first_dispatch = self.ledger.dispatches[0][1]
        label, identity = first_dispatch.evidence[0]
        self.assertEqual(label, "execution_identity")
        self.assertEqual(identity["tools"], self.tool_hashes)
        self.assertEqual(identity["candidate_sha"], self.candidate["candidate_sha"])
        self.assertEqual(identity["product_sha256"], self.candidate["product_sha256"])
        self.assertEqual(identity["scope"], execution.SCOPE)
        original = deepcopy(first_dispatch.evidence)
        changed = self.root / "tools/multi-system/late_usage_execution.py"
        changed.write_bytes(changed.read_bytes() + b"\n# fixture tool revision\n")
        self.new_executor().step("identity", "OBSERVE", "READ", {"job_id": "job-1"}, callback)
        self.assertEqual(callback.call_count, 1)
        self.assertEqual(len(self.ledger.dispatches), 1)
        self.assertEqual(first_dispatch.evidence, original)
        self.new_executor().step("identity.next", "OBSERVE", "READ", {"job_id": "job-1"}, callback)
        latest = self.ledger.dispatches[-1][1].evidence[0][1]
        self.assertEqual(latest["tools"]["late_usage_execution.py"], hashlib.sha256(changed.read_bytes()).hexdigest())
        self.assertNotEqual(latest["tools"], identity["tools"])

    def test_repeated_run_reuses_submission_without_new_model_trigger(self):
        """三轮 WAITING 恢复只观察同一 Job；第四轮耗尽预算也不重提或触发模型。"""
        connections = self.directory / "connections.private.json"
        connections.write_text(json.dumps({"database_url": "unused-no-network"}))
        connections.chmod(0o600)
        sut = {"base_url": "http://sut.invalid", "connections_ref": str(connections)}
        pack = Mock(native={"bindings": {"cases": [self.case]}}, runtime=self.runtime,
                    ready={"base_url": "http://commercial.invalid"}, private={"read_token": "test-placeholder"})
        observer = self.observer(["WAITING"] * 3)
        observer.observe_submission.return_value = {"state": "ACCEPTED", "job_id": "job-1"}
        submissions, model_triggers = [], []
        def api_factory(base_url, headers, *, service, receipts):
            client = Mock()
            def request(method, path, body, **kwargs):
                self.assertEqual(service, "sut")
                self.assertEqual(path, "/integrations/disputes")
                submissions.append(deepcopy(body))
                model_triggers.append(body["operation_key"])
                receipts.append({"status": 202, "method": method, "path": path})
                return {"job_id": "job-1"}
            client.request.side_effect = request
            return client
        with patch.object(execution, "SourceObjects", return_value=pack), \
             patch.object(execution, "Api", side_effect=api_factory), \
             patch.object(execution, "access_token", return_value="test-placeholder"), \
             patch.object(execution, "LateUsageObserver", return_value=observer), \
             patch.object(execution, "assess_observation", return_value={"status": "WAITING"}):
            for _ in range(3):
                value = self.new_executor()
                value.verify_deployment = Mock(return_value=sut)
                report = value.run()
                self.assertEqual(report["cases"], [{"label": "late_usage", "status": "J1_INCOMPLETE"}])
            value = self.new_executor()
            value.verify_deployment = Mock(return_value=sut)
            with self.assertRaisesRegex(SeedError, "JOB_OBSERVATION_BUDGET_EXHAUSTED"):
                value.run()
        self.assertEqual(len(submissions), 1)
        self.assertEqual(len(model_triggers), 1)
        self.assertEqual(submissions[0]["operation_key"], "experiment-unit:late_usage:submit")
        self.assertEqual(observer.wait.call_count, 3)
        self.assertEqual([key for key, _ in self.ledger.dispatches if key.endswith(".submit")], ["late_usage.submit"])
        self.assertNotIn("late_usage.dispatch", self.ledger.rows)


if __name__ == "__main__":
    unittest.main()

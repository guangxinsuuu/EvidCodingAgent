"""宿主恢复边界的少量回归：只用临时文件及mock，不启动服务、连接DB或真实kill。

调用support-lab的实际入口逻辑，验证身份不完整时停在副作用之前；真实HTTP与
PostgreSQL验收另有测试，这里的mock不代表真实进程/容器恢复资格。
"""

import importlib.util
import json
from pathlib import Path
import signal
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("support_lab_under_test", ROOT / "tools/support-lab.py")
assert spec is not None and spec.loader is not None
lab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lab)

INSTANCE = "environment_11111111-1111-4111-8111-111111111111"
LAUNCH = "22222222-2222-4222-8222-222222222222"
START_TIME = "Thu Nov  5 12:34:56 2026"
ROLE_ARGS = f" -m genai_support.runtime --role model --instance {INSTANCE} --config /private/test/model.private.json"
VENV_PYTHON = str(lab.SERVICE / ".venv/bin/python")
APP_PYTHON = str(Path(lab.sys.base_prefix) / "Resources/Python.app/Contents/MacOS/Python")
SIGNATURE = lab.canonical_signature(START_TIME + " " + VENV_PYTHON + ROLE_ARGS)
CHILD_SIGNATURE = START_TIME + " /opt/homebrew/bin/ollama serve"


class HostLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="egap-host-test-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        # 任何新增调用也不能在此单元套件中产生真实kill或数据库初始化副作用。
        self.kill = self.enterContext(patch.object(lab.os, "kill"))
        self.database = self.enterContext(patch.object(lab, "initialize_databases"))
        self.node = self.enterContext(patch.object(lab, "node"))
        self.start_role = self.enterContext(patch.object(lab, "start_role"))
        self.enterContext(patch.object(lab.time, "sleep"))

    def write(self, name: str, value: dict) -> None:
        (self.directory / name).write_text(json.dumps(value), encoding="utf-8")

    def state(self, *, with_pid: bool = True, role: str = "model") -> dict:
        process = {"status": "STARTING", "launch_id": LAUNCH, "config_ref": role + ".private.json"}
        if with_pid:
            process.update(pid=12345, signature=SIGNATURE)
        return {"instance_id": INSTANCE, "processes": {role: process}}

    def receipt(self, **changes) -> dict:
        return {"pid": 12345, "signature": SIGNATURE, "role": "model", "instance_id": INSTANCE,
                "launch_id": LAUNCH, "child_pid": 12346, "child_signature": CHILD_SIGNATURE,
                "status": "STARTING", **changes}

    def metadata(self, *, mode: str = "SCRIPTED_CALIBRATION", expires_at: str = "2099-01-01T00:00:00Z") -> None:
        self.write("environment.json", {"config": {"mode": mode, "model": "qwen2:7b" if mode == "LIVE_MODEL" else None},
            "environment": {"environment_instance_id": INSTANCE, "expires_at": expires_at}})

    def test_python_reexec_aliases_match_but_other_instances_and_programs_do_not(self) -> None:
        venv = START_TIME + " " + VENV_PYTHON + ROLE_ARGS
        app = START_TIME + " " + APP_PYTHON + ROLE_ARGS
        self.assertEqual(lab.canonical_signature(venv), lab.canonical_signature(app))
        resolved_app = START_TIME + " " + str(Path(APP_PYTHON).resolve()) + ROLE_ARGS
        self.assertEqual(lab.canonical_signature(venv), lab.canonical_signature(resolved_app))
        changed_instance = venv.replace(INSTANCE, "environment_33333333-3333-4333-8333-333333333333")
        self.assertNotEqual(lab.canonical_signature(venv), lab.canonical_signature(changed_instance))
        self.assertEqual(lab.canonical_signature(CHILD_SIGNATURE), CHILD_SIGNATURE)
        other_program = START_TIME + " /usr/bin/not-python" + ROLE_ARGS
        self.assertEqual(lab.canonical_signature(other_program), other_program)

    def test_missing_pid_without_matching_receipt_refuses_cleanup(self) -> None:
        for receipt in (None, self.receipt(launch_id="wrong-launch"), self.receipt(instance_id="wrong-instance")):
            with self.subTest(receipt=receipt is not None):
                path = self.directory / "model.receipt.json"
                if path.exists():
                    path.unlink()
                if receipt:
                    self.write(path.name, receipt)
                with self.assertRaisesRegex(RuntimeError, "PROCESS_START_RECEIPT_UNKNOWN"):
                    lab.stop_role(self.directory, "model", self.state(with_pid=False))
        self.kill.assert_not_called()
        self.database.assert_not_called()

    def test_matching_receipt_recovers_missing_parent_pid(self) -> None:
        state = self.state(with_pid=False, role="worker")
        signature = SIGNATURE.replace("--role model", "--role worker")
        self.write("worker.receipt.json", self.receipt(role="worker", signature=signature))
        with patch.object(lab, "process_signature", side_effect=[signature, None]):
            lab.stop_role(self.directory, "worker", state)
        self.kill.assert_called_once_with(12345, signal.SIGTERM)
        self.assertEqual(state["processes"]["worker"]["status"], "STOPPED")

    def test_changed_parent_signature_never_kills_the_reused_pid(self) -> None:
        state = self.state(role="worker")
        with patch.object(lab, "process_signature", return_value=START_TIME + " /unrelated/program"):
            with self.assertRaisesRegex(RuntimeError, "PROCESS_IDENTITY_CHANGED"):
                lab.stop_role(self.directory, "worker", state)
        self.kill.assert_not_called()

    def test_mode_source_and_expiry_drift_reject_before_database_or_process_start(self) -> None:
        self.metadata()
        with self.assertRaisesRegex(RuntimeError, "ENVIRONMENT_MODE_MODEL_MISMATCH"):
            lab.start_services(self.directory, "LIVE_MODEL", "qwen2:7b")
        self.node.assert_not_called()
        self.node.return_value = {"unchanged": False}
        with self.assertRaisesRegex(RuntimeError, "SOURCE_CHANGED_REQUIRES_NEW_ENVIRONMENT"):
            lab.start_services(self.directory, "SCRIPTED_CALIBRATION", "unused")
        self.node.assert_called_once_with("source-check", {"environment_instance_id": INSTANCE})
        self.node.reset_mock()
        self.node.return_value = {"unchanged": True}
        self.metadata(expires_at="2000-01-01T00:00:00Z")
        with self.assertRaisesRegex(RuntimeError, "ENVIRONMENT_EXPIRED"):
            lab.start_services(self.directory, "SCRIPTED_CALIBRATION", "unused")
        self.database.assert_not_called()
        self.start_role.assert_not_called()
        self.kill.assert_not_called()

    def test_matching_source_and_mode_allow_ordered_start(self) -> None:
        self.metadata()
        self.write("secrets.json", {"gateway_token": "g" * 32, "api_token_a": "a" * 32, "api_token_b": "b" * 32})
        self.node.return_value = {"unchanged": True}
        self.database.return_value = {"database_url": "mock-sut", "gateway_database_url": "mock-gateway",
                                      "reader_database_url": "mock-reader", "redis_url": "mock-redis"}
        self.start_role.side_effect = lambda directory, role, config, state: {"port": {"gateway": 5101, "api": 5102}.get(role, 0)}
        with patch.object(lab, "wait_health", return_value={"status": "ok"}):
            state = lab.start_services(self.directory, "SCRIPTED_CALIBRATION", "unused")
        self.assertEqual(state["status"], "RUNNING")
        self.assertEqual([call.args[1] for call in self.start_role.call_args_list], ["gateway", "relay", "worker", "api"])
        self.assertEqual([call.args[0] for call in self.node.call_args_list], ["source-check", "register"])
        self.database.assert_called_once_with(self.directory)
        self.kill.assert_not_called()

    def test_dead_model_wrapper_still_stops_exact_child(self) -> None:
        state = self.state()
        self.write("model.receipt.json", self.receipt())
        with patch.object(lab, "process_signature", side_effect=[None, CHILD_SIGNATURE, None]):
            lab.stop_role(self.directory, "model", state)
        self.kill.assert_called_once_with(12346, signal.SIGTERM)
        self.assertEqual(state["processes"]["model"]["status"], "STOPPED")

    def test_dead_model_wrapper_cannot_kill_wrong_child_or_wrong_launch(self) -> None:
        self.write("model.receipt.json", self.receipt())
        with patch.object(lab, "process_signature", side_effect=[None, START_TIME + " /other/ollama serve"]):
            with self.assertRaisesRegex(RuntimeError, "MODEL_CHILD_IDENTITY_CHANGED"):
                lab.stop_role(self.directory, "model", self.state())
        self.kill.assert_not_called()
        self.write("model.receipt.json", self.receipt(launch_id="another-launch"))
        with patch.object(lab, "process_signature", side_effect=[None, CHILD_SIGNATURE, None]):
            with self.assertRaises(RuntimeError):
                lab.stop_role(self.directory, "model", self.state())
        self.kill.assert_not_called()

    def test_restart_with_live_orphan_model_child_preserves_old_receipts(self) -> None:
        self.metadata(mode="LIVE_MODEL")
        state = self.state()
        self.write("runtime.json", state)
        self.write("model.receipt.json", self.receipt())
        runtime_before = (self.directory / "runtime.json").read_bytes()
        receipt_before = (self.directory / "model.receipt.json").read_bytes()
        self.node.return_value = {"unchanged": True}
        with patch.object(lab, "process_signature", side_effect=[None, CHILD_SIGNATURE]):
            with self.assertRaisesRegex(RuntimeError, "EXISTING_MODEL_CHILD_REQUIRES_STOP"):
                lab.start_services(self.directory, "LIVE_MODEL", "qwen2:7b")
        self.assertEqual((self.directory / "runtime.json").read_bytes(), runtime_before)
        self.assertEqual((self.directory / "model.receipt.json").read_bytes(), receipt_before)
        self.database.assert_not_called()
        self.start_role.assert_not_called()
        self.kill.assert_not_called()

    def test_restart_requires_complete_previous_start_identity(self) -> None:
        for process in ({"status": "STARTING", "launch_id": LAUNCH},
                        {"status": "STARTING", "launch_id": LAUNCH, "pid": 12345, "signature": None}):
            with self.subTest(process=process):
                with patch.object(lab, "process_signature") as inspect:
                    with self.assertRaisesRegex(RuntimeError, "PROCESS_START_RECEIPT_UNKNOWN_STOP_FIRST"):
                        lab.require_previous_launch_stopped(self.directory, {"instance_id": INSTANCE, "processes": {"worker": process}})
                inspect.assert_not_called()
        self.kill.assert_not_called()

    def test_stop_recovers_missing_signature_only_from_matching_pid_receipt(self) -> None:
        state = self.state(role="worker")
        state["processes"]["worker"]["signature"] = None
        signature = SIGNATURE.replace("--role model", "--role worker")
        self.write("worker.receipt.json", self.receipt(role="worker", signature=signature, pid=54321))
        with self.assertRaisesRegex(RuntimeError, "PROCESS_START_RECEIPT_UNKNOWN"):
            lab.stop_role(self.directory, "worker", state)
        self.kill.assert_not_called()
        self.write("worker.receipt.json", self.receipt(role="worker", signature=signature))
        with patch.object(lab, "process_signature", side_effect=[signature, None]):
            lab.stop_role(self.directory, "worker", state)
        self.kill.assert_called_once_with(12345, signal.SIGTERM)
        self.assertEqual(state["processes"]["worker"]["status"], "STOPPED")

    def test_restart_checks_child_identity_even_after_wrapper_exit(self) -> None:
        for receipt, signature, error in (
            (self.receipt(), START_TIME + " /unrelated/program", "MODEL_CHILD_IDENTITY_CHANGED"),
            (self.receipt(launch_id="other-launch"), None, "MODEL_CHILD_RECEIPT_MISMATCH"),
            (self.receipt(child_signature=None), None, "MODEL_CHILD_IDENTITY_UNKNOWN"),
        ):
            with self.subTest(error=error):
                self.write("model.receipt.json", receipt)
                with patch.object(lab, "process_signature", side_effect=[None, signature]):
                    with self.assertRaisesRegex(RuntimeError, error):
                        lab.require_previous_launch_stopped(self.directory, self.state())
        self.kill.assert_not_called()

    def test_restart_allows_confirmed_stopped_wrapper_and_child(self) -> None:
        state = self.state()
        state["processes"]["model"]["status"] = "STOPPED"
        self.write("model.receipt.json", self.receipt())
        with patch.object(lab, "process_signature", return_value=None) as inspect:
            lab.require_previous_launch_stopped(self.directory, state)
        self.assertEqual([call.args[0] for call in inspect.call_args_list], [12345, 12346])
        self.kill.assert_not_called()


if __name__ == "__main__":
    unittest.main()

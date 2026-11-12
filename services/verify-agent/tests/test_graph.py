"""图恢复/准入边界测试；fake Runtime 不是 PostgreSQL 或真实模型验证证据。"""
from copy import deepcopy
import os
import unittest
from unittest.mock import patch

from langsmith import get_tracing_context

from verify_agent.graph import run_agent
from verify_agent.runtime_port import RuntimeTransportError


RUN_ID = "54f5c728-7fc1-4439-aeaf-f7360ef35401"


def proposal(revision=0, *, action="READ"):
    return {
        "schema_version": 1,
        "proposal_id": "c5adfd90-895a-4b74-a84d-" + f"{revision:012x}",
        "based_on_decision_revision": revision, "candidate_epoch": 1,
        "context_view_ref": "context:" + str(revision), "action_kind": action,
        "requirement_refs": ["R5"], "hypothesis_refs": ["H1"],
        "evidence_refs": ["diff:projection.py"],
        "expected_information": "确认拒绝理由的投影边界", "rationale": "候选修改了决定到回复的路径",
        "hypothesis_updates": [{"id": "H1", "statement": "拒绝理由可能缺失",
                                "status": "OPEN" if revision == 0 else "WEAKENED",
                                "evidence_refs": ["diff:projection.py"]}],
        "payload": ({"material_ref": "candidate:projection.py", "start_line": 1, "end_line": 20}
                    if action == "READ" else
                    {"capability_ref": "capability:assess", "scenario_ref": "scenario:late_usage",
                     "objective": "检查重新评估后拒绝理由与事实一致", "observation_requirements": ["实际输入与输出"]}),
    }


class FakeRuntime:
    """仅模拟端口协议：READ 推进事实，下一次必须看到新依据，再提交检查提案。"""

    def __init__(self):
        self.projection = {
            "run_id": RUN_ID, "status": "ACTIVE", "decision_basis_revision": 0,
            "candidate_epoch": 1, "context_view_ref": "context:0", "next_command_id": "decision:0:0",
            "pending_decision": None, "latest_feedback": None,
            "current_candidate_coverage": [], "final_eligibility": {"eligible": False, "gaps": ["NO_RUNNER"]},
        }
        self.decisions, self.submissions, self.gets = [], [], 0

    def get_run_projection(self, run_id):
        self.gets += 1
        return deepcopy(self.projection)

    def decide(self, run_id, command_id, based_on_decision_revision):
        self.decisions.append((command_id, based_on_decision_revision, deepcopy(self.projection.get("latest_feedback"))))
        revision = self.projection["decision_basis_revision"]
        result = {"status": "COMPLETED", "model_call_id": "model:" + str(revision),
                  "proposal": proposal(revision, action="READ" if revision == 0 else "CHECK_INTENT")}
        self.projection["pending_decision"] = result
        return deepcopy(result)

    def submit_proposal(self, run_id, submitted):
        self.submissions.append(deepcopy(submitted))
        self.projection["pending_decision"] = None
        revision = self.projection["decision_basis_revision"] + 1
        self.projection.update(decision_basis_revision=revision, context_view_ref="context:" + str(revision),
                               next_command_id="decision:" + str(revision) + ":1")
        if submitted["action_kind"] == "READ":
            self.projection["latest_feedback"] = {"type": "READ_RECEIPT", "content": "projection copies complete decision"}
        else:
            self.projection["status"] = "PLANNED"
            self.projection["latest_feedback"] = {"type": "CHECK_PROPOSED", "executed": False}
        return {"status": "COMPLETED", "operation_id": "operation:" + str(revision), "result": self.projection["latest_feedback"]}


class GraphTests(unittest.TestCase):
    def test_new_read_feedback_reaches_second_decision_without_inventing_coverage(self):
        runtime = FakeRuntime()
        report = run_agent(runtime, RUN_ID)
        self.assertEqual(report["outcome"], "PLANNED")
        self.assertEqual(len(runtime.decisions), 2)
        self.assertEqual(runtime.decisions[1][1], 1)
        self.assertEqual(runtime.decisions[1][2]["content"], "projection copies complete decision")
        self.assertEqual([row["action_kind"] for row in runtime.submissions], ["READ", "CHECK_INTENT"])
        self.assertEqual(report["projection"]["current_candidate_coverage"], [])
        self.assertEqual(report["business_verdict"], "NOT_EVALUATED")

    def test_resume_completed_model_before_submission_does_not_call_model_again(self):
        runtime = FakeRuntime()
        runtime.decide(RUN_ID, "decision:0:0", 0)
        report = run_agent(runtime, RUN_ID)
        self.assertEqual(report["outcome"], "PLANNED")
        self.assertEqual(len(runtime.decisions), 2)  # 一次崩溃前 READ + 一次新增材料后的 CHECK。
        self.assertEqual(len(runtime.submissions), 2)

    def test_resume_after_admission_does_not_repeat_read(self):
        runtime = FakeRuntime()
        runtime.submit_proposal(RUN_ID, proposal())
        report = run_agent(runtime, RUN_ID)
        self.assertEqual(report["outcome"], "PLANNED")
        self.assertEqual([row["action_kind"] for row in runtime.submissions], ["READ", "CHECK_INTENT"])
        self.assertEqual(len(runtime.decisions), 1)

    def test_unknown_provider_outcome_blocks_without_new_decision(self):
        runtime = FakeRuntime()

        def unknown(*args):
            runtime.decisions.append(args)
            runtime.projection["status"] = "BLOCKED"
            return {"status": "UNKNOWN", "model_call_id": "call:unknown"}

        runtime.decide = unknown
        report = run_agent(runtime, RUN_ID)
        self.assertEqual(report["outcome"], "BLOCKED")
        self.assertEqual(len(runtime.decisions), 1)
        self.assertEqual(runtime.submissions, [])

    def test_dispatched_waits_without_llm_polling(self):
        runtime = FakeRuntime()
        runtime.decide = lambda *args: {"status": "DISPATCHED", "model_call_id": "call:pending"}
        report = run_agent(runtime, RUN_ID)
        self.assertEqual(report["outcome"], "WAITING_RUNTIME")
        self.assertEqual(runtime.gets, 2)
        self.assertEqual(runtime.submissions, [])

    def test_cancellation_during_dispatch_reconciles_terminal_run(self):
        runtime = FakeRuntime()

        def cancelled(*args):
            runtime.projection["status"] = "CANCELLED"
            return {"status": "CANCELLED", "model_call_id": "call:cancelled"}

        runtime.decide = cancelled
        report = run_agent(runtime, RUN_ID)
        self.assertEqual(report["outcome"], "CANCELLED")
        self.assertEqual(runtime.submissions, [])

    def test_invalid_response_can_use_new_authorized_correction_command(self):
        runtime = FakeRuntime()
        original_decide = runtime.decide
        attempted = []

        def invalid_once(run_id, command_id, basis):
            attempted.append(command_id)
            if len(attempted) == 1:
                runtime.projection["next_command_id"] = "decision:0:1"
                runtime.projection["latest_feedback"] = {"status": "INVALID", "error_code": "MODEL_OUTPUT_INVALID"}
                return {"status": "INVALID", "model_call_id": "call:invalid"}
            return original_decide(run_id, command_id, basis)

        runtime.decide = invalid_once
        report = run_agent(runtime, RUN_ID)
        self.assertEqual(report["outcome"], "PLANNED")
        self.assertEqual(attempted[:2], ["decision:0:0", "decision:0:1"])

    def test_lost_submit_response_resumes_from_applied_operation(self):
        runtime = FakeRuntime()
        original_submit = runtime.submit_proposal

        def applied_but_lost(*args):
            original_submit(*args)
            raise RuntimeTransportError("RUNTIME_TRANSPORT_UNCONFIRMED")

        runtime.submit_proposal = applied_but_lost
        self.assertEqual(run_agent(runtime, RUN_ID)["outcome"], "RUNTIME_TRANSPORT_UNCONFIRMED")
        runtime.submit_proposal = original_submit
        self.assertEqual(run_agent(runtime, RUN_ID)["outcome"], "PLANNED")
        self.assertEqual([p["action_kind"] for p in runtime.submissions], ["READ", "CHECK_INTENT"])

    def test_cancelled_run_never_submits_pending_decision(self):
        runtime = FakeRuntime()
        runtime.decide(RUN_ID, "decision:0:0", 0)
        runtime.projection["status"] = "CANCELLED"
        report = run_agent(runtime, RUN_ID)
        self.assertEqual(report["outcome"], "CANCELLED")
        self.assertEqual(len(runtime.decisions), 1)
        self.assertEqual(runtime.submissions, [])

    def test_stale_candidate_epoch_cannot_be_submitted(self):
        runtime = FakeRuntime()
        runtime.decide(RUN_ID, "decision:0:0", 0)
        runtime.projection["candidate_epoch"] = 2
        report = run_agent(runtime, RUN_ID)
        self.assertEqual(report["outcome"], "STALE_DECISION_BINDING")
        self.assertEqual(runtime.submissions, [])

    def test_transport_unknown_never_retries_mutation(self):
        runtime = FakeRuntime()

        def lost_response(*args):
            runtime.decisions.append(args)
            raise RuntimeTransportError("RUNTIME_TRANSPORT_UNCONFIRMED")

        runtime.decide = lost_response
        report = run_agent(runtime, RUN_ID)
        self.assertEqual(report["outcome"], "RUNTIME_TRANSPORT_UNCONFIRMED")
        self.assertEqual(len(runtime.decisions), 1)
        self.assertEqual(runtime.gets, 1)

    def test_pending_admission_not_acknowledged_cannot_busy_loop(self):
        runtime = FakeRuntime()
        runtime.submit_proposal = lambda *args: {"status": "PENDING", "operation_id": "operation:pending"}
        report = run_agent(runtime, RUN_ID)
        self.assertEqual(report["outcome"], "WAITING_RUNTIME")
        self.assertEqual(len(runtime.decisions), 1)

    def test_schema_invalid_output_stops_without_admission(self):
        runtime = FakeRuntime()
        runtime.decide = lambda *args: {"status": "COMPLETED", "proposal": {"action_kind": "PASS"}}
        report = run_agent(runtime, RUN_ID)
        self.assertEqual(report["outcome"], "INVALID_DECISION_PROPOSAL")
        self.assertEqual(runtime.submissions, [])

    def test_inherited_external_tracing_is_disabled(self):
        runtime = FakeRuntime()
        original_get = runtime.get_run_projection

        def assert_disabled(run_id):
            self.assertIs(get_tracing_context()["enabled"], False)
            return original_get(run_id)

        runtime.get_run_projection = assert_disabled
        with patch.dict(os.environ, {"LANGSMITH_TRACING": "true", "LANGCHAIN_TRACING_V2": "true"}):
            self.assertEqual(run_agent(runtime, RUN_ID)["outcome"], "PLANNED")

    def test_malformed_runtime_pass_claim_is_rejected(self):
        runtime = FakeRuntime()
        runtime.projection["final_eligibility"]["eligible"] = True
        report = run_agent(runtime, RUN_ID)
        self.assertEqual(report["outcome"], "UNSUPPORTED_VERIFICATION_CLAIM")
        self.assertEqual(runtime.decisions, [])

    def test_local_loop_limit_is_not_business_success(self):
        runtime = FakeRuntime()
        report = run_agent(runtime, RUN_ID, max_iterations=1)
        self.assertEqual(report["outcome"], "LOCAL_STEP_LIMIT")
        self.assertEqual(report["business_verdict"], "NOT_EVALUATED")
        self.assertEqual(runtime.projection["status"], "ACTIVE")


if __name__ == "__main__":
    unittest.main()

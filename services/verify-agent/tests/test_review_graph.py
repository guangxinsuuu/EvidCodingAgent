"""v3 的逻辑职责分支复用同一决策模型；fake Runtime 只证明路由，不证明业务判断正确。"""
from copy import deepcopy
import unittest

from verify_agent.graph import run_agent
from verify_agent.protocol import ROOT, parse_json
from test_graph import FakeRuntime, RUN_ID


class ReviewRuntime(FakeRuntime):
    def __init__(self):
        super().__init__()
        corpus = parse_json((ROOT / "protocols/verify-agent/v3/conformance.json").read_bytes())
        rows = {row["name"]: row["value"] for row in corpus["cases"] if row["valid"]}
        self.queue = [rows[name] for name in ("valid_direction", "valid_read", "valid_partial_draft", "valid_complete_draft", "valid_accept_plan")]
        self.projection["review_cycle"] = {"directions": []}

    def decide(self, run_id, command_id, based_on_decision_revision):
        self.decisions.append(deepcopy(self.projection))
        p = deepcopy(self.queue[len(self.submissions)])
        p.update(proposal_id="c5adfd90-895a-4b74-a84d-" + f"{based_on_decision_revision:012x}",
                 based_on_decision_revision=based_on_decision_revision, candidate_epoch=1,
                 context_view_ref=self.projection["context_view_ref"])
        result = {"status": "COMPLETED", "model_call_id": "model:" + str(based_on_decision_revision), "proposal": p}
        self.projection["pending_decision"] = result
        return deepcopy(result)

    def submit_proposal(self, run_id, submitted):
        self.submissions.append(deepcopy(submitted))
        revision = self.projection["decision_basis_revision"] + 1
        self.projection.update(pending_decision=None, decision_basis_revision=revision,
                               context_view_ref="context:" + str(revision), next_command_id="decision:" + str(revision))
        action = submitted["action_kind"]
        if action == "UPDATE_DIRECTION":
            self.projection["review_cycle"]["directions"] = [{"focus": submitted["payload"]["focus"]}]
        if action == "PLAN_DRAFT":
            self.projection["latest_feedback"] = {"status": "NEEDS_REVISION" if revision == 3 else "BOUND_PLAN", "executed": False}
        if action == "ACCEPT_PLAN":
            self.projection["status"] = "PLANNED"
        return {"status": "COMPLETED", "operation_id": "operation:" + str(revision)}


class ReviewGraphTests(unittest.TestCase):
    def test_branches_return_to_main_decision_and_bound_plan_requires_acceptance(self):
        runtime = ReviewRuntime()
        report = run_agent(runtime, RUN_ID)
        self.assertEqual(report["outcome"], "PLANNED")
        self.assertEqual([p["action_kind"] for p in runtime.submissions],
                         ["UPDATE_DIRECTION", "READ", "PLAN_DRAFT", "PLAN_DRAFT", "ACCEPT_PLAN"])
        self.assertEqual(runtime.decisions[-1]["status"], "ACTIVE")
        self.assertEqual(runtime.decisions[-1]["latest_feedback"]["status"], "BOUND_PLAN")
        nodes = [row["node"] for row in report["trace"]]
        for node in ("analyze_change_impact", "investigate_evidence", "plan_experiment", "update_review_direction"):
            self.assertIn(node, nodes)
        self.assertEqual(nodes.count("decide_next_action"), 5)
        self.assertFalse(report["projection"]["final_eligibility"]["eligible"])


if __name__ == "__main__":
    unittest.main()

"""商业原文保留给确定性判定，文案模型只见有界事实摘要；不运行真实模型。"""
from copy import deepcopy
import json
import unittest

from genai_support.business_tools import model_reply_context, scripted_draft


class CommercialModelProjectionTests(unittest.TestCase):
    def context(self):
        return {"ticket": {"ticket_id": "cw:1:2", "revision": 1, "body": "UNTRUSTED_MESSAGE_SENTINEL"},
            "commercial_source": {"authority_scope": "LOCAL_LAB", "source_revision": 8,
                "claim": {"claim_id": "claim-1", "claimed_at": "2026-11-19T00:00:00Z"},
                "contracts": [{"source_document": {"text": "UNTRUSTED_CONTRACT_SENTINEL" * 10000}}],
                "coverage": {"completeness_scope": "LOCAL_CONTROLLED_GENERATOR_ONLY"}},
            "customer_message_refs": [{"content": "UNTRUSTED_MESSAGE_SENTINEL"}],
            "policy": {"policy_id": "refund", "version": "lab-v1", "source_document": {"text": "POLICY_FULL_TEXT"}},
            "contract": {"contract_id": "contract-1", "source_document": {"text": "UNTRUSTED_CONTRACT_SENTINEL"}}}

    def test_long_documents_and_customer_instructions_do_not_enter_model_context(self):
        context = self.context()
        before = deepcopy(context)
        decision = {"authority_scope": "LOCAL_LAB", "eligibility": "ELIGIBLE", "policy_ref": "refund:lab-v1"}
        output = model_reply_context(context, decision)
        encoded = json.dumps(output)
        self.assertLess(len(encoded), 10000)
        for sentinel in ("UNTRUSTED_MESSAGE_SENTINEL", "UNTRUSTED_CONTRACT_SENTINEL", "POLICY_FULL_TEXT"):
            self.assertNotIn(sentinel, encoded)
        self.assertEqual(output["decision"], decision)
        self.assertEqual(output["policy"]["version"], "lab-v1")
        self.assertEqual(context, before, "完整冻结输入不能被模型投影原地删改")

    def test_scripted_calibration_also_preserves_explicit_lab_scope(self):
        decision = {"authority_scope": "LOCAL_LAB", "eligibility": "ELIGIBLE", "policy_ref": "refund:lab-v1",
            "billing_record_ref": "invoice:1", "proposed_action": "HUMAN_REVIEW", "evidence_refs": ["claim:1"]}
        draft = scripted_draft(self.context(), decision)
        self.assertIn("仅依据本地实验规则", draft["customer_reply_draft"])
        self.assertEqual(draft["proposed_action"], "HUMAN_REVIEW")


if __name__ == "__main__":
    unittest.main()

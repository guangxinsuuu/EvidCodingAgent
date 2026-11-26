"""观察关系的反例：正确 marker 不能掩盖错正文，其他输入快照不能填补当前证据。"""
from copy import deepcopy
import unittest

from observe_native import observation_checks


class ObservationTests(unittest.TestCase):
    def setUp(self):
        draft = {"customer_reply_draft": "请补充合同和完整用量记录。"}
        self.values = dict(case={"tenant_id": "t-a", "conversation_id": 4, "invoice_id": "inv-a"}, job_id="j-a",
            api_job={"job": {"draft": draft}}, native={"job_id": "j-a", "tenant_id": "t-a", "ticket_id": "cw:1:4", "ticket_revision": 2, "status": "DONE", "draft": draft},
            delivery={"job_id": "j-a", "conversation_id": 4, "invoice_id": "inv-a", "external_message_id": 8, "delivery_state": "COMPLETE"},
            snapshot={"tenant_id": "t-a", "ticket_id": "cw:1:4", "ticket_revision": 2, "context": {"source_kind": "CHATWOOT_LAGO"}, "source_receipts": [{"service": "lago"}]},
            gateway={"job_id": "j-a", "state": "COMPLETE", "draft": draft},
            messages=[{"id": 8, "conversation_id": 4, "private": True, "content": "[EGAP draft j-a]\n\n" + draft["customer_reply_draft"]}], cross_status=403)

    def test_content_and_destination_must_match_in_addition_to_marker(self):
        self.assertTrue(all(observation_checks(**self.values).values()))
        for key, value in (("content", "[EGAP draft j-a]\n\n已批准退款。"), ("conversation_id", 5), ("private", False)):
            values = deepcopy(self.values); values["messages"][0][key] = value
            self.assertFalse(observation_checks(**values)["private_note_observed"])

    def test_stale_input_or_other_gateway_job_cannot_prove_current_job(self):
        values = deepcopy(self.values); values["snapshot"]["ticket_revision"] = 1
        self.assertFalse(observation_checks(**values)["external_snapshot"])
        values = deepcopy(self.values); values["gateway"]["job_id"] = "old-job"
        self.assertFalse(observation_checks(**values)["same_completed_draft"])


if __name__ == "__main__":
    unittest.main()

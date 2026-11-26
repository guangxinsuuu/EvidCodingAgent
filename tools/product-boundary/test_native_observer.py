"""观察关系的反例：正确 marker 不能掩盖错正文，其他输入快照不能填补当前证据。"""
from copy import deepcopy
import hashlib
import json
import unittest

from observe_native import observation_checks, model_observation_checks


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

    def test_failed_job_without_delivery_keeps_missing_relationships_false(self):
        values = deepcopy(self.values)
        values.update(delivery=None, gateway=None, messages=[])
        values["native"].update(status="FAILED", draft=None)
        checks = observation_checks(**values)
        self.assertFalse(checks["same_completed_draft"])
        self.assertFalse(checks["private_note_observed"])

    def test_raw_model_receipt_must_match_current_job_actual_bytes_and_saved_draft(self):
        encode = lambda v: json.dumps(v, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        sha = lambda v: hashlib.sha256(v.encode()).hexdigest()
        native = self.values["native"]
        context = {"ticket":{"tenant_id":native["tenant_id"],"ticket_id":native["ticket_id"],"revision":native["ticket_revision"]},
            "decision":{"eligibility":"NEEDS_MORE_EVIDENCE"}}
        request = {"messages":[{"content":"system"},{"content":encode(context)}]}
        response = {"model":"qwen2:7b", "id":"r-1", "usage":{"total_tokens":10},
            "choices":[{"finish_reason":"stop","message":{"content":encode(native["draft"])}}]}
        record = {"deployment_id":"deployment", "model_name":"qwen2:7b", "model_digest":"digest", "operation":{
            "operation_id":native["job_id"],"state":"COMPLETE", "request_text":encode(request),"response_text":encode(response),
            "request_sha256":sha(encode(request)),"response_sha256":sha(encode(response)),
            "model_before":encode({"digest":"digest"}),"model_after":encode({"digest":"digest"})}}
        gateway = {**self.values["gateway"],"receipt":{"request_sha256":sha(encode(request)),"response_sha256":sha(encode(response)),
            "decision_sha256":sha(encode(context["decision"])),"request_id":"r-1","model_observed":"qwen2:7b","usage":{"total_tokens":10}}}
        binding = {"model":{"name":"qwen2:7b","digest":"digest"}}
        self.assertTrue(all(model_observation_checks(record,binding,"deployment",native,gateway).values()))
        for field,value,key in (("operation_id","old-job","model_operation_binding"),
                                ("request_sha256","wrong","model_request_binding"),
                                ("response_sha256","wrong","model_response_binding")):
            wrong = deepcopy(record); wrong["operation"][field] = value
            self.assertFalse(model_observation_checks(wrong,binding,"deployment",native,gateway)[key])
        changed = deepcopy(native); changed["draft"]["customer_reply_draft"]="different response"
        self.assertFalse(model_observation_checks(record,binding,"deployment",changed,gateway)["model_draft_binding"])


if __name__ == "__main__":
    unittest.main()

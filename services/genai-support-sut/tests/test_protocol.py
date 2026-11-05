"""校验 Python 服务与 TypeScript 客户端对同一 DRAFT 协议的接受边界。"""

from copy import deepcopy
from dataclasses import FrozenInstanceError
import json
from pathlib import Path
import subprocess
import unittest

from genai_support.protocol import (
    ProtocolValidationError,
    parse_support_request,
    validate_support_job,
    validate_support_submission,
)


REQUEST = {"schema_version": 1, "ticket_id": "CS-1042", "ticket_revision": 3}
SUBMISSION = {**REQUEST, "tenant_id": "tenant-a", "job_id": "a" * 32, "status": "QUEUED"}
DRAFT = {
    "resolution_type": "REFUND_ELIGIBILITY_REVIEW", "ticket_id": "CS-1042", "ticket_revision": 3,
    "applicable_policy_ref": "refund-policy-2026-10", "billing_record_ref": "invoice-1042",
    "eligibility": "NEEDS_MORE_EVIDENCE", "proposed_action": "HUMAN_REVIEW",
    "evidence_refs": ["policy-clause"], "customer_reply_draft": "合成待审草稿。",
}
DONE = {**SUBMISSION, "status": "DONE", "draft": DRAFT, "error_code": None}
PARSERS = {"request": parse_support_request, "submission": validate_support_submission, "job": validate_support_job}


def protocol_cases() -> list[tuple[str, object, bool]]:
    """同一测试矩阵交给两种语言，显式声明预期而非让一端替另一端作答案。"""
    cases: list[tuple[str, object, bool]] = [
        ("request", REQUEST, True), ("request", {**REQUEST, "ticket_revision": 3.0}, True),
        ("request", {**REQUEST, "schema_version": 1.0}, True),
        ("request", {**REQUEST, "ticket_revision": 9_007_199_254_740_991}, True),
        ("request", None, False), ("request", [], False),
        ("submission", SUBMISSION, True), ("submission", {**SUBMISSION, "status": "DONE"}, False),
        ("submission", {**SUBMISSION, "debug": "extra"}, False),
        ("job", DONE, True),
        ("job", {**SUBMISSION, "draft": None, "error_code": None}, True),
        ("job", {**SUBMISSION, "status": "PROCESSING", "draft": None, "error_code": None}, True),
        ("job", {**SUBMISSION, "status": "FAILED", "draft": None, "error_code": "PROCESSING_FAILED"}, True),
        ("job", {**DONE, "draft": None}, False), ("job", {**DONE, "status": "PROCESSING"}, False),
        ("job", {**DONE, "error_code": "PROCESSING_FAILED"}, False),
        ("job", {**DONE, "draft": {**DRAFT, "ticket_id": "CS-OTHER"}}, False),
        ("job", {**DONE, "draft": {**DRAFT, "ticket_revision": 2}}, False),
        ("job", {**DONE, "draft": {**DRAFT, "proposed_action": "EXECUTE_REFUND"}}, False),
        ("job", {**DONE, "draft": {**DRAFT, "evidence_refs": ["same", "same"]}}, False),
        ("job", {**DONE, "draft": {**DRAFT, "customer_reply_draft": ""}}, False),
        ("job", {**DONE, "draft": {**DRAFT, "customer_reply_draft": "😀" * 16_384}}, True),
        ("job", {**DONE, "draft": {**DRAFT, "customer_reply_draft": "😀" * 16_385}}, False),
        ("job", {**DONE, "debug": "extra"}, False),
        ("job", {**DONE, "status": "FAILED", "draft": None, "error_code": None}, False),
    ]
    for field, bad in (
        ("tenant_id", "tenant-b"), ("policy_ref", "answer"), ("renewal_date", "2026-11-02"),
        ("ticket_revision", 0), ("ticket_revision", "3"), ("ticket_revision", True),
        ("ticket_revision", 1.5), ("ticket_revision", 9_007_199_254_740_992),
        ("schema_version", True), ("schema_version", 2), ("ticket_id", "CS-1042\n"),
    ):
        cases.append(("request", {**REQUEST, field: bad}, False))
    return cases


class ProtocolTests(unittest.TestCase):
    def test_acceptance_matrix_matches_typescript(self) -> None:
        """真实 Node/Ajv 与 jsonschema 读取同一输入；需要先执行 pnpm build。"""
        cases = protocol_cases()
        py_results = []
        for kind, value, expected in cases:
            with self.subTest(kind=kind, expected=expected, fields=list(value) if isinstance(value, dict) else None):
                try:
                    PARSERS[kind](value)
                    accepted = True
                except ProtocolValidationError:
                    accepted = False
                self.assertEqual(accepted, expected)
                py_results.append(accepted)
        root = Path(__file__).resolve().parents[3]
        script = """
            import { readFileSync } from 'node:fs';
            import { parseSupportRequest, parseSupportSubmission, parseSupportJob } from './packages/contracts/dist/support.js';
            const parsers = {request: parseSupportRequest, submission: parseSupportSubmission, job: parseSupportJob};
            const cases = JSON.parse(readFileSync(0, 'utf8'));
            process.stdout.write(JSON.stringify(cases.map(([kind, value]) => {
                try { parsers[kind](value); return true; } catch { return false; }
            })));
        """
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script], input=json.dumps(cases),
            capture_output=True, text=True, cwd=root, check=True, timeout=15,
        )
        self.assertEqual(json.loads(result.stdout), py_results)

    def test_validated_values_cannot_be_changed_after_check(self) -> None:
        request = parse_support_request(REQUEST)
        with self.assertRaises(FrozenInstanceError):
            request.ticket_revision = 5
        raw = deepcopy(DONE)
        snapshot = validate_support_job(raw)
        raw["draft"]["evidence_refs"].append("changed-after-check")
        self.assertEqual(snapshot["draft"]["evidence_refs"], ("policy-clause",))
        with self.assertRaises(TypeError):
            snapshot["draft"]["eligibility"] = "ELIGIBLE"

    def test_shape_validation_does_not_claim_business_correctness(self) -> None:
        raw = deepcopy(DONE)
        raw["draft"]["applicable_policy_ref"] = "wrong-but-well-formed-policy"
        raw["draft"]["eligibility"] = "ELIGIBLE"
        self.assertEqual(validate_support_job(raw)["draft"]["eligibility"], "ELIGIBLE")
        with self.assertRaisesRegex(ProtocolValidationError, "^INVALID_SUPPORT_PROTOCOL$"):
            parse_support_request({**REQUEST, "private_text": "must-not-be-in-error"})


if __name__ == "__main__":
    unittest.main()

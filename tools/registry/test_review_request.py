"""上游任务必须有准确身份；本地声明不能通过输入文件升级为企业批准。"""
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest

from tools.registry.reference_resolver import MaterialError
from tools.registry.review_request import MAX_REQUEST_BYTES, SCHEMA_PATH, read_review_request, validate_review_request


def request_fixture(baseline="a" * 40, candidate="b" * 40):
    return {
        "schema_version": 1, "request_id": "review:fixture", "revision": 1,
        "requester_ref": "local:developer", "owner_ref": "local:qa",
        "goal": "审查业务事实投影变化", "non_goals": ["不发出退款或公开消息"],
        "baseline": {"commit": baseline, "reason": "本地比较版本，未认定为生产部署", "source_ref": "local:baseline"},
        "candidate": {"commit": candidate, "source_ref": "local:candidate"},
        "requirements": [{"id": "R3", "kind": "PRESERVE", "statement": "保留已确认的业务事实",
                          "material_ref": "rule:R3", "authority": "LOCAL_CALIBRATION",
                          "status": "CONFIRMED_LOCAL", "owner_ref": "local:business"}],
        "known_gaps": [], "predecessor_run_id": None,
    }


class ReviewRequestTests(unittest.TestCase):
    def test_shared_conformance_corpus(self):
        corpus = json.loads(SCHEMA_PATH.with_name("conformance.json").read_text(encoding="utf-8"))
        for case in corpus["cases"]:
            with self.subTest(case=case["name"]):
                if case["valid"]:
                    self.assertEqual(validate_review_request(case["value"]), case["value"])
                else:
                    with self.assertRaises(MaterialError):
                        validate_review_request(case["value"])

    def test_valid_request_is_copied_without_authority_upgrade(self):
        original = request_fixture()
        value = validate_review_request(original)
        original["requirements"][0]["statement"] = "mutated"
        self.assertNotEqual(value["requirements"][0]["statement"], "mutated")
        self.assertEqual(value["requirements"][0]["authority"], "LOCAL_CALIBRATION")

    def test_missing_requirements_are_allowed_without_defaults(self):
        value = request_fixture()
        value["requirements"] = []
        self.assertEqual(validate_review_request(value)["requirements"], [])

    def test_branch_short_commit_and_missing_candidate_are_rejected(self):
        for candidate in ("HEAD", "a" * 12, None):
            value = request_fixture()
            if candidate is None:
                del value["candidate"]
            else:
                value["candidate"]["commit"] = candidate
            with self.subTest(candidate=candidate), self.assertRaisesRegex(MaterialError, "REVIEW_REQUEST_INVALID"):
                validate_review_request(value)

    def test_unknown_fields_enterprise_authority_and_implicit_coercion_rejected(self):
        cases = []
        value = request_fixture(); value["dispatch_authorized"] = True; cases.append(value)
        value = request_fixture(); value["requirements"][0]["authority"] = "COMPANY_APPROVED"; cases.append(value)
        value = request_fixture(); value["revision"] = "1"; cases.append(value)
        value = request_fixture(); value["revision"] = True; cases.append(value)
        for value in cases:
            with self.subTest(value=value), self.assertRaisesRegex(MaterialError, "REVIEW_REQUEST_INVALID"):
                validate_review_request(value)

    def test_duplicate_ids_and_unknown_gap_requirement_rejected(self):
        value = request_fixture()
        value["requirements"].append(deepcopy(value["requirements"][0]))
        with self.assertRaisesRegex(MaterialError, "REVIEW_REQUEST_DUPLICATE_ID"):
            validate_review_request(value)
        value = request_fixture()
        value["known_gaps"] = [{"id": "gap:missing", "requirement_refs": ["R99"], "description": "missing"}]
        with self.assertRaisesRegex(MaterialError, "REVIEW_REQUEST_UNKNOWN_REQUIREMENT_REF"):
            validate_review_request(value)
        value["known_gaps"][0]["requirement_refs"] = ["R3"]
        value["known_gaps"].append(deepcopy(value["known_gaps"][0]))
        with self.assertRaisesRegex(MaterialError, "REVIEW_REQUEST_DUPLICATE_ID"):
            validate_review_request(value)

    def test_file_read_rejects_symlink_fifo_and_oversize(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            regular = root / "request.json"
            regular.write_text(json.dumps(request_fixture()))
            self.assertEqual(read_review_request(regular)["request_id"], "review:fixture")
            link = root / "request-link.json"; link.symlink_to(regular)
            with self.assertRaisesRegex(MaterialError, "REVIEW_REQUEST_READ_FAILED"):
                read_review_request(link)
            fifo = root / "pipe"; os.mkfifo(fifo)
            with self.assertRaisesRegex(MaterialError, "REVIEW_REQUEST_REGULAR_FILE_REQUIRED"):
                read_review_request(fifo)
            regular.write_bytes(b" " * (MAX_REQUEST_BYTES + 1))
            with self.assertRaisesRegex(MaterialError, "REVIEW_REQUEST_TOO_LARGE"):
                read_review_request(regular)

    def test_json_duplicate_key_nan_and_invalid_encoding_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request.json"
            for raw, code in ((b'{"revision":1,"revision":2}', "REVIEW_REQUEST_DUPLICATE_KEY"),
                              (b'{"revision":NaN}', "REVIEW_REQUEST_JSON_INVALID"),
                              (b'\xff', "REVIEW_REQUEST_JSON_INVALID")):
                with self.subTest(code=code):
                    path.write_bytes(raw)
                    with self.assertRaisesRegex(MaterialError, code):
                        read_review_request(path)


if __name__ == "__main__":
    unittest.main()

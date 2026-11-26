"""模拟任务装配与来源边界测试；不把结构测试算作业务覆盖。"""
import base64
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from assemble import normalize_issue
from client import SourceClient, SourceClientError


class IngressTests(unittest.TestCase):
    def issue(self):
        return {"source_kind":"SIMULATED_JIRA_ISSUE","issue_key":"EGAPLAB-100","revision":1,
            "authority_scope":"SYNTHETIC_LOCAL_CALIBRATION","summary":"版本验证","requester_ref":"lab:release",
            "owner_ref":"lab:owner","non_goals":[],"requirements":[
                {"id":"DEPLOY-1","kind":"NEW","statement":"独立部署","owner_ref":"lab:owner"},
                {"id":"R5","kind":"PRESERVE","statement":"失败不可当空事实","owner_ref":"lab:owner",
                 "rule_ref":"rules/refund-rules/versions/lab-v1-extracted"}]}

    def test_unmapped_requirements_retained_and_no_approval_invented(self):
        request=normalize_issue(self.issue(),"a"*40,"b"*40,"git:product")
        self.assertEqual([r["id"] for r in request["requirements"]],["DEPLOY-1","R5"])
        self.assertTrue(all(r["status"]=="PENDING" for r in request["requirements"]))
        self.assertEqual(request["requirements"][0]["material_ref"],"jira:EGAPLAB-100")
        self.assertIsNone(request["predecessor_run_id"])

    def test_rule_version_mismatch_rejected(self):
        issue=self.issue();issue["requirements"][1]["rule_ref"]="rules/refund-rules/versions/latest"
        with self.assertRaisesRegex(SourceClientError,"JIRA_RULE_VERSION_MISMATCH"):
            normalize_issue(issue,"a"*40,"b"*40,"git:product")

    def test_source_envelope_and_private_config_rejected(self):
        conf={"base_url":"http://127.0.0.1:1234","token":"x"*40,"source_kind":"SIMULATED_ENTERPRISE_SOURCE",
              "principal_id":"lab:planner-reader","instance_id":"evid-sources-test","tenant_id":"company-a"}
        client=SourceClient(conf)
        result={"schema_version":1,"instance_id":conf["instance_id"],"tenant_id":"company-a","namespace":"rules",
            "object_id":"refund-rules","version":"v1","source_kind":conf["source_kind"],
            "acl_decision":{"principal_id":conf["principal_id"],"permission":"READ","scope":"EXACT_OBJECT_AND_TENANT","cache_authorization_seconds":0},
            "content_base64":base64.b64encode(b"test").decode(),"content_sha256":hashlib.sha256(b"test").hexdigest(),
            "metadata":{"authority_scope":"SYNTHETIC_LOCAL_CALIBRATION"}}
        with patch.object(client,"request",return_value=result):
            self.assertEqual(client.read("rules","refund-rules","v1")["content"],"test")
            result["tenant_id"]="other"
            with self.assertRaisesRegex(SourceClientError,"SOURCE_RESPONSE_IDENTITY_MISMATCH"):client.read("rules","refund-rules","v1")
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/"private.json";path.write_text("{}");path.chmod(0o644)
            with self.assertRaisesRegex(SourceClientError,"PRIVATE_READER_CONFIG_REQUIRED"):SourceClient.from_file(path)


if __name__=="__main__":unittest.main()

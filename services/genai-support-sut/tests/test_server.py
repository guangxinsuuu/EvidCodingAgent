"""ASGI协议测试，无端口/数据库/模型。真实进程启动另在test_lifecycle验证。"""

import json
import unittest

from fastapi.testclient import TestClient

from genai_support.config import DEFAULT_FIXTURE_PATH, ConfigurationError, Settings, load_config
from genai_support.fixtures import load_fixture_bundle
from genai_support.server import create_app


TOKEN = "synthetic-test-token-not-provider-secret-0001"
VALID = {"schema_version": 1, "ticket_id": "CS-1042", "ticket_revision": 3}


class ServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(api_token=TOKEN, tenant_id="tenant-a")
        self.client = TestClient(create_app(self.settings, load_fixture_bundle(DEFAULT_FIXTURE_PATH)))
        self.addCleanup(self.client.close)
        self.headers = {"Authorization": "Bearer " + TOKEN}

    def test_liveness_never_claims_business_readiness(self) -> None:
        self.assertEqual(self.client.get("/health").json(), {"schema_version": 1, "status": "ok"})
        response = self.client.get("/health/ready")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["scope"], "support-service-skeleton")
        self.assertEqual(response.json()["persistence"], "not_implemented")

    def test_authentication_precedes_body_and_never_echoes_credentials(self) -> None:
        for auth in (None, "Bearer wrong-token", "Bearer " + TOKEN + "suffix"):
            headers = {} if auth is None else {"authorization": auth}
            response = self.client.post("/support-resolution-jobs", content="private-invalid-body", headers=headers)
            self.assertEqual(response.status_code, 401)
            self.assertNotIn(TOKEN, response.text)
            self.assertNotIn("private-invalid-body", response.text)
        response = self.client.post("/support-resolution-jobs", json=VALID,
            headers=[("authorization", "Bearer " + TOKEN), ("authorization", "Bearer " + TOKEN)])
        self.assertEqual(response.status_code, 401)

    def test_valid_request_cannot_be_accepted_without_durable_backend(self) -> None:
        response = self.client.post("/support-resolution-jobs", json=VALID, headers=self.headers)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error_code"], "JOB_BACKEND_NOT_IMPLEMENTED")
        self.assertNotIn("job_id", response.json())
        query = self.client.get("/support-resolution-jobs/" + "a" * 32, headers=self.headers)
        self.assertEqual(query.status_code, 503)

    def test_invalid_revision_or_client_supplied_business_authority_is_rejected(self) -> None:
        for fields in ({"ticket_revision": 0}, {"ticket_revision": True}, {"schema_version": True},
                       {"tenant_id": "tenant-b"}, {"policy_date": "2026-11-01"}, {"expected": "ELIGIBLE"}):
            response = self.client.post("/support-resolution-jobs", json={**VALID, **fields}, headers=self.headers)
            self.assertEqual(response.status_code, 422, fields)

    def test_json_duplicate_constants_encoding_size_and_type_boundaries(self) -> None:
        headers = {**self.headers, "content-type": "application/json"}
        for body in ('{"schema_version":1,"ticket_id":"CS-1042","ticket_revision":3,"ticket_revision":1}',
                     '{"ticket_revision":NaN}', '{', '[]', 'null'):
            self.assertEqual(self.client.post("/support-resolution-jobs", content=body, headers=headers).status_code, 422)
        self.assertEqual(self.client.post("/support-resolution-jobs", content="x" * 4097, headers=headers).status_code, 413)
        self.assertEqual(self.client.post("/support-resolution-jobs", content="{}", headers=self.headers).status_code, 415)
        response = self.client.post("/support-resolution-jobs", content=json.dumps(VALID),
                                   headers={**headers, "content-encoding": "gzip"})
        self.assertEqual(response.status_code, 415)

    def test_ticket_lookup_is_tenant_scoped_and_unknown_is_not_found(self) -> None:
        unknown = {**VALID, "ticket_id": "NONEXISTENT"}
        self.assertEqual(self.client.post("/support-resolution-jobs", json=unknown, headers=self.headers).status_code, 404)
        # tenant-c无fixture，不能借同名CS-1042读取tenant-a的资料。
        with TestClient(create_app(Settings(api_token=TOKEN, tenant_id="tenant-c"), load_fixture_bundle(DEFAULT_FIXTURE_PATH))) as client:
            self.assertEqual(client.post("/support-resolution-jobs", json=VALID, headers=self.headers).status_code, 404)

    def test_get_auth_and_identifier_validation(self) -> None:
        self.assertEqual(self.client.get("/support-resolution-jobs/" + "a" * 32).status_code, 401)
        self.assertEqual(self.client.get("/support-resolution-jobs/not-a-job", headers=self.headers).status_code, 422)

    def test_configuration_is_explicit_loopback_only_and_secret_repr_is_safe(self) -> None:
        valid = {"EGAP_SUPPORT_API_TOKEN": TOKEN, "EGAP_SUPPORT_TENANT_ID": "tenant-a"}
        self.assertNotIn(TOKEN, repr(load_config(valid)))
        for fields in ({"EGAP_SUPPORT_API_TOKEN": ""}, {"EGAP_SUPPORT_HOST": "0.0.0.0"},
                       {"EGAP_SUPPORT_PORT": "-1"}, {"EGAP_SUPPORT_PORT": "65536"}, {"EGAP_SUPPORT_TENANT_ID": ""}):
            with self.assertRaises(ConfigurationError):
                load_config({**valid, **fields})


if __name__ == "__main__":
    unittest.main()

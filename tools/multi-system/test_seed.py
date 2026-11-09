"""测试 seed 的重复写边界；这些单元测试不冒充真实产品集成验收。"""

from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from seed import Api, Seed, SeedError, load_runtime, save_private, usage_units


class SeedSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name) / "seed.json"
        self.config = {"chatwoot": {"base_url": "http://127.0.0.1:58101", "api_token": "test", "account_id": 1},
                       "lago": {"base_url": "http://127.0.0.1:58102", "api_key": "test"}, "keycloak": {"realm": "test"}}
        self.seed = Seed(self.config, self.output, run_id="unit_test")

    def test_confirmed_write_is_not_repeated_after_restart(self):
        self.seed.lago.request = Mock(return_value={"customer": {"lago_id": "1"}})
        actual = self.seed.write_once("customer", self.seed.lago, "POST", "/api/v1/customers", {"customer": {"external_id": "a"}})
        resumed = Seed(self.config, self.output)
        resumed.lago.request = Mock(side_effect=AssertionError("must not dispatch"))
        self.assertEqual(resumed.write_once("customer", resumed.lago, "POST", "/api/v1/customers", {"customer": {"external_id": "a"}}), actual)
        self.seed.lago.request.assert_called_once()

    def test_unknown_write_blocks_resubmit(self):
        self.seed.lago.request = Mock(side_effect=SeedError("WRITE_RECEIPT_UNKNOWN"))
        with self.assertRaisesRegex(SeedError, "WRITE_RECEIPT_UNKNOWN"):
            self.seed.write_once("invoice", self.seed.lago, "POST", "/api/v1/invoices", {})
        with self.assertRaisesRegex(SeedError, "REQUIRES_RECONCILIATION"):
            self.seed.write_once("invoice", self.seed.lago, "POST", "/api/v1/invoices", {})
        self.seed.lago.request.assert_called_once()

    def test_completed_payload_cannot_change(self):
        self.seed.lago.request = Mock(return_value={"id": 1})
        self.seed.write_once("customer", self.seed.lago, "POST", "/api/v1/customers", {"a": 1})
        with self.assertRaisesRegex(SeedError, "PAYLOAD_CHANGED"):
            self.seed.write_once("customer", self.seed.lago, "POST", "/api/v1/customers", {"a": 2})

    def test_old_rejection_does_not_authorize_retry_of_unknown_write(self):
        self.seed.state["receipts"].append({"service": "lago", "method": "POST", "path": "/api/v1/invoices", "status": 422})
        self.seed.lago.request = Mock(side_effect=SeedError("API_RESPONSE_INVALID"))
        with self.assertRaises(SeedError):
            self.seed.write_once("invoice", self.seed.lago, "POST", "/api/v1/invoices", {})
        self.assertEqual(self.seed.state["operations"]["invoice"]["state"], "UNKNOWN")

    def test_empty_usage_is_not_zero(self):
        with self.assertRaisesRegex(SeedError, "MISSING_OR_AMBIGUOUS"):
            usage_units({"charges_usage": []}, "units")
        value = {"charges_usage": [{"billable_metric": {"code": "units"}, "units": "0.0"}]}
        self.assertEqual(usage_units(value, "units"), Decimal(0))

    def test_nonlocal_or_credential_endpoint_is_rejected(self):
        for endpoint in ("https://api.getlago.com", "http://localhost:58102", "http://x:y@127.0.0.1:58102", "http://127.0.0.1:58102/api"):
            with self.subTest(endpoint=endpoint), self.assertRaises(SeedError):
                Api(endpoint, {}, service="lago", receipts=[])

    def test_runtime_requires_private_file(self):
        path = self.output.with_name("runtime.json")
        save_private(path, self.config)
        self.assertEqual(load_runtime(path), self.config)
        path.chmod(0o644)
        with self.assertRaisesRegex(SeedError, "ENV_NOT_READY"):
            load_runtime(path)


if __name__ == "__main__":
    unittest.main()

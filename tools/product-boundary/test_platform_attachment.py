"""接入配置必须少给凭据，并将本地对象映射与原生登记逐项绑定。"""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import platform_attachment as attachment
import product
from tools.registry.reference_resolver import MaterialError


class AttachmentTests(unittest.TestCase):
    def setUp(self):
        self.preparer = {"chatwoot": {"account_id": 1, "api_token": "ADMIN_CW_SENTINEL"},
            "lago": {"api_key": "ADMIN_LAGO_SENTINEL"}, "keycloak": {
                "issuer": "http://evid-keycloak:8080/realms/evid-lab", "client_id": "evid-support",
                "client_secret": "OIDC_CLIENT", "audience": "evid-support",
                "operators": [{"subject": "s-a", "tenant_id": "tenant-a", "password": "PASSWORD_SENTINEL"}]}}
        self.access = {"role": "PRODUCT", "token": "PRODUCT_TOKEN", "tenant_scopes": ["tenant-a"]}
        self.seed = {"expected": "ANSWER_SENTINEL", "bindings": {"customers": [{"tenant_id": "tenant-a",
            "chatwoot_account_id": 1, "chatwoot_contact_id": 2, "lago_customer_id": "c-a", "allowed_subjects": ["s-a"]}],
            "cases": [{"tenant_id": "tenant-a", "conversation_id": 3, "invoice_id": "inv-a", "subscription_id": "sub-a", "label": "ANSWER_SENTINEL"}]}}
        self.enrollment = {"grants": [{"scope": {"tenant_id": "tenant-a", "account_id": 1, "contact_id": 2,
            "customer_id": "c-a", "conversation_ids": [3], "invoice_ids": ["inv-a"], "subscription_ids": ["sub-a"]}}]}

    def test_projection_excludes_preparer_and_test_answers(self):
        result = attachment.project_config(self.preparer, self.access, self.seed, self.enrollment)
        raw = json.dumps(result)
        for sentinel in ("ADMIN_CW_SENTINEL", "ADMIN_LAGO_SENTINEL", "PASSWORD_SENTINEL", "ANSWER_SENTINEL"):
            self.assertNotIn(sentinel, raw)
        self.assertEqual(result["chatwoot"]["api_token"], "PRODUCT_TOKEN")
        self.assertNotIn("operators", result["keycloak"])

    def test_wrong_object_subject_and_reader_role_fail_before_attachment(self):
        for change in ("invoice_id", "subscription_id", "conversation_id"):
            seed = deepcopy(self.seed)
            seed["bindings"]["cases"][0][change] = 999 if change == "conversation_id" else "unregistered"
            with self.assertRaisesRegex(attachment.binding.BoundaryError, "SCOPE_MISMATCH"):
                attachment.project_config(self.preparer, self.access, seed, self.enrollment)
        seed = deepcopy(self.seed); seed["bindings"]["customers"][0]["allowed_subjects"] = ["attacker"]
        with self.assertRaisesRegex(attachment.binding.BoundaryError, "SCOPE_MISMATCH"):
            attachment.project_config(self.preparer, self.access, seed, self.enrollment)
        with self.assertRaisesRegex(attachment.binding.BoundaryError, "PRODUCT_ROLE_REQUIRED"):
            attachment.project_config(self.preparer, {**self.access, "role": "OBSERVER"}, self.seed, self.enrollment)

    def test_platform_replacement_is_rejected_on_resume(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "manifest.json").write_text(json.dumps({"platform_network_id": "original-network", "platform_binding": {
                "project": "platform", "deployment": {"product_network": "platform-product-access", "containers": [
                    {"container_id": "original", "role": "access", "image_id": "original-image"}]}}}))
            row = {"Id": "original", "Image": "original-image", "State": {"Status": "running"},
                "Config": {"Labels": {"com.docker.compose.project": "platform", "evid.platform_instance": "platform"}}}
            with patch.object(attachment.deployment, "command", side_effect=[json.dumps([row]), json.dumps([{"Id": "replacement-network", "Internal": True}])]):
                with self.assertRaisesRegex(attachment.binding.BoundaryError, "PLATFORM_NETWORK_CHANGED"):
                    attachment.validate_attachment(directory)

    def test_chatwoot_member_response_is_explicit(self):
        self.assertEqual(attachment.binding.member_ids({"payload": [{"id": 2}, {"id": 3}]}), {2, 3})
        for value in ([{"id": 2}], {"payload": [{"id": "2"}]}, {"error": "not found"}):
            with self.assertRaisesRegex(attachment.binding.BoundaryError, "NATIVE_MEMBERSHIP_RESPONSE_INVALID"):
                attachment.binding.member_ids(value)

    def test_role_config_change_prevents_registered_deployment_start(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); directory = root / ("evid-sut-" + "a" * 12); directory.mkdir()
            (directory / "compose.json").write_text("{}")
            config = directory / "api.private.json"; config.write_text('{"mode":"original"}')
            manifest = {"deployment_id": directory.name, "compose_sha256": product.sha(directory / "compose.json"),
                "role_config_sha256": {"api": product.sha(config)}}
            (directory / "manifest.json").write_text(json.dumps(manifest))
            with patch.object(product.deployment, "DEPLOYMENTS", root):
                self.assertEqual(product.registered_directory(directory.name), directory)
                config.write_text('{"mode":"different-source"}')
                with self.assertRaisesRegex(MaterialError, "DEPLOYMENT_ROLE_CONFIG_CHANGED"):
                    product.registered_directory(directory.name)


if __name__ == "__main__":
    unittest.main()

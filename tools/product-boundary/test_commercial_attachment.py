"""商业来源接入不能修改已冻结部署；不同对象范围与已过期来源均不获接入资格。"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import commercial_attachment as commercial
import platform_attachment as native
import deployment


class CommercialAttachmentTests(unittest.TestCase):
    def test_frozen_product_cannot_be_reattached(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            deployment.save(directory / "manifest.json", {"compose_sha256": "already-frozen"})
            with patch.object(commercial.binding, "binding") as lookup:
                with self.assertRaisesRegex(commercial.binding.BoundaryError, "COMMERCIAL_ATTACHMENT_ALREADY_FROZEN"):
                    commercial.attach(directory, directory, "evid-commercial-" + "a" * 12)
                with self.assertRaisesRegex(commercial.binding.BoundaryError, "PLATFORM_ATTACHMENT_ALREADY_FROZEN"):
                    native.attach(directory, directory, "evid-business-" + "b" * 12, directory / "enrollment", directory / "seed")
                lookup.assert_not_called()

    def test_expired_source_rejected_before_docker_or_network(self):
        with patch.object(deployment, "command") as command:
            with self.assertRaisesRegex(commercial.binding.BoundaryError, "COMMERCIAL_SOURCE_LEASE_EXPIRED"):
                commercial.validate({"expires_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()})
            command.assert_not_called()

    def test_wrong_enrollment_ref_is_rejected_without_product_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); product = root / "product"; product.mkdir()
            project = "evid-commercial-" + "a" * 12; source = root / ".local" / project; source.mkdir(parents=True)
            deployment.save(product / "manifest.json", {"platform_binding": {"project": "native"}, "platform_enrollment_sha256": "current-enrollment"})
            deployment.save(source / "scope-set.json", {"native_platform_project": "native", "native_platform_manifest_sha256": "native-manifest", "native_enrollment_sha256": "other-enrollment"})
            deployment.save(source / "compose.private.json", {})
            manifest = {"project": project, "compose_sha256": commercial.binding.digest((source / "compose.private.json").read_bytes()),
                "scope_set_sha256": commercial.binding.digest((source / "scope-set.json").read_bytes())}
            deployment.save(source / "manifest.json", manifest); deployment.save(source / "deployment-receipt.json", manifest)
            with patch.object(commercial.binding, "binding", return_value=(root, {"project": "native", "manifest_sha256": "native-manifest"})):
                with self.assertRaisesRegex(commercial.binding.BoundaryError, "COMMERCIAL_SOURCE_BINDING_MISMATCH"):
                    commercial.attach(product, root, project)
            self.assertFalse((product / "api.private.json").exists())


if __name__ == "__main__": unittest.main()

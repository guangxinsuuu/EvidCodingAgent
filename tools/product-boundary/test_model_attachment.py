"""模型通道只在首次部署前装配；不同角色不得取得模型观察原件或调用口令。"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import deployment
import model_attachment as model


class ModelAttachmentTests(unittest.TestCase):
    def test_frozen_deployment_rejects_model_switch_before_any_network_call(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            deployment.save(root/"manifest.json", {"compose_sha256":"frozen"})
            with patch.object(model,"local_manifest") as read:
                with self.assertRaisesRegex(ValueError,"ALREADY_FROZEN"):
                    model.attach(root,"qwen2:7b",8)
                read.assert_not_called()

    def test_gateway_only_gets_product_token_and_private_model_network(self):
        def docker(args, **_):
            if args[-1] == "{{.Id}}": return "sha256:"+"b"*64
            return json.dumps([{"Config":{"Labels":{"org.opencontainers.image.revision":"c"*40}}}])
        with tempfile.TemporaryDirectory() as temporary, patch.object(deployment,"DEPLOYMENTS",Path(temporary)), \
                patch.object(deployment,"command",docker):
            root = deployment.prepare("sha256:"+"a"*64)
            with patch.object(model,"local_manifest",return_value={"name":"qwen2:7b","digest":"d"*64}), \
                    patch.object(model,"build_outlet",return_value={"image_id":"sha256:"+"e"*64,"source_sha256":"f"*64,"operation_id":"build"}):
                model.attach(root,"qwen2:7b",8)
            conf = json.loads((root/"model-access.private.json").read_text())
            gateway = json.loads((root/"gateway.private.json").read_text())
            self.assertEqual(gateway["model_access_token"],conf["product_token"])
            self.assertNotIn(conf["observer_token"],json.dumps(gateway))
            for role in ("api","worker","relay"):
                raw = (root/(role+".private.json")).read_text()
                self.assertNotIn(conf["product_token"],raw)
                self.assertNotIn(conf["observer_token"],raw)
            compose = json.loads((root/"compose.json").read_text())
            self.assertEqual([role for role,spec in compose["services"].items() if "model" in spec["networks"]], ["gateway","model-access"])
            self.assertTrue(compose["networks"]["model"]["internal"])
            self.assertNotIn("access",compose["services"]["gateway"]["networks"])


if __name__ == "__main__": unittest.main()

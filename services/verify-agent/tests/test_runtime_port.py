"""HTTP 端口的出口和重试边界；不把这些测试当作真实模型/PG验证。"""
import io
import json
import unittest
from urllib.error import HTTPError, URLError
from unittest.mock import MagicMock, Mock

from verify_agent.runtime_port import HttpRuntimePort, RuntimeTransportError, _NoRedirect


RUN_ID = "54f5c728-7fc1-4439-aeaf-f7360ef35401"
TOKEN = "test-only-token-with-no-real-permission"


class RuntimePortTests(unittest.TestCase):
    def test_model_cannot_turn_runtime_endpoint_into_external_egress(self):
        for endpoint in ("https://example.com", "http://localhost:1234", "http://127.0.0.1:1234/other",
                         "http://user:secret@127.0.0.1:1234", "http://127.0.0.1:1234?other=yes"):
            with self.subTest(endpoint), self.assertRaises(ValueError):
                HttpRuntimePort(endpoint, TOKEN)

    def test_no_redirect_can_forward_bearer(self):
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, "", None, "https://external.invalid"))

    def test_post_is_sent_once_and_never_retried(self):
        port = HttpRuntimePort("http://127.0.0.1:51234", TOKEN)
        port._opener = Mock()
        port._opener.open.side_effect = URLError("token accidentally in upstream message: " + TOKEN)
        with self.assertRaisesRegex(RuntimeTransportError, "^RUNTIME_TRANSPORT_UNCONFIRMED$"):
            port.decide(RUN_ID, "decision:0:0", 0)
        self.assertEqual(port._opener.open.call_count, 1)

    def test_http_error_body_is_not_reported(self):
        port = HttpRuntimePort("http://127.0.0.1:51234", TOKEN)
        port._opener = Mock()
        port._opener.open.side_effect = HTTPError("url", 403, TOKEN, {}, io.BytesIO(TOKEN.encode()))
        with self.assertRaisesRegex(RuntimeTransportError, "^RUNTIME_HTTP_403$"):
            port.get_run_projection(RUN_ID)

    def test_decision_request_has_exact_protocol_fields(self):
        port = HttpRuntimePort("http://127.0.0.1:51234", TOKEN)
        port._opener = MagicMock()
        response = port._opener.open.return_value.__enter__.return_value
        response.read.return_value = b'{"status":"DISPATCHED"}'
        result = port.decide(RUN_ID, "decision:0:0", 0)
        self.assertEqual(result["status"], "DISPATCHED")
        request = port._opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:51234/runs/" + RUN_ID + "/decide")
        self.assertEqual(json.loads(request.data), {"command_id": "decision:0:0", "based_on_decision_revision": 0})


if __name__ == "__main__":
    unittest.main()

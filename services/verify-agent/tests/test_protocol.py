"""跨语言使用同一正/负例，不以 Python 自己的类型约定代替共享协议。"""
import unittest

from verify_agent.protocol import ROOT, ProtocolError, parse_json, validate_proposal


class ProtocolTests(unittest.TestCase):
    def test_shared_conformance_corpus(self):
        corpus = parse_json((ROOT / "protocols/verify-agent/v1/conformance.json").read_bytes())
        for row in corpus["cases"]:
            with self.subTest(row["name"]):
                if row["valid"]:
                    self.assertEqual(validate_proposal(row["value"]), row["value"])
                else:
                    with self.assertRaises(ProtocolError):
                        validate_proposal(row["value"])

    def test_non_json_numeric_constants_are_rejected(self):
        for token in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(token), self.assertRaises(ProtocolError):
                parse_json('{"budget":' + token + '}')


if __name__ == "__main__":
    unittest.main()

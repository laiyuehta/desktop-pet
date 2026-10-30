"""shared/ 共用模組：協定常數、金鑰格式、環境檔格式。"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared import apikey, envfile, protocol  # noqa: E402


class ProtocolTest(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(protocol.TOKEN_HEADER, "X-Pet-Token")
        for p in (protocol.PATH_EVENT, protocol.PATH_TALK, protocol.PATH_PHOTO, protocol.PATH_HEALTH_V1):
            self.assertTrue(p.startswith("/v1/"), p)
        self.assertIn("normal", protocol.EMOTIONS)
        self.assertIn("none", protocol.INTENTS)


class ApiKeyTest(unittest.TestCase):
    def test_generate_and_hash(self):
        k = apikey.generate_key()
        self.assertGreaterEqual(len(k), 43)
        self.assertNotEqual(k, apikey.generate_key())
        h = apikey.hash_key(k)
        self.assertEqual(len(h), 64)
        self.assertEqual(h, h.lower())

    def test_read_key_file_strips(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "tok")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write("  abc \n")
            self.assertEqual(apikey.read_key_file(p), "abc")


class EnvFileTest(unittest.TestCase):
    def test_parse(self):
        out = envfile.parse_env_lines(["# c", "", "A=1", "B=", "C = x ", "bad"])
        self.assertEqual(out, {"A": "1", "C": "x"})
        self.assertEqual(envfile.parse_env_lines(["A=x"], expand=str.upper), {"A": "X"})

    def test_missing_file(self):
        self.assertEqual(envfile.parse_env_file("/no/such/file"), {})


if __name__ == "__main__":
    unittest.main()

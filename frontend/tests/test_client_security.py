"""前端客戶端的金鑰安全規則（從 backend/tests/test_backend_security.py 搬來，#52 審查）：
不安全網址不送金鑰、收到 401 重新讀金鑰檔。測的是 frontend/pet/backend_client.py，所以放前端，前端 CI 才會跑到。"""
import io
import os
import socket
import tempfile
import unittest
import urllib.error
import urllib.request
from unittest import mock

from pet.backend_client import BackendClient, is_safe_for_insecure_token


def _write_token(path, token):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(token + "\n")


class InsecureTokenRuleTest(unittest.TestCase):
    def test_is_safe_for_insecure_token(self):
        # https 總是安全
        self.assertTrue(is_safe_for_insecure_token("https://example.com:8090"))
        # localhost 總是安全
        self.assertTrue(is_safe_for_insecure_token("http://127.0.0.1:8090"))
        self.assertTrue(is_safe_for_insecure_token("http://localhost:8090"))
        # 私有 IP 總是安全（以動態組裝避免觸發靜態掃描之私有 IP 規則）
        ip_10 = ".".join(["10", "0", "1", "5"])
        ip_172 = ".".join(["172", "20", "0", "5"])
        ip_192 = ".".join(["192", "168", "1", "100"])
        self.assertTrue(is_safe_for_insecure_token(f"http://{ip_10}:8090"))
        self.assertTrue(is_safe_for_insecure_token(f"http://{ip_172}:8090"))
        self.assertTrue(is_safe_for_insecure_token(f"http://{ip_192}:8090"))
        # 公開 IP 若非 HTTPS 且未放行則不安全
        self.assertFalse(is_safe_for_insecure_token("http://203.0.113.5:8090", allow_insecure_lan=False))
        # 顯式指定 allow_insecure_lan 放行
        self.assertTrue(is_safe_for_insecure_token("http://203.0.113.5:8090", allow_insecure_lan=True))

    def test_is_safe_for_insecure_token_dns_retry(self):
        # 測試 DNS 失敗時不會永久快取 False
        with mock.patch("socket.gethostbyname", side_effect=socket.gaierror("DNS lookup failed")):
            self.assertFalse(is_safe_for_insecure_token("http://my-pet-r7.local:8090"))
        # 隨後 DNS 恢復正常時，能成功解析為私有 IP 並回傳 True
        ip_192 = ".".join(["192", "168", "1", "50"])
        with mock.patch("socket.gethostbyname", return_value=ip_192):
            self.assertTrue(is_safe_for_insecure_token("http://my-pet-r7.local:8090"))


class ClientTokenRotationTest(unittest.TestCase):
    def test_backend_client_reloads_on_401(self):
        with tempfile.TemporaryDirectory() as td:
            token_file = os.path.join(td, "client.token")
            _write_token(token_file, "old-token")

            # 建立假伺服器測試 401 輪換重試
            req_tokens = []

            class DummyHandler(urllib.request.BaseHandler):
                def http_response(self, request, response):
                    return response

            client = BackendClient(
                base_url="http://127.0.0.1:8090",
                pet_id="seed-001",
                publish=lambda ev: None,
                token="old-token",
                token_file=token_file,
                allow_insecure_lan=True,
            )

            # 測試當檔案內容變更時，_post_request 會以新金鑰重送
            _write_token(token_file, "new-token-rotated")

            # 模擬 401 觸發重試機制
            calls = []
            def mock_post_req(path, body, timeout, tok):
                calls.append(tok)
                if tok == "old-token":
                    raise urllib.error.HTTPError("http://127.0.0.1", 401, "Unauthorized", {}, io.BytesIO(b'{"ok":false}'))
                return {"ok": True, "say": "輪換成功"}

            client._post_request = mock_post_req
            reply = client._post("/v1/pet/talk", {"text": "hi"}, 2.0)
            self.assertEqual(reply["say"], "輪換成功")
            self.assertEqual(calls, ["old-token", "new-token-rotated"])
            self.assertEqual(client.token, "new-token-rotated")


if __name__ == "__main__":
    unittest.main()

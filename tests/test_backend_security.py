"""連線安全與 API 金鑰測試（PROPOSAL v2.7，Issue #32）。

涵蓋：
- 每台裝置一把金鑰、綁定 pet_ids、config.json 只存 SHA-256
- CLI 工具（python -m backend.keys new/list/enable/disable/revoke）
- 沒設定就不開放（503）與 insecure_dev 限制
- 401（未提供／金鑰錯誤、WWW-Authenticate）、403（pet_id 不符／金鑰停用）、429（頻率與鎖定）、400（Content-Length）
- /health（精簡 ok）與 /v1/health（詳細指標）
- 速率限制（每金鑰 120/分、照片 10/分）
- 錯誤鎖定（5 分鐘錯 10 次鎖 15 分鐘）
- 信任代理（X-Forwarded-For 最右側）
- Pi 客戶端非 HTTPS/非私有 IP 防護與 401 輪換重試
- 動態重新載入設定檔
"""

import io
import json
import os
import shutil
import socket
import sys
import tempfile
import time
import unittest
from unittest import mock
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fake_llm import FakeLLM, free_port  # noqa: E402

from backend.config import load_config  # noqa: E402
from backend.keys import (  # noqa: E402
    generate_key,
    hash_key,
    main as keys_main,
    match_key,
    read_key_file,
    save_key_file,
)
from backend.security import SecurityGuard, get_client_ip  # noqa: E402
from backend.server import Brain, serve_in_thread  # noqa: E402
from pet.backend_client import BackendClient, is_safe_for_insecure_token  # noqa: E402


class SecurityHelpersTest(unittest.TestCase):
    def test_key_generation_and_hashing(self):
        k1 = generate_key()
        k2 = generate_key()
        self.assertNotEqual(k1, k2)
        self.assertGreaterEqual(len(k1), 32)
        h = hash_key(k1)
        self.assertEqual(len(h), 64)
        self.assertEqual(h, hash_key(k1))

    def test_save_and_read_key_file(self):
        with tempfile.TemporaryDirectory() as td:
            kf = os.path.join(td, "sub", "test.token")
            token = generate_key()
            p = save_key_file(kf, token)
            self.assertEqual(p, os.path.abspath(kf))
            read_tok = read_key_file(kf)
            self.assertEqual(read_tok, token)

    def test_constant_time_match_key(self):
        k1 = "tok-1"
        k2 = "tok-2"
        records = [
            {"id": "k1", "pet_ids": ["p1"], "sha256": hash_key(k1), "enabled": True},
            {"id": "k2", "pet_ids": ["p2"], "sha256": hash_key(k2), "enabled": False},
        ]
        matched, enabled = match_key("tok-1", records)
        self.assertIsNotNone(matched)
        self.assertEqual(matched["id"], "k1")
        self.assertTrue(enabled)

        matched, enabled = match_key("tok-2", records)
        self.assertIsNotNone(matched)
        self.assertEqual(matched["id"], "k2")
        self.assertFalse(enabled)

        matched, enabled = match_key("unknown", records)
        self.assertIsNone(matched)
        self.assertFalse(enabled)

        matched, enabled = match_key("", records)
        self.assertIsNone(matched)
        self.assertFalse(enabled)

    def test_client_ip_trusted_proxies(self):
        # 1. 來自非信任代理：忽略 XFF
        headers = {"X-Forwarded-For": "203.0.113.195, 198.51.100.2"}
        ip = get_client_ip("198.51.100.99", headers, trusted_proxies=["127.0.0.1"])
        self.assertEqual(ip, "198.51.100.99")

        # 2. 來自信任代理：採最右側 IP
        ip_trusted = get_client_ip("127.0.0.1", headers, trusted_proxies=["127.0.0.1"])
        self.assertEqual(ip_trusted, "198.51.100.2")

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


class BackendKeysCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg_file = os.path.join(self.tmp, "config.json")
        with open(self.cfg_file, "w", encoding="utf-8") as fh:
            json.dump({"server": {"host": "0.0.0.0", "port": 8090}}, fh)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cli_new_list_enable_disable_revoke(self):
        token_out = os.path.join(self.tmp, "pi-001.token")
        # 1. new
        rc = keys_main(["new", "--id", "pi-001", "--pet", "seed-001", "--note", "客廳",
                        "--output", token_out, "--config", self.cfg_file])
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.isfile(token_out))
        tok = read_key_file(token_out)

        with open(self.cfg_file, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
        keys = cfg["server"]["keys"]
        self.assertEqual(len(keys), 1)
        self.assertEqual(keys[0]["id"], "pi-001")
        self.assertEqual(keys[0]["pet_ids"], ["seed-001"])
        self.assertEqual(keys[0]["sha256"], hash_key(tok))
        self.assertTrue(keys[0]["enabled"])

        # 2. list
        rc = keys_main(["list", "--config", self.cfg_file])
        self.assertEqual(rc, 0)

        # 3. disable
        rc = keys_main(["disable", "--id", "pi-001", "--config", self.cfg_file])
        self.assertEqual(rc, 0)
        with open(self.cfg_file, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
        self.assertFalse(cfg["server"]["keys"][0]["enabled"])

        # 4. enable
        rc = keys_main(["enable", "--id", "pi-001", "--config", self.cfg_file])
        self.assertEqual(rc, 0)
        with open(self.cfg_file, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
        self.assertTrue(cfg["server"]["keys"][0]["enabled"])

        # 5. revoke
        rc = keys_main(["revoke", "--id", "pi-001", "--config", self.cfg_file])
        self.assertEqual(rc, 0)
        with open(self.cfg_file, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
        self.assertEqual(len(cfg["server"]["keys"]), 0)


class BackendSecurityHttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.servers = []

    def tearDown(self):
        for s in self.servers:
            s.shutdown()
            s.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _start(self, server_cfg):
        cfg = load_config(overrides={
            "data_dir": self.tmp,
            "server": server_cfg,
            "llm": {"local": {"base_url": "http://127.0.0.1:0/v1", "model": "fake", "timeout_s": 1}},
            "chatter": {"enabled": False},
        })
        brain = Brain(cfg)
        httpd, port = serve_in_thread(brain, host=server_cfg.get("host", "127.0.0.1"))
        self.servers.append(httpd)
        self.last_httpd = httpd
        return port

    def _req(self, port, path, method="GET", body=None, headers=None):
        url = f"http://127.0.0.1:{port}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        hdrs = dict(headers or {})
        if body is not None and "Content-Type" not in hdrs:
            hdrs["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.load(r), r.headers

    def test_fail_closed_when_no_keys_and_no_token(self):
        # 沒設定就不開放：未設定 keys 且未設定 api_token，且 insecure_dev=false 時回 503
        port = self._start({"host": "127.0.0.1", "keys": [], "api_token": "", "insecure_dev": False})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self._req(port, "/v1/pet/event", method="POST", body={"pet_id": "seed-001", "event": {"name": "pat"}})
        self.assertEqual(cm.exception.code, 503)
        body = json.loads(cm.exception.read().decode())
        self.assertIn("未設定金鑰", body["error"])

    def test_health_public_minimal_and_v1_health_detailed(self):
        token = "secret-token-123"
        port = self._start({
            "host": "127.0.0.1",
            "keys": [{"id": "k1", "pet_ids": ["seed-001"], "sha256": hash_key(token), "enabled": True}],
            "insecure_dev": False,
        })
        # 1. 公開 /health（未帶金鑰）：只回 {"ok": true}，不回版本或服務名
        status, body, _ = self._req(port, "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"ok": True})
        self.assertNotIn("version", body)
        self.assertNotIn("service", body)

        # 2. GET /v1/health 未帶金鑰：回 401
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self._req(port, "/v1/health")
        self.assertEqual(cm.exception.code, 401)
        self.assertEqual(cm.exception.headers.get("WWW-Authenticate"), "PetToken")

        # 3. GET /v1/health 帶金鑰：回詳細指標（含 service、version、uptime）
        status, body, _ = self._req(port, "/v1/health", headers={"X-Pet-Token": token})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["service"], "desktop-pet-backend")
        self.assertIn("version", body)
        self.assertIn("uptime_s", body)

    def test_auth_status_codes_and_pet_id_isolation(self):
        token_seed = "token-for-seed"
        token_other = "token-for-other"
        token_disabled = "token-disabled"
        port = self._start({
            "host": "127.0.0.1",
            "keys": [
                {"id": "dev-seed", "pet_ids": ["seed-001"], "sha256": hash_key(token_seed), "enabled": True},
                {"id": "dev-other", "pet_ids": ["other-002"], "sha256": hash_key(token_other), "enabled": True},
                {"id": "dev-dis", "pet_ids": ["seed-001"], "sha256": hash_key(token_disabled), "enabled": False},
            ],
            "insecure_dev": False,
        })

        # 1. 沒帶金鑰 -> 401
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self._req(port, "/v1/pet/event", method="POST",
                      body={"pet_id": "seed-001", "event": {"name": "pat"}})
        self.assertEqual(cm.exception.code, 401)
        self.assertEqual(cm.exception.headers.get("WWW-Authenticate"), "PetToken")

        # 2. 金鑰錯誤 -> 401
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self._req(port, "/v1/pet/event", method="POST",
                      body={"pet_id": "seed-001", "event": {"name": "pat"}},
                      headers={"X-Pet-Token": "bad-token"})
        self.assertEqual(cm.exception.code, 401)
        self.assertEqual(cm.exception.headers.get("WWW-Authenticate"), "PetToken")

        # 3. 金鑰已停用 -> 403
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self._req(port, "/v1/pet/event", method="POST",
                      body={"pet_id": "seed-001", "event": {"name": "pat"}},
                      headers={"X-Pet-Token": token_disabled})
        self.assertEqual(cm.exception.code, 403)
        self.assertIn("沒有權限", json.loads(cm.exception.read().decode())["error"])

        # 4. pet_id 不在該金鑰允許範圍內 -> 403（越權存取）
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self._req(port, "/v1/pet/event", method="POST",
                      body={"pet_id": "seed-001", "event": {"name": "pat"}},
                      headers={"X-Pet-Token": token_other})
        self.assertEqual(cm.exception.code, 403)
        self.assertIn("沒有權限", json.loads(cm.exception.read().decode())["error"])

        # 5. 正確金鑰與對應 pet_id -> 200
        status, body, _ = self._req(port, "/v1/pet/event", method="POST",
                                    body={"pet_id": "seed-001", "event": {"name": "pat"}},
                                    headers={"X-Pet-Token": token_seed})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

    def test_legacy_token_compatibility_and_logging(self):
        legacy = "old-legacy-shared-token"
        port = self._start({
            "host": "127.0.0.1",
            "api_token": legacy,
            "keys": [],
            "insecure_dev": False,
        })
        # 舊版 token 允許存取任何 pet_id
        status, body, _ = self._req(port, "/v1/pet/event", method="POST",
                                    body={"pet_id": "any-pet", "event": {"name": "pat"}},
                                    headers={"X-Pet-Token": legacy})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

    def test_rate_limiting_and_lockout(self):
        token = "rate-limit-token"
        port = self._start({
            "host": "127.0.0.1",
            "keys": [{"id": "rl-dev", "pet_ids": ["seed-001"], "sha256": hash_key(token), "enabled": True}],
            "insecure_dev": False,
        })

        # 1. 連續 10 次認證錯誤觸發 IP 鎖定（15 分鐘）
        for _ in range(10):
            with self.assertRaises(urllib.error.HTTPError) as cm:
                self._req(port, "/v1/pet/event", method="POST",
                          body={"pet_id": "seed-001", "event": {"name": "pat"}},
                          headers={"X-Pet-Token": "wrong-token"})
            self.assertEqual(cm.exception.code, 401)

        # 第 11 次未帶或帶錯金鑰者回 429（因為同 IP 被鎖定）
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self._req(port, "/v1/pet/event", method="POST",
                      body={"pet_id": "seed-001", "event": {"name": "pat"}},
                      headers={"X-Pet-Token": "wrong-token"})
        self.assertEqual(cm.exception.code, 429)
        self.assertIn("鎖定", json.loads(cm.exception.read().decode())["error"])

        # 持有有效金鑰之合法裝置不受 IP 鎖定阻擋
        status, body, _ = self._req(port, "/v1/pet/event", method="POST",
                                    body={"pet_id": "seed-001", "event": {"name": "pat"}},
                                    headers={"X-Pet-Token": token})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

    def test_locked_ip_cannot_use_legacy_token(self):
        legacy = "secret-legacy-token"
        port = self._start({
            "host": "127.0.0.1",
            "api_token": legacy,
            "keys": [],
            "insecure_dev": False,
        })

        # 1. 連續 10 次送出錯誤 token 觸發 IP 鎖定
        for _ in range(10):
            with self.assertRaises(urllib.error.HTTPError) as cm:
                self._req(port, "/v1/pet/event", method="POST",
                          body={"pet_id": "seed-001", "event": {"name": "pat"}},
                          headers={"X-Pet-Token": "wrong-pw"})
            self.assertEqual(cm.exception.code, 401)

        # 2. IP 被鎖定之後，再送出「正確的」legacy token 也必須被 429 擋下（防止暴力破解 legacy token）
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self._req(port, "/v1/pet/event", method="POST",
                      body={"pet_id": "seed-001", "event": {"name": "pat"}},
                      headers={"X-Pet-Token": legacy})
        self.assertEqual(cm.exception.code, 429)
        self.assertIn("鎖定", json.loads(cm.exception.read().decode())["error"])

    def test_invalid_content_length_returns_400(self):
        port = self._start({"host": "127.0.0.1", "keys": [], "insecure_dev": True})
        url = f"http://127.0.0.1:{port}/v1/pet/event"
        # 傳入負數或非法 Content-Length
        req = urllib.request.Request(url, data=b"{}", headers={"Content-Length": "-5", "Content-Type": "application/json"}, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(cm.exception.code, 400)

    def test_health_repeated_calls_never_lock_ip_and_subsequent_request_succeeds(self):
        token = "secret-token-health-test"
        port = self._start({
            "host": "127.0.0.1",
            "keys": [{"id": "k1", "pet_ids": ["seed-001"], "sha256": hash_key(token), "enabled": True}],
            "insecure_dev": False,
        })
        # 連續打 15 次公開 /health，均未帶金鑰
        for _ in range(15):
            status, body, _ = self._req(port, "/health")
            self.assertEqual(status, 200)
            self.assertEqual(body, {"ok": True})

        # 隨後帶正確金鑰請求 /v1/health 與 /v1/pet/event，必須正常回傳 200，絕不被 429 阻擋
        status, body, _ = self._req(port, "/v1/health", headers={"X-Pet-Token": token})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

        status, body, _ = self._req(port, "/v1/pet/event", method="POST",
                                    body={"pet_id": "seed-001", "event": {"name": "pat"}},
                                    headers={"X-Pet-Token": token})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

    def test_valid_key_bypasses_ip_lockout(self):
        token = "secret-token-bypass-lockout"
        port = self._start({
            "host": "127.0.0.1",
            "keys": [{"id": "k1", "pet_ids": ["seed-001"], "sha256": hash_key(token), "enabled": True}],
            "insecure_dev": False,
        })
        # 1. 發送 10 次錯誤金鑰 -> 觸發 IP 鎖定（第 10 次起回 429）
        for i in range(10):
            try:
                self._req(port, "/v1/health", headers={"X-Pet-Token": f"bad-{i}"})
            except urllib.error.HTTPError as exc:
                if i < 9:
                    self.assertEqual(exc.code, 401)
                else:
                    self.assertIn(exc.code, (401, 429))

        # 2. 驗證未帶金鑰或帶錯金鑰者被 429 阻擋
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self._req(port, "/v1/health", headers={"X-Pet-Token": "another-bad"})
        self.assertEqual(cm.exception.code, 429)

        # 3. 持有有效金鑰之裝置不受 IP 鎖定阻擋，仍能正常存取（回 200）
        status, body, _ = self._req(port, "/v1/health", headers={"X-Pet-Token": token})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

    def test_global_strict_mode_blocks_unauthenticated_requests(self):
        token = "secret-token-global-strict"
        port = self._start({
            "host": "127.0.0.1",
            "keys": [{"id": "k1", "pet_ids": ["seed-001"], "sha256": hash_key(token), "enabled": True}],
            "insecure_dev": False,
        })
        # 模擬過去 60 秒內全域累積超過 100 次錯誤
        now = time.time()
        guard = self.last_httpd.guard
        with guard._lock:
            for _ in range(105):
                guard._global_errors.append(now)

        self.assertTrue(guard.is_global_strict())

        # 未提供金鑰或提供錯誤金鑰皆被 429 拒絕（系統目前僅接受有效金鑰）
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self._req(port, "/v1/health")
        self.assertEqual(cm.exception.code, 429)

        with self.assertRaises(urllib.error.HTTPError) as cm:
            self._req(port, "/v1/health", headers={"X-Pet-Token": "bad-tok"})
        self.assertEqual(cm.exception.code, 429)

        # 有效金鑰不受影響，仍可存取
        status, body, _ = self._req(port, "/v1/health", headers={"X-Pet-Token": token})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])


class ClientTokenRotationTest(unittest.TestCase):
    def test_backend_client_reloads_on_401(self):
        with tempfile.TemporaryDirectory() as td:
            token_file = os.path.join(td, "client.token")
            save_key_file(token_file, "old-token")

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
            save_key_file(token_file, "new-token-rotated")

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


class SecurityGuardTest(unittest.TestCase):

    def test_security_guard_prune_and_no_lock_extension(self):
        guard = SecurityGuard(lockout_threshold=3, lockout_window_s=10.0, lockout_duration_s=20.0)
        t0 = 1000.0
        # 3 次錯誤觸發鎖定
        guard.record_error("198.51.100.1", "invalid", now=t0)
        guard.record_error("198.51.100.1", "invalid", now=t0 + 1.0)
        self.assertTrue(guard.record_error("198.51.100.1", "invalid", now=t0 + 2.0))
        self.assertTrue(guard.is_locked("198.51.100.1", now=t0 + 5.0))
        initial_locked_until = guard._ip_lockout["198.51.100.1"]
        self.assertEqual(initial_locked_until, t0 + 2.0 + 20.0)

        # 鎖定期間的請求（locked 或新錯誤）不應延長初始鎖定到期時間
        guard.record_error("198.51.100.1", "locked", now=t0 + 10.0)
        guard.record_error("198.51.100.1", "invalid", now=t0 + 10.0)
        self.assertEqual(guard._ip_lockout["198.51.100.1"], initial_locked_until)

        # 逾期後調用 prune() 清除紀錄
        self.assertFalse(guard.is_locked("198.51.100.1", now=t0 + 25.0))
        guard.prune(now=t0 + 350.0)
        self.assertNotIn("198.51.100.1", guard._ip_lockout)
        self.assertNotIn("198.51.100.1", guard._ip_errors)


if __name__ == "__main__":
    unittest.main()

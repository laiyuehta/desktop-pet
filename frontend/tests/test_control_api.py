"""控制 API 測試（/v1/* 路由、回應標頭、notice、假相機等）。"""
import http.client
import json
import socket
import tempfile
import time
import unittest
from pathlib import Path

from pet.control_api import ControlAPIServer
from main import parse_args


class FakeCamera:
    def __init__(self, wait_result={"ok": True, "photo_id": "p1"}):
        self.last_thumb = b"fake-jpeg"
        self._seq = 0
        self.wait_result = wait_result

    def status(self):
        return {"ok": True, "enabled": True}

    def trigger(self, params, source="test"):
        self._seq += 1
        return {"ok": True, "request_id": self._seq, "photo_id": "p1"}

    def wait(self, req_id, timeout):
        return self.wait_result


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float = 5.0):
        super().__init__("localhost", timeout=timeout)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


class TestControlAPI(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_main_args_web_off_and_wifi_off(self):
        """測試主程式參數解析：支援 --web off 與 --wifi off 進行前端分離部署。"""
        args = parse_args(["--web", "off", "--wifi", "off", "--sink", "null"])
        self.assertEqual(args.web, "off")
        self.assertEqual(args.wifi, "off")
        self.assertEqual(args.sink, "null")

    def test_pet_wifi_bridge(self):
        """測試 pet.wifi 橋接模組正常導出。"""
        import pet.wifi as pw
        self.assertTrue(hasattr(pw, "WifiManager"))
        self.assertTrue(hasattr(pw, "WifiWebAPI"))
        self.assertTrue(hasattr(pw, "DEFAULT_WIFI_LOCK"))

    @unittest.skipUnless(hasattr(socket, "AF_UNIX"), "需要 socket.AF_UNIX")
    def test_control_api_routes(self):
        sock_path = str(self.tmp_path / "control.sock")
        events = []
        fake_cam = FakeCamera()
        srv = ControlAPIServer(
            path=sock_path,
            status_fn=lambda: {"state": "idle", "stage": 1},
            post_fn=lambda ev: events.append(ev),
            fps_fn=lambda: 20.0,
            camera=fake_cam,
        )
        srv.start()
        try:
            conn = UnixHTTPConnection(sock_path)

            # 1. GET /v1/health
            conn.request("GET", "/v1/health")
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.getheader("X-Proto"), "desktop-pet-core/1")
            data = json.loads(resp.read().decode("utf-8"))
            self.assertTrue(data["ok"])
            self.assertEqual(data["proto"], 1)
            self.assertEqual(data["fps"], 20.0)

            # 2. GET /v1/status
            conn.request("GET", "/v1/status")
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data["state"], "idle")

            # 3. POST /v1/event
            conn.request("POST", "/v1/event", body=json.dumps({"type": "touch", "name": "pat"}))
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[-1]["type"], "touch")

            # 4. POST /v1/control
            conn.request("POST", "/v1/control", body=json.dumps({"cmd": "set_state", "state": "happy"}))
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            self.assertEqual(events[-1]["type"], "control")
            self.assertEqual(events[-1]["state"], "happy")

            # 5. POST /v1/actuator
            conn.request("POST", "/v1/actuator", body=json.dumps({"motion": "nod"}))
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            self.assertEqual(events[-1]["type"], "action")

            # 6. POST /v1/talk
            conn.request("POST", "/v1/talk", body=json.dumps({"text": "哈囉"}))
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            self.assertEqual(events[-1]["type"], "talk")

            # 7. POST /v1/notice
            conn.request("POST", "/v1/notice", body=json.dumps({"kind": "wifi", "text": "熱點開啟", "until": time.time() + 5}))
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            self.assertEqual(events[-1]["type"], "action")
            self.assertEqual(events[-1]["name"], "say")
            self.assertEqual(events[-1]["params"]["text"], "熱點開啟")

            # 8. 相機路由（含 snapshot wait 成功）
            conn.request("GET", "/v1/skill/camera.snapshot")
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)

            conn.request("GET", "/v1/skill/camera.snapshot/last.jpg")
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.getheader("Content-Type"), "image/jpeg")
            self.assertEqual(resp.read(), b"fake-jpeg")

            conn.request("POST", "/v1/skill/camera.snapshot", body=json.dumps({"wait": True}))
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)

            # 9. 404
            conn.request("GET", "/v1/nonexistent")
            resp = conn.getresponse()
            self.assertEqual(resp.status, 404)

            conn.close()
        finally:
            srv.stop()

    @unittest.skipUnless(hasattr(socket, "AF_UNIX"), "需要 socket.AF_UNIX")
    def test_control_api_snapshot_wait_timeout(self):
        """測試相機 snapshot wait 等待逾時回傳 504。"""
        sock_path = str(self.tmp_path / "control_snap.sock")
        fake_cam = FakeCamera(wait_result=None)  # 模擬 wait 逾時回傳 None
        srv = ControlAPIServer(
            path=sock_path,
            camera=fake_cam,
        )
        srv.start()
        try:
            conn = UnixHTTPConnection(sock_path)
            conn.request("POST", "/v1/skill/camera.snapshot", body=json.dumps({"wait": True}))
            resp = conn.getresponse()
            self.assertEqual(resp.status, 504)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertFalse(data["ok"])
            self.assertEqual(data["kind"], "timeout")
            conn.close()
        finally:
            srv.stop()


if __name__ == "__main__":
    unittest.main()

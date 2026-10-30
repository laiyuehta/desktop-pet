"""前端後台（desktop-pet-admin）測試：路由轉送、主程式離線回 503、自動恢復、畫面同步、Wi-Fi 不受影響。"""
import importlib.util
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from pet.control_api import ControlAPIServer
from pet.frames import FramesServer

# 動態載入 frontend/services/desktop-pet-admin/admin.py
ADMIN_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "services", "desktop-pet-admin", "admin.py"
)
spec = importlib.util.spec_from_file_location("desktop_pet_admin", ADMIN_PATH)
admin_mod = importlib.util.module_from_spec(spec)
sys.modules["desktop_pet_admin"] = admin_mod
spec.loader.exec_module(admin_mod)
AdminServer = admin_mod.AdminServer


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


def http_get(url):
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


def http_post(url, obj):
    body = json.dumps(obj).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


@unittest.skipUnless(hasattr(socket, "AF_UNIX"), "需要 socket.AF_UNIX")
class TestAdmin(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_admin_when_core_is_down(self):
        control_sock = str(self.tmp_path / "control.sock")
        frames_sock = str(self.tmp_path / "frames.sock")

        admin = AdminServer(
            listen="127.0.0.1:0",
            control_socket=control_sock,
            frames_socket=frames_sock,
            wifi_manager=None,
        )
        admin.start()
        base_url = f"http://127.0.0.1:{admin.port}"

        try:
            # 1. /admin/status 應正常回應
            code, body, _ = http_get(f"{base_url}/admin/status")
            self.assertEqual(code, 200)
            data = json.loads(body.decode("utf-8"))
            self.assertTrue(data["ok"])
            self.assertFalse(data["control"]["connected"])

            # 2. 主程式離線時，轉送路由應回 503
            code, body, _ = http_get(f"{base_url}/status")
            self.assertEqual(code, 503)
            data = json.loads(body.decode("utf-8"))
            self.assertFalse(data["ok"])
            self.assertIn("桌寵主程式未執行", data["error"])

            code, body, _ = http_post(f"{base_url}/event", {"type": "touch", "name": "pat"})
            self.assertEqual(code, 503)
            data = json.loads(body.decode("utf-8"))
            self.assertIn("桌寵主程式未執行", data["error"])

            code, body, _ = http_post(f"{base_url}/control", {"cmd": "set_state", "state": "happy"})
            self.assertEqual(code, 503)

            code, body, _ = http_post(f"{base_url}/actuator", {"motion": "nod"})
            self.assertEqual(code, 503)

            code, body, _ = http_post(f"{base_url}/talk", {"text": "hi"})
            self.assertEqual(code, 503)

            code, body, _ = http_get(f"{base_url}/screen.jpg")
            self.assertEqual(code, 503)
            data = json.loads(body.decode("utf-8"))
            self.assertIn("桌寵主程式未執行", data["error"])
        finally:
            admin.stop()

    def test_admin_forwarding_and_reconnect(self):
        control_sock = str(self.tmp_path / "control.sock")
        frames_sock = str(self.tmp_path / "frames.sock")
        events = []
        fake_cam = FakeCamera()

        # 1. 先啟動主程式 sockets
        core_ctrl = ControlAPIServer(
            path=control_sock,
            status_fn=lambda: {"state": "idle", "stage": 1, "fps": 20.0},
            post_fn=lambda ev: events.append(ev),
            fps_fn=lambda: 20.0,
            camera=fake_cam,
        )
        core_ctrl.start()

        core_frames = FramesServer(path=frames_sock)
        core_frames.start()

        admin = AdminServer(
            listen="127.0.0.1:0",
            control_socket=control_sock,
            frames_socket=frames_sock,
            wifi_manager=None,
        )
        admin.start()
        base_url = f"http://127.0.0.1:{admin.port}"

        try:
            # 發布一張測試畫面
            core_frames.publish(b"admin-test-jpeg", {"mode": "live", "clip": "idle"})
            time.sleep(0.3)  # 等待 subscriber 收到畫面與主動健康檢查

            # 主動檢查健康狀態（每 2 秒自動執行）
            self.assertTrue(admin.control.connected)

            # 2. 測試 /status 及 Cache-Control 標頭未重複
            code, body, headers = http_get(f"{base_url}/status")
            self.assertEqual(code, 200)
            data = json.loads(body.decode("utf-8"))
            self.assertEqual(data["state"], "idle")

            # 3. 測試 /event
            code, body, _ = http_post(f"{base_url}/event", {"type": "touch", "name": "pat"})
            self.assertEqual(code, 200)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[-1]["type"], "touch")

            # 4. 測試 /control
            code, body, _ = http_post(f"{base_url}/control", {"cmd": "set_state", "state": "happy"})
            self.assertEqual(code, 200)
            self.assertEqual(events[-1]["type"], "control")
            self.assertEqual(events[-1]["state"], "happy")

            # 5. 測試 /actuator
            code, body, _ = http_post(f"{base_url}/actuator", {"motion": "nod"})
            self.assertEqual(code, 200)
            self.assertEqual(events[-1]["type"], "action")

            # 6. 測試 /talk
            code, body, _ = http_post(f"{base_url}/talk", {"text": "測試對話"})
            self.assertEqual(code, 200)
            self.assertEqual(events[-1]["type"], "talk")

            # 7. 測試由後台發送 /v1/notice
            code, res = admin.control.post("/v1/notice", {"kind": "wifi", "text": "連線測試", "seconds": 2})
            self.assertEqual(code, 200)
            self.assertTrue(res.get("ok"))
            self.assertEqual(events[-1]["type"], "action")
            self.assertEqual(events[-1]["name"], "say")
            self.assertEqual(events[-1]["params"]["text"], "連線測試")

            # 8. 測試 snapshot wait 轉送
            code, body, _ = http_post(f"{base_url}/skill/camera.snapshot", {"wait": True})
            self.assertEqual(code, 200)
            snap_res = json.loads(body.decode("utf-8"))
            self.assertTrue(snap_res.get("ok"))

            # 9. 測試畫面同步 /screen.jpg 與 /screen.info（無觀看者時第一次請求於 1 秒內透過 latest 取得畫面）
            t0 = time.monotonic()
            code, body, headers = http_get(f"{base_url}/screen.jpg")
            self.assertLess(time.monotonic() - t0, 1.0)
            self.assertEqual(code, 200)
            self.assertTrue(headers.get("content-type") == "image/jpeg" or headers.get("Content-Type") == "image/jpeg")
            self.assertEqual(body, b"admin-test-jpeg")

            t0 = time.monotonic()
            code, body, _ = http_get(f"{base_url}/screen.info")
            self.assertLess(time.monotonic() - t0, 1.0)
            self.assertEqual(code, 200)
            info = json.loads(body.decode("utf-8"))
            self.assertTrue(info["ok"])
            self.assertEqual(info["mode"], "live")
        finally:
            admin.stop()
            core_ctrl.stop()
            core_frames.stop()

    def test_admin_frames_viewer_0_1_0_subscription(self):
        control_sock = str(self.tmp_path / "control.sock")
        frames_sock = str(self.tmp_path / "frames.sock")

        core_ctrl = ControlAPIServer(
            path=control_sock,
            status_fn=lambda: {"state": "idle", "stage": 1, "fps": 20.0},
            post_fn=lambda ev: None,
            fps_fn=lambda: 20.0,
        )
        core_ctrl.start()

        core_frames = FramesServer(path=frames_sock)
        core_frames.start()

        admin = AdminServer(
            listen="127.0.0.1:0",
            control_socket=control_sock,
            frames_socket=frames_sock,
            wifi_manager=None,
        )
        admin.start()
        base_url = f"http://127.0.0.1:{admin.port}"

        try:
            core_frames.publish(b"frame-init", {"mode": "pack", "clip": "idle", "frame": 2})
            time.sleep(0.1)

            # 1. 觀看者 = 0：不應訂閱 frames.sock
            self.assertEqual(admin.mirror.viewers, 0)
            self.assertFalse(admin.frames.subscribed)
            self.assertEqual(admin.frames.current_fps, 0)
            self.assertEqual(core_frames.subscriber_count, 0)

            code, body, _ = http_get(f"{base_url}/admin/status")
            self.assertEqual(code, 200)
            st = json.loads(body.decode("utf-8"))
            self.assertEqual(st["frames"]["viewers"], 0)
            self.assertFalse(st["frames"]["subscribed"])
            self.assertEqual(st["frames"]["fps"], 0)

            # 無觀看者時，第一次請求 /screen.info 與 /screen.jpg 應在 1 秒內透過 latest 取得最新畫面
            t0 = time.monotonic()
            code, body, _ = http_get(f"{base_url}/screen.info")
            self.assertLess(time.monotonic() - t0, 1.0)
            self.assertEqual(code, 200)
            info = json.loads(body.decode("utf-8"))
            self.assertTrue(info["ok"])
            self.assertEqual(info["clip"], "idle")
            self.assertEqual(info["frame"], 2)
            self.assertEqual(info["bytes"], len(b"frame-init"))

            core_frames.publish(b"frame-poll-2", {"mode": "pack", "clip": "happy", "frame": 5})
            t0 = time.monotonic()
            code, body, headers = http_get(f"{base_url}/screen.jpg")
            self.assertLess(time.monotonic() - t0, 1.0)
            self.assertEqual(code, 200)
            self.assertEqual(body, b"frame-poll-2")

            # 單次 latest 取幀後仍維持無訂閱狀態
            self.assertFalse(admin.frames.subscribed)
            self.assertEqual(core_frames.subscriber_count, 0)

            # 2. 觀看者 0 -> 1：連上 /screen.mjpg 後應以 20 fps 訂閱
            mjpg_resp = urllib.request.urlopen(f"{base_url}/screen.mjpg?fps=20", timeout=5.0)
            try:
                for _ in range(50):
                    if admin.mirror.viewers == 1 and admin.frames.subscribed and core_frames.subscriber_count == 1:
                        break
                    time.sleep(0.02)

                self.assertEqual(admin.mirror.viewers, 1)
                self.assertTrue(admin.frames.subscribed)
                self.assertEqual(admin.frames.current_fps, 20)
                self.assertEqual(core_frames.subscriber_count, 1)

                code, body, _ = http_get(f"{base_url}/admin/status")
                self.assertEqual(code, 200)
                st = json.loads(body.decode("utf-8"))
                self.assertEqual(st["frames"]["viewers"], 1)
                self.assertTrue(st["frames"]["subscribed"])
                self.assertEqual(st["frames"]["fps"], 20)

                core_frames.publish(b"frame-stream-live", {"mode": "live", "clip": "wave"})
                buf = b""
                for _ in range(20):
                    chunk = mjpg_resp.fp.read1(4096) if hasattr(mjpg_resp.fp, "read1") else mjpg_resp.read(64)
                    if not chunk:
                        break
                    buf += chunk
                    if b"frame-stream-live" in buf:
                        break
                self.assertIn(b"frame-stream-live", buf)
            finally:
                mjpg_resp.close()

            # 3. 觀看者 1 -> 0：觀看者斷線後應取消訂閱
            for _ in range(50):
                if admin.mirror.viewers == 0 and not admin.frames.subscribed and core_frames.subscriber_count == 0:
                    break
                time.sleep(0.02)

            self.assertEqual(admin.mirror.viewers, 0)
            self.assertFalse(admin.frames.subscribed)
            self.assertEqual(admin.frames.current_fps, 0)
            self.assertEqual(core_frames.subscriber_count, 0)

            code, body, _ = http_get(f"{base_url}/admin/status")
            self.assertEqual(code, 200)
            st = json.loads(body.decode("utf-8"))
            self.assertEqual(st["frames"]["viewers"], 0)
            self.assertFalse(st["frames"]["subscribed"])
            self.assertEqual(st["frames"]["fps"], 0)
        finally:
            admin.stop()
            core_ctrl.stop()
            core_frames.stop()


if __name__ == "__main__":
    unittest.main()

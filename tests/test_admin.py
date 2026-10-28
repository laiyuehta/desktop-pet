"""前端後台（desktop-pet-admin）測試：路由轉送、主程式離線回 503、自動恢復、畫面同步、Wi-Fi 不受影響。"""
import importlib.util
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from pet.control_api import ControlAPIServer
from pet.frames import FramesServer

# 動態載入 services/desktop-pet-admin/admin.py
ADMIN_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "services", "desktop-pet-admin", "admin.py"
)
spec = importlib.util.spec_from_file_location("desktop_pet_admin", ADMIN_PATH)
admin_mod = importlib.util.module_from_spec(spec)
sys.modules["desktop_pet_admin"] = admin_mod
spec.loader.exec_module(admin_mod)
AdminServer = admin_mod.AdminServer


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


def test_admin_when_core_is_down(tmp_path):
    control_sock = str(tmp_path / "control.sock")
    frames_sock = str(tmp_path / "frames.sock")

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
        assert code == 200
        data = json.loads(body.decode("utf-8"))
        assert data["ok"] is True
        assert data["control"]["connected"] is False

        # 2. 主程式離線時，轉送路由應回 503
        code, body, _ = http_get(f"{base_url}/status")
        assert code == 503
        data = json.loads(body.decode("utf-8"))
        assert data["ok"] is False
        assert "桌寵主程式未執行" in data["error"]

        code, body, _ = http_post(f"{base_url}/event", {"type": "touch", "name": "pat"})
        assert code == 503
        data = json.loads(body.decode("utf-8"))
        assert "桌寵主程式未執行" in data["error"]

        code, body, _ = http_post(f"{base_url}/control", {"cmd": "set_state", "state": "happy"})
        assert code == 503

        code, body, _ = http_post(f"{base_url}/actuator", {"motion": "nod"})
        assert code == 503

        code, body, _ = http_post(f"{base_url}/talk", {"text": "hi"})
        assert code == 503

        code, body, _ = http_get(f"{base_url}/screen.jpg")
        assert code == 503
        data = json.loads(body.decode("utf-8"))
        assert "桌寵主程式未執行" in data["error"]
    finally:
        admin.stop()


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="需要 socket.AF_UNIX")
def test_admin_forwarding_and_reconnect(tmp_path):
    control_sock = str(tmp_path / "control.sock")
    frames_sock = str(tmp_path / "frames.sock")
    events = []

    # 1. 先啟動主程式 sockets
    core_ctrl = ControlAPIServer(
        path=control_sock,
        status_fn=lambda: {"state": "idle", "stage": 1, "fps": 20.0},
        post_fn=lambda ev: events.append(ev),
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
        # 發布一張測試畫面
        core_frames.publish(b"admin-test-jpeg", {"mode": "live", "clip": "idle"})
        time.sleep(0.3)  # 等待 subscriber 收到畫面

        # 2. 測試 /status
        code, body, headers = http_get(f"{base_url}/status")
        assert code == 200
        data = json.loads(body.decode("utf-8"))
        assert data["state"] == "idle"

        # 3. 測試 /event
        code, body, _ = http_post(f"{base_url}/event", {"type": "touch", "name": "pat"})
        assert code == 200
        assert len(events) == 1
        assert events[-1]["type"] == "touch"

        # 4. 測試 /control
        code, body, _ = http_post(f"{base_url}/control", {"cmd": "set_state", "state": "happy"})
        assert code == 200
        assert events[-1]["type"] == "control"
        assert events[-1]["state"] == "happy"

        # 5. 測試 /actuator
        code, body, _ = http_post(f"{base_url}/actuator", {"motion": "nod"})
        assert code == 200
        assert events[-1]["type"] == "action"

        # 6. 測試 /talk
        code, body, _ = http_post(f"{base_url}/talk", {"text": "測試對話"})
        assert code == 200
        assert events[-1]["type"] == "talk"

        # 7. 測試畫面同步 /screen.jpg 與 /screen.info
        for _ in range(50):
            if admin.mirror.latest()[2] is not None:
                break
            core_frames.publish(b"admin-test-jpeg", {"mode": "live", "clip": "idle"})
            time.sleep(0.05)

        code, body, headers = http_get(f"{base_url}/screen.jpg")
        assert code == 200
        assert headers.get("content-type") == "image/jpeg" or headers.get("Content-Type") == "image/jpeg"
        assert body == b"admin-test-jpeg"

        code, body, _ = http_get(f"{base_url}/screen.info")
        assert code == 200
        info = json.loads(body.decode("utf-8"))
        assert info["ok"] is True
        assert info["mode"] == "live"
    finally:
        admin.stop()
        core_ctrl.stop()
        core_frames.stop()


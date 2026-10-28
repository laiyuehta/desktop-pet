"""控制 API 測試（/v1/* 路由、回應標頭、notice、假相機等）。"""
import http.client
import json
import socket
import tempfile
import time
from pathlib import Path

import pytest

from pet.control_api import ControlAPIServer


class FakeCamera:
    def __init__(self):
        self.last_thumb = b"fake-jpeg"
        self._seq = 0

    def status(self):
        return {"ok": True, "enabled": True}

    def trigger(self, params, source="test"):
        self._seq += 1
        return {"ok": True, "request_id": self._seq, "photo_id": "p1"}

    def wait(self, req_id, timeout):
        return {"ok": True, "photo_id": "p1"}


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float = 5.0):
        super().__init__("localhost", timeout=timeout)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="需要 socket.AF_UNIX")
def test_control_api_routes(tmp_path):
    sock_path = str(tmp_path / "control.sock")
    events = []
    fake_cam = FakeCamera()
    srv = ControlAPIServer(
        path=sock_path,
        status_fn=lambda: {"state": "idle", "stage": 1},
        post_fn=lambda ev: events.append(ev),
        fps_fn=lambda: 20.0,
        camera=fake_cam,
        version="0.1.0",
    )
    srv.start()
    try:
        conn = UnixHTTPConnection(sock_path)

        # 1. GET /v1/health
        conn.request("GET", "/v1/health")
        resp = conn.getresponse()
        assert resp.status == 200
        assert resp.getheader("X-Proto") == "desktop-pet-core/1"
        data = json.loads(resp.read().decode("utf-8"))
        assert data["ok"] is True
        assert data["proto"] == 1
        assert data["fps"] == 20.0

        # 2. GET /v1/status
        conn.request("GET", "/v1/status")
        resp = conn.getresponse()
        assert resp.status == 200
        data = json.loads(resp.read().decode("utf-8"))
        assert data["state"] == "idle"

        # 3. POST /v1/event
        conn.request("POST", "/v1/event", body=json.dumps({"type": "touch", "name": "pat"}))
        resp = conn.getresponse()
        assert resp.status == 200
        assert len(events) == 1
        assert events[-1]["type"] == "touch"

        # 4. POST /v1/control
        conn.request("POST", "/v1/control", body=json.dumps({"cmd": "set_state", "state": "happy"}))
        resp = conn.getresponse()
        assert resp.status == 200
        assert events[-1]["type"] == "control"
        assert events[-1]["state"] == "happy"

        # 5. POST /v1/actuator
        conn.request("POST", "/v1/actuator", body=json.dumps({"motion": "nod"}))
        resp = conn.getresponse()
        assert resp.status == 200
        assert events[-1]["type"] == "action"

        # 6. POST /v1/talk
        conn.request("POST", "/v1/talk", body=json.dumps({"text": "哈囉"}))
        resp = conn.getresponse()
        assert resp.status == 200
        assert events[-1]["type"] == "talk"

        # 7. POST /v1/notice
        conn.request("POST", "/v1/notice", body=json.dumps({"kind": "wifi", "text": "熱點開啟", "until": time.time() + 5}))
        resp = conn.getresponse()
        assert resp.status == 200
        assert events[-1]["type"] == "action"
        assert events[-1]["name"] == "say"
        assert events[-1]["params"]["text"] == "熱點開啟"

        # 8. 相機路由
        conn.request("GET", "/v1/skill/camera.snapshot")
        resp = conn.getresponse()
        assert resp.status == 200

        conn.request("GET", "/v1/skill/camera.snapshot/last.jpg")
        resp = conn.getresponse()
        assert resp.status == 200
        assert resp.getheader("Content-Type") == "image/jpeg"
        assert resp.read() == b"fake-jpeg"

        conn.request("POST", "/v1/skill/camera.snapshot", body=json.dumps({"wait": True}))
        resp = conn.getresponse()
        assert resp.status == 200

        # 9. 404
        conn.request("GET", "/v1/nonexistent")
        resp = conn.getresponse()
        assert resp.status == 404

        conn.close()
    finally:
        srv.stop()


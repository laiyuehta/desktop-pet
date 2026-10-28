"""控制 API（/run/desktop-pet/control.sock，HTTP/1.1＋JSON）。

規格見 PROPOSAL.md「主程式 ↔ 前端後台的接口」。
所有路徑都有版本前綴 /v1，回應標頭包含 X-Proto: desktop-pet-core/1。
除錯可直接用 curl --unix-socket /run/desktop-pet/control.sock http://x/v1/status
"""

from __future__ import annotations

import json
import logging
import os
import socket
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs
from typing import Optional

from .events import make_event, normalize_event

log = logging.getLogger("pet.control")
DEFAULT_CONTROL_SOCKET = "/run/desktop-pet/control.sock"
MAX_BODY = 64 * 1024


class ControlAPIHandler(BaseHTTPRequestHandler):
    server_version = "DesktopPetCore/1.0"

    def address_string(self) -> str:
        # Unix domain socket 的 client_address 可能是空字串或 None
        if isinstance(self.client_address, (list, tuple)) and len(self.client_address) >= 2:
            return str(self.client_address[0])
        return "unix"

    def log_message(self, fmt, *args):
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8", extra: Optional[dict] = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("X-Proto", "desktop-pet-core/1")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj: dict, extra: Optional[dict] = None):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), extra=extra)

    def do_GET(self):
        path, _, qs = self.path.partition("?")
        query = {k: v[-1] for k, v in parse_qs(qs).items()} if qs else {}
        server = self.server

        try:
            if path == "/v1/health":
                self._json(200, {
                    "ok": True,
                    "version": getattr(server, "version", "0.1.0"),
                    "proto": 1,
                    "uptime": round(time.time() - getattr(server, "started", time.time()), 1),
                    "fps": round(server.get_fps(), 1),
                })
                return

            if path == "/v1/status":
                self._json(200, server.get_status())
                return

            if path == "/v1/skill/camera.snapshot":
                camera = server.camera
                if not camera:
                    self._json(503, {"ok": False, "error": "相機功能未啟用"})
                    return
                self._json(200, camera.status())
                return

            if path == "/v1/skill/camera.snapshot/last.jpg":
                camera = server.camera
                if not camera or camera.last_thumb is None:
                    self._json(404, {"ok": False, "error": "還沒有照片"})
                    return
                self._send(200, camera.last_thumb, "image/jpeg")
                return

            self._json(404, {"ok": False, "error": "not found"})
        except Exception as exc:
            log.exception("GET %s 處理失敗", path)
            self._json(500, {"ok": False, "error": f"內部錯誤：{type(exc).__name__}"})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        server = self.server

        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                self._json(400, {"ok": False, "error": "內容太大"})
                return
            data = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(data, dict):
                self._json(400, {"ok": False, "error": "需要 JSON 物件"})
                return
        except (ValueError, KeyError, TypeError) as exc:
            self._json(400, {"ok": False, "error": str(exc)})
            return

        try:
            if path == "/v1/event":
                ev = normalize_event(data, default_source="control")
                server.post_event(ev)
                self._json(200, {"ok": True, "event": ev})
                return

            if path == "/v1/control":
                cmd = data.get("cmd")
                if cmd == "set_state":
                    ev = make_event("control", "set_state", source="control", state=str(data["state"]))
                elif cmd == "set_stage":
                    ev = make_event("control", "set_stage", source="control", stage=int(data["stage"]))
                else:
                    self._json(400, {"ok": False, "error": f"未知的 cmd：{cmd}"})
                    return
                server.post_event(ev)
                self._json(200, {"ok": True, "event": ev})
                return

            if path == "/v1/actuator":
                if not data.get("motion"):
                    self._json(400, {"ok": False, "error": "缺少 motion"})
                    return
                ev = make_event("action", "motion", source="control", params=data)
                server.post_event(ev)
                self._json(200, {"ok": True, "event": ev})
                return

            if path == "/v1/talk":
                text = data.get("text")
                if not isinstance(text, str) or not text.strip():
                    self._json(400, {"ok": False, "error": "text 不能是空的"})
                    return
                ev = make_event("talk", "talk", source="control", text=text.strip()[:200])
                server.post_event(ev)
                self._json(200, {"ok": True, "event": ev})
                return

            if path == "/v1/notice":
                text = str(data.get("text", "")).strip()
                if not text:
                    self._json(400, {"ok": False, "error": "text 不能是空的"})
                    return
                until = data.get("until")
                if until is not None:
                    seconds = max(1.0, float(until) - time.time())
                else:
                    seconds = float(data.get("seconds", 4.0))
                kind = str(data.get("kind", "notice"))
                ev = make_event("action", "say", source=kind, params={"text": text, "seconds": seconds})
                server.post_event(ev)
                self._json(200, {"ok": True})
                return

            if path == "/v1/skill/camera.snapshot":
                camera = server.camera
                if not camera:
                    self._json(503, {"ok": False, "error": "相機功能未啟用"})
                    return
                wait = bool(data.pop("wait", False))
                r = camera.trigger(data, source=str(data.pop("source", "control"))[:24] or "control")
                if not r.get("ok"):
                    self._json(503 if r.get("kind") == "service_down" else 400, r)
                    return
                if wait:
                    res = camera.wait(r["request_id"], 30.0)
                    if res is None:
                        self._json(504, {"ok": False, "error": "等太久", "kind": "timeout", "request_id": r["request_id"]})
                        return
                    self._json(200 if res.get("ok") else 502, res)
                    return
                self._json(202, r)
                return

            self._json(404, {"ok": False, "error": "not found"})
        except (ValueError, KeyError, TypeError) as exc:
            self._json(400, {"ok": False, "error": str(exc)})
        except Exception as exc:
            log.exception("POST %s 處理失敗", path)
            self._json(500, {"ok": False, "error": f"內部錯誤：{type(exc).__name__}"})


class ControlAPIServer:
    """Unix socket 控制伺服器。"""

    def __init__(self, path: str = DEFAULT_CONTROL_SOCKET, app=None, camera=None,
                 status_fn=None, post_fn=None, fps_fn=None, version: str = "0.1.0"):
        self.path = path
        self.app = app
        self.camera = camera
        self.version = version
        self.status_fn = status_fn or (app.status if app else lambda: {})
        self.post_fn = post_fn or (app.post if app else lambda ev: None)
        self.fps_fn = fps_fn or (lambda: getattr(app, "fps", 0.0) if app else 0.0)
        self.started = time.time()
        self.httpd = None
        self.thread: Optional[threading.Thread] = None
        self.running = False

    def get_status(self) -> dict:
        return self.status_fn()

    def post_event(self, ev: dict) -> None:
        self.post_fn(ev)

    def get_fps(self) -> float:
        return float(self.fps_fn())

    def start(self) -> None:
        if not hasattr(socket, "AF_UNIX"):
            log.warning("此平台不支援 AF_UNIX，無法啟動 control.sock")
            return

        if os.path.exists(self.path):
            try:
                os.unlink(self.path)
            except OSError as exc:
                log.warning("無法刪除舊的 socket %s：%s", self.path, exc)

        parent = os.path.dirname(os.path.abspath(self.path))
        if parent:
            try:
                os.makedirs(parent, exist_ok=True)
            except OSError as exc:
                log.warning("無法建立 socket 資料夾 %s：%s", parent, exc)

        class UnixServer(socketserver.ThreadingUnixStreamServer):
            daemon_threads = True
            allow_reuse_address = True

        try:
            server = UnixServer(self.path, ControlAPIHandler)
            server.version = self.version
            server.started = self.started
            server.camera = self.camera
            server.get_status = self.get_status
            server.post_event = self.post_event
            server.get_fps = self.get_fps

            try:
                os.chmod(self.path, 0o660)
            except OSError:
                pass

            self.httpd = server
            self.running = True
            self.thread = threading.Thread(target=self.httpd.serve_forever, name="control-api", daemon=True)
            self.thread.start()
            log.info("控制 API 服務已啟動：%s", self.path)
        except OSError as exc:
            log.warning("無法啟動 control.sock %s：%s（略過啟動）", self.path, exc)
            self.running = False
            self.httpd = None

    def stop(self) -> None:
        self.running = False
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
        if os.path.exists(self.path):
            try:
                os.unlink(self.path)
            except OSError:
                pass
        log.info("控制 API 服務已停止：%s", self.path)


#!/usr/bin/env python3
"""桌寵前端後台（desktop-pet-admin）。

規格見 PROPOSAL.md「整體架構」1-2 前端後台與遷移步驟第 1 步：
- 獨立程式，管理網頁（:8080）、遙控、螢幕畫面同步、Wi‑Fi 設定與救援熱點。
- 透過 Unix socket 與桌寵主程式溝通：
  - 控制 API：/run/desktop-pet/control.sock（HTTP JSON）
  - 畫面供應：/run/desktop-pet/frames.sock（串流）
- 主程式重啟或未執行時，網頁遙控回 503（提示重啟中），Wi‑Fi 與救援熱點照常可用。
- 每 2 秒自動重試連線主程式 socket。
"""

from __future__ import annotations

import argparse
import http.client
import json
import logging
import os
import signal
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs
from typing import Callable, Optional, Tuple

_DIR = os.path.dirname(os.path.abspath(__file__))
if _DIR not in sys.path:
    sys.path.insert(0, _DIR)

from screen import ScreenMirror, ScreenWebAPI
from wifi import DEFAULT_HOTSPOT_CONF, HOTSPOT_SSID, WifiManager, WifiWebAPI

log = logging.getLogger("admin")

DEFAULT_LISTEN = "0.0.0.0:8080"
DEFAULT_CONTROL_SOCKET = "/run/desktop-pet/control.sock"
DEFAULT_FRAMES_SOCKET = "/run/desktop-pet/frames.sock"
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_HTML_PATH = os.path.join(ROOT, "pet", "web", "remote.html")
MAX_BODY = 64 * 1024


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float = 2.0):
        super().__init__("localhost", timeout=timeout)
        self.path = path

    def connect(self):
        if not hasattr(socket, "AF_UNIX"):
            raise OSError("此平台不支援 AF_UNIX")
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


class ControlClient:
    def __init__(self, socket_path: str = DEFAULT_CONTROL_SOCKET, timeout: float = 2.0):
        self.socket_path = socket_path
        self.timeout = timeout
        self.last_ok: Optional[float] = None
        self.last_error: Optional[str] = None
        self.connected = False

    def forward(self, method: str, path: str, body: Optional[bytes] = None,
                headers: Optional[dict] = None) -> Tuple[int, bytes, str, dict]:
        """轉送請求給 control.sock，主程式不在時回傳 503。"""
        if not hasattr(socket, "AF_UNIX") or not os.path.exists(self.socket_path):
            self.connected = False
            self.last_error = "socket 不存在"
            return 503, json.dumps({"ok": False, "error": "桌寵主程式未執行"}).encode("utf-8"), "application/json; charset=utf-8", {}

        conn = UnixHTTPConnection(self.socket_path, timeout=self.timeout)
        try:
            req_headers = {"User-Agent": "desktop-pet-admin/1.0"}
            if headers:
                for k in ("Content-Type", "Content-Length", "X-Proto"):
                    if k in headers:
                        req_headers[k] = headers[k]
            conn.request(method, path, body=body, headers=req_headers)
            resp = conn.getresponse()
            data = resp.read()
            ctype = resp.getheader("Content-Type", "application/json; charset=utf-8")
            extra = {}
            for h in ("X-Proto", "Cache-Control", "X-Frame-Seq", "X-Frame-Ts", "X-Frame-Mode"):
                v = resp.getheader(h)
                if v is not None:
                    extra[h] = v
            self.connected = True
            self.last_ok = time.time()
            self.last_error = None
            return resp.status, data, ctype, extra
        except (OSError, http.client.HTTPException) as exc:
            self.connected = False
            self.last_error = str(exc)
            log.debug("連線 control.sock 失敗：%s", exc)
            return 503, json.dumps({"ok": False, "error": "桌寵主程式未執行"}).encode("utf-8"), "application/json; charset=utf-8", {}
        finally:
            conn.close()

    def post(self, path: str, obj: dict) -> Tuple[int, dict]:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        code, resp_bytes, _, _ = self.forward("POST", path, body=body, headers={"Content-Type": "application/json"})
        try:
            return code, json.loads(resp_bytes.decode("utf-8") or "{}")
        except Exception:
            return code, {"ok": False, "error": "回應非 JSON"}


class FramesSubscriber:
    """背景連接 frames.sock 並餵畫面給 ScreenMirror。"""

    def __init__(self, socket_path: str, mirror: ScreenMirror):
        self.socket_path = socket_path
        self.mirror = mirror
        self.connected = False
        self.last_ok: Optional[float] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="frames-subscriber", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            if not hasattr(socket, "AF_UNIX") or not os.path.exists(self.socket_path):
                self.connected = False
                self._stop.wait(2.0)
                continue

            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.settimeout(3.0)
                sock.connect(self.socket_path)
                sock.sendall(json.dumps({"v": 1, "cmd": "subscribe", "fps": 10}).encode("utf-8") + b"\n")
                first = sock.recv(1024)
                first_cmd = json.loads(first.decode("utf-8").strip() or "{}")
                if not first_cmd.get("ok"):
                    log.warning("訂閱畫面失敗：%s", first_cmd.get("error"))
                    sock.close()
                    self._stop.wait(2.0)
                    continue

                self.connected = True
                sock.settimeout(5.0)
                buf = b""
                while not self._stop.is_set():
                    while b"\n" not in buf:
                        chunk = sock.recv(4096)
                        if not chunk:
                            raise OSError("連線關閉")
                        buf += chunk
                    line, buf = buf.split(b"\n", 1)
                    header = json.loads(line.decode("utf-8"))

                    while len(buf) < 4:
                        chunk = sock.recv(4096)
                        if not chunk:
                            raise OSError("連線關閉")
                        buf += chunk
                    n = int.from_bytes(buf[:4], "big")
                    buf = buf[4:]

                    while len(buf) < n:
                        chunk = sock.recv(min(65536, n - len(buf) + 4096))
                        if not chunk:
                            raise OSError("連線關閉")
                        buf += chunk
                    jpeg_bytes = buf[:n]
                    buf = buf[n:]

                    self.mirror.publish(jpeg_bytes, header)
                    self.last_ok = time.time()
            except (OSError, ValueError) as exc:
                self.connected = False
                log.debug("frames.sock 連線中斷：%s（2 秒後重連）", exc)
            finally:
                try:
                    sock.close()
                except OSError:
                    pass
            self._stop.wait(2.0)


class AdminServer:
    def __init__(self, listen: str = DEFAULT_LISTEN,
                 control_socket: str = DEFAULT_CONTROL_SOCKET,
                 frames_socket: str = DEFAULT_FRAMES_SOCKET,
                 html_path: str = DEFAULT_HTML_PATH,
                 mirror_viewers: int = 3,
                 wifi_manager: Optional[WifiManager] = None):
        self.listen = listen
        self.control_socket = control_socket
        self.frames_socket = frames_socket
        self.html_path = html_path
        self.control = ControlClient(control_socket)
        self.mirror = ScreenMirror(max_viewers=mirror_viewers)
        self.frames = FramesSubscriber(frames_socket, self.mirror)
        self.wifi = wifi_manager
        self.wifi_api = WifiWebAPI(wifi_manager) if wifi_manager else None
        self.screen_api = ScreenWebAPI(
            self.mirror,
            status_fn=lambda: self.control.forward("GET", "/v1/status")[1],
            is_core_online_fn=lambda: self.control.connected or self.frames.connected,
        )
        self.started_ts = time.time()
        self.httpd: Optional[ThreadingHTTPServer] = None
        self.server_thread: Optional[threading.Thread] = None
        self.port = 0

    def start(self) -> None:
        server_obj = self
        control = self.control
        mirror = self.mirror
        frames = self.frames
        wifi = self.wifi
        wifi_api = self.wifi_api
        screen_api = self.screen_api
        html_path = self.html_path
        started_ts = self.started_ts

        def get_status_with_wifi() -> Tuple[int, bytes, str, dict]:
            code, body, ctype, extra = control.forward("GET", "/v1/status")
            try:
                st = json.loads(body.decode("utf-8") or "{}")
            except Exception:
                st = {"ok": False, "error": "無法解析狀態回應"}

            if wifi:
                st["wifi"] = wifi.brief()
            merged = json.dumps(st, ensure_ascii=False).encode("utf-8")
            return code, merged, "application/json; charset=utf-8", extra

        class AdminHandler(BaseHTTPRequestHandler):
            server_version = "DesktopPetAdmin/0.1"

            def log_message(self, fmt, *args):
                log.debug("%s - %s", self.address_string(), fmt % args)

            def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8", extra=None):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _json(self, code: int, obj) -> None:
                self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

            def do_GET(self):
                path, _, qs = self.path.partition("?")
                query = {k: v[-1] for k, v in parse_qs(qs).items()} if qs else {}

                # 1. 遙控網頁 HTML
                if path in ("/", "/index.html"):
                    try:
                        with open(html_path, "rb") as fh:
                            self._send(200, fh.read(), "text/html; charset=utf-8")
                    except OSError:
                        self._json(500, {"ok": False, "error": f"找不到 HTML 檔案：{html_path}"})
                    return

                # 2. 後台自身的狀態資訊
                if path == "/admin/status":
                    self._json(200, {
                        "ok": True,
                        "name": "desktop-pet-admin",
                        "version": "0.1.0",
                        "uptime": round(time.time() - started_ts, 1),
                        "control": {
                            "connected": control.connected,
                            "socket": control.socket_path,
                            "last_ok": control.last_ok,
                            "last_error": control.last_error,
                        },
                        "frames": {
                            "connected": frames.connected,
                            "socket": frames.socket_path,
                            "last_ok": frames.last_ok,
                            "viewers": mirror.viewers,
                            "max_viewers": mirror.max_viewers,
                        },
                        "wifi": wifi.brief() if wifi else None,
                    })
                    return

                # 3. Wi-Fi API 路由
                if wifi_api and path.startswith("/wifi/"):
                    res = wifi_api.handle("GET", path, query, self.headers)
                    if res is not None:
                        self._json(res[0], res[1])
                        return

                # 4. 螢幕畫面 API 路由
                if path.startswith("/screen."):
                    res = screen_api.handle("GET", path, query, self.headers)
                    if res is not None:
                        if res[0] == "stream":
                            res[1](self)
                        elif len(res) == 4:
                            self._send(res[0], res[1], res[2], res[3])
                        elif len(res) == 3:
                            self._send(res[0], res[1], res[2])
                        else:
                            self._json(res[0], res[1])
                        return

                # 5. /status 路由（轉送並合併 Wi-Fi 狀態）
                if path == "/status":
                    code, body, ctype, extra = get_status_with_wifi()
                    self._send(code, body, ctype, extra)
                    return

                # 6. 相機縮圖路由轉送
                if path in ("/skill/camera.snapshot", "/v1/skill/camera.snapshot"):
                    code, body, ctype, extra = control.forward("GET", "/v1/skill/camera.snapshot")
                    self._send(code, body, ctype, extra)
                    return
                if path in ("/skill/camera.snapshot/last.jpg", "/v1/skill/camera.snapshot/last.jpg"):
                    code, body, ctype, extra = control.forward("GET", "/v1/skill/camera.snapshot/last.jpg")
                    self._send(code, body, ctype, extra)
                    return

                self._json(404, {"ok": False, "error": "not found"})

            def do_POST(self):
                path = self.path.split("?", 1)[0]
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    if n > MAX_BODY:
                        raise ValueError("內容太大")
                    raw_body = self.rfile.read(n) if n else b"{}"
                    data = json.loads(raw_body.decode("utf-8") or "{}")
                except (ValueError, KeyError, TypeError) as exc:
                    self._json(400, {"ok": False, "error": str(exc)})
                    return

                # 1. Wi-Fi API 路由
                if wifi_api and path.startswith("/wifi/"):
                    res = wifi_api.handle("POST", path, data, self.headers)
                    if res is not None:
                        self._json(res[0], res[1])
                        return

                # 2. 轉送至主程式 control.sock 的路由
                route_map = {
                    "/event": "/v1/event",
                    "/v1/event": "/v1/event",
                    "/control": "/v1/control",
                    "/v1/control": "/v1/control",
                    "/actuator": "/v1/actuator",
                    "/v1/actuator": "/v1/actuator",
                    "/talk": "/v1/talk",
                    "/v1/talk": "/v1/talk",
                    "/skill/camera.snapshot": "/v1/skill/camera.snapshot",
                    "/v1/skill/camera.snapshot": "/v1/skill/camera.snapshot",
                }
                target_path = route_map.get(path)
                if target_path:
                    code, body, ctype, extra = control.forward("POST", target_path, body=raw_body,
                                                               headers={"Content-Type": "application/json"})
                    self._send(code, body, ctype, extra)
                    return

                self._json(404, {"ok": False, "error": "not found"})

        host, _, port_s = self.listen.rpartition(":")
        port = int(port_s)
        self.httpd = ThreadingHTTPServer((host or "0.0.0.0", port), AdminHandler)
        self.port = self.httpd.server_address[1]
        log.info("前端後台網頁介面已開在：http://%s:%d/", host or "0.0.0.0", self.port)

        self.frames.start()
        if self.wifi:
            self.wifi.start()

        self.server_thread = threading.Thread(target=self.httpd.serve_forever, name="admin-web", daemon=True)
        self.server_thread.start()

    def stop(self) -> None:
        log.info("正在停止前端後台...")
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
        self.frames.stop()
        if self.wifi:
            self.wifi.stop()
        log.info("前端後台已結束")


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="桌寵前端後台 desktop-pet-admin")
    p.add_argument("--listen", "--web", default=DEFAULT_LISTEN,
                   help="監聽位址（預設 0.0.0.0:8080；測試可用 127.0.0.1:8081）")
    p.add_argument("--control-socket", default=DEFAULT_CONTROL_SOCKET,
                   help="主程式控制 API Unix socket 路徑")
    p.add_argument("--frames-socket", default=DEFAULT_FRAMES_SOCKET,
                   help="主程式畫面供應 Unix socket 路徑")
    p.add_argument("--html", default=DEFAULT_HTML_PATH,
                   help="遙控網頁 HTML 路徑（預設 remote.html）")
    p.add_argument("--mirror-viewers", type=int, default=3,
                   help="螢幕畫面同時觀看人數上限（預設 3）")
    p.add_argument("--wifi", choices=["auto", "on", "off"], default="auto",
                   help="Wi‑Fi 設定與救援熱點（預設 auto）")
    p.add_argument("--wifi-iface", default="wlan0", help="管理的 Wi‑Fi 介面")
    p.add_argument("--wifi-hotspot", choices=["on", "off"], default="on", help="開救援熱點")
    p.add_argument("--wifi-hotspot-ssid", default=HOTSPOT_SSID, help="熱點名稱")
    p.add_argument("--wifi-hotspot-conf", default=DEFAULT_HOTSPOT_CONF, help="熱點密碼設定檔")
    p.add_argument("--wifi-hotspot-after", type=float, default=120, help="幾秒連不上開熱點")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def run_admin(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log.info("啟動前端後台 desktop-pet-admin")

    control = ControlClient(args.control_socket)
    wifi = None
    if args.wifi == "on" or (args.wifi == "auto" and WifiManager.available(args.wifi_iface)):
        def wifi_notice(text, seconds):
            log.info("Wi‑Fi 提示：%s（%g 秒）", text, seconds)
            control.post("/v1/notice", {"kind": "wifi", "text": text, "seconds": seconds})

        wifi = WifiManager(iface=args.wifi_iface, notify=wifi_notice,
                           hotspot=args.wifi_hotspot == "on",
                           hotspot_conf=args.wifi_hotspot_conf,
                           hotspot_ssid=args.wifi_hotspot_ssid,
                           hotspot_after=args.wifi_hotspot_after)
        log.info("Wi‑Fi 管理：已啟動（%s；熱點 %s）", args.wifi_iface,
                 args.wifi_hotspot_ssid if wifi.hotspot_enabled else "關")

    admin_server = AdminServer(
        listen=args.listen,
        control_socket=args.control_socket,
        frames_socket=args.frames_socket,
        html_path=args.html,
        mirror_viewers=args.mirror_viewers,
        wifi_manager=wifi,
    )
    admin_server.start()

    stop_flag = threading.Event()

    def on_signal(*_):
        stop_flag.set()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    try:
        while not stop_flag.is_set():
            stop_flag.wait(0.5)
    finally:
        admin_server.stop()

    return 0


if __name__ == "__main__":
    sys.exit(run_admin())

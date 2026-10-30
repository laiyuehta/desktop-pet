"""桌寵前端後台服務 desktop-pet-admin (v2.6 遷移第 1 步)。

獨立執行的網頁遙控與狀態管理服務：
- 提供網頁介面（預設 :8080）。
- 控制 API 轉送至主程式 Unix socket (/run/desktop-pet/control.sock)。
- 畫面訂閱轉送至主程式 Unix socket (/run/desktop-pet/frames.sock)。
- 整合 Wi‑Fi 管理（從 pet/wifi.py 移入），主程式不在時 Wi‑Fi 依然可用。
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
from typing import Optional, Tuple

__version__ = "0.1.0"

_DIR = os.path.dirname(os.path.abspath(__file__))
if _DIR not in sys.path:
    sys.path.insert(0, _DIR)

from screen import ScreenMirror, ScreenWebAPI
from wifi import DEFAULT_HOTSPOT_CONF, HOTSPOT_SSID, WifiManager, WifiWebAPI

log = logging.getLogger("admin")

DEFAULT_LISTEN = "0.0.0.0:8080"
DEFAULT_CONTROL_SOCKET = "/run/desktop-pet/control.sock"
DEFAULT_FRAMES_SOCKET = "/run/desktop-pet/frames.sock"
DEFAULT_PI_ENV_FILE = "/etc/desktop-pet/env"
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_HTML_PATH = os.path.join(ROOT, "pet", "web", "remote.html")
# repo 根目錄（frontend/ 的上一層）：deploy.env 只放這裡（與 pet/env.py、common.sh 一致）
REPO_ROOT = os.path.dirname(ROOT)
LEGACY_DEPLOY_ENV = os.path.join(REPO_ROOT, "deploy.env")
MAX_BODY = 64 * 1024


def _parse_env_file(path: str) -> dict:
    out = {}
    if not path or not os.path.isfile(path):
        return out
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return out
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if k and v:
            out[k] = v
    return out


def _load_admin_env(env_file: Optional[str] = None, environ: Optional[dict] = None) -> dict:
    proc = {k: v for k, v in (os.environ if environ is None else environ).items() if v != ""}
    chosen = env_file or proc.get("PET_ENV_FILE")
    if not chosen:
        if os.path.isfile(DEFAULT_PI_ENV_FILE):
            chosen = DEFAULT_PI_ENV_FILE
        else:
            legacy = LEGACY_DEPLOY_ENV
            if os.path.isfile(legacy):
                chosen = legacy
    merged = _parse_env_file(chosen) if chosen else {}
    merged.update(proc)
    return merged


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
        self._stop_monitor = threading.Event()
        self._monitor_thread: Optional[threading.Thread] = None

    def check_health(self) -> bool:
        """主動檢查 control.sock 健康狀態（每 2 秒一次）。"""
        if not hasattr(socket, "AF_UNIX") or not os.path.exists(self.socket_path):
            self.connected = False
            self.last_error = "socket 不存在"
            return False

        conn = UnixHTTPConnection(self.socket_path, timeout=1.5)
        try:
            conn.request("GET", "/v1/health", headers={"User-Agent": f"desktop-pet-admin/{__version__}"})
            resp = conn.getresponse()
            resp.read()
            if resp.status == 200:
                self.connected = True
                self.last_ok = time.time()
                self.last_error = None
                return True
            else:
                self.connected = False
                self.last_error = f"HTTP {resp.status}"
                return False
        except (socket.timeout, TimeoutError) as exc:
            self.connected = False
            self.last_error = f"健康檢查逾時：{exc}"
            return False
        except (OSError, http.client.HTTPException) as exc:
            self.connected = False
            self.last_error = str(exc)
            return False
        finally:
            conn.close()

    def start_monitor(self, interval: float = 2.0) -> None:
        if self._monitor_thread and self._monitor_thread.is_alive():
            return
        self._stop_monitor.clear()
        def loop():
            while not self._stop_monitor.is_set():
                self.check_health()
                self._stop_monitor.wait(interval)
        self._monitor_thread = threading.Thread(target=loop, name="control-monitor", daemon=True)
        self._monitor_thread.start()

    def stop_monitor(self) -> None:
        self._stop_monitor.set()
        if self._monitor_thread:
            self._monitor_thread = None

    def forward(self, method: str, path: str, body: Optional[bytes] = None,
                headers: Optional[dict] = None, timeout: Optional[float] = None) -> Tuple[int, bytes, str, dict]:
        """轉送請求給 control.sock，主程式不在時回傳 503，逾時回傳 504。"""
        if not hasattr(socket, "AF_UNIX") or not os.path.exists(self.socket_path):
            self.connected = False
            self.last_error = "socket 不存在"
            return 503, json.dumps({"ok": False, "error": "桌寵主程式未執行"}, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", {}

        # 拍照請求最多等待 30 秒，使用至少 35 秒逾時；其餘請求使用預設 timeout
        req_timeout = timeout if timeout is not None else (35.0 if "snapshot" in path else self.timeout)
        conn = UnixHTTPConnection(self.socket_path, timeout=req_timeout)
        try:
            req_headers = {"User-Agent": f"desktop-pet-admin/{__version__}"}
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
        except (socket.timeout, TimeoutError) as exc:
            self.last_error = f"逾時：{exc}"
            log.warning("轉送 control.sock 逾時：%s", exc)
            return 504, json.dumps({"ok": False, "error": "轉送請求逾時"}, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", {}
        except (OSError, http.client.HTTPException) as exc:
            self.connected = False
            self.last_error = str(exc)
            log.debug("連線 control.sock 失敗：%s", exc)
            return 503, json.dumps({"ok": False, "error": "桌寵主程式未執行"}, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", {}
        finally:
            conn.close()

    def post(self, path: str, obj: dict, timeout: Optional[float] = None) -> Tuple[int, dict]:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        code, resp_bytes, _, _ = self.forward("POST", path, body=body,
                                              headers={"Content-Type": "application/json"},
                                              timeout=timeout)
        try:
            return code, json.loads(resp_bytes.decode("utf-8") or "{}")
        except Exception:
            return code, {"ok": False, "error": "回應非 JSON"}


class FramesSubscriber:
    """背景連接 frames.sock 並餵畫面給 ScreenMirror。無觀看者時取消訂閱以節省 CPU。"""

    def __init__(self, socket_path: str, mirror: ScreenMirror, fps: int = 20):
        self.socket_path = socket_path
        self.mirror = mirror
        self.target_fps = max(1, min(20, int(fps)))
        self.connected = False
        self.subscribed = False
        self.current_fps = 0
        self.last_ok: Optional[float] = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._sock_lock = threading.Lock()
        self._active_sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self.mirror.on_viewers_change = self._on_viewers_change

    def _on_viewers_change(self, viewers: int) -> None:
        if viewers > 0:
            self._wake.set()
        else:
            self.subscribed = False
            self.connected = False
            self.current_fps = 0
            self._close_active_sock()
            self._wake.set()

    def _close_active_sock(self) -> None:
        with self._sock_lock:
            sock = self._active_sock
            self._active_sock = None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def start(self) -> None:
        self._stop.clear()
        if self.mirror.viewers > 0:
            self._wake.set()
        self._thread = threading.Thread(target=self._run, name="frames-subscriber", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.subscribed = False
        self.connected = False
        self.current_fps = 0
        self._close_active_sock()
        self._wake.set()

    def fetch_latest(self, timeout: float = 1.0) -> bool:
        """單次向 frames.sock 請求最新畫面（cmd=latest），供無訂閱時的 /screen.jpg 與 /screen.info 使用。"""
        if not hasattr(socket, "AF_UNIX") or not os.path.exists(self.socket_path):
            return False

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(timeout)
            sock.connect(self.socket_path)
            sock.sendall(json.dumps({"v": 1, "cmd": "latest"}).encode("utf-8") + b"\n")
            buf = b""
            while b"\n" not in buf:
                chunk = sock.recv(4096)
                if not chunk:
                    raise OSError("連線關閉")
                buf += chunk
            line, buf = buf.split(b"\n", 1)
            header = json.loads(line.decode("utf-8").strip() or "{}")
            if not header.get("ok"):
                return False

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
            self.mirror.publish(jpeg_bytes, meta=header)
            self.last_ok = time.time()
            return True
        except Exception as exc:
            log.debug("向 frames.sock 取得 latest 失敗：%s", exc)
            return False
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def ensure_fresh(self, max_age: float = 0.5) -> bool:
        """確保 mirror 中有近期畫面：若正在訂閱且已有畫面則直接沿用，否則向 frames.sock 請求 latest。"""
        _, ts, data, _ = self.mirror.latest()
        if self.subscribed and data is not None and (time.time() - ts) <= max_age:
            return True
        return self.fetch_latest()

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            if self.mirror.viewers <= 0:
                self.connected = False
                self.subscribed = False
                self.current_fps = 0
                self._wake.wait(1.0)
                continue

            if not hasattr(socket, "AF_UNIX") or not os.path.exists(self.socket_path):
                self.connected = False
                self.subscribed = False
                self.current_fps = 0
                self._stop.wait(0.5)
                continue

            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            with self._sock_lock:
                if self.mirror.viewers <= 0 or self._stop.is_set():
                    try:
                        sock.close()
                    except OSError:
                        pass
                    continue
                self._active_sock = sock

            try:
                sock.settimeout(3.0)
                sock.connect(self.socket_path)
                # 有觀看者時以 20 fps 訂閱主程式畫面，再由 ScreenMirror / ScreenWebAPI 依觀看者需求降頻
                sock.sendall(json.dumps({"v": 1, "cmd": "subscribe", "fps": self.target_fps}).encode("utf-8") + b"\n")
                buf = b""
                while b"\n" not in buf:
                    chunk = sock.recv(1024)
                    if not chunk:
                        raise OSError("連線關閉")
                    buf += chunk
                first_line, buf = buf.split(b"\n", 1)
                first_cmd = json.loads(first_line.decode("utf-8").strip() or "{}")
                if not first_cmd.get("ok"):
                    log.warning("訂閱畫面失敗：%s", first_cmd.get("error"))
                    self._close_active_sock()
                    self._stop.wait(2.0)
                    continue

                if self.mirror.viewers <= 0 or self._stop.is_set():
                    self._close_active_sock()
                    continue

                self.connected = True
                self.subscribed = True
                self.current_fps = self.target_fps
                sock.settimeout(5.0)
                while not self._stop.is_set() and self.mirror.viewers > 0:
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

                    self.mirror.publish(jpeg_bytes, meta=header)
                    self.last_ok = time.time()
            except Exception as exc:
                self.connected = False
                self.subscribed = False
                self.current_fps = 0
                if self.mirror.viewers > 0 and not self._stop.is_set():
                    log.debug("frames.sock 斷線或異常：%s", exc)
                    self._stop.wait(1.0)
            finally:
                self.connected = False
                self.subscribed = False
                self.current_fps = 0
                self._close_active_sock()
                try:
                    sock.close()
                except OSError:
                    pass


class AdminServer:
    def __init__(self,
                 listen: str = DEFAULT_LISTEN,
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
        def get_core_status():
            try:
                code, body, _, _ = self.control.forward("GET", "/v1/status")
                if code == 200:
                    return json.loads(body.decode("utf-8"))
            except Exception:
                pass
            return {}

        def is_core_online() -> bool:
            if not os.path.exists(self.control.socket_path):
                self.control.connected = False
                return False
            if self.control.connected:
                return True
            return self.control.check_health()

        self.screen_api = ScreenWebAPI(
            mirror=self.mirror,
            status_fn=get_core_status,
            is_core_online_fn=is_core_online,
            ensure_fresh_fn=self.frames.ensure_fresh,
        )
        self.httpd: Optional[ThreadingHTTPServer] = None
        self.server_thread: Optional[threading.Thread] = None
        self.port: int = 0
        self.started_ts = time.time()

    def start(self) -> None:
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
            server_version = f"DesktopPetAdmin/{__version__}"

            def log_message(self, fmt, *args):
                log.debug("%s - %s", self.address_string(), fmt % args)

            def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8", extra=None):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                has_cache_control = False
                for k, v in (extra or {}).items():
                    if k.lower() == "cache-control":
                        has_cache_control = True
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                if not has_cache_control:
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
                        "version": __version__,
                        "uptime": round(time.time() - started_ts, 1),
                        "control": {
                            "connected": control.connected,
                            "socket": control.socket_path,
                            "last_ok": control.last_ok,
                            "last_error": control.last_error,
                        },
                        "frames": {
                            "connected": frames.connected,
                            "subscribed": frames.subscribed,
                            "fps": frames.current_fps,
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
                    res = wifi_api.handle("GET", path, None, self.headers)
                    if res is not None:
                        self._json(res[0], res[1])
                        return

                # 4. 畫面同步路由
                if path in ("/screen.mjpg", "/screen.jpg", "/screen.info"):
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

                # 5. 主程式狀態路由（合併 Wi-Fi 狀態）
                if path == "/status":
                    code, body, ctype, extra = get_status_with_wifi()
                    self._send(code, body, ctype, extra)
                    return

                # 6. 相機最後一張照片與狀態轉送
                if path in ("/skill/camera.snapshot/last.jpg", "/v1/skill/camera.snapshot/last.jpg"):
                    code, body, ctype, extra = control.forward("GET", "/v1/skill/camera.snapshot/last.jpg")
                    self._send(code, body, ctype, extra)
                    return
                if path in ("/skill/camera.snapshot", "/v1/skill/camera.snapshot"):
                    code, body, ctype, extra = control.forward("GET", "/v1/skill/camera.snapshot")
                    self._send(code, body, ctype, extra)
                    return

                # 7. 主程式通用 GET 控制 API 轉送
                if path.startswith("/v1/"):
                    code, body, ctype, extra = control.forward("GET", path)
                    self._send(code, body, ctype, extra)
                    return

                self._json(404, {"ok": False, "error": "not found"})

            def do_POST(self):
                path, _, _ = self.path.partition("?")
                cl = self.headers.get("Content-Length")
                raw_body = b""
                data = None
                if cl:
                    try:
                        n = min(int(cl), MAX_BODY)
                        raw_body = self.rfile.read(n)
                        if raw_body:
                            data = json.loads(raw_body.decode("utf-8"))
                    except Exception as exc:
                        self._json(400, {"ok": False, "error": f"JSON 解析失敗：{exc}"})
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

        self.control.start_monitor()
        self.frames.start()
        if self.wifi:
            self.wifi.start()

        self.server_thread = threading.Thread(target=self.httpd.serve_forever, name="admin-web", daemon=True)
        self.server_thread.start()

    def stop(self) -> None:
        log.info("正在停止前端後台...")
        self.mirror.close()
        self.frames.stop()
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
        self.control.stop_monitor()
        if self.wifi:
            self.wifi.stop()
        log.info("前端後台已結束")


def parse_args(argv=None, env=None, env_file=None):
    merged_env = _load_admin_env(env_file=env_file, environ=env)
    if env is None and merged_env.get("PET_HOTSPOT_URL") and not os.environ.get("PET_HOTSPOT_URL"):
        os.environ["PET_HOTSPOT_URL"] = merged_env["PET_HOTSPOT_URL"]
    listen_default = merged_env.get("PET_WEB_LISTEN") or DEFAULT_LISTEN
    wifi_iface_default = merged_env.get("PET_WIFI_IFACE") or "wlan0"

    p = argparse.ArgumentParser(description="桌寵前端後台 desktop-pet-admin")
    p.add_argument("--listen", "--web", default=listen_default,
                   help="監聽位址（預設讀 PET_WEB_LISTEN 或 0.0.0.0:8080；測試可用 127.0.0.1:8081）")
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
    p.add_argument("--wifi-iface", default=wifi_iface_default, help="管理的 Wi‑Fi 介面（預設讀 PET_WIFI_IFACE 或 wlan0）")
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

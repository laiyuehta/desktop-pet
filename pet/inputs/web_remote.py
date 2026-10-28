"""網頁遙控（內部控制器）+ HTTP 事件模擬器，只用標準函式庫 http.server。

路由：
  GET  /           遙控網頁（pet/web/remote.html）
  GET  /status     目前狀態 JSON（網頁每秒輪詢）
  POST /event      送標準事件，例如 {"type":"touch","name":"pat"}
  POST /control    {"cmd":"set_state","state":"sleep"} 或 {"cmd":"set_stage","stage":2}
  POST /actuator   {"motion":"nod"}、{"motion":"wheels","action":"forward"} 等 → 外部控制器接口
  POST /talk       {"text":"你好"} → 對寵物說話（有接後端時由後端 LLM 回覆）

用 curl 測：
  curl -X POST localhost:8080/event -d '{"type":"touch","name":"pat"}'
"""

from __future__ import annotations

import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional

from ..events import make_event, normalize_event
from .base import InputAdapter

log = logging.getLogger("pet.web")
HTML_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "web", "remote.html")
MAX_BODY = 64 * 1024


class WebRemoteInput(InputAdapter):
    name = "web"

    def __init__(self, emit, host: str = "0.0.0.0", port: int = 8080,
                 status_fn: Optional[Callable[[], dict]] = None, routes: Optional[list] = None):
        super().__init__(emit, source="web-remote")
        # 額外路由（例如 Wi‑Fi 設定）：物件要有 handle(method, path, data, headers) → (code, obj) 或 None
        self.routes = list(routes or [])
        self.host, self.port = host, port
        self.status_fn = status_fn or (lambda: {})
        self.httpd: Optional[ThreadingHTTPServer] = None
        self.thread: Optional[threading.Thread] = None

    def start(self) -> None:
        adapter = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "DesktopPet/0.1"

            def log_message(self, fmt, *args):  # 改走 logging，避免洗版
                log.debug("%s - %s", self.address_string(), fmt % args)

            def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _json(self, code: int, obj) -> None:
                self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

            def _route(self, method: str, path: str, data) -> bool:
                for r in adapter.routes:
                    try:
                        res = r.handle(method, path, data, self.headers)
                    except Exception as exc:   # 路由出錯不能讓伺服器掛掉
                        log.exception("路由 %s %s 出錯", method, path)
                        self._json(500, {"ok": False, "error": f"內部錯誤：{type(exc).__name__}"})
                        return True
                    if res is not None:
                        self._json(res[0], res[1])
                        return True
                return False

            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if adapter.routes and self._route("GET", path, None):
                    return
                if path in ("/", "/index.html"):
                    try:
                        with open(HTML_PATH, "rb") as fh:
                            self._send(200, fh.read(), "text/html; charset=utf-8")
                    except OSError:
                        self._json(500, {"ok": False, "error": "找不到 remote.html"})
                elif path == "/status":
                    self._json(200, adapter.status_fn())
                else:
                    self._json(404, {"ok": False, "error": "not found"})

            def do_POST(self):
                path = self.path.split("?", 1)[0]
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    if n > MAX_BODY:
                        raise ValueError("內容太大")
                    data = json.loads(self.rfile.read(n) or b"{}")
                    if adapter.routes and self._route("POST", path, data):
                        return
                    ev = adapter.build_event(path, data)
                except (ValueError, KeyError, TypeError) as exc:
                    self._json(400, {"ok": False, "error": str(exc)})
                    return
                if ev is None:
                    self._json(404, {"ok": False, "error": "not found"})
                    return
                adapter.emit(ev)
                self._json(200, {"ok": True, "event": ev})

        self.httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        self.port = self.httpd.server_address[1]   # port=0 時取得實際埠號
        self.running = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="web-remote", daemon=True)
        self.thread.start()
        log.info("網頁遙控：http://%s:%d/", self.host, self.port)

    def build_event(self, path: str, data: dict) -> Optional[dict]:
        if not isinstance(data, dict):
            raise ValueError("需要 JSON 物件")
        if path == "/event":
            return normalize_event(data, default_source=self.source)
        if path == "/control":
            cmd = data.get("cmd")
            if cmd == "set_state":
                return make_event("control", "set_state", source=self.source, state=str(data["state"]))
            if cmd == "set_stage":
                return make_event("control", "set_stage", source=self.source, stage=int(data["stage"]))
            raise ValueError(f"未知的 cmd：{cmd}")
        if path == "/talk":
            text = data.get("text")
            if not isinstance(text, str) or not text.strip():
                raise ValueError("text 不能是空的")
            return make_event("talk", "talk", source=self.source, text=text.strip()[:200])
        if path == "/actuator":
            if not data.get("motion"):
                raise ValueError("缺少 motion")
            return make_event("action", "motion", source=self.source, params=data)
        return None

    def stop(self) -> None:
        super().stop()
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()

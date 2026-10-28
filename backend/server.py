"""後端 HTTP JSON API（標準函式庫 http.server）。

POST /v1/pet/event   {"pet_id","event":{標準事件},"state","stage"} → {"directives":[...], "mood", "growth"}
POST /v1/pet/talk    {"pet_id","text"} → {"say","emotion","intent","source","directives":[...]}
GET  /v1/pet/<id>/state                → 存檔內容（心情、成長、短期記憶）
POST /v1/pet/photo?pet_id=&reason=&taken=   內容是 JPEG → {"ok","photo_id"}（一定要 token）
GET  /v1/pet/<id>/photos[?limit=50]    → 最近照片清單（一定要 token）
GET  /v1/pet/<id>/photos/<photo_id>    → JPEG（一定要 token）
GET  /health                           → 公開：只回 ok／版本；帶 token（或沒設 token）才有 LLM 等細節

指令（directive）種類：
  {"type":"set_state","state":"sleep"}      {"type":"set_stage","stage":2}
  {"type":"say","text":"…","seconds":4}     {"type":"emotion","emotion":"happy","seconds":2}
  {"type":"actuator","motion":"nod"}
  {"type":"skill","name":"camera.snapshot","reason":"llm"}   請裝置執行能力模組（例如拍照）
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs

from . import __version__
from .mood import MoodEngine
from .photos import MAX_PHOTO_BYTES, PhotoStore, valid_photo_id
from .router import Router
from .store import PetStore, valid_pet_id
from .talk import TalkService, intent_directives

log = logging.getLogger("backend.server")
MAX_BODY = 32 * 1024
MAX_TEXT = 200
STATE_RE = re.compile(r"^/v1/pet/([^/]+)/state$")
PHOTOS_RE = re.compile(r"^/v1/pet/([^/]+)/photos$")
PHOTO_RE = re.compile(r"^/v1/pet/([^/]+)/photos/([^/]+)$")
DIRECTIVE_TYPES = {"set_state", "say", "emotion", "set_stage", "actuator", "skill"}
SKILL_NAMES = {"camera.snapshot"}
SILENT_EVENTS = {"heartbeat"}          # 不記進短期記憶的事件


class ApiError(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code = code


class Brain:
    """後端核心邏輯（和 HTTP 分開，方便測試）。"""

    def __init__(self, cfg: Dict[str, Any], router: Optional[Router] = None):
        self.cfg = cfg
        self.store = PetStore(cfg["data_dir"], cfg["mood"]["neutral"], cfg.get("memory_size", 20))
        self.mood = MoodEngine(cfg)
        self.router = router or Router(cfg["llm"])
        self.talker = TalkService(cfg, self.router)
        pcfg = cfg.get("photos") or {}
        self.photos = PhotoStore(cfg["data_dir"], int(pcfg.get("retention", 200)))
        self.started = time.time()
        self._health_cache: Tuple[float, Dict[str, Any]] = (0.0, {})

    # -- 共用 ---------------------------------------------------------------------
    @staticmethod
    def _pet_id(body: Dict[str, Any]) -> str:
        pid = body.get("pet_id")
        if not valid_pet_id(pid):
            raise ApiError(400, "pet_id 不合法（英數字、_、-，1~64 字）")
        return pid

    @staticmethod
    def _touch(rec: Dict[str, Any], body: Dict[str, Any], now: float) -> None:
        rec["last_seen"] = now
        if isinstance(body.get("state"), str):
            rec["device_state"] = body["state"][:32]
        st = body.get("stage")
        if isinstance(st, int) and 1 <= st <= 99:
            rec["growth"]["stage"] = st      # 裝置端（遙控）手動切的階段也記下來

    @staticmethod
    def _clean(directives):
        return [d for d in directives if isinstance(d, dict) and d.get("type") in DIRECTIVE_TYPES
                and (d["type"] != "skill" or d.get("name") in SKILL_NAMES)]

    # -- API ----------------------------------------------------------------------
    def handle_event(self, body: Dict[str, Any]) -> Dict[str, Any]:
        pid = self._pet_id(body)
        ev = body.get("event")
        if not isinstance(ev, dict) or not isinstance(ev.get("name"), str) or not ev["name"]:
            raise ApiError(400, "event 必須是含 name 的物件")
        now = time.time()
        with self.store.lock(pid):
            rec = self.store.load(pid)
            self.mood.decay(rec, now)
            self._touch(rec, body, now)
            if ev.get("type") == "pet" and ev.get("name") == "state_changed" and isinstance(ev.get("new"), str):
                rec["device_state"] = ev["new"][:32]
            directives = self.mood.apply_event(rec, ev, now)
            if ev["name"] not in SILENT_EVENTS and ev.get("type") != "pet":
                self.store.remember(rec, {"kind": "event", "type": ev.get("type"), "name": ev["name"][:40]})
            self.store.save(rec)
            return {"ok": True, "directives": self._clean(directives),
                    "mood": rec["mood"], "growth": rec["growth"]}

    def handle_talk(self, body: Dict[str, Any]) -> Dict[str, Any]:
        pid = self._pet_id(body)
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ApiError(400, "text 不能是空的")
        text = text.strip()[:MAX_TEXT]
        now = time.time()
        with self.store.lock(pid):
            rec = self.store.load(pid)
            self.mood.decay(rec, now)
            self._touch(rec, body, now)
            reply = self.talker.reply(rec, text)
            directives = [{"type": "say", "text": reply["say"], "seconds": 5},
                          {"type": "emotion", "emotion": reply["emotion"], "seconds": 3}]
            directives += intent_directives(reply.get("intent", "none"))
            directives += self.mood.apply_event(rec, {"type": "talk", "name": "talk"}, now)
            # 去掉心情規則重複產生的 say（對話已經有一句了）
            seen_say = False
            uniq = []
            for d in directives:
                if d["type"] == "say":
                    if seen_say:
                        continue
                    seen_say = True
                uniq.append(d)
            self.store.remember(rec, {"kind": "talk", "text": text, "reply": reply["say"],
                                      "emotion": reply["emotion"], "source": reply["source"]})
            self.store.save(rec)
        out = dict(reply)
        out["ok"] = True
        out["directives"] = self._clean(uniq)
        return out

    def get_state(self, pid: str) -> Dict[str, Any]:
        if not valid_pet_id(pid):
            raise ApiError(400, "pet_id 不合法")
        if not self.store.exists(pid):
            raise ApiError(404, "找不到這隻寵物")
        with self.store.lock(pid):
            rec = self.store.load(pid)
            self.mood.decay(rec)       # 只計算，不存檔
        return rec

    # -- 照片 ------------------------------------------------------------------------
    def save_photo(self, query: Dict[str, str], data: bytes) -> Dict[str, Any]:
        pid = query.get("pet_id")
        if not valid_pet_id(pid):
            raise ApiError(400, "pet_id 不合法")
        try:
            taken = float(query.get("taken")) if query.get("taken") else None
        except ValueError:
            taken = None
        try:
            meta = self.photos.save(pid, data, query.get("reason") or "manual", taken)
        except ValueError as exc:
            raise ApiError(400, str(exc))
        with self.store.lock(pid):
            rec = self.store.load(pid)
            recent = rec.setdefault("photos", [])
            recent.append({k: meta[k] for k in ("photo_id", "taken", "reason")})
            del recent[:-int((self.cfg.get("photos") or {}).get("recent_in_record", 20))]
            self.store.save(rec)
        return {"ok": True, "photo_id": meta["photo_id"], "bytes": meta["bytes"]}

    def list_photos(self, pid: str, limit: int) -> Dict[str, Any]:
        if not valid_pet_id(pid):
            raise ApiError(400, "pet_id 不合法")
        return {"ok": True, "pet_id": pid, "count": self.photos.count(pid), "photos": self.photos.list(pid, limit)}

    def health_public(self) -> Dict[str, Any]:
        return {"ok": True, "service": "desktop-pet-backend", "version": __version__}

    def health(self) -> Dict[str, Any]:
        ttl = float(self.cfg["llm"].get("health_cache_s", 10))
        ts, cached = self._health_cache
        if time.time() - ts > ttl or not cached:
            cached = {"local": self.router.local.ping(), "cloud": self.router.cloud.ping(),
                      "router_mode": self.router.mode}
            self._health_cache = (time.time(), cached)
        try:
            pets = len([f for f in os.listdir(self.cfg["data_dir"]) if f.endswith(".json") and not f.startswith(".")])
        except OSError:
            pets = 0
        return {"ok": True, "service": "desktop-pet-backend", "version": __version__,
                "uptime_s": round(time.time() - self.started, 1), "pets": pets, "llm": cached}


def make_server(brain: Brain, host: str, port: int) -> ThreadingHTTPServer:
    token = str(brain.cfg["server"].get("api_token") or "")

    class Handler(BaseHTTPRequestHandler):
        server_version = f"DesktopPetBackend/{__version__}"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            log.debug("%s %s", self.address_string(), fmt % args)

        def _json(self, code: int, obj: Any) -> None:
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _auth(self) -> bool:
            if not token:
                return True
            got = self.headers.get("X-Pet-Token", "")
            return hmac.compare_digest(got.encode(), token.encode())

        def _strict_auth(self) -> None:
            """照片 API：一定要設定 token 並帶對。"""
            if not token:
                raise ApiError(403, "後端沒有設定 api_token，照片 API 關閉")
            if not self._auth():
                raise ApiError(401, "token 錯誤")

        def _query(self) -> Dict[str, str]:
            q = self.path.split("?", 1)[1] if "?" in self.path else ""
            return {k: v[0] for k, v in parse_qs(q).items() if v}

        def _send_bytes(self, code: int, data: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _photo_upload(self) -> Dict[str, Any]:
            self._strict_auth()
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0:
                raise ApiError(400, "沒有照片內容")
            if n > MAX_PHOTO_BYTES:
                raise ApiError(413, "照片太大")
            return brain.save_photo(self._query(), self.rfile.read(n))

        def _body(self) -> Dict[str, Any]:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                raise ApiError(413, "內容太大")
            try:
                data = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise ApiError(400, "內容不是 JSON")
            if not isinstance(data, dict):
                raise ApiError(400, "需要 JSON 物件")
            return data

        def _run(self, fn):
            try:
                if self.path.startswith("/v1/") and not self._auth():
                    raise ApiError(401, "token 錯誤")
                self._json(200, fn())
            except ApiError as exc:
                self.close_connection = True      # 可能還有沒讀的內容（例如被擋下的照片）
                self._json(exc.code, {"ok": False, "error": str(exc)})
            except Exception:   # noqa: BLE001
                log.exception("處理 %s 失敗", self.path)
                self._json(500, {"ok": False, "error": "伺服器內部錯誤"})

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/health":
                self._run(brain.health if self._auth() else brain.health_public)
                return
            m = STATE_RE.match(path)
            if m:
                self._run(lambda: brain.get_state(m.group(1)))
                return
            m = PHOTOS_RE.match(path)
            if m:
                def _list():
                    self._strict_auth()
                    try:
                        limit = int(self._query().get("limit", 50))
                    except ValueError:
                        limit = 50
                    return brain.list_photos(m.group(1), limit)
                self._run(_list)
                return
            m = PHOTO_RE.match(path)
            if m:
                try:
                    self._strict_auth()
                    if not valid_pet_id(m.group(1)) or not valid_photo_id(m.group(2)):
                        raise ApiError(400, "id 不合法")
                    data = brain.photos.get(m.group(1), m.group(2))
                    if data is None:
                        raise ApiError(404, "找不到這張照片")
                    self._send_bytes(200, data, "image/jpeg")
                except ApiError as exc:
                    self._json(exc.code, {"ok": False, "error": str(exc)})
                return
            self._json(404, {"ok": False, "error": "not found"})

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            if path == "/v1/pet/photo":
                self._run(self._photo_upload)
                return
            routes = {"/v1/pet/event": brain.handle_event, "/v1/pet/talk": brain.handle_talk}
            fn = routes.get(path)
            if fn is None:
                self._json(404, {"ok": False, "error": "not found"})
                return
            self._run(lambda: fn(self._body()))

    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    return httpd


def serve_in_thread(brain: Brain, host: str = "127.0.0.1", port: int = 0):
    """測試用：背景啟動，回傳 (httpd, port)。"""
    httpd = make_server(brain, host, port)
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return httpd, httpd.server_address[1]

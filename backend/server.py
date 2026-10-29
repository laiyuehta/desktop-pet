"""後端 HTTP JSON API（標準函式庫 http.server）。

POST /v1/pet/event   {"pet_id","event":{標準事件},"state","stage"} → {"directives":[...], "mood", "growth"}
POST /v1/pet/talk    {"pet_id","text"} → {"say","emotion","intent","source","directives":[...]}
GET  /v1/pet/<id>/state                → 存檔內容（心情、成長、短期記憶）
POST /v1/pet/photo?pet_id=&reason=&taken=   內容是 JPEG → {"ok","photo_id"}
GET  /v1/pet/<id>/photos[?limit=50]    → 最近照片清單
GET  /v1/pet/<id>/photos/<photo_id>    → JPEG
GET  /health                           → 公開：無金鑰時僅回 {"ok": true}
GET  /v1/health                        → 詳細：需金鑰，回版本、uptime、LLM 狀態等細節

連線安全（PROPOSAL v2.7）：
- 每台裝置一把金鑰，綁定 pet_ids；config.json 只存 SHA-256。
- 所有 /v1/* 均需 X-Pet-Token 標頭。
- 沒設定就不開放：無金鑰時 /v1/* 回 503；insecure_dev 僅限綁 127.0.0.1。
- 401（未提供／金鑰錯誤，附 WWW-Authenticate: PetToken）、403（無權限／pet_id 不符／金鑰停用）、429（頻率超標或鎖定）、503（未設定金鑰）。
- 速率限制（每金鑰 120/分、照片 10/分）、同 IP 5 分鐘內錯 10 次鎖定 15 分鐘。
- 讀取逾時 15 秒、最多 32 條連線、標頭單行上限 8 KB、Content-Length 不合法回 400。
"""

from __future__ import annotations

import copy
import hmac
import json
import logging
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs

from . import __version__
from .keys import hash_key, match_key, resolve_config_path
from .mood import MoodEngine
from .photos import MAX_PHOTO_BYTES, PhotoStore, valid_photo_id
from .router import Router
from .security import SecurityGuard
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
    def __init__(self, code: int, msg: str, headers: Optional[Dict[str, str]] = None):
        super().__init__(msg)
        self.code = code
        self.headers = headers or {}


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
        """公開健康檢查：不對外透露版本、服務或細節。"""
        return {"ok": True}

    def health(self) -> Dict[str, Any]:
        """完整健康檢查（需金鑰）：回傳詳細系統指標。"""
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


class LimitedThreadingHTTPServer(ThreadingHTTPServer):
    """具備最大連線數上限（32 條）的 HTTP 伺服器。"""

    def __init__(self, server_address, RequestHandlerClass, max_connections: int = 32):
        super().__init__(server_address, RequestHandlerClass)
        self.daemon_threads = True
        self.connection_semaphore = threading.BoundedSemaphore(max_connections)

    def process_request_thread(self, request, client_address):
        if not self.connection_semaphore.acquire(blocking=False):
            try:
                msg = b"HTTP/1.1 503 Service Unavailable\r\nContent-Type: application/json\r\nRetry-After: 5\r\nConnection: close\r\n\r\n{\"ok\":false,\"error\":\"伺服器忙碌，已達連線上限\"}"
                request.sendall(msg)
            except Exception:
                pass
            finally:
                self.shutdown_request(request)
            return

        try:
            super().process_request_thread(request, client_address)
        finally:
            self.connection_semaphore.release()


def make_server(brain: Brain, host: str, port: int, config_path: Optional[str] = None) -> LimitedThreadingHTTPServer:
    srv_cfg = brain.cfg.setdefault("server", {})
    guard = SecurityGuard(trusted_proxies=srv_cfg.get("trusted_proxies"))

    resolved_cfg_path = config_path or resolve_config_path()
    last_cfg_mtime = [os.path.getmtime(resolved_cfg_path) if os.path.isfile(resolved_cfg_path) else -1.0]

    def _maybe_reload_config():
        if not resolved_cfg_path or not os.path.isfile(resolved_cfg_path):
            return
        try:
            mtime = os.path.getmtime(resolved_cfg_path)
            if mtime != last_cfg_mtime[0]:
                last_cfg_mtime[0] = mtime
                with open(resolved_cfg_path, "r", encoding="utf-8") as fh:
                    raw = json.load(fh)
                    if isinstance(raw, dict) and "server" in raw:
                        new_srv = raw["server"]
                        if "keys" in new_srv:
                            srv_cfg["keys"] = new_srv["keys"]
                        if "api_token" in new_srv:
                            srv_cfg["api_token"] = new_srv["api_token"]
                        if "trusted_proxies" in new_srv:
                            srv_cfg["trusted_proxies"] = new_srv["trusted_proxies"]
                            guard.trusted_proxies = new_srv["trusted_proxies"]
                log.info("偵測到設定檔更新，已重新載入金鑰設定")
        except Exception as exc:
            log.warning("動態重新載入設定檔失敗：%s", exc)

    class Handler(BaseHTTPRequestHandler):
        server_version = f"DesktopPetBackend/{__version__}"
        protocol_version = "HTTP/1.1"
        max_line = 8192      # 標頭單行上限 8 KB

        def setup(self):
            super().setup()
            # 每個連線讀取逾時 15 秒
            self.request.settimeout(15.0)

        def log_message(self, fmt, *args):
            log.debug("%s %s", self.address_string(), fmt % args)

        def _json(self, code: int, obj: Any, headers: Optional[Dict[str, str]] = None) -> None:
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            if headers:
                for k, v in headers.items():
                    self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

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

        def _content_length(self) -> int:
            cl = self.headers.get("Content-Length")
            if cl is None:
                return 0
            try:
                val = int(cl.strip())
                if val < 0:
                    raise ValueError()
                return val
            except (ValueError, TypeError):
                raise ApiError(400, "Content-Length 不合法")

        def _check_auth(self, pet_id_to_check: Optional[str] = None) -> Optional[Dict[str, Any]]:
            _maybe_reload_config()
            client_ip = guard.resolve_ip(self.client_address[0], self.headers)

            # 1. 檢查是否被鎖定
            if guard.is_locked(client_ip):
                guard.record_error(client_ip, "locked", self.path, self.command)
                raise ApiError(429, "請求過於頻繁，已被暫時鎖定")

            keys = srv_cfg.get("keys") or []
            legacy_token = str(srv_cfg.get("api_token") or "")
            insecure_dev = bool(srv_cfg.get("insecure_dev", False))
            is_local = host in ("127.0.0.1", "localhost", "::1")

            # 2. 沒設定就不開放（insecure_dev 僅限綁定 127.0.0.1 時有效）
            if insecure_dev and is_local:
                return None

            if not keys and not legacy_token:
                raise ApiError(503, "後端未設定金鑰")

            # 3. 取得傳入的金鑰
            token = self.headers.get("X-Pet-Token", "").strip()
            if not token:
                auth_hdr = self.headers.get("Authorization", "").strip()
                if auth_hdr.startswith("Bearer "):
                    token = auth_hdr[7:].strip()
                elif auth_hdr.startswith("PetToken "):
                    token = auth_hdr[9:].strip()

            if not token:
                guard.record_error(client_ip, "missing", self.path, self.command)
                raise ApiError(401, "未提供認證金鑰", headers={"WWW-Authenticate": "PetToken"})

            # 4. 常數時間比對 server.keys
            matched, is_enabled = match_key(token, keys)
            if matched is not None:
                if not is_enabled:
                    guard.record_error(client_ip, "forbidden", self.path, self.command, key_id=matched.get("id"))
                    raise ApiError(403, "沒有權限")

                if pet_id_to_check is not None:
                    allowed_pets = matched.get("pet_ids", [])
                    if "*" not in allowed_pets and pet_id_to_check not in allowed_pets:
                        guard.record_error(client_ip, "forbidden", self.path, self.command, key_id=matched.get("id"))
                        raise ApiError(403, "沒有權限")

                is_photo = (self.path.split("?", 1)[0] == "/v1/pet/photo")
                if not guard.check_rate_limit(matched.get("id", "key"), is_photo=is_photo):
                    guard.record_error(client_ip, "rate_limited", self.path, self.command, key_id=matched.get("id"))
                    raise ApiError(429, "請求過於頻繁")

                matched["last_used"] = time.time()
                return matched

            # 5. 比對舊版 api_token（過渡期相容）
            if legacy_token and hmac.compare_digest(token.encode("utf-8"), legacy_token.encode("utf-8")):
                log.info("[LegacyToken] 收到舊版 api_token 請求 (來源: %s, 路徑: %s)", client_ip, self.path)
                is_photo = (self.path.split("?", 1)[0] == "/v1/pet/photo")
                if not guard.check_rate_limit("legacy", is_photo=is_photo):
                    guard.record_error(client_ip, "rate_limited", self.path, self.command, key_id="legacy")
                    raise ApiError(429, "請求過於頻繁")
                return {"id": "legacy", "pet_ids": ["*"], "enabled": True}

            # 6. 比對失敗
            tok_prefix = hash_key(token)[:8]
            guard.record_error(client_ip, "invalid", self.path, self.command, token_hash_prefix=tok_prefix)
            raise ApiError(401, "金鑰錯誤", headers={"WWW-Authenticate": "PetToken"})

        def _photo_upload(self) -> Dict[str, Any]:
            q = self._query()
            pid = q.get("pet_id")
            self._check_auth(pet_id_to_check=pid)
            n = self._content_length()
            if n <= 0:
                raise ApiError(400, "沒有照片內容")
            if n > MAX_PHOTO_BYTES:
                raise ApiError(413, "照片太大")
            return brain.save_photo(q, self.rfile.read(n))

        def _body(self) -> Dict[str, Any]:
            n = self._content_length()
            if n > MAX_BODY:
                raise ApiError(413, "內容太大")
            try:
                raw = self.rfile.read(n).decode("utf-8") if n > 0 else "{}"
                data = json.loads(raw or "{}")
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise ApiError(400, "內容不是 JSON")
            if not isinstance(data, dict):
                raise ApiError(400, "需要 JSON 物件")
            return data

        def _run(self, fn):
            try:
                self._json(200, fn())
            except ApiError as exc:
                self.close_connection = True      # 可能還有沒讀的內容（例如被擋下的照片）
                self._json(exc.code, {"ok": False, "error": str(exc)}, headers=exc.headers)
            except Exception:   # noqa: BLE001
                log.exception("處理 %s 失敗", self.path)
                self._json(500, {"ok": False, "error": "伺服器內部錯誤"})

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/health":
                # 公開健康檢查：未帶金鑰只回 {"ok": true}；帶金鑰或 insecure_dev 則回完整細節
                auth_ok = False
                try:
                    self._check_auth(pet_id_to_check=None)
                    auth_ok = True
                except ApiError:
                    auth_ok = False
                self._run(brain.health if auth_ok else brain.health_public)
                return

            if path == "/v1/health":
                # 詳細健康檢查：需驗證金鑰
                try:
                    self._check_auth(pet_id_to_check=None)
                except ApiError as exc:
                    self._json(exc.code, {"ok": False, "error": str(exc)}, headers=exc.headers)
                    return
                self._run(brain.health)
                return

            m = STATE_RE.match(path)
            if m:
                pid = m.group(1)
                try:
                    self._check_auth(pet_id_to_check=pid)
                except ApiError as exc:
                    self._json(exc.code, {"ok": False, "error": str(exc)}, headers=exc.headers)
                    return
                self._run(lambda: brain.get_state(pid))
                return

            m = PHOTOS_RE.match(path)
            if m:
                pid = m.group(1)
                try:
                    self._check_auth(pet_id_to_check=pid)
                except ApiError as exc:
                    self._json(exc.code, {"ok": False, "error": str(exc)}, headers=exc.headers)
                    return

                def _list():
                    try:
                        limit = int(self._query().get("limit", 50))
                    except ValueError:
                        limit = 50
                    return brain.list_photos(pid, limit)
                self._run(_list)
                return

            m = PHOTO_RE.match(path)
            if m:
                pid = m.group(1)
                photo_id = m.group(2)
                try:
                    self._check_auth(pet_id_to_check=pid)
                    if not valid_pet_id(pid) or not valid_photo_id(photo_id):
                        raise ApiError(400, "id 不合法")
                    data = brain.photos.get(pid, photo_id)
                    if data is None:
                        raise ApiError(404, "找不到這張照片")
                    self._send_bytes(200, data, "image/jpeg")
                except ApiError as exc:
                    self._json(exc.code, {"ok": False, "error": str(exc)}, headers=exc.headers)
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

            try:
                body = self._body()
                pid = body.get("pet_id")
                self._check_auth(pet_id_to_check=pid)
            except ApiError as exc:
                self._json(exc.code, {"ok": False, "error": str(exc)}, headers=exc.headers)
                return

            self._run(lambda: fn(body))

    httpd = LimitedThreadingHTTPServer((host, port), Handler)
    return httpd


def serve_in_thread(brain: Brain, host: str = "127.0.0.1", port: int = 0, config_path: Optional[str] = None):
    """測試用：背景啟動，回傳 (httpd, port)。"""
    httpd = make_server(brain, host, port, config_path=config_path)
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return httpd, httpd.server_address[1]

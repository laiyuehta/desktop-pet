"""能力模組：拍照傳給後端（camera.snapshot）。規格見 PROPOSAL.md「相機常駐服務：picam」「能力模組：拍照傳給後端」。

v2.1 起改成相機常駐服務 picam 的 client（不再自己開相機）：
- 觸發（都走 trigger()，可以同時來自很多來源）：
  事件 {"type":"skill","name":"camera.snapshot","params"?:{...}}（任何來源，包含後端指令）、
  網頁遙控 POST /skill/camera.snapshot（CameraWebAPI；{"wait": true} 會等結果再回）
- 參數（可選）：reason（manual/touch/timer…）、width/height（預設 1280×960）、quality（預設 85）
- 每個觸發各自跟 picam 要照片；picam 把 0.5 秒內的請求合併成同一張（同一個 photo_id）。
  同一個 photo_id 只上傳後端一次，結果事件 camera.snapshot.done 分送給每一個觸發（帶 request_id、trigger）。
- 沒有冷卻、沒有「相機忙碌」：避免狂拍由 picam 的頻率上限（每秒最多 2 次實際拍照）負責。
- picam 沒在跑：回報「相機服務未啟動」，寵物照常運作。
- 上傳失敗重試一次，還是失敗就留在本機待傳資料夾（最多 20 張），之後自動補傳。
- 拍照時請寵物「瞇眼＋喀嚓！」（同一秒內多個觸發只演一次）。照片內容不寫進日誌。
"""

from __future__ import annotations

import collections
import io
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, List, Optional, Tuple

from services.picam import picam_client
from services.picam.picam_client import PicamError

from .base import Extension

log = logging.getLogger("pet.ext.camera")

DEFAULT_PENDING_DIR = os.path.expanduser("~/.local/share/desktop-pet/pending-photos")
DEFAULT_PICAM_SOCKET = picam_client.DEFAULT_SOCKET
REASON_RE = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class CameraError(Exception):
    def __init__(self, kind: str, msg: str):
        super().__init__(msg)
        self.kind = kind


ERROR_TEXT = {
    "service_down": picam_client.SERVICE_DOWN, "camera_error": "相機出錯", "timeout": "拍照逾時",
    "bad_params": "參數不對", "bad_request": "參數不對", "protocol": "picam 回應不對",
}


def picam_snapshot(socket_path: str = DEFAULT_PICAM_SOCKET, timeout: float = 25.0):
    """回傳 capture(width, height, quality, client, reason) → (meta, jpeg)，錯誤丟 CameraError。"""
    def capture(width: int, height: int, quality: int, client: str, reason: str) -> Tuple[dict, bytes]:
        try:
            return picam_client.snapshot(width, height, quality, client=client, reason=reason,
                                         path=socket_path, timeout=timeout)
        except PicamError as exc:
            raise CameraError(exc.kind, str(exc)) from exc
    capture.available = lambda: os.path.exists(socket_path)   # type: ignore[attr-defined]
    return capture


class PhotoUploader:
    """POST /v1/pet/photo（內容是 JPEG），回傳 photo_id。"""

    def __init__(self, base_url: str, pet_id: str, token: str = "", timeout: float = 15.0,
                 token_file: Optional[str] = None, allow_insecure_lan: bool = False):
        from ..backend_client import is_safe_for_insecure_token
        self.base_url = base_url.rstrip("/")
        self.pet_id = pet_id
        self.token = token
        self.timeout = timeout
        self.token_file = token_file
        self.allow_insecure_lan = allow_insecure_lan
        self._is_safe_url = is_safe_for_insecure_token(self.base_url, self.allow_insecure_lan)

    def _upload_once(self, data: bytes, meta: dict, token: str) -> str:
        q = urllib.parse.urlencode({"pet_id": self.pet_id, "reason": meta.get("reason", "manual"),
                                    "taken": "%.3f" % (round(float(meta.get("taken", time.time())) * 1000) / 1000)})
        headers = {"Content-Type": "image/jpeg", "Content-Length": str(len(data))}
        if token and self._is_safe_url:
            headers["X-Pet-Token"] = token
        elif token:
            log.warning("非 HTTPS 且非私有 IP/localhost，拒絕送出金鑰（可設定 --allow-insecure-lan 放行）：%s", self.base_url)
        req = urllib.request.Request(f"{self.base_url}/v1/pet/photo?{q}", data=data, headers=headers, method="POST")
        with _OPENER.open(req, timeout=self.timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        if not body.get("ok") or not body.get("photo_id"):
            raise ValueError(f"後端回應不對：{body.get('error')}")
        return str(body["photo_id"])

    def upload(self, data: bytes, meta: dict) -> str:
        try:
            return self._upload_once(data, meta, self.token)
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and self.token_file:
                try:
                    exp_path = os.path.expanduser(self.token_file)
                    if os.path.isfile(exp_path):
                        with open(exp_path, "r", encoding="utf-8") as fh:
                            new_tok = fh.read().strip()
                        if new_tok and new_tok != self.token:
                            log.info("上傳照片收到 401，已自 %s 重新載入金鑰並重試一次", exp_path)
                            self.token = new_tok
                            return self._upload_once(data, meta, self.token)
                except Exception as read_exc:
                    log.warning("上傳照片重新讀取金鑰檔失敗：%s", read_exc)
            raise


def make_thumb(data: bytes, size=(320, 240)) -> Optional[bytes]:
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(data))
        im.draft("RGB", size)          # JPEG 直接用縮小解碼，很省 CPU
        im = im.convert("RGB")
        im.thumbnail(size)
        out = io.BytesIO()
        im.save(out, "JPEG", quality=75)
        return out.getvalue()
    except Exception:   # noqa: BLE001  縮圖失敗不影響拍照
        log.debug("縮圖失敗", exc_info=True)
        return None


class _Photo:
    """同一個 photo_id 的共用狀態：第一個拿到的請求負責存檔＋上傳，其他的等結果。"""

    def __init__(self, photo_id: str, created: float):
        self.photo_id = photo_id
        self.created = created
        self.candidates: List[Tuple[bytes, dict]] = []
        self.done = threading.Event()
        self.result: Optional[dict] = None
        self.triggers: List[str] = []


class CameraSnapshot(Extension):
    name = "camera.snapshot"

    def __init__(self, uploader: Optional[PhotoUploader] = None, pending_dir: str = DEFAULT_PENDING_DIR,
                 max_pending: int = 20, enabled: bool = True,
                 capture: Optional[Callable[..., Tuple[dict, bytes]]] = None,
                 socket_path: str = DEFAULT_PICAM_SOCKET, retry_delay: float = 2.0,
                 resend_interval: float = 30.0, defaults: Optional[dict] = None, max_workers: int = 8,
                 upload_grace: float = 0.15, react_gap: float = 1.0):
        super().__init__(enabled=enabled)
        self.uploader = uploader
        self.pending_dir = pending_dir
        self.max_pending = max_pending
        self.capture = capture or picam_snapshot(socket_path)
        self.retry_delay = retry_delay
        self.resend_interval = resend_interval
        self.upload_grace = upload_grace
        self.react_gap = react_gap
        self.defaults = dict({"reason": "manual", "width": 1280, "height": 960, "quality": 85}, **(defaults or {}))
        self.sleep = time.sleep
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="camera-req")
        self._photos: Dict[str, _Photo] = {}
        self._waiters: Dict[int, threading.Event] = {}
        self._req_seq = 0
        self._in_flight = 0
        self._last_react = -1e9
        self._upload_lock = threading.Lock()      # 上傳／補傳一次一個，避免同一張傳兩次
        self._thread: Optional[threading.Thread] = None
        self.last: Optional[dict] = None          # 最後一張照片（給遙控頁）
        self.recent = collections.deque(maxlen=30)   # 最近每個觸發的結果
        self.results: Dict[int, dict] = {}
        self.last_thumb: Optional[bytes] = None
        self.last_error: Optional[str] = None
        self.uploaded = 0
        self.photos_total = 0
        self._stop = threading.Event()

    # -- 生命週期 -----------------------------------------------------------------
    def start(self) -> None:
        os.makedirs(self.pending_dir, mode=0o700, exist_ok=True)
        self.subscribe("skill:camera.snapshot", self._on_event)
        self._thread = threading.Thread(target=self._resend_loop, name="camera-resend", daemon=True)
        self._thread.start()
        n = len(self.pending())
        if n:
            log.info("拍照：有 %d 張待傳照片，會自動補傳", n)

    def stop(self) -> None:
        self._stop.set()
        self._pool.shutdown(wait=False)

    def service_available(self) -> bool:
        fn = getattr(self.capture, "available", None)
        return True if fn is None else bool(fn())

    # -- 觸發 -----------------------------------------------------------------------
    def _on_event(self, ev: dict) -> None:
        params = dict(ev.get("params") or {})
        for k in ("reason", "width", "height", "quality"):
            if k in ev and k not in params:
                params[k] = ev[k]
        self.trigger(params, source=str(ev.get("source", "event")))

    def _params(self, params: dict) -> dict:
        p = dict(self.defaults)
        for k, v in (params or {}).items():
            if k in ("width", "height", "quality") and v is not None:
                p[k] = int(v)
            elif k == "reason" and v:
                p[k] = str(v)
        if not REASON_RE.match(p["reason"]):
            raise ValueError("reason 只能用英數字、_ . -（1~32 字）")
        if not (64 <= p["width"] <= 2592 and 48 <= p["height"] <= 1944):
            raise ValueError("width/height 超出範圍（最大 2592×1944）")
        if not (1 <= p["quality"] <= 100):
            raise ValueError("quality 要 1～100")
        return p

    def trigger(self, params: Optional[dict] = None, source: str = "api") -> dict:
        """不阻塞：交給背景執行緒跟 picam 要照片。回傳 {"ok", "request_id"} 或錯誤。"""
        try:
            p = self._params(params or {})
        except (ValueError, TypeError) as exc:
            return self._reject("bad_params", str(exc), source)
        if not self.service_available():
            return self._reject("service_down", ERROR_TEXT["service_down"], source)
        with self._lock:
            self._req_seq += 1
            rid = self._req_seq
            self._in_flight += 1
            self._waiters[rid] = threading.Event()
            now = time.monotonic()
            react = now - self._last_react >= self.react_gap
            if react:
                self._last_react = now
        if react and self.bus:
            self.request_action("expression", expression="squint", seconds=0.6)
            self.request_action("say", text="喀嚓！", seconds=2)
        job = dict(p, source=source, request_id=rid, requested=time.time())
        try:
            self._pool.submit(self._run_safe, job)
        except RuntimeError:          # 已經 stop
            self._finish(job, self._failed_result(job, "service_down", "拍照模組已停止"))
        return {"ok": True, "accepted": True, "request_id": rid, "params": p}

    def wait(self, request_id: int, timeout: float = 30.0) -> Optional[dict]:
        ev = self._waiters.get(request_id)
        if ev is not None:
            ev.wait(timeout)
        return self.results.get(request_id)

    def _reject(self, kind: str, msg: str, source: str) -> dict:
        log.info("拍照不能執行（%s）：%s", source, msg)
        self.last_error = msg
        if self.bus:
            self.publish("camera.snapshot.failed", type_="skill", reason=kind, kind=kind, error=msg, message=msg,
                         trigger=source)
        return {"ok": False, "error": msg, "kind": kind}

    # -- 背景 -------------------------------------------------------------------------
    def _run_safe(self, job: dict) -> None:
        try:
            res = self.run_job(job)
        except Exception as exc:   # noqa: BLE001
            log.exception("拍照工作失敗")
            res = self._failed_result(job, "camera_error", f"內部錯誤：{type(exc).__name__}")
        self._finish(job, res)

    def _finish(self, job: dict, res: dict) -> None:
        rid = job["request_id"]
        with self._lock:
            self._in_flight -= 1
            self.results[rid] = res
            for old in [k for k in self.results if k <= rid - 200]:
                self.results.pop(old, None)
                self._waiters.pop(old, None)
            ev = self._waiters.get(rid)
        self.recent.appendleft(res)
        if ev:
            ev.set()
        if self.bus:
            if res.get("ok"):
                self.publish("camera.snapshot.done", type_="skill", request_id=rid, trigger=res["trigger"],
                             photo_id=res["photo_id"], file=res.get("file"), uploaded=res["uploaded"],
                             queued=res["queued"], shared=res.get("shared"))
            else:
                self.publish("camera.snapshot.failed", type_="skill", request_id=rid, trigger=res.get("trigger"),
                             reason=res.get("kind"), kind=res.get("kind"), error=res.get("error"),
                             message=res.get("error"))

    def _failed_result(self, job: dict, kind: str, msg: str) -> dict:
        self.last_error = msg
        log.warning("拍照失敗（%s）：%s", job.get("source"), msg)
        return {"ok": False, "error": msg, "kind": kind, "request_id": job.get("request_id"),
                "trigger": job.get("source"), "reason": job.get("reason"), "time": time.time()}

    def run_job(self, job: dict) -> dict:
        """一個觸發：跟 picam 要照片 → 同一個 photo_id 只存一次、傳一次 → 回傳這個觸發的結果。"""
        try:
            meta, data = self.capture(job["width"], job["height"], job["quality"],
                                      f"desktop-pet:{job['source']}"[:32], job["reason"])
        except CameraError as exc:
            return self._failed_result(job, exc.kind, str(exc))
        pid = str(meta.get("photo_id") or "")
        if not pid or not data:
            return self._failed_result(job, "protocol", "picam 沒有給 photo_id")
        with self._lock:
            ph = self._photos.get(pid)
            owner = ph is None
            if owner:
                ph = self._photos[pid] = _Photo(pid, time.monotonic())
                self._prune_photos()
            ph.candidates.append((data, dict(meta, reason=job["reason"], source=job["source"])))
            ph.triggers.append(job["source"])
        if owner:
            try:
                self._handle_photo(ph)
            finally:
                ph.done.set()
        else:
            ph.done.wait(120)
        r = ph.result or {"ok": False, "error": "照片處理失敗", "kind": "camera_error"}
        if not r.get("ok"):
            return dict(r, request_id=job["request_id"], trigger=job["source"])
        return dict(r, request_id=job["request_id"], trigger=job["source"], reason=job["reason"],
                    shared=int(meta.get("shared") or 1))

    def _prune_photos(self) -> None:
        now = time.monotonic()
        for k in [k for k, v in self._photos.items() if v.done.is_set() and now - v.created > 120]:
            self._photos.pop(k, None)

    def _handle_photo(self, ph: _Photo) -> None:
        if self.upload_grace:
            self.sleep(self.upload_grace)          # 同一張的其他尺寸幾乎同時到，挑最大的上傳
        with self._lock:
            data, meta = max(ph.candidates, key=lambda c: len(c[0]))
        taken = float(meta.get("ts") or time.time())
        name, i = ph.photo_id, 1
        while os.path.exists(os.path.join(self.pending_dir, name + ".jpg")):
            i += 1
            name = f"{ph.photo_id}-{i}"
        path = os.path.join(self.pending_dir, name + ".jpg")
        umeta = {"reason": meta.get("reason", "manual"), "taken": taken, "source": meta.get("source"),
                 "width": meta.get("width"), "height": meta.get("height"), "photo_id": ph.photo_id}
        with open(path, "wb") as fh:
            fh.write(data)
        with open(path[:-4] + ".json", "w", encoding="utf-8") as fh:
            json.dump(umeta, fh)
        self.last_thumb = make_thumb(data) or self.last_thumb
        backend_id = None
        with self._upload_lock:
            for attempt in (1, 2):
                backend_id = self._upload(data, umeta)
                if backend_id or not self.uploader:
                    break
                if attempt == 1:
                    self.sleep(self.retry_delay)
            if backend_id:
                self._rm(path)
                self._rm(path[:-4] + ".json")
            else:
                self._trim_pending()
        self.photos_total += 1
        ph.result = {"ok": True, "photo_id": ph.photo_id, "backend_photo_id": backend_id, "file": name + ".jpg",
                     "uploaded": bool(backend_id), "queued": not backend_id, "time": taken, "bytes": len(data),
                     "width": meta.get("width"), "height": meta.get("height")}
        self.last = dict(ph.result, reason=umeta["reason"], trigger=umeta["source"])
        log.info("拍照完成 %s（%d KB）%s", ph.photo_id, len(data) // 1024,
                 f"→ 後端 {backend_id}" if backend_id else "→ 先存在本機待傳")
        if backend_id:
            self.flush_pending()          # 順便補傳之前沒傳成功的

    def _upload(self, data: bytes, meta: dict) -> Optional[str]:
        if not self.uploader:
            return None
        try:
            pid = self.uploader.upload(data, meta)
            self.uploaded += 1
            self.last_error = None
            return pid
        except (urllib.error.URLError, OSError, ValueError) as exc:
            code = getattr(exc, "code", None)
            self.last_error = f"上傳失敗：{code or getattr(exc, 'reason', exc)}"
            log.warning("照片上傳失敗（%s）", code or str(getattr(exc, "reason", exc))[:80])
            return None

    def _resend_loop(self) -> None:
        while not self._stop.wait(self.resend_interval):
            try:
                self.flush_pending()
            except Exception:   # noqa: BLE001
                log.exception("補傳失敗")

    # -- 待傳資料夾 -------------------------------------------------------------------
    def pending(self) -> List[str]:
        try:
            return sorted((f for f in os.listdir(self.pending_dir) if f.endswith(".jpg") and not f.startswith(".")),
                          key=lambda f: f[:-4])
        except OSError:
            return []

    @staticmethod
    def _rm(p: str) -> None:
        try:
            os.remove(p)
        except OSError:
            pass

    def _trim_pending(self) -> None:
        files = self.pending()
        for f in files[:-self.max_pending] if len(files) > self.max_pending else []:
            log.warning("待傳照片超過 %d 張，刪掉最舊的 %s", self.max_pending, f)
            self._rm(os.path.join(self.pending_dir, f))
            self._rm(os.path.join(self.pending_dir, f[:-4] + ".json"))

    def flush_pending(self) -> int:
        """補傳待傳照片（舊的先傳；遇到失敗就停，下次再試）。"""
        if not self.uploader:
            return 0
        if not self._upload_lock.acquire(blocking=False):
            return 0
        n = 0
        try:
            for f in self.pending():
                p = os.path.join(self.pending_dir, f)
                try:
                    with open(p, "rb") as fh:
                        data = fh.read()
                except OSError:
                    continue
                try:
                    with open(p[:-4] + ".json", "r", encoding="utf-8") as fh:
                        meta = json.load(fh)
                except (OSError, ValueError):
                    meta = {"reason": "unknown", "taken": os.path.getmtime(p)}
                meta["reason"] = meta.get("reason") or "unknown"
                pid = self._upload(data, meta)
                if not pid:
                    break
                self._rm(p)
                self._rm(p[:-4] + ".json")
                n += 1
                if self.last and self.last.get("file") == f:
                    self.last = dict(self.last, backend_photo_id=pid, uploaded=True, queued=False)
        finally:
            self._upload_lock.release()
        if n:
            log.info("補傳了 %d 張照片", n)
        return n

    # -- 狀態 -----------------------------------------------------------------------
    def status(self) -> dict:
        return {"ok": True, "enabled": self.enabled, "service": self.service_available(),
                "service_text": None if self.service_available() else ERROR_TEXT["service_down"],
                "busy": self._in_flight > 0, "in_flight": self._in_flight, "requests": self._req_seq,
                "photos": self.photos_total, "pending": len(self.pending()), "max_pending": self.max_pending,
                "uploaded": self.uploaded, "uploader": bool(self.uploader), "last": self.last,
                "recent": list(self.recent)[:10], "last_error": self.last_error,
                "has_thumb": self.last_thumb is not None}


class CameraWebAPI:
    """網頁遙控路由：POST /skill/camera.snapshot（{"wait":true} 等結果）、GET 狀態、GET last.jpg 縮圖。"""

    PATH = "/skill/camera.snapshot"

    def __init__(self, ext: CameraSnapshot):
        self.ext = ext

    def handle(self, method: str, path: str, data, headers=None):
        if path == self.PATH and method == "POST":
            data = data if isinstance(data, dict) else {}
            wait = bool(data.pop("wait", False))
            r = self.ext.trigger(data, source=str(data.pop("source", "web"))[:24] or "web")
            if not r.get("ok"):
                return (503 if r.get("kind") == "service_down" else 400), r
            if wait:
                res = self.ext.wait(r["request_id"], 30.0)
                if res is None:
                    return 504, {"ok": False, "error": "等太久", "kind": "timeout", "request_id": r["request_id"]}
                return (200 if res.get("ok") else 502), res
            return 202, r
        if path == self.PATH and method == "GET":
            return 200, self.ext.status()
        if path == self.PATH + "/last.jpg" and method == "GET":
            if self.ext.last_thumb is None:
                return 404, {"ok": False, "error": "還沒有照片"}
            return 200, self.ext.last_thumb, "image/jpeg"
        return None

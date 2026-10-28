"""能力模組：拍照傳給後端（camera.snapshot）。規格見 PROPOSAL.md「能力模組：拍照傳給後端」。

觸發（都走 trigger()）：
- 事件 {"type":"skill","name":"camera.snapshot", "params"?: {...}}（任何來源，包含後端指令）
- 網頁遙控 POST /skill/camera.snapshot（CameraWebAPI）
參數（可選）：reason（manual/touch/timer…）、width/height（預設 1280×960）、quality（預設 85）

流程：冷卻（預設 5 秒）→ 相機占用檢查（camcast 等，不強搶）→ rpicam-still（不開預覽）→
背景上傳 POST /v1/pet/photo（失敗重試一次；還是失敗就留在本機待傳資料夾，最多 20 張，之後自動補傳）。
拍照時請寵物「瞇眼＋喀嚓！」（動作請求，寵物本體決定怎麼演）；結果發 skill 事件
camera.snapshot.done / camera.snapshot.failed。照片內容不寫進日誌。
"""

from __future__ import annotations

import io
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Dict, List, Optional

from .base import Extension

log = logging.getLogger("pet.ext.camera")

DEFAULT_PENDING_DIR = os.path.expanduser("~/.local/share/desktop-pet/pending-photos")
REASON_RE = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")
BUSY_PATTERNS = ("camcast", "rpicam-", "libcamera-", "raspivid", "raspistill")
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class CameraError(Exception):
    def __init__(self, kind: str, msg: str):
        super().__init__(msg)
        self.kind = kind


ERROR_TEXT = {
    "cooldown": "冷卻中", "in_progress": "正在拍上一張", "camera_busy": "相機忙碌（別的程式正在用）",
    "no_camera": "找不到相機", "capture_failed": "拍照失敗", "timeout": "拍照逾時", "bad_params": "參數不對",
}


def camera_busy(proc_root: str = "/proc") -> Optional[str]:
    """有別的程式在用相機就回傳它的名字（看 /proc/*/cmdline，不動它）。"""
    me = os.getpid()
    try:
        pids = [p for p in os.listdir(proc_root) if p.isdigit()]
    except OSError:
        return None
    for p in pids:
        if int(p) == me:
            continue
        try:
            with open(os.path.join(proc_root, p, "cmdline"), "rb") as fh:
                cmd = fh.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
        except OSError:
            continue
        if not cmd:
            continue
        low = cmd.lower()
        for pat in BUSY_PATTERNS:
            if pat in low and "--list-cameras" not in low:
                return pat.rstrip("-")
    return None


def rpicam_capture(path: str, width: int, height: int, quality: int, binary: str = "rpicam-still",
                   warmup_ms: int = 1000, timeout: float = 25.0) -> None:
    """用 rpicam-still 拍一張 JPEG（-n 不開預覽，不碰螢幕）。"""
    if not shutil.which(binary):
        raise CameraError("no_camera", f"沒有 {binary}")
    cmd = [binary, "-n", "-t", str(int(warmup_ms)), "--width", str(width), "--height", str(height),
           "-q", str(quality), "-e", "jpg", "-o", path]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           preexec_fn=lambda: os.nice(10) if hasattr(os, "nice") else None)
    except subprocess.TimeoutExpired:
        raise CameraError("timeout", "拍照逾時")
    err = (p.stderr or "").lower()
    if p.returncode != 0 or not os.path.exists(path) or os.path.getsize(path) < 64:
        if "busy" in err or "in use" in err or "failed to acquire" in err:
            raise CameraError("camera_busy", ERROR_TEXT["camera_busy"])
        if "no cameras" in err or "no camera" in err:
            raise CameraError("no_camera", ERROR_TEXT["no_camera"])
        tail = (p.stderr or "").strip().splitlines()[-1:] or [f"結束碼 {p.returncode}"]
        raise CameraError("capture_failed", f"拍照失敗：{tail[0][:120]}")


class PhotoUploader:
    """POST /v1/pet/photo（內容是 JPEG），回傳 photo_id。"""

    def __init__(self, base_url: str, pet_id: str, token: str = "", timeout: float = 15.0):
        self.base_url = base_url.rstrip("/")
        self.pet_id = pet_id
        self.token = token
        self.timeout = timeout

    def upload(self, data: bytes, meta: dict) -> str:
        q = urllib.parse.urlencode({"pet_id": self.pet_id, "reason": meta.get("reason", "manual"),
                                    "taken": "%.3f" % (round(float(meta.get("taken", time.time())) * 1000) / 1000)})
        headers = {"Content-Type": "image/jpeg"}
        if self.token:
            headers["X-Pet-Token"] = self.token
        req = urllib.request.Request(f"{self.base_url}/v1/pet/photo?{q}", data=data, headers=headers, method="POST")
        with _OPENER.open(req, timeout=self.timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        if not body.get("ok") or not body.get("photo_id"):
            raise ValueError(f"後端回應不對：{body.get('error')}")
        return str(body["photo_id"])


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


class CameraSnapshot(Extension):
    name = "camera.snapshot"

    def __init__(self, uploader: Optional[PhotoUploader] = None, pending_dir: str = DEFAULT_PENDING_DIR,
                 cooldown: float = 5.0, max_pending: int = 20, enabled: bool = True,
                 capture: Optional[Callable[[str, int, int, int], None]] = None,
                 busy_check: Optional[Callable[[], Optional[str]]] = None,
                 clock: Callable[[], float] = time.monotonic, retry_delay: float = 2.0,
                 resend_interval: float = 30.0, defaults: Optional[dict] = None):
        super().__init__(enabled=enabled)
        self.uploader = uploader
        self.pending_dir = pending_dir
        self.cooldown = cooldown
        self.max_pending = max_pending
        self.capture = capture or rpicam_capture
        self.busy_check = busy_check or camera_busy
        self.clock = clock
        self.retry_delay = retry_delay
        self.resend_interval = resend_interval
        self.defaults = dict({"reason": "manual", "width": 1280, "height": 960, "quality": 85}, **(defaults or {}))
        self.sleep = time.sleep
        self._lock = threading.Lock()
        self._jobs: "queue.Queue[Optional[dict]]" = queue.Queue()
        self._busy = False
        self._last_trigger = -1e9
        self._thread: Optional[threading.Thread] = None
        self.last: Optional[dict] = None          # 最後一次結果（給遙控頁）
        self.last_thumb: Optional[bytes] = None
        self.last_error: Optional[str] = None
        self.uploaded = 0
        self._stop = threading.Event()

    # -- 生命週期 -----------------------------------------------------------------
    def start(self) -> None:
        os.makedirs(self.pending_dir, mode=0o700, exist_ok=True)
        self.subscribe("skill:camera.snapshot", self._on_event)
        self._thread = threading.Thread(target=self._worker, name="camera-snapshot", daemon=True)
        self._thread.start()
        n = len(self.pending())
        if n:
            log.info("拍照：有 %d 張待傳照片，會自動補傳", n)

    def stop(self) -> None:
        self._stop.set()
        self._jobs.put(None)

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
        """不阻塞：檢查冷卻後排進背景執行緒。回傳 {"ok", "error"?, "retry_after"?}。"""
        try:
            p = self._params(params or {})
        except (ValueError, TypeError) as exc:
            return self._reject("bad_params", str(exc), source)
        with self._lock:
            now = self.clock()
            if self._busy:
                return self._reject("in_progress", ERROR_TEXT["in_progress"], source)
            left = self.cooldown - (now - self._last_trigger)
            if left > 0:
                return self._reject("cooldown", f"冷卻中，{left:.1f} 秒後再試", source, retry_after=round(left, 1))
            self._last_trigger = now
            self._busy = True
        self._jobs.put(dict(p, source=source, taken=time.time()))
        return {"ok": True, "accepted": True, "params": p}

    def _reject(self, kind: str, msg: str, source: str, **extra) -> dict:
        log.info("拍照被擋下（%s）：%s", source, msg)
        self._emit_failed(kind, msg, source)
        return dict({"ok": False, "error": msg, "kind": kind}, **extra)

    def _emit_failed(self, kind: str, msg: str, source: str) -> None:
        if self.bus:
            self.publish("camera.snapshot.failed", type_="skill", reason=kind, message=msg, trigger=source)

    # -- 背景執行緒 ------------------------------------------------------------------
    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                job = self._jobs.get(timeout=self.resend_interval)
            except queue.Empty:
                job = "resend"
            if job is None:
                break
            if job == "resend":
                self.flush_pending()
                continue
            try:
                self.run_job(job)
            except Exception:   # noqa: BLE001
                log.exception("拍照工作失敗")
            finally:
                with self._lock:
                    self._busy = False

    def run_job(self, job: dict) -> dict:
        """拍照＋上傳（背景執行緒；測試可以直接呼叫）。"""
        src = job.get("source", "api")
        who = self.busy_check()
        if who:
            return self._fail(job, "camera_busy", f"相機忙碌（{who} 正在用）")
        ms = int(round(job["taken"] * 1000))      # 跟後端 photo_id 同一套算法
        name = time.strftime("%Y%m%d-%H%M%S", time.localtime(ms // 1000)) + "-%03d" % (ms % 1000)
        base, i = name, 1
        while os.path.exists(os.path.join(self.pending_dir, name + ".jpg")):
            i += 1
            name = f"{base}-{i}"
        path = os.path.join(self.pending_dir, name + ".jpg")
        meta = {"reason": job["reason"], "taken": job["taken"], "source": src, "width": job["width"],
                "height": job["height"]}
        if self.bus:
            self.request_action("expression", expression="squint", seconds=0.6)
            self.request_action("say", text="喀嚓！", seconds=2)
        try:
            self.capture(path, job["width"], job["height"], job["quality"])
        except CameraError as exc:
            self._rm(path)
            return self._fail(job, exc.kind, str(exc))
        with open(path, "rb") as fh:
            data = fh.read()
        with open(path[:-4] + ".json", "w", encoding="utf-8") as fh:
            json.dump(meta, fh)
        self.last_thumb = make_thumb(data) or self.last_thumb
        photo_id = None
        for attempt in (1, 2):
            photo_id = self._upload(data, meta)
            if photo_id or not self.uploader:
                break
            if attempt == 1:
                self.sleep(self.retry_delay)
        if photo_id:
            self._rm(path)
            self._rm(path[:-4] + ".json")
        else:
            self._trim_pending()
        result = {"ok": True, "file": name + ".jpg", "photo_id": photo_id, "uploaded": bool(photo_id),
                  "queued": not photo_id, "reason": job["reason"], "trigger": src, "time": job["taken"],
                  "bytes": len(data)}
        self.last = result
        log.info("拍照完成 %s（%d KB）%s", name, len(data) // 1024,
                 f"→ 後端 {photo_id}" if photo_id else "→ 先存在本機待傳")
        if self.bus:
            self.publish("camera.snapshot.done", type_="skill", file=name + ".jpg", photo_id=photo_id,
                         uploaded=bool(photo_id), queued=not photo_id, trigger=src)
        if photo_id:
            self.flush_pending()          # 順便補傳之前沒傳成功的
        return result

    def _fail(self, job: dict, kind: str, msg: str) -> dict:
        log.warning("拍照失敗：%s", msg)
        self.last_error = msg
        self.last = {"ok": False, "error": msg, "kind": kind, "reason": job.get("reason"),
                     "trigger": job.get("source"), "time": job.get("taken")}
        self._emit_failed(kind, msg, job.get("source", "api"))
        return self.last

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
        n = 0
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
                self.last = dict(self.last, photo_id=pid, uploaded=True, queued=False)
        if n:
            log.info("補傳了 %d 張照片", n)
        return n

    # -- 狀態 -----------------------------------------------------------------------
    def status(self) -> dict:
        left = max(0.0, self.cooldown - (self.clock() - self._last_trigger))
        return {"ok": True, "enabled": self.enabled, "busy": self._busy, "cooldown_s": self.cooldown,
                "cooldown_left": round(left, 1), "pending": len(self.pending()), "max_pending": self.max_pending,
                "uploaded": self.uploaded, "uploader": bool(self.uploader), "last": self.last,
                "last_error": self.last_error, "has_thumb": self.last_thumb is not None}


class CameraWebAPI:
    """網頁遙控路由：POST /skill/camera.snapshot、GET /skill/camera.snapshot、GET /skill/camera.snapshot/last.jpg。"""

    PATH = "/skill/camera.snapshot"

    def __init__(self, ext: CameraSnapshot):
        self.ext = ext

    def handle(self, method: str, path: str, data, headers=None):
        if path == self.PATH and method == "POST":
            r = self.ext.trigger(data if isinstance(data, dict) else {}, source="web")
            if r.get("ok"):
                return 202, r
            return (429 if r.get("kind") in ("cooldown", "in_progress") else 400), r
        if path == self.PATH and method == "GET":
            return 200, self.ext.status()
        if path == self.PATH + "/last.jpg" and method == "GET":
            if self.ext.last_thumb is None:
                return 404, {"ok": False, "error": "還沒有照片"}
            return 200, self.ext.last_thumb, "image/jpeg"
        return None

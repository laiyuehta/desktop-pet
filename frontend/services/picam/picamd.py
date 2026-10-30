#!/usr/bin/env python3
"""picamd：相機常駐服務（systemd picam.service）。協定與設計見同目錄 README.md、PROPOSAL.md「相機常駐服務：picam」。

- 開機就打開 Pi 相機（Picamera2 / libcamera）並一直占著；只有它碰相機，其他程式都當 client。
- Unix socket /run/picam/picam.sock（執行使用者的主要群組 0660），JSON 一行一個請求：snapshot / subscribe / status。
- 拍照合併：0.5 秒內（以及拍照中）到的請求只拍一次、同一個 photo_id；解析度取最大，較小的請求縮圖給。
- 頻率上限：每秒最多 2 次實際拍照，多的請求併到下一張。
- 有預覽訂閱時照樣可拍照：預覽暫停約 1 秒再自動恢復。
- 狀態 /run/picam/status.json；相機出錯會關掉重開（退避 2～60 秒），請求一律回清楚的錯誤。
照片內容不寫日誌（只記 photo_id、尺寸、請求數）。
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import os
import signal
import socket
import struct
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

log = logging.getLogger("picam")

DEFAULT_SOCKET = "/run/picam/picam.sock"
DEFAULT_STATUS = "/run/picam/status.json"
MAX_W, MAX_H = 2592, 1944
MAX_PREVIEW_W, MAX_PREVIEW_H, MAX_PREVIEW_FPS = 1296, 972, 30
TEXT_OK = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-:")


def photo_id_for(ts: float) -> str:
    """年月日-時分秒-毫秒（毫秒四捨五入；跟桌寵、後端同一套算法）。"""
    ms = int(round(ts * 1000))
    return time.strftime("%Y%m%d-%H%M%S", time.localtime(ms // 1000)) + "-%03d" % (ms % 1000)


def clean_text(v, default: str = "") -> str:
    s = "".join(c for c in str(v or default) if c in TEXT_OK)[:32]
    return s or default


class CameraFault(Exception):
    def __init__(self, kind: str, msg: str):
        super().__init__(msg)
        self.kind = kind


# ---------------------------------------------------------------------------------------------
# 相機後端：Picamera2（真的）／測試用假的。所有方法只會在 picam 的相機執行緒呼叫。
# ---------------------------------------------------------------------------------------------
class CameraBackend:
    model = "?"

    def open(self) -> None: ...
    def close(self) -> None: ...
    def capture(self, width: int, height: int):  # -> PIL.Image (RGB)
        raise NotImplementedError
    def start_preview(self, width: int, height: int, fps: int, quality: int, on_frame: Callable[[bytes], None]) -> None: ...
    def stop_preview(self) -> None: ...
    def info(self) -> dict:
        return {"model": self.model}


class Picamera2Backend(CameraBackend):
    """Picamera2：相機一直開著。閒置時跑低速 640×480 串流（AE/AWB 維持收斂，拍照不用等）；
    拍照時切 still 設定拍一張再切回；預覽用硬體 MJPEG 編碼（沒有就用軟體 JPEG）。"""

    def __init__(self, idle_fps: float = 5.0, settle_frames: int = 2):
        self.idle_fps = idle_fps
        self.settle_frames = settle_frames
        self.cam = None
        self.preview: Optional[Tuple[int, int, int, int, Callable]] = None
        self.encoder = None
        self.encoder_name = None
        self.mode = "closed"
        self.sensor_modes: List[dict] = []
        self._raw_modes: List[dict] = []

    def open(self) -> None:
        try:
            from picamera2 import Picamera2
        except ImportError as exc:
            raise CameraFault("camera_error", "沒有安裝 picamera2（sudo apt install python3-picamera2）") from exc
        cams = Picamera2.global_camera_info()
        if not cams:
            raise CameraFault("no_camera", "找不到相機（排線或相機沒接好？）")
        try:
            self.cam = Picamera2(0)
        except Exception as exc:  # noqa: BLE001
            raise CameraFault("camera_error", f"相機打不開：{exc}") from exc
        self.model = str(cams[0].get("Model", "?"))
        try:
            self._raw_modes = list(self.cam.sensor_modes)
            self.sensor_modes = [{"size": list(m["size"]), "fps": round(float(m.get("fps", 0)), 1),
                                  "bit_depth": m.get("bit_depth"), "crop": list(m.get("crop_limits") or [])}
                                 for m in self._raw_modes]
        except Exception:  # noqa: BLE001
            self._raw_modes, self.sensor_modes = [], []
        self._idle()

    def close(self) -> None:
        self.preview = None
        if self.cam is not None:
            try:
                self._stop_encoder()
                self.cam.stop()
            except Exception:  # noqa: BLE001
                pass
            try:
                self.cam.close()
            except Exception:  # noqa: BLE001
                pass
        self.cam = None
        self.mode = "closed"

    # -- 內部 --
    def _stop_encoder(self) -> None:
        if self.encoder is not None:
            try:
                self.cam.stop_encoder(self.encoder)
            except Exception:  # noqa: BLE001
                log.debug("stop_encoder", exc_info=True)
            self.encoder = None

    def _restart(self, cfg) -> None:
        self._stop_encoder()
        self.cam.stop()
        self.cam.configure(cfg)

    def _idle(self) -> None:
        if self.idle_fps <= 0:
            self._stop_encoder()
            self.cam.stop()
            self.mode = "idle"
            return
        cfg = self.cam.create_video_configuration(main={"size": (640, 480), "format": "YUV420"}, buffer_count=2,
                                                  controls={"FrameRate": float(self.idle_fps)})
        self._restart(cfg)
        self.cam.start()
        self.mode = "idle"

    def _crop_for(self, w: int, h: int):
        """中央裁切成目標長寬比（預覽 800×480 才不會變形）。"""
        try:
            x, y, cw, ch = self.cam.camera_controls["ScalerCrop"][2]
        except Exception:  # noqa: BLE001
            return None
        if cw * h > ch * w:
            nw = ch * w // h
            return (x + (cw - nw) // 2, y, nw, ch)
        nh = cw * h // w
        return (x, y + (ch - nh) // 2, cw, nh)

    def _full_fov_mode(self, w: int, h: int, fps: int):
        """預覽用的 sensor 模式：視角完整（不是中央裁切的 1080p 模式）、夠大、fps 夠，挑最小的。
        ov5647：800×480 會選 1296×972（2×2 binning，全視角），不會選 1920×1080（只有中間一塊）。"""
        modes = [m for m in self._raw_modes if m.get("crop_limits")]
        if not modes:
            return None
        full_w = max(m["crop_limits"][2] for m in modes)
        full = [m for m in modes if m["crop_limits"][2] >= full_w * 0.98]
        ok = [m for m in full if m["size"][0] >= w and m["size"][1] >= h and float(m.get("fps", 0)) >= fps]
        pick = min(ok, key=lambda m: m["size"][0] * m["size"][1]) if ok else None
        return pick

    def _start_preview_hw(self) -> None:
        w, h, fps, q, on_frame = self.preview
        from picamera2.outputs import Output

        class _CB(Output):
            def outputframe(self, frame, keyframe=True, timestamp=None, *args, **kw):
                on_frame(bytes(frame))

        mode = self._full_fov_mode(w, h, fps)
        extra = {"sensor": {"output_size": tuple(mode["size"]), "bit_depth": mode.get("bit_depth", 10)}} if mode else {}
        cfg = self.cam.create_video_configuration(main={"size": (w, h), "format": "YUV420"}, buffer_count=4,
                                                  controls={"FrameRate": float(fps)}, **extra)
        self._restart(cfg)
        enc = None
        try:
            from picamera2.encoders import MJPEGEncoder
            enc = MJPEGEncoder(bitrate=int(w * h * fps * (0.6 + q / 100.0) * 1.2))
            self.cam.start_encoder(enc, _CB())
            self.encoder_name = "MJPEGEncoder（硬體）"
        except Exception as exc:  # noqa: BLE001  沒有硬體編碼器就改軟體 JPEG
            log.warning("硬體 MJPEG 不能用（%s），改用軟體 JPEG", exc)
            from picamera2.encoders import JpegEncoder
            enc = JpegEncoder(q=q)
            self.cam.start_encoder(enc, _CB())
            self.encoder_name = "JpegEncoder（軟體）"
        self.encoder = enc
        self.cam.start()
        crop = self._crop_for(w, h)
        if crop:
            self.cam.set_controls({"ScalerCrop": crop})
        self.mode = "preview"

    # -- 介面 --
    def start_preview(self, width, height, fps, quality, on_frame) -> None:
        self.preview = (int(width), int(height), int(fps), int(quality), on_frame)
        self._start_preview_hw()

    def stop_preview(self) -> None:
        self.preview = None
        self._idle()

    def capture(self, width: int, height: int):
        cfg = self.cam.create_still_configuration(main={"size": (int(width), int(height)), "format": "RGB888"},
                                                  buffer_count=2, display=None)
        self.mode = "capturing"
        try:
            self._restart(cfg)
            self.cam.start()
            for _ in range(self.settle_frames):     # 切模式後丟掉幾格，讓曝光穩定
                self.cam.capture_request().release()
            img = self.cam.capture_image("main")
        finally:
            try:
                if self.preview:
                    self._start_preview_hw()
                else:
                    self._idle()
            except Exception:  # noqa: BLE001  切回失敗交給上層重開相機
                self.mode = "error"
                raise
        return img.convert("RGB") if img.mode != "RGB" else img

    def info(self) -> dict:
        return {"model": self.model, "mode": self.mode, "sensor_modes": self.sensor_modes,
                "encoder": self.encoder_name if self.preview else None, "idle_fps": self.idle_fps}


# ---------------------------------------------------------------------------------------------
# 排程：合併請求、頻率上限、預覽管理
# ---------------------------------------------------------------------------------------------
class SnapRequest:
    __slots__ = ("w", "h", "q", "client", "reason", "t", "event", "head", "data", "error")

    def __init__(self, w: int, h: int, q: int, client: str, reason: str):
        self.w, self.h, self.q, self.client, self.reason = w, h, q, client, reason
        self.t = time.monotonic()
        self.event = threading.Event()
        self.head: Optional[dict] = None
        self.data: Optional[bytes] = None
        self.error: Optional[Tuple[str, str]] = None


class Batch:
    def __init__(self, t_first: float):
        self.t_first = t_first
        self.members: List[SnapRequest] = []
        self.cap: Optional[Tuple[int, int]] = None
        self.closed = False


class Subscription:
    def __init__(self, w: int, h: int, fps: int, client: str):
        self.w, self.h, self.fps, self.client = w, h, fps, client
        self.cond = threading.Condition()
        self.frame: Optional[bytes] = None
        self.seq = 0
        self.closed = False
        self.sent = 0
        self.dropped = 0

    def offer(self, data: bytes) -> None:
        with self.cond:                     # 只留最新一張；慢的 client 自然丟舊畫面
            self.frame, self.seq = data, self.seq + 1
            self.cond.notify_all()


def encode_variant(img, w: int, h: int, q: int) -> bytes:
    from PIL import Image
    im = img
    if (w, h) != img.size:
        sw, sh = img.size
        if sw * h > sh * w:           # 原圖比較寬 → 裁左右
            cw = sh * w / h
            box = ((sw - cw) / 2, 0, (sw + cw) / 2, sh)
        else:
            ch = sw * h / w
            box = (0, (sh - ch) / 2, sw, (sh + ch) / 2)
        im = img.resize((w, h), Image.BILINEAR, box=box, reducing_gap=2.0)
    out = io.BytesIO()
    im.save(out, "JPEG", quality=q, optimize=False)
    return out.getvalue()


class Picam:
    def __init__(self, backend: CameraBackend, window: float = 0.5, max_rate: float = 2.0,
                 request_timeout: float = 20.0, reopen_min: float = 2.0, reopen_max: float = 60.0,
                 preview_quality: int = 80, preview_stall: float = 6.0):
        self.backend = backend
        self.window = window
        self.min_interval = 1.0 / max_rate if max_rate > 0 else 0.0
        self.request_timeout = request_timeout
        self.reopen_min, self.reopen_max = reopen_min, reopen_max
        self.preview_quality = preview_quality
        self.preview_stall = preview_stall
        self.cond = threading.Condition()
        self.next_batch: Optional[Batch] = None
        self.current: Optional[Batch] = None
        self.subs: List[Subscription] = []
        self.preview_cfg: Optional[Tuple[int, int, int]] = None
        self.opened = False
        self.running = False
        self.last_start = -1e9
        self.last_pid = None
        self.started = time.time()
        self.stats = {"captures": 0, "requests": 0, "merged": 0, "errors": 0, "reopens": 0,
                      "preview_frames": 0, "clients": 0, "subscribers": 0}
        self.last_error = ""
        self.last_error_ts = None
        self.last_capture: Optional[dict] = None
        self._last_frame_t = 0.0
        self._fps_n, self._fps_t0, self.preview_fps = 0, time.monotonic(), 0.0
        self._next_open = 0.0
        self._reopen_delay = reopen_min
        self.thread: Optional[threading.Thread] = None

    # -- 公開 --
    def start(self) -> None:
        self.running = True
        self.thread = threading.Thread(target=self._camera_loop, name="picam-camera", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        with self.cond:
            self.running = False
            self.cond.notify_all()
        if self.thread:
            self.thread.join(10)
        try:
            self.backend.close()
        except Exception:  # noqa: BLE001
            pass

    def snapshot(self, w: int, h: int, q: int, client: str = "", reason: str = "") -> SnapRequest:
        req = SnapRequest(w, h, q, client, reason)
        with self.cond:
            self.stats["requests"] += 1
            cur = self.current
            if not self.opened and time.monotonic() < self._next_open and self.last_error:
                req.error = ("camera_error", f"相機目前不能用：{self.last_error}")
                req.event.set()
                return req
            if cur is not None and not cur.closed and cur.cap and w <= cur.cap[0] and h <= cur.cap[1]:
                cur.members.append(req)              # 拍照中：掛在這一張上
                self.stats["merged"] += 1
            else:
                nb = self.next_batch
                if nb is None:
                    nb = self.next_batch = Batch(req.t)
                else:
                    self.stats["merged"] += 1
                nb.members.append(req)
            self.cond.notify_all()
        if not req.event.wait(self.request_timeout):
            with self.cond:
                if not req.event.is_set():
                    req.error = ("timeout", "拍照逾時")
                    req.event.set()
        return req

    def add_sub(self, sub: Subscription) -> None:
        with self.cond:
            self.subs.append(sub)
            self.stats["subscribers"] = len(self.subs)
            self.cond.notify_all()

    def remove_sub(self, sub: Subscription) -> None:
        with self.cond:
            if sub in self.subs:
                self.subs.remove(sub)
            self.stats["subscribers"] = len(self.subs)
            self.cond.notify_all()
        with sub.cond:
            sub.closed = True
            sub.cond.notify_all()

    def status(self) -> dict:
        try:
            binfo = self.backend.info()
        except Exception:  # noqa: BLE001
            binfo = {}
        with self.cond:
            st = dict(self.stats)
            st.update({
                "ok": True, "service": "picam", "start": self.started, "uptime": round(time.time() - self.started, 1),
                "camera_open": self.opened, "camera": binfo.get("model"), "mode": binfo.get("mode", "?"),
                "sensor_modes": binfo.get("sensor_modes"), "encoder": binfo.get("encoder"),
                "idle_fps": binfo.get("idle_fps"),
                "preview": ({"width": self.preview_cfg[0], "height": self.preview_cfg[1], "fps": self.preview_cfg[2],
                             "actual_fps": round(self.preview_fps, 1)} if self.preview_cfg else None),
                "window_s": self.window, "max_rate": round(1.0 / self.min_interval, 2) if self.min_interval else None,
                "queued": len(self.next_batch.members) if self.next_batch else 0,
                "last_capture": self.last_capture, "last_error": self.last_error, "last_error_ts": self.last_error_ts,
                "subscriber_clients": [s.client for s in self.subs],
            })
        return st

    # -- 相機執行緒 --
    def _fault(self, msg: str) -> None:
        self.stats["errors"] += 1
        self.last_error = msg[:200]
        self.last_error_ts = time.time()
        log.error("相機錯誤：%s", msg)

    def _ensure_open(self) -> bool:
        if self.opened:
            return True
        if time.monotonic() < self._next_open:
            return False
        try:
            self.backend.open()
            self.opened = True
            self.preview_cfg = None
            self._reopen_delay = self.reopen_min
            log.info("相機已打開：%s", self.backend.info().get("model"))
            return True
        except Exception as exc:  # noqa: BLE001
            self._fault(f"相機打不開：{exc}")
            self._next_open = time.monotonic() + self._reopen_delay
            self._reopen_delay = min(self.reopen_max, self._reopen_delay * 2)
            with self.cond:                       # 等著的請求直接回錯，不要讓 client 卡住
                self._fail_batch(self.next_batch, "camera_error", f"相機打不開：{exc}")
                self.next_batch = None
            return False

    def _reopen(self, why: str) -> None:
        self._fault(why)
        self.stats["reopens"] += 1
        try:
            self.backend.close()
        except Exception:  # noqa: BLE001
            pass
        self.opened = False
        self.preview_cfg = None
        self._next_open = time.monotonic() + self.reopen_min

    @staticmethod
    def _fail_batch(b: Optional[Batch], kind: str, msg: str) -> None:
        if not b:
            return
        for m in b.members:
            if not m.event.is_set():
                m.error = (kind, msg)
                m.event.set()
        b.closed = True

    def _desired_preview(self) -> Optional[Tuple[int, int, int]]:
        if not self.subs:
            return None
        return (max(s.w for s in self.subs), max(s.h for s in self.subs), max(s.fps for s in self.subs))

    def _on_frame(self, data: bytes) -> None:
        now = time.monotonic()
        self._last_frame_t = now
        self.stats["preview_frames"] += 1
        self._fps_n += 1
        if now - self._fps_t0 >= 2.0:
            self.preview_fps = self._fps_n / (now - self._fps_t0)
            self._fps_n, self._fps_t0 = 0, now
        for s in list(self.subs):
            s.offer(data)

    def _camera_loop(self) -> None:
        while True:
            with self.cond:
                if not self.running:
                    return
            if not self._ensure_open():
                with self.cond:
                    self.cond.wait(0.5)
                continue
            batch = None
            with self.cond:
                now = time.monotonic()
                nb = self.next_batch
                due = max(nb.t_first + self.window, self.last_start + self.min_interval) if nb else None
                want = self._desired_preview()
                if nb and now >= due:
                    batch, self.next_batch = nb, None
                    batch.cap = (max(m.w for m in batch.members), max(m.h for m in batch.members))
                    self.current = batch
                elif want == self.preview_cfg:
                    stall = (self.preview_cfg and self._last_frame_t and now - self._last_frame_t > self.preview_stall)
                    if not stall:
                        self.cond.wait(min(1.0, due - now) if due else 1.0)
                        continue
            if batch is not None:
                self._capture(batch)
                continue
            if want == self.preview_cfg:            # 預覽卡住了
                self._reopen("預覽超過 %.0f 秒沒有畫面" % self.preview_stall)
                continue
            try:
                if want is None:
                    self.backend.stop_preview()
                    log.info("預覽停止（沒有訂閱者）")
                else:
                    self.backend.start_preview(want[0], want[1], want[2], self.preview_quality, self._on_frame)
                    log.info("預覽開始 %dx%d @%d fps", *want)
                self._last_frame_t = time.monotonic()
                self.preview_cfg = want
            except Exception as exc:  # noqa: BLE001
                self._reopen(f"切換預覽失敗：{exc}")

    def _capture(self, batch: Batch) -> None:
        self.last_start = time.monotonic()
        cw, ch = batch.cap
        t0 = time.monotonic()
        try:
            img = self.backend.capture(cw, ch)
            ts = time.time()
        except Exception as exc:  # noqa: BLE001
            with self.cond:
                self._fail_batch(batch, "camera_error", f"拍照失敗：{exc}")
                self.current = None
            self._reopen(f"拍照失敗：{exc}")
            return
        if self.preview_cfg:
            self._last_frame_t = time.monotonic()
        pid = photo_id_for(ts)
        if pid == self.last_pid:
            ts += 0.001
            pid = photo_id_for(ts)
        self.last_pid = pid
        cache: Dict[Tuple[int, int, int], bytes] = {}
        served = 0
        clients = set()
        while True:
            with self.cond:
                todo = [m for m in batch.members if not m.event.is_set()]
                if not todo:
                    batch.closed = True
                    self.current = None
                    n = len(batch.members)
                    break
            for m in todo:
                key = (m.w, m.h, m.q)
                try:
                    if key not in cache:
                        cache[key] = encode_variant(img, m.w, m.h, m.q)
                    data = cache[key]
                    m.head = {"ok": True, "photo_id": pid, "ts": round(ts, 3), "size": len(data), "width": m.w,
                              "height": m.h, "quality": m.q, "capture": [cw, ch], "shared": len(batch.members)}
                    m.data = data
                except Exception as exc:  # noqa: BLE001
                    m.error = ("camera_error", f"編碼失敗：{exc}")
                m.event.set()
                served += 1
                clients.add(m.client or "?")
        dt = time.monotonic() - t0
        with self.cond:
            self.stats["captures"] += 1
            self.last_capture = {"photo_id": pid, "ts": round(ts, 3), "width": cw, "height": ch, "requests": n,
                                 "seconds": round(dt, 2)}
        log.info("拍照 %s %dx%d（%.2f 秒）→ %d 個請求（%s）", pid, cw, ch, dt, n, ",".join(sorted(clients)))


# ---------------------------------------------------------------------------------------------
# Socket 伺服器
# ---------------------------------------------------------------------------------------------
def _int(d: dict, k: str, default: int, lo: int, hi: int) -> int:
    v = d.get(k, default)
    if v is None:
        v = default
    v = int(v)
    if not lo <= v <= hi:
        raise ValueError(f"{k} 要在 {lo}～{hi}")
    return v


class Server:
    def __init__(self, picam: Picam, path: str = DEFAULT_SOCKET, group: Optional[str] = None,
                 status_path: Optional[str] = DEFAULT_STATUS, status_every: float = 2.0):
        self.picam = picam
        self.path = path
        self.group = group
        self.status_path = status_path
        self.status_every = status_every
        self.sock: Optional[socket.socket] = None
        self.stop_event = threading.Event()
        self.threads: List[threading.Thread] = []

    def start(self) -> None:
        d = os.path.dirname(self.path)
        if d:
            os.makedirs(d, exist_ok=True)
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(self.path)
        if self.group:
            try:
                import grp
                os.chown(self.path, -1, grp.getgrnam(self.group).gr_gid)
            except (KeyError, PermissionError, OSError) as exc:
                log.warning("socket 群組設不了 %s：%s", self.group, exc)
        os.chmod(self.path, 0o660)
        s.listen(16)
        s.settimeout(0.5)
        self.sock = s
        for fn, name in ((self._accept_loop, "picam-accept"), (self._status_loop, "picam-status")):
            t = threading.Thread(target=fn, name=name, daemon=True)
            t.start()
            self.threads.append(t)
        log.info("picam 在 %s 等 client", self.path)

    def stop(self) -> None:
        self.stop_event.set()
        if self.sock:
            self.sock.close()
        try:
            os.unlink(self.path)
        except OSError:
            pass
        for t in self.threads:
            t.join(2)

    def _status_loop(self) -> None:
        while not self.stop_event.is_set():
            self.write_status()
            self.stop_event.wait(self.status_every)

    def write_status(self) -> None:
        if not self.status_path:
            return
        try:
            st = self.picam.status()
            st.pop("ok", None)
            tmp = self.status_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(st, fh, ensure_ascii=False)
            os.chmod(tmp, 0o644)
            os.replace(tmp, self.status_path)
        except OSError:
            log.debug("寫 status.json 失敗", exc_info=True)

    def _accept_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                if self.stop_event.is_set():
                    return
                time.sleep(0.2)
                continue
            threading.Thread(target=self._client, args=(conn,), name="picam-client", daemon=True).start()

    @staticmethod
    def _send_line(conn: socket.socket, obj: dict) -> None:
        conn.sendall((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))

    def _client(self, conn: socket.socket) -> None:
        p = self.picam
        with p.cond:
            p.stats["clients"] += 1
        conn.settimeout(30)
        buf = b""
        try:
            while not self.stop_event.is_set():
                while b"\n" not in buf:
                    d = conn.recv(4096)
                    if not d:
                        return
                    buf += d
                    if len(buf) > 8192:
                        self._send_line(conn, {"ok": False, "kind": "bad_request", "error": "請求太長"})
                        return
                line, buf = buf.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    req = json.loads(line.decode("utf-8"))
                    if not isinstance(req, dict):
                        raise ValueError("要 JSON 物件")
                except (ValueError, UnicodeDecodeError) as exc:
                    self._send_line(conn, {"ok": False, "kind": "bad_request", "error": f"看不懂的請求：{exc}"})
                    continue
                cmd = req.get("cmd")
                if cmd == "status":
                    self._send_line(conn, p.status())
                elif cmd == "snapshot":
                    self._snapshot(conn, req)
                elif cmd == "subscribe":
                    self._subscribe(conn, req)
                    return
                else:
                    self._send_line(conn, {"ok": False, "kind": "bad_request", "error": f"未知的 cmd：{cmd}"})
        except (OSError, socket.timeout):
            pass
        finally:
            with p.cond:
                p.stats["clients"] -= 1
            try:
                conn.close()
            except OSError:
                pass

    def _snapshot(self, conn: socket.socket, req: dict) -> None:
        try:
            w = _int(req, "width", 1280, 64, MAX_W)
            h = _int(req, "height", 960, 48, MAX_H)
            q = _int(req, "quality", 85, 1, 100)
        except (ValueError, TypeError) as exc:
            self._send_line(conn, {"ok": False, "kind": "bad_request", "error": str(exc)})
            return
        conn.settimeout(self.picam.request_timeout + 10)
        r = self.picam.snapshot(w, h, q, clean_text(req.get("client"), "?"), clean_text(req.get("reason"), "manual"))
        if r.error or r.data is None:
            kind, msg = r.error or ("camera_error", "沒有照片")
            self._send_line(conn, {"ok": False, "kind": kind, "error": msg})
            return
        conn.sendall((json.dumps(r.head) + "\n").encode("utf-8") + struct.pack(">I", len(r.data)) + r.data)

    def _subscribe(self, conn: socket.socket, req: dict) -> None:
        try:
            fps = _int(req, "fps", 15, 1, MAX_PREVIEW_FPS)
            w = _int(req, "width", 800, 64, MAX_PREVIEW_W)
            h = _int(req, "height", 480, 48, MAX_PREVIEW_H)
        except (ValueError, TypeError) as exc:
            self._send_line(conn, {"ok": False, "kind": "bad_request", "error": str(exc)})
            return
        sub = Subscription(w, h, fps, clean_text(req.get("client"), "?"))
        self.picam.add_sub(sub)
        log.info("預覽訂閱 +1（%s %dx%d @%d）", sub.client, w, h, fps)
        try:
            with self.picam.cond:
                cfg = self.picam._desired_preview() or (w, h, fps)
            self._send_line(conn, {"ok": True, "width": cfg[0], "height": cfg[1], "fps": fps,
                                   "note": "拍照時預覽會暫停約 1 秒"})
            conn.settimeout(10)
            seen, next_t = 0, 0.0
            while not self.stop_event.is_set():
                with sub.cond:
                    while sub.seq == seen and not sub.closed and not self.stop_event.is_set():
                        sub.cond.wait(1.0)
                    if sub.closed:
                        return
                    if sub.seq == seen:
                        continue
                    if seen and sub.seq - seen > 1:
                        sub.dropped += sub.seq - seen - 1
                    data, seen = sub.frame, sub.seq
                now = time.monotonic()
                if now < next_t:              # 這個 client 要的 fps 比較低：跳過這格
                    sub.dropped += 1
                    continue
                next_t = max(next_t + 1.0 / sub.fps, now) if next_t else now + 1.0 / sub.fps
                conn.sendall(struct.pack(">I", len(data)) + data)
                sub.sent += 1
        except (OSError, socket.timeout):
            pass
        finally:
            self.picam.remove_sub(sub)
            log.info("預覽訂閱 -1（%s，送 %d 格、丟 %d 格）", sub.client, sub.sent, sub.dropped)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="picam 相機常駐服務")
    ap.add_argument("--socket", default=DEFAULT_SOCKET)
    ap.add_argument("--status", default=DEFAULT_STATUS)
    ap.add_argument("--group", default=None, help="socket 群組（0660；預設 = 執行使用者的主要群組）")
    ap.add_argument("--window", type=float, default=0.5, help="合併視窗秒數（預設 0.5）")
    ap.add_argument("--max-rate", type=float, default=2.0, help="每秒最多實際拍幾次（預設 2）")
    ap.add_argument("--idle-fps", type=float, default=5.0, help="閒置時相機串流 fps（0 = 停住但仍占著相機）")
    ap.add_argument("--settle-frames", type=int, default=2, help="切到拍照模式後丟幾格再拍")
    ap.add_argument("--preview-quality", type=int, default=80)
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    picam = Picam(Picamera2Backend(idle_fps=a.idle_fps, settle_frames=a.settle_frames), window=a.window,
                  max_rate=a.max_rate, preview_quality=a.preview_quality)
    srv = Server(picam, a.socket, a.group, a.status)
    stop = threading.Event()
    for sg in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sg, lambda *_: stop.set())
    picam.start()
    srv.start()
    try:
        while not stop.is_set():
            stop.wait(1.0)
    finally:
        log.info("picam 結束")
        srv.stop()
        picam.stop()
        srv.write_status()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

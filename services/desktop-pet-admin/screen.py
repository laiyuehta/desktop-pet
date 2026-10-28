"""前端後台螢幕畫面同步模組（desktop-pet-admin 用）。

規格見 PROPOSAL.md「主程式 ↔ 前端後台的接口」：
- frames.sock 訂閱主程式畫面後交給 ScreenMirror。
- 提供網頁路由：
  GET /screen.mjpg?fps=10   MJPEG 串流
  GET /screen.jpg           最新一張
  GET /screen.info          畫面序號、狀態、FPS 等 JSON
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from typing import Callable, Optional, Tuple

log = logging.getLogger("admin.screen")
BOUNDARY = b"petframe"


class ScreenMirror:
    def __init__(self, max_viewers: int = 3):
        self.cond = threading.Condition()
        self.data: Optional[bytes] = None
        self.seq = 0
        self.ts = 0.0
        self.meta: dict = {}
        self.max_viewers = max(1, int(max_viewers))
        self.viewers = 0
        self.rejected = 0
        self.streamed = 0
        self.closed = False

    def publish(self, data: bytes, meta: Optional[dict] = None) -> None:
        with self.cond:
            self.data, self.meta = data, meta or {}
            self.seq += 1
            self.ts = time.time()
            if self.viewers:
                self.cond.notify_all()

    def latest(self) -> Tuple[int, float, Optional[bytes], dict]:
        with self.cond:
            return self.seq, self.ts, self.data, self.meta

    def wait_newer(self, seq: int, timeout: float) -> Optional[Tuple[int, float, bytes, dict]]:
        with self.cond:
            if self.seq == seq and not self.closed:
                self.cond.wait(timeout)
            if self.seq == seq or self.data is None:
                return None
            return self.seq, self.ts, self.data, self.meta

    def acquire(self) -> bool:
        with self.cond:
            if self.viewers >= self.max_viewers:
                self.rejected += 1
                return False
            self.viewers += 1
            return True

    def release(self) -> None:
        with self.cond:
            self.viewers = max(0, self.viewers - 1)

    def close(self) -> None:
        with self.cond:
            self.closed = True
            self.cond.notify_all()


def _clamp_fps(q: Optional[dict], default: int = 10) -> int:
    try:
        v = int(float((q or {}).get("fps", default)))
    except (TypeError, ValueError):
        v = default
    return max(1, min(20, v))


class ScreenWebAPI:
    def __init__(self, mirror: ScreenMirror, status_fn: Optional[Callable[[], dict]] = None,
                 is_core_online_fn: Optional[Callable[[], bool]] = None):
        self.mirror = mirror
        self.status_fn = status_fn or (lambda: {})
        self.is_core_online_fn = is_core_online_fn or (lambda: True)

    def info(self) -> dict:
        seq, ts, data, meta = self.mirror.latest()
        st = self.status_fn() or {}
        render = st.get("render") or {}
        mode = meta.get("mode") or render.get("mode") or "live"
        core_online = self.is_core_online_fn()
        return {
            "ok": data is not None and core_online,
            "seq": seq,
            "ts": round(ts, 3) if ts else None,
            "age_ms": round((time.time() - ts) * 1000) if ts else None,
            "bytes": len(data) if data else 0,
            "sha1": hashlib.sha1(data).hexdigest() if data else None,
            "mode": mode,
            "source": "即時繪圖" if mode == "live" else ("動畫包＋對話框" if mode == "bubble" else "動畫包"),
            "pack": render.get("pack"),
            "clip": meta.get("clip"),
            "frame": meta.get("frame"),
            "state": st.get("state"),
            "fps": st.get("fps"),
            "viewers": self.mirror.viewers,
            "max_viewers": self.mirror.max_viewers,
            "core_online": core_online,
        }

    def handle(self, method: str, path: str, data, headers=None):
        if method != "GET" or not path.startswith("/screen."):
            return None
        if path == "/screen.info":
            return 200, self.info()
        if path == "/screen.jpg":
            seq, ts, frame, meta = self.mirror.latest()
            if frame is None or not self.is_core_online_fn():
                return 503, {"ok": False, "error": "桌寵主程式未執行"}
            return 200, frame, "image/jpeg", {
                "X-Frame-Seq": str(seq), "X-Frame-Ts": "%.3f" % ts,
                "X-Frame-Mode": str(meta.get("mode", "")),
                "X-Frame-Clip": str(meta.get("clip") or ""),
                "X-Frame-Index": str(meta.get("frame") if meta.get("frame") is not None else ""),
            }
        if path == "/screen.mjpg":
            fps = _clamp_fps(data if isinstance(data, dict) else None)
            return "stream", lambda handler: self.stream(handler, fps)
        return None

    def stream(self, h, fps: int) -> None:
        m = self.mirror
        if not m.acquire():
            h._json(503, {"ok": False, "error": f"同時觀看人數已滿（上限 {m.max_viewers}）"})
            return
        h.close_connection = True
        interval = 1.0 / fps
        sent = 0
        try:
            h.send_response(200)
            h.send_header("Content-Type", "multipart/x-mixed-replace; boundary=" + BOUNDARY.decode())
            h.send_header("Cache-Control", "no-store, no-cache")
            h.send_header("Pragma", "no-cache")
            h.send_header("Connection", "close")
            h.end_headers()
            h.connection.settimeout(10)
            seq, next_t = 0, 0.0
            while not m.closed:
                f = m.wait_newer(seq, 2.0)
                if f is None:
                    continue
                now = time.monotonic()
                if now < next_t:
                    time.sleep(next_t - now)
                    f = m.wait_newer(seq, 0) or f
                seq, ts, frame, meta = f
                head = (b"--" + BOUNDARY + b"\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n"
                        b"X-Frame-Seq: %d\r\nX-Frame-Ts: %.3f\r\n\r\n" % (len(frame), seq, ts))
                h.wfile.write(head + frame + b"\r\n")
                sent += 1
                next_t = max(next_t + interval, time.monotonic() - interval) if next_t else time.monotonic() + interval
        except (OSError, ValueError):
            pass
        finally:
            m.streamed += sent
            m.release()
            log.debug("螢幕串流結束：送了 %d 張", sent)


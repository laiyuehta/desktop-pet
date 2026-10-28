"""PiscreenSink：把畫面編成 JPEG，經 Unix socket 丟給 Pi 上的 piscreend。

協定（見 PROPOSAL.md 第 3 章）：重複送 [4 byte big-endian 長度][JPEG bytes]
到 /run/piscreen/piscreen.sock。800×480 baseline JPEG 會直接轉送。
桌寵程式不直接碰 USB，也不重啟 piscreen 服務。
連線失敗時自動以退避時間重連，期間的畫面直接丟掉。
"""

from __future__ import annotations

import io
import logging
import socket
import struct
import time

from PIL import Image

from .base import OutputSink

log = logging.getLogger("pet.piscreen")
DEFAULT_SOCKET = "/run/piscreen/piscreen.sock"


def encode_jpeg(frame: Image.Image, quality: int = 80) -> bytes:
    """baseline JPEG（progressive=False），4:2:0 取樣，速度優先。"""
    if frame.mode != "RGB":
        frame = frame.convert("RGB")
    buf = io.BytesIO()
    frame.save(buf, format="JPEG", quality=quality, progressive=False, optimize=False, subsampling=2)
    return buf.getvalue()


def pack_frame(jpeg: bytes) -> bytes:
    return struct.pack(">I", len(jpeg)) + jpeg


class PiscreenSink(OutputSink):
    name = "piscreen"

    def __init__(self, path: str = DEFAULT_SOCKET, quality: int = 80, size=(800, 480),
                 retry_min: float = 0.5, retry_max: float = 5.0, timeout: float = 2.0):
        self.path = path
        self.quality = quality
        self.size = tuple(size)
        self.sock = None
        self.retry_min, self.retry_max = retry_min, retry_max
        self._retry = retry_min
        self._next_try = 0.0
        self.timeout = timeout
        self.sent = 0
        self.dropped = 0
        self.last_jpeg_size = 0
        self.mirror = None          # ScreenMirror：送成功的每一格同步一份（同一份 bytes）
        self.frame_meta = None      # 這一格的來源資訊（PackDriver 設定）

    def open(self) -> None:
        self._connect()

    def _connect(self) -> bool:
        now = time.monotonic()
        if now < self._next_try:
            return False
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.settimeout(self.timeout)
            s.connect(self.path)
            self.sock = s
            self._retry = self.retry_min
            log.info("已連上 piscreen：%s", self.path)
            return True
        except OSError as exc:
            s.close()
            self._next_try = now + self._retry
            log.warning("連不上 piscreen（%s），%.1f 秒後重試", exc, self._retry)
            self._retry = min(self.retry_max, self._retry * 2)
            return False

    def send(self, frame: Image.Image) -> None:
        if frame.size != self.size:
            frame = frame.resize(self.size)
        if self.sock is None and not self._connect():
            self.dropped += 1
            return
        data = encode_jpeg(frame, self.quality)
        self.last_jpeg_size = len(data)
        try:
            self.sock.sendall(pack_frame(data))
            self.sent += 1
            if self.mirror is not None:
                self.mirror.publish(data, self.frame_meta)
        except OSError as exc:
            log.warning("送畫面失敗（%s），之後重連", exc)
            self.dropped += 1
            self.close()

    def send_jpeg(self, data: bytes) -> None:
        """動畫包的幀已經是 800×480 baseline JPEG：直接送，不解碼、不重新編碼。"""
        if self.sock is None and not self._connect():
            self.dropped += 1
            return
        self.last_jpeg_size = len(data)
        try:
            self.sock.sendall(pack_frame(data))
            self.sent += 1
            if self.mirror is not None:
                self.mirror.publish(data, self.frame_meta)
        except OSError as exc:
            log.warning("送畫面失敗（%s），之後重連", exc)
            self.dropped += 1
            self.close()

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

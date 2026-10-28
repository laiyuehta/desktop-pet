"""picam client：跟相機常駐服務 picamd 要照片／預覽（協定見 README.md）。只用標準函式庫。

    from picam_client import snapshot, status, Subscriber
    meta, jpeg = snapshot(width=1280, height=960, quality=85, client="me", reason="manual")
    for jpeg in Subscriber(fps=15, width=800, height=480).frames(): ...   # 斷線會自動重連

錯誤一律丟 PicamError(kind, 訊息)：
  service_down  picam 沒在跑（socket 不存在或連不上）→「相機服務未啟動」
  camera_error  picam 在跑但相機打不開／拍照失敗
  bad_request   參數不對
  timeout       等太久
  protocol      回應格式不對
"""
from __future__ import annotations

import json
import os
import socket
import struct
import threading
import time
from typing import Iterator, Optional, Tuple

DEFAULT_SOCKET = "/run/picam/picam.sock"
SERVICE_DOWN = "相機服務未啟動"


class PicamError(Exception):
    def __init__(self, kind: str, msg: str):
        super().__init__(msg)
        self.kind = kind


def _connect(path: str, timeout: float) -> socket.socket:
    if not os.path.exists(path):
        raise PicamError("service_down", SERVICE_DOWN)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(path)
    except (FileNotFoundError, ConnectionRefusedError) as exc:
        s.close()
        raise PicamError("service_down", SERVICE_DOWN) from exc
    except socket.timeout as exc:
        s.close()
        raise PicamError("timeout", "連 picam 逾時") from exc
    except OSError as exc:
        s.close()
        raise PicamError("service_down", f"{SERVICE_DOWN}（{exc.strerror or exc}）") from exc
    return s


class _Reader:
    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.buf = b""

    def _fill(self) -> None:
        d = self.sock.recv(65536)
        if not d:
            raise PicamError("service_down", "picam 中斷連線")
        self.buf += d

    def line(self, limit: int = 65536) -> dict:
        while b"\n" not in self.buf:
            if len(self.buf) > limit:
                raise PicamError("protocol", "回應太長")
            self._fill()
        raw, self.buf = self.buf.split(b"\n", 1)
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError as exc:
            raise PicamError("protocol", "回應不是 JSON") from exc

    def blob(self, limit: int = 32 << 20) -> bytes:
        while len(self.buf) < 4:
            self._fill()
        n = struct.unpack(">I", self.buf[:4])[0]
        if n > limit:
            raise PicamError("protocol", f"圖片太大（{n} bytes）")
        while len(self.buf) < 4 + n:
            self._fill()
        data, self.buf = self.buf[4:4 + n], self.buf[4 + n:]
        return data


def _wrap(fn):
    try:
        return fn()
    except PicamError:
        raise
    except socket.timeout as exc:
        raise PicamError("timeout", "等 picam 回應逾時") from exc
    except OSError as exc:
        raise PicamError("service_down", f"picam 連線中斷（{exc.strerror or exc}）") from exc


def request(cmd: dict, path: str = DEFAULT_SOCKET, timeout: float = 20.0, with_blob: bool = False):
    s = _connect(path, timeout)
    try:
        def go():
            s.sendall((json.dumps(cmd) + "\n").encode("utf-8"))
            r = _Reader(s)
            head = r.line()
            if not head.get("ok"):
                raise PicamError(str(head.get("kind") or "camera_error"), str(head.get("error") or "picam 回報錯誤"))
            return (head, r.blob()) if with_blob else head
        return _wrap(go)
    finally:
        s.close()


def snapshot(width: int = 1280, height: int = 960, quality: int = 85, client: str = "", reason: str = "manual",
             path: str = DEFAULT_SOCKET, timeout: float = 20.0) -> Tuple[dict, bytes]:
    """拍一張（picam 會把 0.5 秒內的請求合併成一張，photo_id 相同）。回傳 (meta, jpeg)。"""
    return request({"cmd": "snapshot", "width": int(width), "height": int(height), "quality": int(quality),
                    "client": client, "reason": reason}, path, timeout, with_blob=True)


def status(path: str = DEFAULT_SOCKET, timeout: float = 5.0) -> dict:
    return request({"cmd": "status"}, path, timeout)


class Subscriber:
    """預覽訂閱：frames() 持續產生 JPEG；picam 重啟或斷線時自動重連（退避 0.5～5 秒）。"""

    def __init__(self, fps: int = 15, width: int = 800, height: int = 480, client: str = "",
                 path: str = DEFAULT_SOCKET, on_state=None):
        self.cmd = {"cmd": "subscribe", "fps": int(fps), "width": int(width), "height": int(height), "client": client}
        self.path = path
        self.stop_event = threading.Event()
        self.on_state = on_state or (lambda state, detail: None)
        self.reconnects = 0
        self.info: Optional[dict] = None

    def stop(self) -> None:
        self.stop_event.set()

    def frames(self, idle_timeout: float = 10.0) -> Iterator[bytes]:
        backoff = 0.5
        while not self.stop_event.is_set():
            try:
                s = _connect(self.path, 5.0)
            except PicamError as exc:
                self.on_state("down", str(exc))
                self.stop_event.wait(backoff)
                backoff = min(5.0, backoff * 2)
                continue
            try:
                s.sendall((json.dumps(self.cmd) + "\n").encode("utf-8"))
                s.settimeout(idle_timeout)     # 拍照時預覽會停約 1 秒，10 秒都沒畫面才當成斷線
                r = _Reader(s)
                head = _wrap(r.line)
                if not head.get("ok"):
                    raise PicamError(str(head.get("kind") or "camera_error"), str(head.get("error")))
                self.info = head
                self.on_state("up", head)
                backoff = 0.5
                while not self.stop_event.is_set():
                    yield _wrap(r.blob)
            except PicamError as exc:
                self.on_state("down", str(exc))
                self.reconnects += 1
                self.stop_event.wait(backoff)
                backoff = min(5.0, backoff * 2)
            finally:
                s.close()

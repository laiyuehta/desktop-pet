"""畫面供應（/run/desktop-pet/frames.sock，串流協定）。

規格見 PROPOSAL.md「主程式 ↔ 前端後台的接口」：
- client 送 {"v":1,"cmd":"subscribe","fps":10}。伺服器回 {"ok":true,"proto":1}，接著每格送：
  一行 JSON 標頭 {"seq","ts","mode":"pack|live","clip","w","h","size"} ＋ [4 byte big-endian 長度][JPEG]。
- client 送 {"v":1,"cmd":"latest"}：回最新一張。
- 訂閱上限 2。慢的訂閱者丟舊畫面，不拖慢 20 fps 主迴圈。沒有訂閱者時不做任何事。
"""

from __future__ import annotations

import json
import logging
import os
import queue
import socket
import struct
import threading
import time
from typing import Optional, Set, Tuple

log = logging.getLogger("pet.frames")
DEFAULT_FRAMES_SOCKET = "/run/desktop-pet/frames.sock"


class _Subscriber:
    def __init__(self, sock: socket.socket, fps: int):
        self.sock = sock
        self.fps = max(1, min(20, int(fps)))
        self.queue: queue.Queue = queue.Queue(maxsize=1)
        self.closed = False

    def offer(self, frame_item: Tuple[int, float, bytes, dict]):
        if self.closed:
            return
        try:
            self.queue.put_nowait(frame_item)
        except queue.Full:
            try:
                self.queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self.queue.put_nowait(frame_item)
            except queue.Full:
                pass

    def close(self):
        self.closed = True
        try:
            self.sock.close()
        except OSError:
            pass


class FramesServer:
    def __init__(self, path: str = DEFAULT_FRAMES_SOCKET, max_subscribers: int = 2):
        self.path = path
        self.max_subscribers = max(1, int(max_subscribers))
        self._lock = threading.Lock()
        self._subscribers: Set[_Subscriber] = set()
        self._latest_frame: Optional[Tuple[int, float, bytes, dict]] = None
        self._seq = 0
        self._running = False
        self._sock: Optional[socket.socket] = None
        self._accept_thread: Optional[threading.Thread] = None

    def publish(self, data: bytes, meta: Optional[dict] = None) -> None:
        """主迴圈呼叫：提供最新畫面。沒有訂閱者時不做多餘運算。"""
        with self._lock:
            self._seq += 1
            ts = time.time()
            meta = meta or {}
            self._latest_frame = (self._seq, ts, data, meta)
            if not self._subscribers:
                return
            subs = list(self._subscribers)
        for s in subs:
            s.offer((self._seq, ts, data, meta))

    def latest(self) -> Optional[Tuple[int, float, bytes, dict]]:
        with self._lock:
            return self._latest_frame

    def start(self) -> None:
        if not hasattr(socket, "AF_UNIX"):
            log.warning("此平台不支援 AF_UNIX，無法啟動 frames.sock")
            return

        if os.path.exists(self.path):
            try:
                os.unlink(self.path)
            except OSError as exc:
                log.warning("無法刪除舊的 socket %s：%s", self.path, exc)

        parent = os.path.dirname(os.path.abspath(self.path))
        if parent:
            try:
                os.makedirs(parent, exist_ok=True)
            except OSError as exc:
                log.warning("無法建立 socket 資料夾 %s：%s", parent, exc)

        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.bind(self.path)
            try:
                os.chmod(self.path, 0o660)
            except OSError:
                pass
            s.listen(4)
            self._sock = s
            self._running = True
            self._accept_thread = threading.Thread(target=self._accept_loop, name="frames-accept", daemon=True)
            self._accept_thread.start()
            log.info("畫面供應服務已啟動：%s", self.path)
        except OSError as exc:
            log.warning("無法啟動 frames.sock %s：%s（略過啟動）", self.path, exc)
            self._running = False
            self._sock = None

    def _accept_loop(self) -> None:
        while self._running and self._sock:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                break
            threading.Thread(target=self._handle_client, args=(conn,), daemon=True).start()

    def _handle_client(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(5.0)
            line = b""
            while b"\n" not in line:
                chunk = conn.recv(1024)
                if not chunk:
                    break
                line += chunk
            if not line:
                conn.close()
                return

            cmd_data = json.loads(line.decode("utf-8").strip() or "{}")
            cmd = cmd_data.get("cmd")

            if cmd == "latest":
                frame = self.latest()
                if frame is None:
                    conn.sendall(json.dumps({"ok": False, "error": "還沒有畫面"}, ensure_ascii=False).encode("utf-8") + b"\n")
                else:
                    seq, ts, data, meta = frame
                    header = {
                        "ok": True, "proto": 1, "seq": seq, "ts": round(ts, 3),
                        "mode": meta.get("mode", "live"), "clip": meta.get("clip"),
                        "w": meta.get("w", 800), "h": meta.get("h", 480), "size": len(data),
                    }
                    payload = (json.dumps(header, ensure_ascii=False).encode("utf-8") + b"\n" +
                               struct.pack(">I", len(data)) + data)
                    conn.sendall(payload)
                conn.close()
                return

            if cmd == "subscribe":
                fps = cmd_data.get("fps", 10)
                sub = _Subscriber(conn, fps)
                with self._lock:
                    if len(self._subscribers) >= self.max_subscribers:
                        conn.sendall(json.dumps({"ok": False, "error": f"訂閱人數已滿（上限 {self.max_subscribers}）"}).encode("utf-8") + b"\n")
                        conn.close()
                        return
                    self._subscribers.add(sub)
                    if self._latest_frame is not None:
                        sub.offer(self._latest_frame)

                conn.sendall(json.dumps({"ok": True, "proto": 1}).encode("utf-8") + b"\n")
                conn.settimeout(5.0)
                self._stream_subscriber(sub)
                return

            conn.sendall(json.dumps({"ok": False, "error": f"未知的指令：{cmd}"}).encode("utf-8") + b"\n")
            conn.close()
        except Exception as exc:
            log.debug("處理 frames client 失敗：%s", exc)
            try:
                conn.close()
            except OSError:
                pass

    def _stream_subscriber(self, sub: _Subscriber) -> None:
        interval = 1.0 / sub.fps
        next_t = time.monotonic()
        try:
            while self._running and not sub.closed:
                try:
                    item = sub.queue.get(timeout=1.0)
                except queue.Empty:
                    continue

                now = time.monotonic()
                if now < next_t:
                    time.sleep(next_t - now)
                    # 睡醒後檢查是否有更新的幀
                    try:
                        item = sub.queue.get_nowait()
                    except queue.Empty:
                        pass

                seq, ts, data, meta = item
                header = {
                    "seq": seq, "ts": round(ts, 3),
                    "mode": meta.get("mode", "live"), "clip": meta.get("clip"),
                    "w": meta.get("w", 800), "h": meta.get("h", 480), "size": len(data),
                }
                payload = (json.dumps(header, ensure_ascii=False).encode("utf-8") + b"\n" +
                           struct.pack(">I", len(data)) + data)
                sub.sock.sendall(payload)
                next_t = max(next_t + interval, time.monotonic() - interval) if next_t else time.monotonic() + interval
        except OSError:
            pass
        finally:
            with self._lock:
                self._subscribers.discard(sub)
            sub.close()

    def stop(self) -> None:
        self._running = False
        with self._lock:
            subs = list(self._subscribers)
            self._subscribers.clear()
        for s in subs:
            s.close()
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        if os.path.exists(self.path):
            try:
                os.unlink(self.path)
            except OSError:
                pass
        log.info("畫面供應服務已停止：%s", self.path)


class FrameBroadcaster:
    """畫面廣播轉接器：可同時向 ScreenMirror 與 FramesServer 分送畫面。"""

    def __init__(self, *targets):
        self.targets = [t for t in targets if t is not None]

    def publish(self, data: bytes, meta: Optional[dict] = None) -> None:
        for t in self.targets:
            try:
                t.publish(data, meta)
            except Exception:
                pass


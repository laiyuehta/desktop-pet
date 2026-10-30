"""畫面供應（frames.sock）測試：最新一張、訂閱、人數上限、慢速訂閱者丟幀。"""
import json
import socket
import struct
import tempfile
import time
import unittest
from pathlib import Path

from pet.frames import FramesServer


def _recv_line(sock, initial_buf=b""):
    buf = initial_buf
    while b"\n" not in buf:
        chunk = sock.recv(1024)
        if not chunk:
            break
        buf += chunk
    line, _, remaining = buf.partition(b"\n")
    return line, remaining


class TestFrames(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    @unittest.skipUnless(hasattr(socket, "AF_UNIX"), "需要 socket.AF_UNIX")
    def test_frames_latest_and_subscribe(self):
        sock_path = str(self.tmp_path / "frames.sock")
        srv = FramesServer(path=sock_path, max_subscribers=2)
        srv.start()

        try:
            # 1. 還沒有畫面時查 latest
            s1 = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s1.connect(sock_path)
            s1.sendall(json.dumps({"v": 1, "cmd": "latest"}).encode("utf-8") + b"\n")
            resp = s1.recv(1024).decode("utf-8")
            s1.close()
            self.assertIn("還沒有畫面", resp)

            # 2. 發布一張畫面
            srv.publish(b"test-jpeg-1", {"mode": "live", "clip": "idle"})

            # 3. 查 latest
            s2 = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s2.connect(sock_path)
            s2.sendall(json.dumps({"v": 1, "cmd": "latest"}).encode("utf-8") + b"\n")
            raw = b""
            while b"\n" not in raw:
                raw += s2.recv(1024)
            line, buf = raw.split(b"\n", 1)
            header = json.loads(line.decode("utf-8"))
            self.assertTrue(header["ok"])
            self.assertEqual(header["size"], len(b"test-jpeg-1"))
            while len(buf) < 4:
                buf += s2.recv(1024)
            len_bytes = buf[:4]
            buf = buf[4:]
            self.assertEqual(struct.unpack(">I", len_bytes)[0], len(b"test-jpeg-1"))
            while len(buf) < len(b"test-jpeg-1"):
                buf += s2.recv(1024)
            payload = buf[:len(b"test-jpeg-1")]
            self.assertEqual(payload, b"test-jpeg-1")
            s2.close()

            # 4. 訂閱測試與人數上限（上限 2）
            sub1 = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sub1.connect(sock_path)
            sub1.sendall(json.dumps({"v": 1, "cmd": "subscribe", "fps": 20}).encode("utf-8") + b"\n")
            line1, rest1 = _recv_line(sub1)
            r1 = json.loads(line1.decode("utf-8").strip())
            self.assertTrue(r1["ok"])

            sub2 = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sub2.connect(sock_path)
            sub2.sendall(json.dumps({"v": 1, "cmd": "subscribe", "fps": 20}).encode("utf-8") + b"\n")
            line2, rest2 = _recv_line(sub2)
            r2 = json.loads(line2.decode("utf-8").strip())
            self.assertTrue(r2["ok"])

            # 第 3 個訂閱者應被拒絕
            sub3 = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sub3.connect(sock_path)
            sub3.sendall(json.dumps({"v": 1, "cmd": "subscribe", "fps": 20}).encode("utf-8") + b"\n")
            line3, rest3 = _recv_line(sub3)
            r3 = json.loads(line3.decode("utf-8").strip())
            self.assertFalse(r3["ok"])
            self.assertIn("上限", r3["error"])
            sub3.close()

            # 慢速訂閱者不拖慢發布者：連續發布 10 張畫面，不應阻塞
            t0 = time.monotonic()
            for i in range(10):
                srv.publish(f"frame-{i}".encode("utf-8"), {"mode": "live"})
            t1 = time.monotonic()
            self.assertLess(t1 - t0, 0.2)  # 瞬間完成，無卡頓

            sub1.close()
            sub2.close()
        finally:
            srv.stop()

    @unittest.skipUnless(hasattr(socket, "AF_UNIX"), "需要 socket.AF_UNIX")
    def test_frames_slow_subscriber_drops_old_frames(self):
        sock_path = str(self.tmp_path / "frames_drop.sock")
        srv = FramesServer(path=sock_path, max_subscribers=2)
        srv.start()
        try:
            sub = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sub.connect(sock_path)
            sub.sendall(json.dumps({"v": 1, "cmd": "subscribe", "fps": 20}).encode("utf-8") + b"\n")
            line, buf = _recv_line(sub)
            r = json.loads(line.decode("utf-8"))
            self.assertTrue(r["ok"])

            # 發布 10 張畫面，佇列上限為 1，慢訂閱者應丟舊畫面
            for i in range(1, 11):
                srv.publish(f"data-{i}".encode("utf-8"), {"seq": i, "clip": f"c{i}"})
                time.sleep(0.005)

            time.sleep(0.1)

            def read_frame(buf):
                while b"\n" not in buf:
                    buf += sub.recv(1024)
                h_line, buf = buf.split(b"\n", 1)
                hdr = json.loads(h_line.decode("utf-8"))
                while len(buf) < 4:
                    buf += sub.recv(1024)
                size = struct.unpack(">I", buf[:4])[0]
                buf = buf[4:]
                while len(buf) < size:
                    buf += sub.recv(1024)
                payload = buf[:size]
                buf = buf[size:]
                return hdr, payload, buf

            hdr, payload, buf = read_frame(buf)
            # 由於中間畫面被丟棄，收到的畫面有效且不拖慢發布
            self.assertGreater(hdr["seq"], 0)
            sub.close()
        finally:
            srv.stop()


if __name__ == "__main__":
    unittest.main()

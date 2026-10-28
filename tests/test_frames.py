"""畫面供應（frames.sock）測試：最新一張、訂閱、人數上限、慢速訂閱者丟幀。"""
import json
import socket
import struct
import time

import pytest

from pet.frames import FramesServer


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="需要 socket.AF_UNIX")
def test_frames_latest_and_subscribe(tmp_path):
    sock_path = str(tmp_path / "frames.sock")
    srv = FramesServer(path=sock_path, max_subscribers=2)
    srv.start()

    try:
        # 1. 還沒有畫面時查 latest
        s1 = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s1.connect(sock_path)
        s1.sendall(json.dumps({"v": 1, "cmd": "latest"}).encode("utf-8") + b"\n")
        resp = s1.recv(1024).decode("utf-8")
        s1.close()
        assert "還沒有畫面" in resp

        # 2. 發布一張畫面
        srv.publish(b"test-jpeg-1", {"mode": "live", "clip": "idle"})

        # 3. 查 latest
        s2 = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s2.connect(sock_path)
        s2.sendall(json.dumps({"v": 1, "cmd": "latest"}).encode("utf-8") + b"\n")
        line = b""
        while b"\n" not in line:
            line += s2.recv(1024)
        header = json.loads(line.decode("utf-8"))
        assert header["ok"] is True
        assert header["size"] == len(b"test-jpeg-1")
        len_bytes = s2.recv(4)
        assert struct.unpack(">I", len_bytes)[0] == len(b"test-jpeg-1")
        payload = s2.recv(len(b"test-jpeg-1"))
        assert payload == b"test-jpeg-1"
        s2.close()

        # 4. 訂閱測試與人數上限（上限 2）
        sub1 = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sub1.connect(sock_path)
        sub1.sendall(json.dumps({"v": 1, "cmd": "subscribe", "fps": 20}).encode("utf-8") + b"\n")
        r1 = json.loads(sub1.recv(1024).decode("utf-8").strip())
        assert r1["ok"] is True

        sub2 = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sub2.connect(sock_path)
        sub2.sendall(json.dumps({"v": 1, "cmd": "subscribe", "fps": 20}).encode("utf-8") + b"\n")
        r2 = json.loads(sub2.recv(1024).decode("utf-8").strip())
        assert r2["ok"] is True

        # 第 3 個訂閱者應被拒絕
        sub3 = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sub3.connect(sock_path)
        sub3.sendall(json.dumps({"v": 1, "cmd": "subscribe", "fps": 20}).encode("utf-8") + b"\n")
        r3 = json.loads(sub3.recv(1024).decode("utf-8").strip())
        assert r3["ok"] is False
        assert "上限" in r3["error"]
        sub3.close()

        # 慢速訂閱者不拖慢發布者：連續發布 10 張畫面，不應阻塞
        t0 = time.monotonic()
        for i in range(10):
            srv.publish(f"frame-{i}".encode("utf-8"), {"mode": "live"})
        t1 = time.monotonic()
        assert t1 - t0 < 0.2  # 瞬間完成，無卡頓

        sub1.close()
        sub2.close()
    finally:
        srv.stop()

"""螢幕畫面同步：sink 分流（同一份 bytes）、/screen.jpg、/screen.mjpg（fps、人數上限、慢的網頁丟舊畫面）。"""
import hashlib
import http.client
import io
import json
import os
import socket
import struct
import threading
import time
import urllib.error
import urllib.request

import pytest
from PIL import Image

from pet.inputs import WebRemoteInput
from pet.outputs.mirror import ScreenMirror, ScreenWebAPI
from pet.outputs.piscreen import PiscreenSink


def jpeg(color=(1, 2, 3)):
    b = io.BytesIO()
    Image.new("RGB", (800, 480), color).save(b, "JPEG", quality=60)
    return b.getvalue()


class FakePiscreen:
    def __init__(self, path):
        self.path = path
        self.frames = []
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(path)
        self.srv.listen(1)
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        c, _ = self.srv.accept()
        buf = b""
        while True:
            d = c.recv(65536)
            if not d:
                return
            buf += d
            while len(buf) >= 4:
                n = struct.unpack(">I", buf[:4])[0]
                if len(buf) < 4 + n:
                    break
                self.frames.append(buf[4:4 + n])
                buf = buf[4 + n:]


def test_sink_tees_exact_bytes(tmp_path):
    fake = FakePiscreen(str(tmp_path / "s.sock"))
    m = ScreenMirror()
    sink = PiscreenSink(str(tmp_path / "s.sock"))
    sink.mirror = m
    sink.open()
    raw = jpeg()
    sink.frame_meta = {"mode": "pack", "clip": "idle", "frame": 3}
    sink.send_jpeg(raw)
    assert m.latest()[2] is raw and m.latest()[3]["clip"] == "idle"     # 動畫包：同一個物件，沒重新編碼
    sink.frame_meta = {"mode": "bubble"}
    sink.send(Image.new("RGB", (800, 480), (9, 9, 9)))
    for _ in range(100):
        if len(fake.frames) == 2:
            break
        time.sleep(0.01)
    assert fake.frames[0] == raw
    assert fake.frames[1] == m.latest()[2]                                # 對話框：就是送出的那張
    assert m.latest()[0] == 2
    sink.close()


@pytest.fixture
def web():
    m = ScreenMirror(max_viewers=3)
    status = {"state": "idle", "fps": 20.0, "render": {"mode": "pack", "pack": "seed"}}
    w = WebRemoteInput(lambda e: None, "127.0.0.1", 0, routes=[ScreenWebAPI(m, lambda: status)])
    w.start()
    stop = threading.Event()

    def feed():
        i = 0
        frames = [jpeg((i * 40 % 255, 0, 0)) for i in range(5)]
        while not stop.is_set():
            m.publish(frames[i % 5], {"mode": "pack", "clip": "idle", "frame": i})
            i += 1
            stop.wait(0.05)                 # 20 fps
    t = threading.Thread(target=feed, daemon=True)
    yield m, w, t, stop
    stop.set()
    m.close()
    w.stop()


def read_parts(port, fps, seconds, slow=0.0):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", f"/screen.mjpg?fps={fps}")
    r = c.getresponse()
    assert r.status == 200 and "multipart/x-mixed-replace" in r.getheader("Content-Type")
    seqs, t0 = [], time.time()
    while time.time() - t0 < seconds:
        line = r.fp.readline()
        if line.startswith(b"--petframe"):
            hdr = {}
            while True:
                l = r.fp.readline().strip()
                if not l:
                    break
                k, v = l.decode().split(":", 1)
                hdr[k.lower()] = v.strip()
            data = r.fp.read(int(hdr["content-length"]))
            assert data[:2] == b"\xff\xd8"
            seqs.append(int(hdr["x-frame-seq"]))
            if slow:
                time.sleep(slow)
    c.close()
    return seqs


def test_snapshot_and_info(web):
    m, w, t, stop = web
    base = f"http://127.0.0.1:{w.port}"
    with pytest.raises(urllib.error.HTTPError) as ei:
        urllib.request.urlopen(base + "/screen.jpg")
    assert ei.value.code == 503
    t.start()
    time.sleep(0.2)
    r = urllib.request.urlopen(base + "/screen.jpg")
    body = r.read()
    assert r.headers["Content-Type"] == "image/jpeg" and int(r.headers["X-Frame-Seq"]) >= 1
    info = json.loads(urllib.request.urlopen(base + "/screen.info").read())
    assert info["ok"] and info["source"] == "動畫包" and info["pack"] == "seed" and info["state"] == "idle"
    assert info["clip"] == "idle" and info["max_viewers"] == 3 and len(info["sha1"]) == 40


def test_stream_fps_and_viewer_limit(web):
    m, w, t, stop = web
    t.start()
    out = {}
    ths = [threading.Thread(target=lambda k=k: out.setdefault(k, read_parts(w.port, 5, 2.0))) for k in range(3)]
    for th in ths:
        th.start()
    time.sleep(0.5)
    with pytest.raises(urllib.error.HTTPError) as ei:     # 第 4 個人
        urllib.request.urlopen(f"http://127.0.0.1:{w.port}/screen.mjpg")
    assert ei.value.code == 503
    for th in ths:
        th.join(5)
    for k in range(3):
        assert 7 <= len(out[k]) <= 12, out[k]              # 5 fps × 2 秒
        assert out[k] == sorted(out[k])
    for _ in range(30):                                   # 網頁關掉後名額會放回來
        if m.viewers == 0:
            break
        time.sleep(0.1)
    assert m.viewers == 0


def test_slow_viewer_gets_latest_and_does_not_block(web):
    m, w, t, stop = web
    t.start()

    class SlowW:
        def __init__(self):
            self.seqs = []

        def write(self, b):
            seq = int(b.split(b"X-Frame-Seq: ", 1)[1].split(b"\r\n", 1)[0])
            self.seqs.append(seq)
            time.sleep(0.3)                                 # 網頁很慢
            if len(self.seqs) >= 5:
                raise BrokenPipeError

    class H:
        close_connection = False
        wfile = SlowW()

        class connection:
            @staticmethod
            def settimeout(x):
                pass

        def send_response(self, c):
            pass

        def send_header(self, k, v):
            pass

        def end_headers(self):
            pass

    h = H()
    t0 = m.latest()[0]
    ScreenWebAPI(m).stream(h, 20)
    gaps = [b - a for a, b in zip(h.wfile.seqs, h.wfile.seqs[1:])]
    assert min(gaps) >= 3                                   # 跳過舊畫面，只送最新
    assert m.latest()[0] - t0 >= 25                         # 發布端照常 20 fps
    assert m.viewers == 0

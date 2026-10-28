"""picam 相機常駐服務測試：相機用假的，socket 用暫存資料夾。"""
import io
import json
import os
import threading
import time

import pytest
from PIL import Image

from services.picam import picam_client as pc
from services.picam import picamd


class FakeBackend(picamd.CameraBackend):
    model = "fake-ov5647"

    def __init__(self, capture_s=0.2, fail_open=0):
        self.capture_s = capture_s
        self.fail_open = fail_open
        self.captures = []
        self.mode = "closed"
        self.preview = None
        self._pt = None
        self.preview_starts = 0
        self.fail_capture = False

    def open(self):
        if self.fail_open:
            self.fail_open -= 1
            raise picamd.CameraFault("no_camera", "找不到相機")
        self.mode = "idle"

    def close(self):
        self.stop_preview()
        self.mode = "closed"

    def capture(self, w, h):
        self.captures.append((time.monotonic(), w, h))
        paused = self.preview
        self.preview = None            # 拍照時預覽暫停
        time.sleep(self.capture_s)
        self.preview = paused
        if self.fail_capture:
            raise RuntimeError("sensor timeout")
        return Image.new("RGB", (w, h), (10, 120, 200))

    def start_preview(self, w, h, fps, q, on_frame):
        self.stop_preview()
        self.preview_starts += 1
        buf = io.BytesIO()
        Image.new("RGB", (w, h), (0, 200, 0)).save(buf, "JPEG", quality=50)
        frame = buf.getvalue()
        stop = threading.Event()
        self.preview = (w, h, fps)

        def run():
            while not stop.is_set():
                if self.preview:
                    on_frame(frame)
                stop.wait(1.0 / fps)
        t = threading.Thread(target=run, daemon=True)
        t.start()
        self._pt = (t, stop)
        self.mode = "preview"

    def stop_preview(self):
        if self._pt:
            self._pt[1].set()
            self._pt[0].join(1)
            self._pt = None
        self.preview = None
        self.mode = "idle"

    def info(self):
        return {"model": self.model, "mode": self.mode}


@pytest.fixture
def svc(tmp_path):
    made = []

    def make(**kw):
        be = FakeBackend(**{k: kw.pop(k) for k in ("capture_s", "fail_open") if k in kw})
        p = picamd.Picam(be, reopen_min=0.2, **kw)
        sock = str(tmp_path / "picam.sock")
        srv = picamd.Server(p, sock, None, str(tmp_path / "status.json"), status_every=0.2)
        p.start()
        srv.start()
        made.append((p, srv))
        return be, p, srv, sock
    yield make
    for p, srv in made:
        srv.stop()
        p.stop()


def snap_many(sock, n, delay=0.0, **kw):
    out = [None] * n

    def one(i):
        try:
            out[i] = pc.snapshot(path=sock, client=f"c{i}", **kw.get(f"args{i}", {}))
        except pc.PicamError as exc:
            out[i] = exc
    ts = []
    for i in range(n):
        t = threading.Thread(target=one, args=(i,))
        t.start()
        ts.append(t)
        if delay:
            time.sleep(delay)
    for t in ts:
        t.join(20)
    return out


def test_three_concurrent_requests_one_capture_same_id(svc):
    be, p, srv, sock = svc()
    res = snap_many(sock, 3, delay=0.1)
    assert len(be.captures) == 1
    ids = {r[0]["photo_id"] for r in res}
    assert len(ids) == 1
    assert all(r[1][:2] == b"\xff\xd8" for r in res)
    assert p.stats["captures"] == 1 and p.stats["requests"] == 3 and p.stats["merged"] == 2


def test_largest_resolution_wins_and_smaller_is_downscaled(svc):
    be, p, srv, sock = svc()
    res = snap_many(sock, 2, delay=0.05, args0={"width": 640, "height": 480}, args1={"width": 1280, "height": 960})
    assert be.captures[0][1:] == (1280, 960)
    small, big = res
    assert Image.open(io.BytesIO(small[1])).size == (640, 480)
    assert Image.open(io.BytesIO(big[1])).size == (1280, 960)
    assert small[0]["photo_id"] == big[0]["photo_id"]
    assert small[0]["capture"] == [1280, 960]


def test_request_during_capture_attaches(svc):
    be, p, srv, sock = svc(capture_s=0.8)
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("a", pc.snapshot(path=sock)))
    t.start()
    time.sleep(0.9)                    # 合併視窗 0.5 秒已過，正在拍
    b = pc.snapshot(path=sock)
    t.join(5)
    assert len(be.captures) == 1
    assert out["a"][0]["photo_id"] == b[0]["photo_id"]


def test_rate_cap_and_every_request_answered(svc):
    be, p, srv, sock = svc(capture_s=0.05)
    t0 = time.monotonic()
    res = snap_many(sock, 24, delay=0.1)        # 2.4 秒內狂按 24 次
    span = time.monotonic() - t0
    assert all(not isinstance(r, Exception) for r in res)
    starts = [c[0] for c in be.captures]
    assert len(starts) <= int(span * 2) + 2
    assert all(b - a >= 0.5 - 0.02 for a, b in zip(starts, starts[1:]))
    assert len({r[0]["photo_id"] for r in res}) == len(starts)


def test_snapshot_during_preview(svc):
    be, p, srv, sock = svc(capture_s=0.3)
    got = []
    sub = pc.Subscriber(fps=20, width=320, height=240, path=sock, client="camcast")

    def run():
        for f in sub.frames():
            got.append(time.monotonic())
    t = threading.Thread(target=run, daemon=True)
    t.start()
    time.sleep(0.8)
    n0 = len(got)
    assert n0 > 3
    meta, data = pc.snapshot(path=sock)
    time.sleep(0.6)
    assert meta["ok"] and len(got) > n0 + 3      # 拍完預覽恢復
    assert p.status()["preview"]["width"] == 320
    sub.stop()
    t.join(3)


def test_service_down_and_subscriber_reconnects(tmp_path, svc):
    with pytest.raises(pc.PicamError) as ei:
        pc.snapshot(path=str(tmp_path / "nope.sock"))
    assert ei.value.kind == "service_down" and "相機服務未啟動" in str(ei.value)
    be, p, srv, sock = svc()
    states = []
    sub = pc.Subscriber(fps=20, width=160, height=120, path=sock, on_state=lambda s, d: states.append(s))
    got = []

    def run():
        for f in sub.frames():
            got.append(f)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    time.sleep(0.5)
    srv.stop()                         # 模擬 picam 重啟
    p.stop()
    time.sleep(0.3)
    p2 = picamd.Picam(FakeBackend(), reopen_min=0.2)
    srv2 = picamd.Server(p2, sock, None, None)
    p2.start()
    srv2.start()
    try:
        n = len(got)
        deadline = time.time() + 8
        while time.time() < deadline and len(got) <= n + 3:
            time.sleep(0.1)
        assert len(got) > n + 3
        assert "down" in states and states.count("up") >= 2 and sub.reconnects >= 1
    finally:
        sub.stop()
        t.join(3)
        srv2.stop()
        p2.stop()


def test_camera_errors_are_reported(svc):
    be, p, srv, sock = svc(fail_open=100)
    time.sleep(0.3)
    with pytest.raises(pc.PicamError) as ei:
        pc.snapshot(path=sock, timeout=5)
    assert ei.value.kind == "camera_error"
    st = pc.status(path=sock)
    assert st["camera_open"] is False and st["errors"] >= 1 and "找不到相機" in st["last_error"]


def test_capture_failure_reopens_and_recovers(svc):
    be, p, srv, sock = svc()
    be.fail_capture = True
    with pytest.raises(pc.PicamError) as ei:
        pc.snapshot(path=sock)
    assert ei.value.kind == "camera_error" and "拍照失敗" in str(ei.value)
    be.fail_capture = False
    time.sleep(0.4)
    meta, data = pc.snapshot(path=sock)
    assert meta["ok"] and p.stats["reopens"] == 1


def test_bad_request_and_status_file(svc, tmp_path):
    be, p, srv, sock = svc()
    with pytest.raises(pc.PicamError) as ei:
        pc.snapshot(width=99999, path=sock)
    assert ei.value.kind == "bad_request"
    time.sleep(0.5)
    st = json.load(open(tmp_path / "status.json"))
    assert st["service"] == "picam" and "captures" in st and st["camera"] == "fake-ov5647"


def test_photo_id_format():
    assert picamd.photo_id_for(1793193081.375) == time.strftime("%Y%m%d-%H%M%S", time.localtime(1793193081)) + "-375"

"""camera.snapshot 能力模組測試：相機與上傳都用假的。"""
import io
import json
import os
import urllib.error
import urllib.request

import pytest
from PIL import Image

from pet.backend_client import directive_to_event
from pet.extensions import CameraSnapshot, CameraWebAPI, EventBus
from pet.extensions.camera import CameraError, camera_busy


def jpeg_bytes(w=64, h=48):
    out = io.BytesIO()
    Image.new("RGB", (w, h), (200, 100, 50)).save(out, "JPEG")
    return out.getvalue()


class FakeCam:
    def __init__(self):
        self.calls = []
        self.error = None

    def __call__(self, path, w, h, q):
        self.calls.append((w, h, q))
        if self.error:
            raise self.error
        with open(path, "wb") as fh:
            fh.write(jpeg_bytes())


class FakeUploader:
    def __init__(self):
        self.down = False
        self.got = []

    def upload(self, data, meta):
        if self.down:
            raise urllib.error.URLError("connection refused")
        self.got.append((len(data), dict(meta)))
        return "photo-%d" % len(self.got)


class Clock:
    t = 100.0

    def __call__(self):
        return self.t


@pytest.fixture
def cam(tmp_path):
    bus = EventBus()
    events = []
    bus.subscribe("*", events.append)
    up = FakeUploader()
    fc = FakeCam()
    clock = Clock()
    ext = CameraSnapshot(up, pending_dir=str(tmp_path / "pending"), capture=fc, busy_check=lambda: None,
                         clock=clock, retry_delay=0, max_pending=3)
    ext.sleep = lambda s: None
    ext.attach(bus)
    os.makedirs(ext.pending_dir)
    return ext, bus, events, up, fc, clock


def run_next(ext):
    job = ext._jobs.get_nowait()
    try:
        return ext.run_job(job)
    finally:
        ext._busy = False


def names(bus, events):
    bus.dispatch_pending()
    return [(e["type"], e["name"]) for e in events]


def test_snapshot_uploads_and_reacts(cam):
    ext, bus, events, up, fc, clock = cam
    assert ext.trigger({"reason": "manual"})["ok"]
    r = run_next(ext)
    assert r["uploaded"] and r["photo_id"] == "photo-1" and fc.calls == [(1280, 960, 85)]
    assert up.got[0][1]["reason"] == "manual"
    n = names(bus, events)
    assert ("action", "say") in n and ("action", "expression") in n
    assert ("skill", "camera.snapshot.done") in n
    assert ext.pending() == [] and ext.last_thumb[:2] == b"\xff\xd8"


def test_params_and_validation(cam):
    ext, bus, events, up, fc, clock = cam
    assert ext.trigger({"width": 640, "height": 480, "quality": 70, "reason": "timer"})["ok"]
    run_next(ext)
    assert fc.calls[-1] == (640, 480, 70)
    clock.t += 10
    r = ext.trigger({"width": 99999})
    assert not r["ok"] and r["kind"] == "bad_params"
    assert not ext.trigger({"reason": "../x"})["ok"]


def test_cooldown_blocks_and_reports(cam):
    ext, bus, events, up, fc, clock = cam
    assert ext.trigger()["ok"]
    run_next(ext)
    clock.t += 2
    r = ext.trigger()
    assert not r["ok"] and r["kind"] == "cooldown" and 2.9 <= r["retry_after"] <= 3.0
    assert ("skill", "camera.snapshot.failed") in names(bus, events)
    clock.t += 3.1
    assert ext.trigger()["ok"]


def test_in_progress_blocks(cam):
    ext, *_ = cam
    ext.cooldown = 0
    assert ext.trigger()["ok"]
    assert ext.trigger()["kind"] == "in_progress"


def test_camera_busy_and_missing(cam):
    ext, bus, events, up, fc, clock = cam
    ext.busy_check = lambda: "camcast"
    ext.trigger()
    r = run_next(ext)
    assert not r["ok"] and r["kind"] == "camera_busy" and "camcast" in r["error"]
    assert fc.calls == []                         # 不強搶相機
    ext.busy_check = lambda: None
    fc.error = CameraError("no_camera", "找不到相機")
    clock.t += 10
    ext.trigger()
    r = run_next(ext)
    assert r["kind"] == "no_camera" and ext.pending() == []
    ev = [e for e in events if e.get("name") == "camera.snapshot.failed"]
    bus.dispatch_pending()
    assert any(e.get("reason") == "no_camera" for e in events)


def test_backend_down_queue_and_resend(cam):
    ext, bus, events, up, fc, clock = cam
    up.down = True
    for i in range(5):
        clock.t += 10
        ext.trigger({"reason": "r%d" % i})
        r = run_next(ext)
        assert r["ok"] and r["queued"] and not r["uploaded"]
    assert len(ext.pending()) == 3                 # 上限 3 張（測試設定），最舊的刪掉
    assert ext.flush_pending() == 0
    up.down = False
    assert ext.flush_pending() == 3
    assert ext.pending() == [] and [m["reason"] for _, m in up.got] == ["r2", "r3", "r4"]
    assert ext.last["uploaded"]


def test_success_flushes_older_pending(cam):
    ext, bus, events, up, fc, clock = cam
    up.down = True
    ext.trigger({"reason": "old"})
    run_next(ext)
    up.down = False
    clock.t += 10
    ext.trigger({"reason": "new"})
    run_next(ext)
    assert [m["reason"] for _, m in up.got] == ["new", "old"] and ext.pending() == []


def test_retry_once(cam):
    ext, bus, events, up, fc, clock = cam
    calls = {"n": 0}
    real = up.upload

    def flaky(data, meta):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.URLError("timeout")
        return real(data, meta)
    up.upload = flaky
    ext.trigger()
    assert run_next(ext)["uploaded"] and calls["n"] == 2


def test_event_trigger_and_backend_directive(cam):
    ext, bus, events, up, fc, clock = cam
    ext.start()
    try:
        ev = directive_to_event({"type": "skill", "name": "camera.snapshot", "width": 320, "height": 240})
        assert ev["type"] == "skill" and ev["params"]["reason"] == "backend"
        bus.publish(ev)
        bus.dispatch_pending()
        for _ in range(200):
            if ext.last:
                break
            import time
            time.sleep(0.01)
        assert ext.last and ext.last["uploaded"] and fc.calls[-1][:2] == (320, 240)
        assert ext.last["trigger"] == "backend"
    finally:
        ext.stop()


def test_camera_busy_scan(tmp_path):
    p = tmp_path / "proc" / "4242"
    p.mkdir(parents=True)
    (p / "cmdline").write_bytes(b"python3\0/home/x/camcast.py\0")
    assert camera_busy(str(tmp_path / "proc")) == "camcast"
    (p / "cmdline").write_bytes(b"bash\0")
    assert camera_busy(str(tmp_path / "proc")) is None


def test_web_api_and_thumbnail(cam):
    from pet.inputs import WebRemoteInput
    ext, bus, events, up, fc, clock = cam
    api = CameraWebAPI(ext)
    w = WebRemoteInput(lambda e: None, "127.0.0.1", 0, routes=[api])
    w.start()
    try:
        base = f"http://127.0.0.1:{w.port}"
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(base + "/skill/camera.snapshot/last.jpg")
        assert ei.value.code == 404
        req = urllib.request.Request(base + "/skill/camera.snapshot", data=b'{"reason":"manual"}',
                                     headers={"Content-Type": "application/json"})
        assert urllib.request.urlopen(req).status == 202
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(req)
        assert ei.value.code == 429
        run_next(ext)
        st = json.loads(urllib.request.urlopen(base + "/skill/camera.snapshot").read())
        assert st["last"]["uploaded"] and st["has_thumb"] and st["pending"] == 0
        r = urllib.request.urlopen(base + "/skill/camera.snapshot/last.jpg")
        assert r.headers["Content-Type"] == "image/jpeg" and r.read()[:2] == b"\xff\xd8"
    finally:
        w.stop()


def test_app_ignores_skill_events_in_fsm():
    from pet.app import PetApp
    app = PetApp(use_clock=False)
    st = app.fsm.state
    app.post({"type": "skill", "name": "camera.snapshot", "source": "t", "ts": 0})
    app.step(0.05)
    assert app.fsm.state == st
